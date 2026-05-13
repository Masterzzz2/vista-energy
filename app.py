"""
Energy Optimizer - Main Application
Optimiert Batterie- und EV-Ladung basierend auf Tibber-Preisen, PV-Prognose und Verbrauchsprofil.
"""

import os
import json
import logging
import requests
import asyncio
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# NEXUS Control - shared paths with nexus_ocpp_server.py / pv_controller.py
NEXUS_DIR = Path(os.getenv('NEXUS_DIR', '/home/werner/.nexus'))
NEXUS_DIR.mkdir(parents=True, exist_ok=True)
WALLBOX_MODE_FILE = NEXUS_DIR / 'wallbox_mode'
BATTERY_PLAN_FILE = NEXUS_DIR / 'battery_plan.json'
CONTROLLER_STATE_FILE = NEXUS_DIR / 'controller_state.json'
RFID_ALIASES_FILE = NEXUS_DIR / 'rfid_aliases.json'
SCHEDULES_FILE = NEXUS_DIR / 'schedules.json'
AUTOMATION_MODE_FILE = NEXUS_DIR / 'automation_mode'
PROGRAM_STATE_FILE = NEXUS_DIR / 'program_state.json'
POOL_STATE_FILE = NEXUS_DIR / 'pool_state.json'
UPDATE_STATE_FILE = NEXUS_DIR / 'update_state.json'
EV_VEHICLES_FILE = NEXUS_DIR / 'ev_vehicles.json'
OCPP_CONTROL_WS = 'ws://127.0.0.1:8889'

# EV-Fahrzeuge mit Akkugroesse (kWh) fuer SOC-Schaetzung
_DEFAULT_EV_VEHICLES = {
    "Werner": {"name": "Tesla Model 3 SR+ LFP", "battery_kwh": 59.0},
    "Moni":   {"name": "Mercedes C300e",         "battery_kwh": 11.0},
}


def load_ev_vehicles() -> dict:
    """Mapping alias -> {name, battery_kwh}."""
    try:
        if EV_VEHICLES_FILE.exists():
            return json.loads(EV_VEHICLES_FILE.read_text())
    except Exception:
        pass
    return dict(_DEFAULT_EV_VEHICLES)


def save_ev_vehicles(d: dict):
    EV_VEHICLES_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))

# Optionale Bayern-Feiertage
try:
    import holidays as _holidays_lib
    _HOLIDAYS = _holidays_lib.DE(prov='BY')
except Exception:
    _HOLIDAYS = None


def load_schedules() -> list:
    try:
        if SCHEDULES_FILE.exists():
            d = json.loads(SCHEDULES_FILE.read_text())
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def save_schedules(schedules: list):
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=2, ensure_ascii=False))


def _ensure_default_schedules():
    """Legt einmalig die Default-Schedule fuer Moni an, falls die Datei noch leer ist."""
    if SCHEDULES_FILE.exists():
        return
    default = [{
        "name": "Moni Mercedes 300e",
        "alias": "Moni",
        "days": [2, 3, 5],          # Di, Mi, Fr
        "start": "13:30",
        "end": "17:00",
        "max_amps": 16,             # 16 A * 3 ph * 230 V = 11 kW
        "phases": 3,
        "skip_holidays": True,
        "active": True,
        "notes": "Mercedes 300e - Dienstag/Mittwoch/Freitag voll laden"
    }]
    save_schedules(default)


_ensure_default_schedules()


def load_rfid_aliases() -> dict:
    """Mapping idTag -> Alias, z.B. {"ABC123": "Werner", "DEF456": "Moni"}."""
    try:
        if RFID_ALIASES_FILE.exists():
            return json.loads(RFID_ALIASES_FILE.read_text())
    except Exception:
        pass
    return {}


def save_rfid_aliases(d: dict):
    RFID_ALIASES_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))

# Berlin timezone (auto-handles DST: CET+1h in winter, CEST+2h in summer)
BERLIN_TZ = ZoneInfo('Europe/Berlin')

def now_berlin():
    """Return current time in Berlin timezone (handles DST automatically)."""
    return datetime.now(BERLIN_TZ)


def as_berlin(dt: datetime) -> datetime:
    """Normalize aware/naive datetimes to Europe/Berlin."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BERLIN_TZ)
    return dt.astimezone(BERLIN_TZ)


def parse_local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(BERLIN_TZ).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def db_datetime(dt: datetime | None = None) -> str:
    dt = as_berlin(dt or now_berlin()).replace(tzinfo=None)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session
from flask_cors import CORS
from flask_login import LoginManager, login_required, login_user, logout_user, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from apscheduler.schedulers.background import BackgroundScheduler

from models.database import db, init_db, EnergyReading, PriceData, ChargeLog
from models.profiles import ConsumptionProfile
from services.tibber_service import TibberService
from services.forecast_solar import ForecastSolarService
from services.weather_service import WeatherService
from services.evcc_service import EVCCService
from services.optimizer import Optimizer
from services.learning import LearningService
from services.license_service import LicenseService
from services.payment_service import PaymentService
from services.plugin_registry import PluginRegistry
from services.plugin_adapters import InverterAdapter, TariffAdapter, WallboxAdapter
from services.plugin_registry import PluginRegistry
try:
    from services.heatpump_service import HeatpumpService
    HEATPUMP_AVAILABLE = True
except Exception:
    HEATPUMP_AVAILABLE = False
    HeatpumpService = None

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__, template_folder='templates', static_folder='static')
# SECRET_KEY: stabil aus .env oder generiert (persistent durch File)
_secret_file = NEXUS_DIR / 'flask_secret.key'
if _secret_file.exists():
    app.config['SECRET_KEY'] = _secret_file.read_text().strip()
else:
    import secrets as _secrets
    s = _secrets.token_hex(32)
    _secret_file.write_text(s)
    _secret_file.chmod(0o600)
    app.config['SECRET_KEY'] = s

# Hinter Cloudflare Tunnel (X-Forwarded-Proto)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1, x_host=1)

# Session-Cookie-Settings: praktisch unbegrenzt (100 Jahre), HTTPONLY, SameSite=Lax
# Cookie laeuft erst nach Jahren ab; nur ein /logout oder neuer flask_secret.key invalidiert ihn.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=36500)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=36500)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
# SECURE-Flag nur wenn der Request via HTTPS kam (Cloudflare setzt X-Forwarded-Proto=https)
# -> Flask setzt das automatisch korrekt durch ProxyFix oben

CORS(app, origins=['http://localhost:8080', 'http://127.0.0.1:8080'])

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Simple user store - aus .env
from flask_login import UserMixin
class User(UserMixin):
    def __init__(self, uid):
        self.id = uid


def _parse_env_list(value: str) -> list:
    return [x.strip() for x in (value or '').split(',') if x.strip()]


def _users_from_env() -> dict:
    """Liest WEB_USER + WEB_PASSWORD (Default: werner/energy2026).
       Optional:
         WEB_USERS=name1:pw1,name2:pw2
         WEB_GUEST_USER=gast + WEB_GUEST_PASSWORD=...
         WEB_READONLY_USERS=gast,viewer
    """
    out = {}
    multi = os.getenv('WEB_USERS', '').strip()
    if multi:
        for pair in multi.split(','):
            if ':' in pair:
                u, p = pair.split(':', 1)
                u = u.strip()
                if u:
                    out[u] = {'password': p.strip(), 'role': 'admin'}
    user = os.getenv('WEB_USER', 'werner').strip()
    pw = os.getenv('WEB_PASSWORD', 'energy2026').strip()
    if user:
        out[user] = {'password': pw, 'role': 'admin'}

    guest_user = os.getenv('WEB_GUEST_USER', '').strip()
    guest_pw = os.getenv('WEB_GUEST_PASSWORD', '').strip()
    if guest_user and guest_pw:
        out[guest_user] = {'password': guest_pw, 'role': 'guest'}

    readonly_users = set(_parse_env_list(os.getenv('WEB_READONLY_USERS', '')))
    if guest_user:
        readonly_users.add(guest_user)
    for username in readonly_users:
        if username in out:
            out[username]['role'] = 'guest'
    return out


USERS = _users_from_env()


def user_role(username: str | None) -> str:
    if not username:
        return 'guest'
    data = USERS.get(username)
    if isinstance(data, dict):
        return data.get('role', 'admin')
    return 'admin'


def is_readonly_user() -> bool:
    return current_user.is_authenticated and user_role(current_user.id) == 'guest'


@login_manager.user_loader
def load_user(user_id):
    if user_id in USERS:
        return User(user_id)
    return None


# Schutz aller /api/*, /api/login + statische Dateien sind frei
PUBLIC_ENDPOINTS = {'login', 'api_login', 'static', 'favicon', 'setup_wizard', 'api_setup_test_inverter', 'api_setup_complete', 'api_payment_webhook'}


@app.before_request
def _require_login():
    # Public files
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return None
    # Authentifiziert?
    if current_user.is_authenticated:
        if is_readonly_user() and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'guest_readonly'}), 403
            return Response('Gastzugang: nur Ansicht', status=403)
        return None
    # API -> 401 JSON
    if request.path.startswith('/api/'):
        return jsonify({'error': 'unauthorized'}), 401
    # HTML -> redirect
    return redirect(url_for('login', next=request.path))

# Services (initialized lazily)
tibber_service = None
forecast_service = None
weather_service = None
evcc_service = None
optimizer_service = None
learning_service = None
license_service = None
plugin_registry = None
wallbox_service = None

# Scheduler
scheduler = BackgroundScheduler()

VALID_AUTOMATION_MODES = ('manual', 'auto', 'ki')


def normalize_automation_mode(mode: str) -> str:
    mode = (mode or '').strip().lower()
    aliases = {
        'automatic': 'auto',
        'automatisch': 'auto',
        'vollautomatisch': 'ki',
        'full_auto': 'ki',
        'ai': 'ki',
    }
    mode = aliases.get(mode, mode)
    return mode if mode in VALID_AUTOMATION_MODES else 'auto'


def read_automation_mode() -> str:
    try:
        if AUTOMATION_MODE_FILE.exists():
            return normalize_automation_mode(AUTOMATION_MODE_FILE.read_text())
    except Exception:
        pass
    return 'auto'


def write_automation_mode(mode: str) -> str:
    mode = normalize_automation_mode(mode)
    AUTOMATION_MODE_FILE.write_text(mode + '\n')
    return mode


# Current mode (legacy "global" mode of the dashboard)
current_mode = read_automation_mode()  # manual, auto, ki
price_threshold = 28.0  # ct/kWh - EV charging threshold

# Battery target by 18:30 (max SOC in % so that we don't have grid pull at night)
TARGET_SOC_1830 = 65.0
# Battery usable capacity (kWh) and house overnight load estimate (kWh)
BATTERY_USABLE_KWH = float(os.getenv('BATTERY_USABLE_KWH', 7.0))
NIGHT_LOAD_KWH = 5.5  # 18:30 - 06:00 typical consumption

# Automatisches Netzladen des Hausakkus, nur bei wirklich guenstigem Tibber-Preis.
AUTO_GRID_CHARGE_ABS_CHEAP_CT = float(os.getenv('AUTO_GRID_CHARGE_ABS_CHEAP_CT', 20.0))
AUTO_GRID_CHARGE_MAX_CT = float(os.getenv('AUTO_GRID_CHARGE_MAX_CT', 28.0))
AUTO_GRID_CHARGE_MIN_SAVINGS_CT = float(os.getenv('AUTO_GRID_CHARGE_MIN_SAVINGS_CT', 6.0))
AUTO_GRID_CHARGE_CHEAP_RANK = int(os.getenv('AUTO_GRID_CHARGE_CHEAP_RANK', 3))
AUTO_GRID_CHARGE_MAX_W = int(os.getenv('AUTO_GRID_CHARGE_MAX_W', 3500))
AUTO_GRID_CHARGE_TARGET_MAX_SOC = float(os.getenv('AUTO_GRID_CHARGE_TARGET_MAX_SOC', 92.0))

# Langzeitbetrieb auf dem Mini-PC: Rohdaten begrenzen, Lern-/Ladehistorie behalten.
ENERGY_READING_RETENTION_DAYS = int(os.getenv('ENERGY_READING_RETENTION_DAYS', 730))
PRICE_DATA_RETENTION_DAYS = int(os.getenv('PRICE_DATA_RETENTION_DAYS', 730))
PV_FORECAST_RETENTION_DAYS = int(os.getenv('PV_FORECAST_RETENTION_DAYS', 1095))

# Pool als grosser Verbraucher. Start/Stop wird manuell markiert; Prognose
# rechnet mit 1,8 kW und ca. 2 C Temperaturgewinn pro Stunde.
POOL_POWER_W = float(os.getenv('POOL_POWER_W', 1800))
POOL_TEMP_GAIN_C_PER_HOUR = float(os.getenv('POOL_TEMP_GAIN_C_PER_HOUR', 2.0))
POOL_DEFAULT_MORNING_TEMP_C = float(os.getenv('POOL_DEFAULT_MORNING_TEMP_C', 22.0))
POOL_COOL_BASE_C_PER_HOUR = float(os.getenv('POOL_COOL_BASE_C_PER_HOUR', 0.035))
POOL_COOL_DELTA_FACTOR = float(os.getenv('POOL_COOL_DELTA_FACTOR', 0.012))
POOL_COOL_MAX_C_PER_HOUR = float(os.getenv('POOL_COOL_MAX_C_PER_HOUR', 0.22))
POOL_DAY_TARGET_C = float(os.getenv('POOL_DAY_TARGET_C', 33.0))
POOL_EVENING_TARGET_C = float(os.getenv('POOL_EVENING_TARGET_C', 38.0))
POOL_DEFAULT_START_MINUTE = int(os.getenv('POOL_DEFAULT_START_MINUTE', 10 * 60 + 45))
POOL_GOOD_PV_KWH = float(os.getenv('POOL_GOOD_PV_KWH', 18.0))
POOL_GOOD_SUN_HOURS = float(os.getenv('POOL_GOOD_SUN_HOURS', 5.0))
POOL_BAD_PV_KWH = float(os.getenv('POOL_BAD_PV_KWH', 10.0))
POOL_BAD_SUN_HOURS = float(os.getenv('POOL_BAD_SUN_HOURS', 3.0))


# ---------------------------------------------------------------------------
# Wallbox control (talks to nexus_ocpp_server.py via WS on 8889)
# ---------------------------------------------------------------------------
def wallbox_send_cmd(cmd: dict, timeout: float = 5.0) -> dict:
    """Send a command to the OCPP control socket and return its response."""
    try:
        import websocket  # websocket-client (synchronous)
        ws = websocket.WebSocket()
        ws.settimeout(timeout)
        ws.connect(OCPP_CONTROL_WS, timeout=timeout)
        ws.send(json.dumps(cmd))
        response = ws.recv()
        ws.close()
        return json.loads(response)
    except Exception as e:
        logger.warning(f'wallbox_send_cmd({cmd}) failed: {e}')
        return {'status': 'error', 'reason': str(e)}


VALID_MODES = ('eco', 'mix', 'fast', 'off', 'manual')


def read_wallbox_mode() -> str:
    try:
        if WALLBOX_MODE_FILE.exists():
            m = WALLBOX_MODE_FILE.read_text().strip().lower()
            if m == 'standard':
                m = 'mix'  # legacy migration
            if m in VALID_MODES:
                return m
    except Exception:
        pass
    return 'mix'


def write_wallbox_mode(mode: str):
    mode = (mode or '').lower()
    if mode == 'standard':
        mode = 'mix'
    if mode not in VALID_MODES:
        raise ValueError(f'invalid mode: {mode}')
    WALLBOX_MODE_FILE.write_text(mode + '\n')
    # mode-hint zum OCPP server schicken (informativ)
    wallbox_send_cmd({'cmd': 'mode_hint', 'mode': mode})


def _bool_value(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ('1', 'true', 'yes', 'on', 'an'):
            return True
        if value in ('0', 'false', 'no', 'off', 'aus'):
            return False
    return default


def read_program_state() -> dict:
    """Master switch state. Service stays reachable; controls can be paused."""
    state = {
        'enabled': True,
        'previous_automation_mode': 'ki',
        'previous_wallbox_mode': 'mix',
    }
    try:
        if PROGRAM_STATE_FILE.exists():
            data = json.loads(PROGRAM_STATE_FILE.read_text())
            if isinstance(data, dict):
                state.update(data)
    except Exception:
        pass

    state['enabled'] = _bool_value(state.get('enabled'), True)
    state['previous_automation_mode'] = normalize_automation_mode(state.get('previous_automation_mode', 'ki'))
    wallbox_mode = (state.get('previous_wallbox_mode') or 'mix').strip().lower()
    if wallbox_mode == 'standard':
        wallbox_mode = 'mix'
    state['previous_wallbox_mode'] = wallbox_mode if wallbox_mode in VALID_MODES else 'mix'
    return state


def write_program_state(enabled: bool, previous_automation_mode: str | None = None,
                        previous_wallbox_mode: str | None = None) -> dict:
    state = read_program_state()
    state['enabled'] = bool(enabled)
    if previous_automation_mode is not None:
        state['previous_automation_mode'] = normalize_automation_mode(previous_automation_mode)
    if previous_wallbox_mode is not None:
        previous_wallbox_mode = (previous_wallbox_mode or 'mix').strip().lower()
        if previous_wallbox_mode == 'standard':
            previous_wallbox_mode = 'mix'
        state['previous_wallbox_mode'] = previous_wallbox_mode if previous_wallbox_mode in VALID_MODES else 'mix'
    PROGRAM_STATE_FILE.write_text(json.dumps(state, indent=2))
    return state


def is_program_enabled() -> bool:
    return bool(read_program_state().get('enabled', True))


def set_program_enabled(enabled: bool) -> dict:
    """Logical master switch: keep dashboard alive, pause/resume active control."""
    global current_mode, _force_charge, _last_battery_action

    if enabled:
        state = read_program_state()
        auto_mode = normalize_automation_mode(state.get('previous_automation_mode', 'ki'))
        wallbox_mode = state.get('previous_wallbox_mode', 'mix')
        if auto_mode in ('auto', 'ki') and wallbox_mode in ('off', 'manual'):
            wallbox_mode = 'mix'
        current_mode = write_automation_mode(auto_mode)
        write_wallbox_mode(wallbox_mode)
        state = write_program_state(True, auto_mode, wallbox_mode)
        logger.info(f'Program master switch ON: automation={auto_mode}, wallbox={wallbox_mode}')
        return state

    prev_auto = read_automation_mode()
    prev_wallbox = read_wallbox_mode()
    if prev_wallbox in ('off', 'manual'):
        prev_wallbox = 'mix'
    state = write_program_state(False, prev_auto, prev_wallbox)
    current_mode = write_automation_mode('manual')
    write_wallbox_mode('off')

    try:
        if BATTERY_MODBUS_OK and _battery_modbus is not None:
            _battery_modbus.release()
    except Exception as e:
        logger.debug(f'program off battery release failed: {e}')
    try:
        _force_charge = {'active': False, 'target_soc': None, 'max_w': None, 'started': None}
        _last_battery_action = {'action': 'program_off', 'pct': None}
    except Exception:
        pass

    logger.info(f'Program master switch OFF: previous automation={prev_auto}, wallbox={prev_wallbox}')
    return state


def read_controller_state() -> dict:
    try:
        if CONTROLLER_STATE_FILE.exists():
            return json.loads(CONTROLLER_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def live_fronius_flow() -> dict:
    """Read the current power flow via Plugin-System."""
    try:
        state = get_services()['evcc'].get_current_state()
        return {
            'pv_w': float(state.get('pv_production') or 0),
            'load_w': float(state.get('house_consumption') or 0),
            'grid_w': float(state.get('grid_power') or 0),
            'akku_w': float(state.get('battery_charge') or 0),
            'soc': round(float(state.get('battery_soc') or 0) * 100, 1),
        }
    except Exception as e:
        logger.debug(f'live_fronius_flow failed: {e}')
        return {}


# ---------------------------------------------------------------------------
# Update check / install (Cloudflare R2)
# ---------------------------------------------------------------------------
UPDATE_URL = os.getenv('VISTA_UPDATE_URL', 'https://update.vista-pv.com')
UPDATE_VERSION_FILE = NEXUS_DIR / 'update_version'

def update_repo_path() -> Path:
    return Path(os.getenv('VISTA_UPDATE_PATH', Path(__file__).resolve().parent)).resolve()


def _get_current_version() -> str:
    try:
        return UPDATE_VERSION_FILE.read_text().strip()
    except Exception:
        return 'unknown'


def read_update_state() -> dict:
    state = {
        'status': 'unknown',
        'configured': True,
        'update_available': False,
        'auto_install': False,
        'message': 'Noch keine Update-Pruefung gelaufen.',
        'checked_at': None,
        'source': 'cloudflare-r2',
    }
    try:
        if UPDATE_STATE_FILE.exists():
            data = json.loads(UPDATE_STATE_FILE.read_text())
            if isinstance(data, dict):
                state.update(data)
    except Exception:
        pass
    return state


def write_update_state(**kwargs) -> dict:
    state = read_update_state()
    state.update(kwargs)
    state['updated_at'] = now_berlin().isoformat()
    UPDATE_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return state


def check_update_job(auto_install: bool = False) -> dict:
    import urllib.request
    now_iso = now_berlin().isoformat()
    current = _get_current_version()

    try:
        req = urllib.request.Request(f'{UPDATE_URL}/manifest.json', headers={'User-Agent': 'VistaEnergy/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            manifest = json.loads(resp.read().decode())
    except Exception as e:
        return write_update_state(
            status='error',
            configured=True,
            update_available=False,
            checked_at=now_iso,
            current=current,
            message=f'Update-Server nicht erreichbar: {e}',
        )

    latest = manifest.get('version', 'unknown')
    update_available = latest != current

    if update_available:
        status = 'update_available'
        message = f'Update verfuegbar: {current} → {latest}'
    else:
        status = 'up_to_date'
        message = f'VistaEnergy ist aktuell ({current}).'

    state = write_update_state(
        status=status,
        configured=True,
        update_available=update_available,
        checked_at=now_iso,
        current=current,
        latest=latest,
        archive=manifest.get('archive'),
        published_at=manifest.get('published_at'),
        message=message,
    )

    if auto_install and update_available:
        threading.Thread(target=install_update_job, daemon=True).start()
    return state


def install_update_job() -> dict:
    import urllib.request
    import tarfile
    import shutil

    repo = update_repo_path()
    current = _get_current_version()
    state = read_update_state()
    archive_name = state.get('archive')
    latest = state.get('latest', 'unknown')

    if not archive_name:
        return write_update_state(status='error', message='Kein Update-Archiv bekannt. Zuerst pruefen.')

    write_update_state(status='installing', message=f'Update {latest} wird installiert...')

    tmpdir = Path('/tmp/vista-update')
    tmpdir.mkdir(exist_ok=True)
    archive_path = tmpdir / archive_name

    try:
        req = urllib.request.Request(f'{UPDATE_URL}/{archive_name}', headers={'User-Agent': 'VistaEnergy/1.0'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            archive_path.write_bytes(resp.read())
    except Exception as e:
        return write_update_state(status='error', message=f'Download fehlgeschlagen: {e}')

    backup_dir = NEXUS_DIR / f'backup-{now_berlin().strftime("%Y%m%d-%H%M%S")}'
    backup_dir.mkdir(exist_ok=True)
    for name in ['app.py', 'templates', 'services']:
        src = repo / name
        if src.exists():
            dst = backup_dir / name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(path=str(repo), filter='data')
    except Exception as e:
        return write_update_state(status='error', message=f'Entpacken fehlgeschlagen: {e}')

    python_bin = repo / 'venv' / 'bin' / 'python3'
    py = str(python_bin) if python_bin.exists() else 'python3'
    compile_res = subprocess.run(
        [py, '-m', 'py_compile', str(repo / 'app.py')],
        capture_output=True, text=True, timeout=30,
    )
    if compile_res.returncode != 0:
        for name in ['app.py', 'templates', 'services']:
            src = backup_dir / name
            dst = repo / name
            if src.exists():
                if dst.exists():
                    if dst.is_dir():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        return write_update_state(
            status='error',
            message=f'Syntaxfehler in Update {latest}! Backup wiederhergestellt.',
            detail=compile_res.stderr[:2000],
        )

    UPDATE_VERSION_FILE.write_text(latest)
    write_update_state(
        status='installed',
        update_available=False,
        current=latest,
        latest=latest,
        installed_at=now_berlin().isoformat(),
        message=f'Update {latest} installiert. Dienste starten gleich neu.',
    )

    try:
        shutil.rmtree(tmpdir)
    except Exception:
        pass

    subprocess.Popen(
        ['sh', '-lc', 'sleep 2; sudo -n systemctl restart energy-dashboard nexus-pv-controller nexus-ocpp'],
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return read_update_state()


# ---------------------------------------------------------------------------
# Pool tracking / prediction
# ---------------------------------------------------------------------------
def ensure_pool_schema():
    """Create the pool event table used by the manual Pool AN/AUS buttons."""
    import sqlite3

    db_path = Path(__file__).parent / 'energy_optimizer.db'
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                stopped_at TEXT,
                duration_min INTEGER,
                energy_kwh REAL,
                temp_start_c REAL,
                temp_end_c REAL,
                temp_gain_c REAL,
                power_w REAL,
                day_target_c REAL,
                evening_target_c REAL,
                pv_forecast_kwh REAL,
                sunshine_hours REAL,
                weather_condition TEXT,
                price_per_kwh REAL,
                note TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pool_events_started ON pool_events(started_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pool_events_stopped ON pool_events(stopped_at)")
        conn.commit()
    finally:
        conn.close()


def _format_minutes(total_min: int | float | None) -> str:
    if total_min is None:
        return '–'
    total_min = int(round(total_min))
    total_min = max(0, min(24 * 60 - 1, total_min))
    return f'{total_min // 60:02d}:{total_min % 60:02d}'


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _pool_weather_class(pv_kwh: float | None, sun_h: float | None) -> dict:
    pv_kwh = float(pv_kwh or 0)
    sun_h = float(sun_h or 0)
    if pv_kwh >= POOL_GOOD_PV_KWH and sun_h >= POOL_GOOD_SUN_HOURS:
        return {
            'class': 'good',
            'probability': 0.85,
            'target_c': POOL_EVENING_TARGET_C,
            'reason': f'gutes Pool-Wetter ({pv_kwh:.1f} kWh PV, {sun_h:.1f} h Sonne)',
        }
    if pv_kwh < POOL_BAD_PV_KWH or sun_h < POOL_BAD_SUN_HOURS:
        return {
            'class': 'bad',
            'probability': 0.08,
            'target_c': POOL_DEFAULT_MORNING_TEMP_C,
            'reason': f'schlechtes Pool-Wetter ({pv_kwh:.1f} kWh PV, {sun_h:.1f} h Sonne)',
        }
    return {
        'class': 'mixed',
        'probability': 0.45,
        'target_c': POOL_DAY_TARGET_C,
        'reason': f'wechselhaftes Pool-Wetter ({pv_kwh:.1f} kWh PV, {sun_h:.1f} h Sonne)',
    }


def read_pool_state() -> dict:
    state = {
        'active': False,
        'active_event_id': None,
        'started_at': None,
        'temp_start_c': POOL_DEFAULT_MORNING_TEMP_C,
        'power_w': POOL_POWER_W,
        'last_stopped_at': None,
        'last_energy_kwh': None,
        'last_duration_min': None,
        'last_temp_end_c': None,
        'temp_current_c': POOL_DEFAULT_MORNING_TEMP_C,
        'temp_updated_at': None,
        'target_c': POOL_DAY_TARGET_C,
    }
    try:
        if POOL_STATE_FILE.exists():
            data = json.loads(POOL_STATE_FILE.read_text())
            if isinstance(data, dict):
                state.update(data)
    except Exception:
        pass
    state['active'] = _bool_value(state.get('active'), False)
    try:
        state['power_w'] = float(state.get('power_w') or POOL_POWER_W)
    except Exception:
        state['power_w'] = POOL_POWER_W
    try:
        state['temp_start_c'] = float(state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C)
    except Exception:
        state['temp_start_c'] = POOL_DEFAULT_MORNING_TEMP_C
    try:
        state['temp_current_c'] = float(state.get('temp_current_c') or state.get('last_temp_end_c') or state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C)
    except Exception:
        state['temp_current_c'] = POOL_DEFAULT_MORNING_TEMP_C
    try:
        if state.get('last_temp_end_c') is not None:
            state['last_temp_end_c'] = float(state.get('last_temp_end_c'))
    except Exception:
        state['last_temp_end_c'] = None
    try:
        state['target_c'] = float(state.get('target_c') or POOL_DAY_TARGET_C)
    except Exception:
        state['target_c'] = POOL_DAY_TARGET_C
    return state


def write_pool_state(state: dict) -> dict:
    clean = read_pool_state()
    clean.update(state or {})
    POOL_STATE_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    return clean


def _pool_air_temp_c(services: dict | None = None) -> float | None:
    try:
        services = services or get_services()
        weather = services['weather'].get_current_weather()
        temp = weather.get('temperature')
        return float(temp) if temp is not None else None
    except Exception:
        return None


def estimate_pool_temperature(state: dict | None = None, services: dict | None = None,
                              now: datetime | None = None) -> dict:
    """Estimate current pool temperature from the last manual value/run.

    There is no pool sensor yet, so this is deliberately conservative:
    heating is based on the known 1.8 kW / 2 C per hour model, cooling is
    a slow weather-dependent drift since the last measured or stopped value.
    """
    state = state or read_pool_state()
    now_local = (now or now_berlin()).replace(tzinfo=None)

    if state.get('active'):
        started = parse_local_datetime(state.get('started_at'))
        temp_start = float(state.get('temp_start_c') or state.get('temp_current_c') or POOL_DEFAULT_MORNING_TEMP_C)
        elapsed_min = max(0, int((now_local - started).total_seconds() / 60)) if started else 0
        gain_c = elapsed_min / 60.0 * POOL_TEMP_GAIN_C_PER_HOUR
        return {
            'temp_c': round(temp_start + gain_c, 1),
            'source': 'active_estimate',
            'baseline_c': round(temp_start, 1),
            'elapsed_min': elapsed_min,
            'temp_gain_c': round(gain_c, 1),
            'cooling_c': 0.0,
        }

    baseline = state.get('temp_current_c')
    baseline_at = state.get('temp_updated_at')
    source = 'manual'
    if baseline is None:
        baseline = state.get('last_temp_end_c')
        baseline_at = state.get('last_stopped_at')
        source = 'last_stop'
    if baseline is None:
        baseline = state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C
        source = 'default'
    try:
        baseline = float(baseline)
    except Exception:
        baseline = POOL_DEFAULT_MORNING_TEMP_C
        source = 'default'

    marked_at = parse_local_datetime(baseline_at)
    if not marked_at:
        return {
            'temp_c': round(baseline, 1),
            'source': source,
            'baseline_c': round(baseline, 1),
            'elapsed_min': 0,
            'temp_gain_c': 0.0,
            'cooling_c': 0.0,
        }

    hours = max(0.0, (now_local - marked_at).total_seconds() / 3600.0)
    air_temp = _pool_air_temp_c(services)
    if air_temp is None:
        hourly_loss = POOL_COOL_BASE_C_PER_HOUR
    else:
        delta = max(0.0, baseline - air_temp)
        hourly_loss = POOL_COOL_BASE_C_PER_HOUR + delta * POOL_COOL_DELTA_FACTOR
        if 9 <= now_local.hour <= 18:
            hourly_loss *= 0.65
    hourly_loss = max(0.0, min(POOL_COOL_MAX_C_PER_HOUR, hourly_loss))
    cooling = min(20.0, hourly_loss * hours)

    # Plausibilitaet: Pool kann nie kaelter sein als Aussentemperatur
    # Bei Sonnenschein (tagsüber) zusaetzlich +3 C Aufschlag durch Solargewinne
    estimated = baseline - cooling
    if air_temp is not None:
        sun_bonus = 3.0 if 9 <= now_local.hour <= 18 else 0.0
        min_pool_temp = air_temp + sun_bonus
        estimated = max(estimated, min_pool_temp)
    estimated = max(4.0, estimated)

    return {
        'temp_c': round(estimated, 1),
        'source': source,
        'baseline_c': round(baseline, 1),
        'baseline_at': baseline_at,
        'elapsed_min': int(hours * 60),
        'temp_gain_c': 0.0,
        'cooling_c': round(cooling, 1),
        'air_temp_c': round(air_temp, 1) if air_temp is not None else None,
    }


def _current_pool_snapshot(services: dict | None = None) -> dict:
    services = services or get_services()
    now = now_berlin()
    pv_forecast = {}
    weather = {}
    prices = []
    try:
        pv_forecast = services['forecast'].get_pv_forecast()
    except Exception:
        pass
    try:
        weather = services['weather'].get_current_weather()
    except Exception:
        pass
    try:
        prices = services['tibber'].get_current_prices() or []
    except Exception:
        pass
    price = None
    for p in prices:
        try:
            ts = as_berlin(p['timestamp'])
            if ts.date() == now.date() and ts.hour == now.hour:
                price = p.get('price')
                break
        except Exception:
            continue
    return {
        'pv_forecast_kwh': pv_forecast.get('today_kwh') or (pv_forecast.get('total_w', 0) or 0) / 1000.0,
        'sunshine_hours': pv_forecast.get('sunshine_hours_today'),
        'weather_condition': weather.get('condition') or weather.get('description') or '',
        'temperature_c': weather.get('temperature'),
        'price_per_kwh': price,
    }


def _get_pool_events(start: datetime | None = None, end: datetime | None = None) -> list:
    import sqlite3

    ensure_pool_schema()
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT *
            FROM pool_events
            WHERE (? IS NULL OR COALESCE(stopped_at, datetime('now')) >= ?)
              AND (? IS NULL OR started_at <= ?)
            ORDER BY started_at DESC
        """, (
            db_datetime(start) if start else None,
            db_datetime(start) if start else None,
            db_datetime(end) if end else None,
            db_datetime(end) if end else None,
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _pool_required_minutes(target_c: float, start_c: float = POOL_DEFAULT_MORNING_TEMP_C) -> int:
    gain_needed = max(0.0, target_c - start_c)
    if POOL_TEMP_GAIN_C_PER_HOUR <= 0:
        return 0
    return int(round((gain_needed / POOL_TEMP_GAIN_C_PER_HOUR) * 60))


def get_pool_learning_summary(days: int = 90) -> dict:
    """Small app-level fallback; LearningService exposes the same data too."""
    now = now_berlin().replace(tzinfo=None)
    start = now - timedelta(days=days)
    events = [e for e in _get_pool_events(start, now) if e.get('stopped_at')]
    completed = []
    for e in events:
        st = parse_local_datetime(e.get('started_at'))
        en = parse_local_datetime(e.get('stopped_at'))
        if not st or not en or en <= st:
            continue
        duration = e.get('duration_min') or int((en - st).total_seconds() / 60)
        if duration < 10:
            continue
        completed.append({
            **e,
            'start_min': _minutes_since_midnight(st),
            'duration_min': duration,
            'energy_kwh': e.get('energy_kwh') or duration * float(e.get('power_w') or POOL_POWER_W) / 60000.0,
        })

    if not completed:
        return {
            'event_count': 0,
            'typical_start_min': None,
            'typical_start': None,
            'avg_duration_min': None,
            'avg_energy_kwh': None,
            'avg_power_w': POOL_POWER_W,
        }

    avg_start = sum(e['start_min'] for e in completed) / len(completed)
    avg_duration = sum(e['duration_min'] for e in completed) / len(completed)
    avg_energy = sum(e['energy_kwh'] for e in completed) / len(completed)
    avg_power = sum(float(e.get('power_w') or POOL_POWER_W) for e in completed) / len(completed)
    return {
        'event_count': len(completed),
        'typical_start_min': round(avg_start),
        'typical_start': _format_minutes(avg_start),
        'avg_duration_min': round(avg_duration),
        'avg_energy_kwh': round(avg_energy, 2),
        'avg_power_w': round(avg_power),
        'last_event': completed[0],
    }


def build_pool_plan(pv_forecast: dict | None = None, learning: dict | None = None,
                    day_offset: int = 0) -> dict:
    """Predict pool load for today/tomorrow without controlling the pool hardware."""
    pv_forecast = pv_forecast or {}
    learning = learning or get_pool_learning_summary()
    state = read_pool_state()
    now = now_berlin()
    target_date = (now + timedelta(days=day_offset)).date()

    if day_offset == 0:
        pv_kwh = pv_forecast.get('today_kwh') or (pv_forecast.get('total_w', 0) or 0) / 1000.0
        sun_h = pv_forecast.get('sunshine_hours_today')
    else:
        pv_kwh = pv_forecast.get('tomorrow_kwh') or 0
        sun_h = pv_forecast.get('sunshine_hours_tomorrow')

    weather = _pool_weather_class(pv_kwh, sun_h)
    state_target_c = float(state.get('target_c') or weather['target_c'])
    if day_offset == 0:
        weather = dict(weather)
        weather['target_c'] = state_target_c
        weather['reason'] += f' · Pool-Ziel {state_target_c:.1f} °C'
    start_min = learning.get('typical_start_min') or POOL_DEFAULT_START_MINUTE
    temp_info = estimate_pool_temperature(state, services=get_services())
    temp_start = float(temp_info.get('temp_c') or POOL_DEFAULT_MORNING_TEMP_C)
    required_min = _pool_required_minutes(weather['target_c'], temp_start)
    if learning.get('event_count', 0) >= 3 and learning.get('avg_duration_min'):
        avg = float(learning['avg_duration_min'])
        required_min = int(round(required_min * 0.65 + avg * 0.35))
    required_min = max(0, min(8 * 60, required_min))

    active = False
    source = 'forecast'
    probability = weather['probability']

    if day_offset == 0 and state.get('active'):
        started = parse_local_datetime(state.get('started_at')) or now.replace(tzinfo=None)
        start_min = _minutes_since_midnight(started)
        temp_start = float(temp_info.get('baseline_c') or state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C)
        required_min = max(
            _pool_required_minutes(weather['target_c'], temp_start),
            int((now.replace(tzinfo=None) - started).total_seconds() / 60) + 30,
        )
        probability = 1.0
        active = True
        source = 'manual_active'
    elif weather['class'] == 'bad':
        required_min = 0
    elif day_offset == 0 and now.date() == target_date and _minutes_since_midnight(now) > start_min + required_min:
        probability = 0.0
        required_min = 0

    end_min = min(24 * 60, start_min + required_min)
    expected_kwh = (POOL_POWER_W / 1000.0) * (required_min / 60.0) * probability
    temp_gain = (required_min / 60.0) * POOL_TEMP_GAIN_C_PER_HOUR
    target_reached = min(weather['target_c'], temp_start + temp_gain)

    return {
        'active': active,
        'source': source,
        'date': target_date.isoformat(),
        'weather_class': weather['class'],
        'weather_reason': weather['reason'],
        'probability': round(probability, 2),
        'expected': bool(active or probability >= 0.5),
        'power_w': round(POOL_POWER_W),
        'start_min': int(start_min),
        'end_min': int(end_min),
        'start': _format_minutes(start_min),
        'end': _format_minutes(end_min) if required_min > 0 else None,
        'duration_min': int(required_min),
        'expected_kwh': round(expected_kwh, 2),
        'temp_start_c': round(temp_start, 1),
        'temp_current_c': round(float(temp_info.get('temp_c') or temp_start), 1),
        'temp_source': temp_info.get('source'),
        'temp_cooling_c': temp_info.get('cooling_c'),
        'temp_target_c': round(weather['target_c'], 1),
        'temp_est_end_c': round(target_reached, 1),
        'target_c': round(weather['target_c'], 1),
        'day_target_c': POOL_DAY_TARGET_C,
        'evening_target_c': POOL_EVENING_TARGET_C,
        'pv_kwh': round(float(pv_kwh or 0), 1),
        'sunshine_hours': round(float(sun_h or 0), 1),
        'learning': {
            'event_count': learning.get('event_count', 0),
            'typical_start': learning.get('typical_start'),
            'avg_duration_min': learning.get('avg_duration_min'),
            'avg_energy_kwh': learning.get('avg_energy_kwh'),
        },
    }


def pool_curve_kwh(plan: dict) -> list:
    curve = [0.0] * 24
    if not plan or not plan.get('duration_min') or not plan.get('expected'):
        return curve
    probability = 1.0 if plan.get('active') else float(plan.get('probability') or 0)
    start_min = int(plan.get('start_min') or 0)
    end_min = int(plan.get('end_min') or 0)
    for hour in range(24):
        h_start = hour * 60
        h_end = h_start + 60
        overlap = max(0, min(end_min, h_end) - max(start_min, h_start))
        if overlap > 0:
            curve[hour] = (POOL_POWER_W / 1000.0) * (overlap / 60.0) * probability
    return curve


def pool_series_for_labels(labels: list, bucket_min: int, include_expected: bool = False,
                           expected_plan: dict | None = None) -> tuple[list, list]:
    if not labels:
        return [], []
    parsed = [parse_local_datetime(label) for label in labels]
    valid = [p for p in parsed if p is not None]
    if not valid:
        return [None] * len(labels), [None] * len(labels)
    start = min(valid) - timedelta(minutes=bucket_min)
    end = max(valid) + timedelta(minutes=bucket_min)
    events = _get_pool_events(start, end)
    now = now_berlin().replace(tzinfo=None)
    actual = []
    expected = []

    for dt in parsed:
        if dt is None:
            actual.append(None)
            expected.append(None)
            continue
        bucket_end = dt + timedelta(minutes=bucket_min)
        active_power = None
        for ev in events:
            st = parse_local_datetime(ev.get('started_at'))
            en = parse_local_datetime(ev.get('stopped_at')) or now
            if not st or en <= dt or st >= bucket_end:
                continue
            overlap = max(0.0, (min(en, bucket_end) - max(st, dt)).total_seconds() / 60.0)
            if overlap > 0:
                active_power = float(ev.get('power_w') or POOL_POWER_W) * min(1.0, overlap / max(1, bucket_min))
                break
        actual.append(round(active_power, 1) if active_power else None)

        exp_power = None
        if include_expected and expected_plan and dt >= now and expected_plan.get('expected'):
            minute = _minutes_since_midnight(dt)
            if int(expected_plan.get('start_min') or 0) <= minute < int(expected_plan.get('end_min') or 0):
                prob = 1.0 if expected_plan.get('active') else float(expected_plan.get('probability') or 0)
                exp_power = POOL_POWER_W * prob
        expected.append(round(exp_power, 1) if exp_power else None)

    return actual, expected


# ---------------------------------------------------------------------------
# Battery plan: ziel ist max TARGET_SOC_1830 % um 18:30
# ---------------------------------------------------------------------------
def hourly_pv_curve_kwh(total_kwh: float, hour_now: int) -> list:
    """Sehr einfache Bell-Kurve fuer PV-Erzeugung pro Stunde (06:00 - 21:00)."""
    weights = []
    for h in range(24):
        if h < 6 or h > 21:
            weights.append(0.0)
        else:
            # symmetrische Glocke um 13:00
            x = (h - 13.0) / 4.5
            weights.append(max(0.0, 1.0 - x * x))
    s = sum(weights) or 1.0
    return [w / s * total_kwh for w in weights]


# Realistisches Default-Verbrauchsprofil fuer Eresing (300 W Grundlast)
# Werte in kWh pro Stunde
DEFAULT_LOAD_CURVE_KWH = [
    0.25, 0.25, 0.25, 0.25, 0.25, 0.25,   # 00-05  Nacht/Grundlast (~250W Standby)
    0.30, 0.40, 0.35, 0.30, 0.30, 0.30,   # 06-11  Morgen
    0.35, 0.30, 0.30, 0.30, 0.35, 0.50,   # 12-17  Mittag/Nachmittag
    0.70, 0.80, 0.65, 0.45, 0.35, 0.25,   # 18-23  Abend (Herd, Waesche, TV)
]


def merged_load_curve(profile: dict | None) -> list:
    """Merge gelerntes Profil mit Default.

    Lernwerte werden ab dem ersten Messpunkt genutzt. Der LearningService
    liefert bereits ein konservativ gemischtes Profil; alte Profile ohne
    confidence werden hier ebenfalls anteilig eingemischt.

    WICHTIG: ``house_consumption_w`` vom SmartMeter enthaelt den GESAMTEN
    Verbrauch (Haus + Poolpumpe + Wallbox). Fuer die Prognose-Last muessen
    Pool und Wallbox abgezogen werden, da sie separat gesteuert werden.
    Pool wird spaeter bei Bedarf explizit addiert (pool_curve_kwh).
    Wallbox laeuft dynamisch und darf die Basis-Last nicht aufblaehen.
    """
    out = list(DEFAULT_LOAD_CURVE_KWH)
    if not profile:
        return out
    for h in range(24):
        entry = profile.get(h) or profile.get(str(h)) or {}
        # 1) Bevorzugt base_consumption_w (ohne Pool, falls korrekt berechnet)
        avg_w = entry.get('base_consumption_w')
        if avg_w is None:
            avg_w = entry.get('avg_consumption_w')
        if avg_w is None:
            continue
        # 2) Seit der Learning-Service-Verbesserung ist base_consumption_w
        #    bereits bereinigt (ohne Wallbox und Pool). Falls das Profil noch
        #    alte Daten hat, zusaetzlicher Plausibilitaets-Cap als Fallback.
        clean_w = max(100.0, avg_w)
        # Plausibilitaetscheck: reiner Hausverbrauch (ohne Pool/Wallbox)
        # Normalbereich 200-500 W, Abendspitze bis ~1 kW (Kochen, Backofen).
        # Werte ueber dem Cap deuten auf nicht-abgezogene Grossverbraucher.
        max_house_w = 800 if 16 <= h <= 21 else 500
        if clean_w > max_house_w:
            clean_w = max_house_w
        cnt = entry.get('sample_count', 0) or 0
        confidence = entry.get('confidence')
        if confidence is None:
            confidence = min(1.0, cnt / 12.0)
        if cnt > 0 or entry.get('source') == 'learned_blend':
            kwh = clean_w / 1000.0
            if 0.05 <= kwh <= 5.0:
                if entry.get('confidence') is None and cnt < 12:
                    out[h] = out[h] * (1.0 - confidence) + kwh * confidence
                else:
                    out[h] = kwh
    return out


def price_window_stats(prices: list, now: datetime, hours_ahead: int = 24) -> dict:
    """Return Tibber price context for the current hour and next hours."""
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    cutoff = current_hour + timedelta(hours=hours_ahead)
    by_hour = {}
    current_ct = None

    for p in prices or []:
        try:
            ts = as_berlin(p['timestamp']).replace(minute=0, second=0, microsecond=0)
            price_ct = float(p['price']) * 100.0
        except Exception:
            continue
        key = ts.isoformat()
        if ts == current_hour:
            current_ct = price_ct
        if current_hour <= ts < cutoff:
            old = by_hour.get(key)
            if old is None or price_ct < old['price_ct']:
                by_hour[key] = {'hour': key, 'price_ct': price_ct}

    window = sorted(by_hour.values(), key=lambda x: x['hour'])
    ranked = sorted(window, key=lambda x: x['price_ct'])
    rank = None
    current_key = current_hour.isoformat()
    for idx, item in enumerate(ranked, start=1):
        if item['hour'] == current_key:
            rank = idx
            break

    avg_ct = sum(x['price_ct'] for x in window) / len(window) if window else None
    return {
        'current_ct': current_ct,
        'avg_ct': avg_ct,
        'min_ct': ranked[0]['price_ct'] if ranked else None,
        'max_ct': max((x['price_ct'] for x in window), default=None),
        'rank': rank,
        'window_count': len(window),
        'cheapest_hours': ranked[:AUTO_GRID_CHARGE_CHEAP_RANK],
    }


def build_grid_charge_decision(
    soc: float,
    projected_soc_1830: float,
    target_soc_1830: float,
    pv_tomorrow_kwh: float,
    sun_tomorrow_h: float,
    load_curve: list,
    price_stats: dict,
) -> dict:
    """Decide whether the house battery should be grid-charged automatically."""
    current_ct = price_stats.get('current_ct')
    avg_ct = price_stats.get('avg_ct')
    rank = price_stats.get('rank')
    learned_day_load = sum(load_curve)
    learned_evening_load = sum(load_curve[18:24])

    weak_weather = pv_tomorrow_kwh < 12.0 or sun_tomorrow_h < 4.0
    very_weak_weather = pv_tomorrow_kwh < 8.0 or sun_tomorrow_h < 2.5
    target_soc = target_soc_1830
    if very_weak_weather:
        target_soc = max(target_soc, 90.0)
    elif weak_weather:
        target_soc = max(target_soc, 85.0)
    if learned_evening_load >= 4.5:
        target_soc = max(target_soc, 85.0)
    target_soc = min(target_soc, AUTO_GRID_CHARGE_TARGET_MAX_SOC)

    plan_gap = target_soc - projected_soc_1830
    soc_gap = target_soc - soc
    need_energy = plan_gap >= 5.0 or (weak_weather and soc_gap >= 8.0)

    cheap_abs = current_ct is not None and current_ct <= AUTO_GRID_CHARGE_ABS_CHEAP_CT
    cheap_relative = (
        current_ct is not None and avg_ct is not None and rank is not None
        and rank <= AUTO_GRID_CHARGE_CHEAP_RANK
        and current_ct <= AUTO_GRID_CHARGE_MAX_CT
        and (avg_ct - current_ct) >= AUTO_GRID_CHARGE_MIN_SAVINGS_CT
    )
    cheap = bool(cheap_abs or cheap_relative)
    should_charge = bool(cheap and need_energy and soc < (target_soc - 2.0))

    if current_ct is None:
        reason = 'kein aktueller Tibber-Preis'
    elif not cheap:
        reason = f'Preis {current_ct:.1f} ct nicht guenstig genug'
    elif not need_energy:
        reason = f'kein Bedarf: Prognose {projected_soc_1830:.1f}% bei Ziel {target_soc:.1f}%'
    elif soc >= (target_soc - 2.0):
        reason = f'Akku schon nahe Ziel ({soc:.1f}%/{target_soc:.1f}%)'
    else:
        price_reason = 'absolut guenstig' if cheap_abs else f'Rang {rank} der guenstigsten Stunden'
        reason = (
            f'Netzladen sinnvoll: {price_reason}, Ziel {target_soc:.1f}%, '
            f'morgen PV {pv_tomorrow_kwh:.1f} kWh, Lernlast abends {learned_evening_load:.1f} kWh'
        )

    return {
        'should_charge': should_charge,
        'target_soc': round(target_soc, 1),
        'max_w': AUTO_GRID_CHARGE_MAX_W,
        'price_ct': round(current_ct, 2) if current_ct is not None else None,
        'avg_price_ct': round(avg_ct, 2) if avg_ct is not None else None,
        'price_rank': rank,
        'cheap': cheap,
        'need_energy': bool(need_energy),
        'weak_weather': bool(weak_weather),
        'learned_day_load_kwh': round(learned_day_load, 2),
        'learned_evening_load_kwh': round(learned_evening_load, 2),
        'reason': reason,
    }


def build_battery_plan(services: dict) -> dict:
    """Schreibe die battery_plan.json fuer den pv_controller.

    Berechnet projizierten Akku-SOC um 18:30 basierend auf:
    - aktuellem SOC
    - PV-Forecast verteilt auf Stunden
    - geschaetztem Hausverbrauch
    - moeglicher EV-Ladung (wenn Modus=standard und Auto da)
    """
    state = services['evcc'].get_current_state()
    pv_forecast = services['forecast'].get_pv_forecast()
    prices = services['tibber'].get_current_prices() or []

    now = now_berlin()
    target_dt = now.replace(hour=18, minute=30, second=0, microsecond=0)

    price_stats = price_window_stats(prices, now)
    current_price_ct = price_stats.get('current_ct')

    soc = (state.get('battery_soc', 0) or 0) * 100  # %
    pv_total_kwh = pv_forecast.get('today_kwh') or (pv_forecast.get('total_w', 0) or 0) / 1000.0
    pv_tomorrow_kwh = pv_forecast.get('tomorrow_kwh') or 0
    sun_tomorrow_h = pv_forecast.get('sunshine_hours_tomorrow') or 0
    pv_curve = hourly_pv_curve_kwh(pv_total_kwh, now.hour)

    # Dynamisches Ziel-SOC um 18:30 abhaengig vom morgigen Wetter
    # Viel Sonne morgen   -> Akku heute Abend leer (65%) - PV laedt morgen voll
    # Mittel               -> Akku auf 80%
    # Wenig Sonne morgen  -> Akku heute moeglichst voll (90%) als Reserve
    if pv_tomorrow_kwh >= 25:
        dyn_target = 65.0
        weather_hint = f"morgen sonnig ({pv_tomorrow_kwh:.1f} kWh erwartet)"
    elif pv_tomorrow_kwh >= 12:
        dyn_target = 80.0
        weather_hint = f"morgen wechselhaft ({pv_tomorrow_kwh:.1f} kWh erwartet)"
    else:
        dyn_target = 90.0
        weather_hint = f"morgen wenig Sonne ({pv_tomorrow_kwh:.1f} kWh erwartet)"

    # Geschaetzter Hausverbrauch je Stunde (kWh) - aus Profil + Default-Mix
    try:
        profile = services['learning'].get_daily_profile()
    except Exception as e:
        logger.debug(f'profile fallback ({e})')
        profile = None
    base_load_curve = merged_load_curve(profile)
    try:
        pool_learning = services['learning'].get_pool_learning_summary()
    except Exception as e:
        logger.debug(f'pool learning fallback ({e})')
        pool_learning = get_pool_learning_summary()
    pool_plan = build_pool_plan(pv_forecast, pool_learning, day_offset=0)
    pool_used_in_plan = bool(pool_plan.get('active') or (pool_learning.get('event_count', 0) >= 2 and pool_plan.get('expected')))
    pool_curve = pool_curve_kwh(pool_plan) if pool_used_in_plan else [0.0] * 24
    pool_plan['used_in_battery_plan'] = pool_used_in_plan
    load_curve = [base_load_curve[h] + pool_curve[h] for h in range(24)]

    # Akku-Bilanz simulieren bis 18:30
    sim_soc = soc
    if now < target_dt:
        for h in range(now.hour, 19):
            pv = pv_curve[h] if h < 24 else 0
            load = load_curve[h] if h < len(load_curve) else 0.5
            # Begrenzung der letzten Stunde auf Anteil bis 18:30
            if h == 18:
                pv *= 0.5
                load *= 0.5
            net = pv - load   # positiv = lade Akku, negativ = entlade
            sim_soc += (net / BATTERY_USABLE_KWH) * 100
            sim_soc = max(0.0, min(100.0, sim_soc))

    projected_overshoot = sim_soc > dyn_target + 2.0
    # Wichtig: PV-Laden erst begrenzen, wenn der Akku das Tagesziel bereits
    # erreicht hat. Eine reine Prognose darf einen leeren Akku nicht am Laden
    # hindern.
    charge_limit_eligible = soc >= dyn_target - 1.0

    grid_charge = build_grid_charge_decision(
        soc=soc,
        projected_soc_1830=sim_soc,
        target_soc_1830=dyn_target,
        pv_tomorrow_kwh=pv_tomorrow_kwh,
        sun_tomorrow_h=sun_tomorrow_h,
        load_curve=load_curve,
        price_stats=price_stats,
    )

    plan = {
        'updated': now.isoformat(),
        'target_soc_1830': dyn_target,
        'current_soc': round(soc, 1),
        'projected_soc_1830': round(sim_soc, 1),
        'current_price_ct': round(current_price_ct, 2) if current_price_ct is not None else None,
        'pv_total_today_kwh': round(pv_total_kwh, 2),
        'pv_tomorrow_kwh': round(pv_tomorrow_kwh, 2),
        'sunshine_hours_tomorrow': round(sun_tomorrow_h, 1),
        'pv_remaining_kwh': round(sum(pv_curve[now.hour:]), 2),
        'overshoot_projected': projected_overshoot,
        'overshoot': projected_overshoot and charge_limit_eligible,
        'undershoot': sim_soc < dyn_target - 5.0,
        'weather_hint': weather_hint,
        'learned_load_today_kwh': round(sum(load_curve), 2),
        'base_load_today_kwh': round(sum(base_load_curve), 2),
        'pool_expected_kwh': round(sum(pool_curve), 2),
        'pool': pool_plan,
        'learned_evening_load_kwh': grid_charge['learned_evening_load_kwh'],
        'price_stats': {
            'avg_ct': round(price_stats['avg_ct'], 2) if price_stats.get('avg_ct') is not None else None,
            'min_ct': round(price_stats['min_ct'], 2) if price_stats.get('min_ct') is not None else None,
            'max_ct': round(price_stats['max_ct'], 2) if price_stats.get('max_ct') is not None else None,
            'rank': price_stats.get('rank'),
            'window_count': price_stats.get('window_count'),
        },
        'grid_charge': grid_charge,
    }
    return plan


# Direkter BYD-Akku-Zugriff via Modbus (SunSpec 124)
try:
    import sys as _sys
    _sys.path.insert(0, '/home/werner')
    from battery_modbus import BatteryModbus
    _battery_modbus = BatteryModbus()
    BATTERY_MODBUS_OK = True
except Exception as _e:
    logger.warning(f'BatteryModbus nicht verfuegbar: {_e}')
    _battery_modbus = None
    BATTERY_MODBUS_OK = False

# Letzter gesetzter Akku-Modus (verhindert unnoetige Modbus-Schreibvorgaenge)
_last_battery_action = {'action': None, 'pct': None}


def apply_battery_control(plan: dict):
    """Akku-Steuerung: IMMER voll laden!

    Grundregel: PV-Ueberschuss geht IMMER in den Akku.
    65% SOC um 18:30 ist nur das Minimum-Sicherheitsnetz.
    Ladung wird NIE begrenzt - erst bei 100% geht Strom ins Netz.

    Ausnahmen:
    - Force-Charge aktiv: Netzladung bis Ziel-SOC
    - Auto-Grid-Charge: guenstiger Netzstrom zum Laden nutzen
    - Programm aus / manueller Modus: keine Automatik
    """
    global _last_battery_action
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return
    if not is_program_enabled():
        if _last_battery_action.get('action') != 'program_off':
            try:
                _battery_modbus.release()
            except Exception as e:
                logger.debug(f'program off release: {e}')
            _last_battery_action = {'action': 'program_off', 'pct': None}
            logger.info('Battery auto-control skipped (program off)')
        return
    # Wenn Force-Charge aktiv: nicht ueberschreiben - aber Auto-Stop wenn:
    #   a) SOC nahe Ziel (Toleranz 2%, Akkus erreichen oft nicht exakt 100%)
    #   b) Cap: Wenn Ziel >= 98% behandeln wir 96.5% schon als "voll genug"
    #   c) Akku-BMS sagt FULL/HOLDING und SOC > 90%  (BYD weigert sich weiter zu laden)
    #   d) Notbremse: laeuft laenger als 4h
    if _force_charge.get('active'):
        try:
            bm = _battery_modbus.read()
            current_soc = bm.get('soc', 0)
            cha_st_name = bm.get('cha_st_name', '')
            target = float(_force_charge.get('target_soc', 100))
            # bei Ziel >=98% nehmen wir 96.5% als "voll genug" (BYD endet typisch bei 97-98%)
            effective_target = min(target, 96.5) if target >= 98 else target
            tolerance = 2.0
            stop_reason = None
            if current_soc >= (effective_target - tolerance):
                stop_reason = f'SOC {current_soc}% nahe Ziel {target}%'
            elif current_soc > 90 and cha_st_name in ('FULL', 'HOLDING'):
                stop_reason = f'Akku-BMS meldet {cha_st_name} bei {current_soc}%'
            else:
                # Notbremse: 4h
                started_iso = _force_charge.get('started')
                if started_iso:
                    try:
                        started_dt = datetime.fromisoformat(started_iso)
                        elapsed_h = (now_berlin() - started_dt).total_seconds() / 3600.0
                        if elapsed_h > 4:
                            stop_reason = f'Notbremse: laeuft seit {elapsed_h:.1f} h'
                    except Exception:
                        pass
            if stop_reason:
                logger.info(f'Force-Charge Auto-Stop: {stop_reason}')
                _battery_modbus.release()
                _force_charge['active'] = False
                _last_battery_action = {'action': 'released', 'pct': 100}
        except Exception as e:
            logger.debug(f'force-charge auto-stop check: {e}')
        return
    if read_automation_mode() == 'manual':
        if _last_battery_action.get('action') != 'manual_skip':
            logger.info('Battery auto-control skipped (manual mode)')
            _last_battery_action = {'action': 'manual_skip', 'pct': None}
        return
    try:
        grid_charge = plan.get('grid_charge') or {}
        if grid_charge.get('should_charge'):
            target = float(grid_charge.get('target_soc') or plan.get('target_soc_1830') or 80)
            max_w = int(grid_charge.get('max_w') or AUTO_GRID_CHARGE_MAX_W)
            action = {'action': 'auto_grid_charge', 'pct': round(target, 1), 'max_w': max_w}
            if _last_battery_action != action:
                _battery_modbus.force_charge(target, max_w)
                _last_battery_action = action
                logger.info(
                    f"Auto-Grid-Charge START: target={target}% max={max_w}W "
                    f"price={grid_charge.get('price_ct')}ct reason={grid_charge.get('reason')}"
                )
            return

        if _last_battery_action.get('action') == 'auto_grid_charge':
            _battery_modbus.release()
            _last_battery_action = {'action': 'released', 'pct': 100}
            logger.info(f"Auto-Grid-Charge STOP: {grid_charge.get('reason', 'Bedingung nicht mehr aktiv')}")

        current_soc = float(plan.get('current_soc') or 0)
        target_soc = float(plan.get('target_soc_1830') or 0)
        if current_soc < target_soc - 1.0:
            if _last_battery_action['action'] != 'released':
                _battery_modbus.release()
                _last_battery_action = {'action': 'released', 'pct': 100}
                logger.info(
                    f'Battery release (SOC {current_soc:.1f}% unter Ziel {target_soc:.1f}%, '
                    'PV-Laden nicht begrenzen)'
                )
            return

        # Tageszeit beachten - nachts macht das Sperren keinen Sinn
        now = now_berlin()
        # Zwischen 06:00 und 18:30 ueberhaupt nur drosseln
        if not (now.replace(hour=6, minute=0) <= now <= now.replace(hour=18, minute=30)):
            if _last_battery_action['action'] != 'released':
                _battery_modbus.release()
                _last_battery_action = {'action': 'released', 'pct': 100}
                logger.info('Battery release (auseralb 06-18:30)')
            return

        # GRUNDREGEL: Akku IMMER voll laden!
        # 65% um 18:30 ist nur das Minimum-Sicherheitsnetz fuer die Nacht.
        # Darueber hinaus: PV-Ueberschuss geht IMMER in den Akku.
        # Erst wenn Akku voll (100% / BMS sagt FULL) geht Strom ins Netz.
        # → Keine Ladebegrenzung, kein Overshoot-Limit, kein Preis-Check!
        if _last_battery_action['action'] != 'released':
            _battery_modbus.release()
            _last_battery_action = {'action': 'released', 'pct': 100}
            logger.info(
                f'Battery release (Eigenverbrauch-Vorrang: '
                f'Akku immer voll laden, SOC={current_soc:.1f}%)'
            )
    except Exception as e:
        logger.warning(f'apply_battery_control failed: {e}')


def write_battery_plan_job():
    """Aktualisiert /home/werner/.nexus/battery_plan.json fuer den pv_controller
    UND wendet die Akku-Steuerung via Modbus an."""
    try:
        services = get_services()
        plan = build_battery_plan(services)

        # Akku-Live-Daten via Modbus mitschreiben (falls verfuegbar)
        if BATTERY_MODBUS_OK and _battery_modbus:
            try:
                bm = _battery_modbus.read()
                if 'error' not in bm:
                    plan['battery_modbus'] = bm
                    # SOC vom Modbus ist genauer als 0-100% Solar API * 100
                    plan['current_soc'] = bm['soc']
                    target_soc = float(plan.get('target_soc_1830') or 0)
                    projected_soc = float(plan.get('projected_soc_1830') or 0)
                    current_soc = float(plan.get('current_soc') or 0)
                    plan['overshoot_projected'] = projected_soc > target_soc + 2.0
                    plan['overshoot'] = bool(
                        plan['overshoot_projected']
                        and current_soc >= target_soc - 1.0
                    )
            except Exception as e:
                logger.debug(f'modbus read in plan: {e}')

        BATTERY_PLAN_FILE.write_text(json.dumps(plan, indent=2))
        logger.info(
            f"BatteryPlan: SOC={plan['current_soc']}% -> 18:30 proj={plan['projected_soc_1830']}% "
            f"(target {plan['target_soc_1830']}%) price={plan['current_price_ct']}ct "
            f"pv_rest={plan['pv_remaining_kwh']}kWh overshoot={plan['overshoot']} "
            f"grid_charge={plan.get('grid_charge', {}).get('should_charge')} "
            f"pool_used={plan.get('pool', {}).get('used_in_battery_plan')}:{plan.get('pool_expected_kwh')}kWh "
            f"pool_expected={plan.get('pool', {}).get('expected')}"
        )

        # Akku-Steuerung anwenden
        apply_battery_control(plan)

    except Exception as e:
        logger.error(f'write_battery_plan_job failed: {e}')

def get_services():
    """Lazily initialize services via Plugin-System.

    Nutzt PluginRegistry + Adapter fuer Rueckwaertskompatibilitaet:
      services['evcc']   → InverterAdapter (delegiert an InverterBase-Plugin)
      services['tibber'] → TariffAdapter (delegiert an TariffBase-Plugin)
    Alle bestehenden Aufrufe funktionieren unveraendert.
    """
    global tibber_service, forecast_service, weather_service, evcc_service
    global optimizer_service, learning_service, license_service, plugin_registry
    global wallbox_service

    app_dir = Path(__file__).parent

    # Plugin-Registry initialisieren (liest config.yaml)
    if plugin_registry is None:
        plugin_registry = PluginRegistry(str(app_dir))

    # Wechselrichter via Plugin + Adapter (ersetzt EVCCService)
    if evcc_service is None:
        inverter = plugin_registry.get_inverter()
        if inverter:
            evcc_service = InverterAdapter(inverter)
            logger.info(f"Inverter Plugin: {inverter.plugin_name()} via Adapter")
        else:
            # Fallback: alter EVCCService (falls kein Plugin konfiguriert)
            evcc_service = EVCCService(
                api_url=os.getenv('EVCC_API_URL'),
                api_token=os.getenv('EVCC_API_TOKEN')
            )
            logger.info("Inverter: Fallback auf EVCCService (kein Plugin)")

    # Stromtarif via Plugin + Adapter (ersetzt TibberService)
    if tibber_service is None:
        tariff = plugin_registry.get_tariff()
        if tariff:
            tibber_service = TariffAdapter(tariff)
            logger.info(f"Tariff Plugin: {tariff.plugin_name()} via Adapter")
        else:
            # Fallback: alter TibberService
            tibber_service = TibberService(api_token=os.getenv('TIBBER_API_TOKEN'))
            logger.info("Tariff: Fallback auf TibberService (kein Plugin)")

    # Wallbox via Plugin + Adapter
    if wallbox_service is None:
        wallbox = plugin_registry.get_wallbox()
        if wallbox:
            wallbox_service = WallboxAdapter(wallbox)
            logger.info(f"Wallbox Plugin: {wallbox.plugin_name()} via Adapter")
        else:
            wallbox_service = None
            logger.debug("Keine Wallbox konfiguriert")

    if forecast_service is None:
        forecast_service = ForecastSolarService(
            api_key=os.getenv('FORECAST_SOLAR_API_KEY'),
            lat=float(os.getenv('FORECAST_SOLAR_LAT', 52.52)),
            lon=float(os.getenv('FORECAST_SOLAR_LON', 13.405)),
            kwp=float(os.getenv('FORECAST_SOLAR_KWP', 6.0))
        )

    if weather_service is None:
        weather_service = WeatherService(
            lat=float(os.getenv('OPEN_METEO_LAT', 52.52)),
            lon=float(os.getenv('OPEN_METEO_LON', 13.405))
        )

    if optimizer_service is None:
        optimizer_service = Optimizer(
            battery_capacity=float(os.getenv('BATTERY_USABLE_KWH', 7.0)),
            max_charge_power=float(os.getenv('WATTPLOT_MAX_KW', 22)) * 1000
        )

    if learning_service is None:
        db_path = app_dir / 'energy_optimizer.db'
        learning_service = LearningService(db_path)

    if license_service is None:
        license_service = LicenseService(app_dir)

    # Payment Service (PayPal)
    config = {}
    config_path = app_dir / 'config.yaml'
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass
    payment_service = PaymentService(config)

    return {
        'tibber': tibber_service,
        'forecast': forecast_service,
        'weather': weather_service,
        'evcc': evcc_service,
        'wallbox': wallbox_service,
        'optimizer': optimizer_service,
        'learning': learning_service,
        'license': license_service,
        'payment': payment_service,
        'registry': plugin_registry,
    }


def collect_data_job():
    """Collect all data every 15 minutes using direct SQL."""
    logger.info("Collecting data...")
    
    import sqlite3
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    
    try:
        services = get_services()
        
        # 1. Get current state from EVCC/Fronius
        state = services['evcc'].get_current_state()
        
        # 2. Get Tibber prices
        prices = services['tibber'].get_current_prices()
        
        # 3. Get weather
        weather = services['weather'].get_current_weather()
        
        # 4. Get PV forecast
        pv_forecast = services['forecast'].get_pv_forecast()
        
        # Get current price (first available)
        current_price = 0
        if prices and len(prices) > 0:
            current_price = prices[0].get('price', 0)
        
        # Store in database using direct SQL
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO energy_readings 
            (timestamp, house_consumption_w, pv_production_w, battery_soc, battery_charge_w,
             grid_power_w, wattpilot_power_w, temperature, weather_condition, pv_forecast_w, price_per_kwh)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            state.get('house_consumption', 0),
            state.get('pv_production', 0),
            state.get('battery_soc', 0),
            state.get('battery_charge', 0),
            state.get('grid_power', 0),
            state.get('wattpilot_power', 0),
            weather.get('temperature', 0),
            weather.get('condition', 'unknown'),
            pv_forecast.get('total_w', 0),
            current_price
        ))
        
        conn.commit()
        conn.close()
        
        logger.info("Data collection completed successfully")
        
    except Exception as e:
        logger.error(f"Data collection failed: {e}")


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _delete_old_rows(cursor, table_name: str, column_name: str, cutoff: str) -> int:
    if not _table_exists(cursor, table_name):
        return 0
    cursor.execute(f"DELETE FROM {table_name} WHERE {column_name} < ?", (cutoff,))
    return cursor.rowcount if cursor.rowcount is not None else 0


def maintenance_job():
    """Keep the long-running Mini-PC installation compact and recoverable."""
    import sqlite3

    db_path = Path(__file__).parent / 'energy_optimizer.db'
    conn = None
    try:
        ensure_pool_schema()
        now = now_berlin()
        cutoff_readings = (now - timedelta(days=ENERGY_READING_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        cutoff_prices = (now - timedelta(days=PRICE_DATA_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        cutoff_forecast = (now - timedelta(days=PV_FORECAST_RETENTION_DAYS)).strftime('%Y-%m-%d')

        conn = sqlite3.connect(str(db_path), timeout=15)
        cursor = conn.cursor()

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_energy_readings_timestamp ON energy_readings(timestamp)")
        if _table_exists(cursor, 'price_data'):
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_data_timestamp ON price_data(timestamp)")

        removed_readings = _delete_old_rows(cursor, 'energy_readings', 'timestamp', cutoff_readings)
        removed_prices = _delete_old_rows(cursor, 'price_data', 'timestamp', cutoff_prices)
        removed_forecasts = _delete_old_rows(cursor, 'pv_forecast_log', 'date', cutoff_forecast)

        conn.commit()

        cursor.execute("PRAGMA optimize")
        try:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as e:
            logger.debug(f"maintenance wal_checkpoint skipped: {e}")

        db_size_kb = db_path.stat().st_size / 1024 if db_path.exists() else 0
        logger.info(
            "Maintenance completed: "
            f"db={db_size_kb:.0f}KB removed readings={removed_readings}, "
            f"prices={removed_prices}, forecasts={removed_forecasts}"
        )

        # Langzeit-Lernprofil aktualisieren (EMA aus gestrigen Daten)
        try:
            services = get_services()
            services['learning'].update_aggregated_profile()
        except Exception as e:
            logger.warning(f"Learned profile update failed: {e}")

        # Lizenz online verifizieren (1x taeglich)
        try:
            services = get_services()
            lic = services['license']
            if lic.should_verify():
                if lic.verify_online():
                    logger.info("Lizenz online verifiziert: OK")
                else:
                    status = lic.get_status()
                    logger.warning(f"Lizenz-Status: {status['mode']} — {status['message']}")
        except Exception as e:
            logger.warning(f"License verification failed: {e}")
    except Exception as e:
        logger.warning(f"Maintenance failed: {e}")
    finally:
        if conn is not None:
            conn.close()



def forecast_log_job():
    """Taeglich um 23:30: PV-Prognose vs. tatsaechliche Produktion in pv_forecast_log speichern."""
    import sqlite3

    db_path = Path(__file__).parent / 'energy_optimizer.db'
    today = now_berlin().strftime('%Y-%m-%d')

    try:
        services = get_services()

        # 1. Forecast fuer heute holen (kWh)
        pv_forecast = services['forecast'].get_pv_forecast()
        forecast_kwh = pv_forecast.get('today_kwh') or (pv_forecast.get('total_w', 0) or 0) / 1000.0

        # 2. Tatsaechliche PV-Produktion aus energy_readings (15-min Intervalle)
        conn = sqlite3.connect(str(db_path), timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            """SELECT SUM(pv_production_w) / 4.0 / 1000.0
               FROM energy_readings
               WHERE date(timestamp) = ?""",
            (today,),
        )
        row = cursor.fetchone()
        actual_kwh = round(row[0], 2) if row and row[0] else 0.0

        # 3. Wetter-Klasse bestimmen
        weather = services['weather'].get_current_weather()
        condition = (weather.get('condition') or weather.get('description') or 'unknown').lower()
        if any(w in condition for w in ('clear', 'sun', 'fair')):
            weather_class = 'clear'
        elif any(w in condition for w in ('cloud', 'overcast', 'partly')):
            weather_class = 'cloudy'
        elif any(w in condition for w in ('rain', 'drizzle', 'shower', 'storm')):
            weather_class = 'rainy'
        else:
            weather_class = 'unknown'

        # 4. Ratio berechnen (actual / forecast)
        ratio = round(actual_kwh / forecast_kwh, 3) if forecast_kwh > 0 else None

        # 5. In pv_forecast_log schreiben
        cursor.execute(
            """INSERT OR REPLACE INTO pv_forecast_log
               (date, forecast_kwh, actual_kwh, weather_class, ratio, updated)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (today, round(forecast_kwh, 2), actual_kwh, weather_class,
             ratio, now_berlin().isoformat()),
        )
        conn.commit()
        conn.close()

        logger.info(
            f"PV Forecast Log: {today} forecast={forecast_kwh:.1f} kWh, "
            f"actual={actual_kwh:.1f} kWh, ratio={ratio}, weather={weather_class}"
        )
    except Exception as e:
        logger.error(f"PV Forecast Log failed: {e}")


def optimize_job():
    """Supervise automation state.

    Hardware control is intentionally split:
      - pv_controller.py adjusts the Wallbox only when an EV is connected.
      - write_battery_plan_job/apply_battery_control handles battery limits.

    This job must not send RemoteStart/RemoteStop commands.
    """
    logger.info("Running automation supervisor...")
    try:
        global current_mode
        current_mode = read_automation_mode()
        services = get_services()

        # Lizenz-Check: ohne aktive Lizenz keine Optimierung
        if not services['license'].is_feature_enabled('tibber_optimization'):
            logger.info("Lizenz abgelaufen — Optimierung pausiert")
            return

        if not is_program_enabled():
            logger.info("Program off - automation supervisor paused")
            return

        if current_mode == 'manual':
            logger.info("Manual mode - automatic optimizer is paused")
            return

        # Get current state
        state = services['evcc'].get_current_state()

        # Get prices
        prices = services['tibber'].get_current_prices()

        # Get NEXT hour price (we decide based on what we pay next, not last hour)
        now = now_berlin()
        next_hour = (now.hour + 1) % 24
        current_price_ct = 0
        for p in prices:
            if p['timestamp'].hour == next_hour:
                current_price_ct = p['price'] * 100
                break
        # Fallback to current hour if next hour not found
        if current_price_ct == 0:
            for p in prices:
                if p['timestamp'].hour == now.hour:
                    current_price_ct = p['price'] * 100
                    break

        # Get Wattpilot status
        wattpilot = services['evcc'].get_wattpilot_status()
        battery_soc = state.get('battery_soc', 0) * 100
        cstate = read_controller_state()
        decision = cstate.get('decision') or {}

        logger.info(
            f"Automation supervisor: mode={current_mode} wallbox={read_wallbox_mode()} "
            f"price={current_price_ct:.1f}ct battery={battery_soc:.0f}% "
            f"wattpilot={wattpilot.get('status')} {wattpilot.get('power_w', 0)}W "
            f"controller_action={decision.get('action')} reason={decision.get('reason')}"
        )

    except Exception as e:
        logger.error(f"Optimization failed: {e}")


def generate_morning_report():
    """Generate morning report with daily summary."""
    logger.info("Generating morning report...")
    try:
        services = get_services()
        
        # Get yesterday's data
        yesterday = datetime.now() - timedelta(days=1)
        readings = EnergyReading.query.filter(
            EnergyReading.timestamp >= yesterday.replace(hour=0, minute=0),
            EnergyReading.timestamp < yesterday.replace(hour=23, minute=59)
        ).all()
        
        if not readings:
            return "No data available for yesterday"
        
        # Calculate stats
        total_consumption = sum(abs(r.house_consumption_w or 0) for r in readings) / len(readings)
        total_pv = sum(r.pv_production_w for r in readings) / len(readings)
        avg_battery_soc = sum(r.battery_soc for r in readings) / len(readings) * 100
        
        # Get today's prices
        prices = services['tibber'].get_current_prices()
        cheap_hours = [p for p in prices if p['price'] < 20]  # < 20 ct/kWh
        
        report = f"""
=== Energy Optimizer Morning Report ===
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}

Yesterday Summary:
- Avg. House Consumption: {total_consumption/1000:.1f} kW
- Avg. PV Production: {total_pv/1000:.1f} kW
- Avg. Battery SoC: {avg_battery_soc:.1f} %

Today Recommendations:
- Cheap Hours (<20ct): {len(cheap_hours)} hours
- Best Charging Windows: {cheap_hours[:3] if cheap_hours else 'None identified'}

Current Mode: {read_automation_mode().upper()}
Battery Status: {services['evcc'].get_current_state().get('battery_soc', 0)*100:.0f}%

Weather Today: {services['weather'].get_current_weather().get('condition', 'unknown')}
PV Forecast: {services['forecast'].get_pv_forecast().get('total_w', 0)/1000:.1f} kW peak
"""
        return report
        
    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        return f"Error generating report: {e}"


# Login routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    nxt = request.args.get('next') or request.form.get('next') or '/'
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user_data = USERS.get(username) or {}
        stored_password = user_data.get('password') if isinstance(user_data, dict) else user_data
        if username in USERS and stored_password == password:
            user = User(username)
            login_user(user, remember=True)
            session.permanent = True
            return redirect(nxt if nxt.startswith('/') else '/')
        error = 'Login fehlgeschlagen'
    return render_template('login.html', error=error, next=nxt)


@app.route('/api/login', methods=['POST'])
def api_login():
    """JSON login endpoint (alternative zu HTML form)."""
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    user_data = USERS.get(username) or {}
    stored_password = user_data.get('password') if isinstance(user_data, dict) else user_data
    if username in USERS and stored_password == password:
        login_user(User(username), remember=True)
        session.permanent = True
        return jsonify({'success': True, 'user': username, 'role': user_role(username), 'readonly': user_role(username) == 'guest'})
    return jsonify({'success': False, 'error': 'invalid credentials'}), 401


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/api/whoami')
def api_whoami():
    return jsonify({
        'user': current_user.id if current_user.is_authenticated else None,
        'authenticated': current_user.is_authenticated,
        'role': user_role(current_user.id) if current_user.is_authenticated else None,
        'readonly': is_readonly_user(),
    })


# ---------------------------------------------------------------------------
# Heatpump (Bosch CS5000 DW) - vorbereitet, deaktiviert bis Geraet da ist
# ---------------------------------------------------------------------------
_heatpump = HeatpumpService() if HEATPUMP_AVAILABLE else None


@app.route('/api/heatpump/status')
def api_heatpump_status():
    if _heatpump is None:
        return jsonify({'enabled': False, 'reason': 'service nicht geladen'})
    return jsonify(_heatpump.status())


@app.route('/api/heatpump/sg_ready', methods=['POST'])
def api_heatpump_sg_ready():
    if _heatpump is None:
        return jsonify({'success': False, 'error': 'service nicht geladen'}), 503
    data = request.get_json(silent=True) or {}
    try:
        level = int(data.get('level', 2))
    except Exception:
        return jsonify({'success': False, 'error': 'level int 1..4'}), 400
    return jsonify(_heatpump.set_sg_ready(level))


@app.route('/api/heatpump/auto_decide')
def api_heatpump_auto_decide():
    """Schlaegt SG-Ready-Level vor (regelbasiert, kein Schreiben)."""
    if _heatpump is None:
        return jsonify({'recommended': 2, 'reason': 'service nicht geladen'})
    cstate = read_controller_state()
    fr = (cstate.get('fronius') or {})
    plan = {}
    try:
        if BATTERY_PLAN_FILE.exists():
            plan = json.loads(BATTERY_PLAN_FILE.read_text())
    except Exception:
        pass
    return jsonify(_heatpump.auto_decide(
        pv_w=fr.get('pv_w', 0),
        surplus_w=(cstate.get('decision') or {}).get('surplus_w', 0),
        soc=fr.get('soc', 0),
        price_ct=plan.get('current_price_ct')
    ))


# ---------------------------------------------------------------------------
# Pool marker: manuell AN/AUS fuer Lernmodus und Prognose
# ---------------------------------------------------------------------------
@app.route('/api/pool/status')
def api_pool_status():
    services = get_services()
    try:
        pv_forecast = services['forecast'].get_pv_forecast()
    except Exception:
        pv_forecast = {}
    try:
        learning = services['learning'].get_pool_learning_summary()
    except Exception:
        learning = get_pool_learning_summary()

    state = read_pool_state()
    temp_info = estimate_pool_temperature(state, services)
    now = now_berlin().replace(tzinfo=None)
    active_info = {}
    if state.get('active'):
        started = parse_local_datetime(state.get('started_at'))
        if started:
            elapsed_min = max(0, int((now - started).total_seconds() / 60))
            power_w = float(state.get('power_w') or POOL_POWER_W)
            temp_start = float(state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C)
            temp_gain = elapsed_min / 60.0 * POOL_TEMP_GAIN_C_PER_HOUR
            active_info = {
                'elapsed_min': elapsed_min,
                'energy_kwh': round(elapsed_min / 60.0 * power_w / 1000.0, 2),
                'temp_est_c': temp_info.get('temp_c', round(temp_start + temp_gain, 1)),
                'temp_gain_c': temp_info.get('temp_gain_c', round(temp_gain, 1)),
            }

    today_plan = build_pool_plan(pv_forecast, learning, day_offset=0)
    tomorrow_plan = build_pool_plan(pv_forecast, learning, day_offset=1)
    return jsonify({
        'state': state,
        'active_info': active_info,
        'current_temp_c': temp_info.get('temp_c'),
        'temperature': temp_info,
        'today': today_plan,
        'tomorrow': tomorrow_plan,
        'learning': learning,
    })


@app.route('/api/pool/temperature', methods=['POST'])
def api_pool_temperature():
    import sqlite3

    ensure_pool_schema()
    data = request.get_json(silent=True) or {}
    raw_temp = data.get('temp_current_c', data.get('temp_c', data.get('temperature')))
    try:
        temp_c = float(raw_temp)
    except Exception:
        return jsonify({'success': False, 'error': 'invalid_temperature'}), 400
    temp_c = max(4.0, min(45.0, temp_c))

    now = now_berlin()
    state = read_pool_state()
    update = {
        'temp_current_c': round(temp_c, 1),
        'temp_updated_at': db_datetime(now),
    }
    if data.get('target_c') is not None:
        try:
            update['target_c'] = max(10.0, min(45.0, float(data.get('target_c'))))
        except Exception:
            pass

    if state.get('active'):
        started = parse_local_datetime(state.get('started_at'))
        elapsed_min = 0
        if started:
            elapsed_min = max(0, int((now.replace(tzinfo=None) - started).total_seconds() / 60))
        temp_gain = elapsed_min / 60.0 * POOL_TEMP_GAIN_C_PER_HOUR
        adjusted_start = round(temp_c - temp_gain, 1)
        update['temp_start_c'] = adjusted_start
        event_id = state.get('active_event_id')
        if event_id:
            db_path = Path(__file__).parent / 'energy_optimizer.db'
            conn = sqlite3.connect(str(db_path), timeout=15)
            try:
                conn.execute("UPDATE pool_events SET temp_start_c=? WHERE id=?", (adjusted_start, event_id))
                conn.commit()
            finally:
                conn.close()
    else:
        update.update({
            'temp_start_c': round(temp_c, 1),
            'last_temp_end_c': round(temp_c, 1),
        })

    state = write_pool_state(update)
    logger.info(f'Pool temperature set to {temp_c:.1f}C')
    return jsonify({
        'success': True,
        'state': state,
        'current_temp_c': round(temp_c, 1),
        'temperature': estimate_pool_temperature(state),
    })


@app.route('/api/pool/target', methods=['POST'])
def api_pool_target():
    data = request.get_json(silent=True) or {}
    try:
        target_c = float(data.get('target_c'))
    except Exception:
        return jsonify({'success': False, 'error': 'invalid_target'}), 400
    target_c = max(10.0, min(45.0, target_c))
    state = write_pool_state({'target_c': round(target_c, 1)})
    logger.info(f'Pool target set to {target_c:.1f}C')
    return jsonify({'success': True, 'state': state, 'target_c': round(target_c, 1)})


@app.route('/api/pool/start', methods=['POST'])
def api_pool_start():
    import sqlite3

    ensure_pool_schema()
    data = request.get_json(silent=True) or {}
    state = read_pool_state()
    if state.get('active'):
        return jsonify({'success': True, 'state': state, 'already_active': True})

    now = now_berlin()
    current_temp = estimate_pool_temperature(state).get('temp_c', POOL_DEFAULT_MORNING_TEMP_C)
    temp_start = data.get('temp_start_c', current_temp)
    power_w = data.get('power_w', POOL_POWER_W)
    target_c = data.get('target_c', state.get('target_c', POOL_DAY_TARGET_C))
    try:
        temp_start = float(temp_start)
    except Exception:
        temp_start = POOL_DEFAULT_MORNING_TEMP_C
    try:
        power_w = float(power_w)
    except Exception:
        power_w = POOL_POWER_W
    try:
        target_c = max(10.0, min(45.0, float(target_c)))
    except Exception:
        target_c = POOL_DAY_TARGET_C

    snapshot = _current_pool_snapshot()
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pool_events
            (started_at, temp_start_c, power_w, day_target_c, evening_target_c,
             pv_forecast_kwh, sunshine_hours, weather_condition, price_per_kwh, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            db_datetime(now),
            temp_start,
            power_w,
            target_c,
            target_c,
            snapshot.get('pv_forecast_kwh'),
            snapshot.get('sunshine_hours'),
            snapshot.get('weather_condition'),
            snapshot.get('price_per_kwh'),
            (data.get('note') or '').strip(),
        ))
        event_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    state = write_pool_state({
        'active': True,
        'active_event_id': event_id,
        'started_at': db_datetime(now),
        'temp_start_c': temp_start,
        'target_c': target_c,
        'power_w': power_w,
    })
    logger.info(f'Pool START event={event_id} temp_start={temp_start}C power={power_w}W')
    return jsonify({'success': True, 'state': state})


@app.route('/api/pool/stop', methods=['POST'])
def api_pool_stop():
    import sqlite3

    ensure_pool_schema()
    state = read_pool_state()
    if not state.get('active'):
        return jsonify({'success': True, 'state': state, 'already_inactive': True})

    now = now_berlin()
    started = parse_local_datetime(state.get('started_at')) or now.replace(tzinfo=None)
    stopped = now.replace(tzinfo=None)
    duration_min = max(0, int((stopped - started).total_seconds() / 60))
    power_w = float(state.get('power_w') or POOL_POWER_W)
    temp_start = float(state.get('temp_start_c') or POOL_DEFAULT_MORNING_TEMP_C)
    energy_kwh = duration_min / 60.0 * power_w / 1000.0
    temp_gain = duration_min / 60.0 * POOL_TEMP_GAIN_C_PER_HOUR
    temp_end = temp_start + temp_gain
    event_id = state.get('active_event_id')

    db_path = Path(__file__).parent / 'energy_optimizer.db'
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        cur = conn.cursor()
        if event_id:
            cur.execute("""
                UPDATE pool_events
                SET stopped_at=?, duration_min=?, energy_kwh=?, temp_end_c=?, temp_gain_c=?
                WHERE id=?
            """, (db_datetime(now), duration_min, energy_kwh, temp_end, temp_gain, event_id))
        else:
            cur.execute("""
                INSERT INTO pool_events
                (started_at, stopped_at, duration_min, energy_kwh, temp_start_c,
                 temp_end_c, temp_gain_c, power_w, day_target_c, evening_target_c)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                db_datetime(started), db_datetime(now), duration_min, energy_kwh,
                temp_start, temp_end, temp_gain, power_w, POOL_DAY_TARGET_C, POOL_EVENING_TARGET_C,
            ))
            event_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    state = write_pool_state({
        'active': False,
        'active_event_id': None,
        'started_at': None,
        'last_stopped_at': db_datetime(now),
        'last_energy_kwh': round(energy_kwh, 2),
        'last_duration_min': duration_min,
        'last_temp_end_c': round(temp_end, 1),
        'temp_current_c': round(temp_end, 1),
        'temp_updated_at': db_datetime(now),
        'target_c': float(state.get('target_c') or POOL_DAY_TARGET_C),
    })
    logger.info(f'Pool STOP event={event_id} duration={duration_min}min energy={energy_kwh:.2f}kWh')
    return jsonify({'success': True, 'state': state, 'event': {
        'id': event_id,
        'duration_min': duration_min,
        'energy_kwh': round(energy_kwh, 2),
        'temp_end_c': round(temp_end, 1),
        'temp_gain_c': round(temp_gain, 1),
    }})


# Main routes
def _render_dashboard(expert_mode: bool = False):
    services = get_services()
    state = services['evcc'].get_current_state()
    
    # Build status object with all data needed by template
    try:
        prices = services['tibber'].get_current_prices()
        weather = services['weather'].get_current_weather()
        pv_forecast = services['forecast'].get_pv_forecast()
        learning_days = services['learning'].get_learning_days()
    except:
        prices = []
        weather = {}
        pv_forecast = {}
        learning_days = 0
    
    # Find NEXT hour price (we pay for the next hour, not last hour)
    now = now_berlin()
    next_hour = (now.hour + 1) % 24
    current_price = 0.0
    for p in prices:
        if p['timestamp'].hour == next_hour:
            current_price = p['price']
            break
    if current_price == 0.0 and prices:
        current_price = prices[0].get('price', 0.0)
    
    status = {
        'mode': read_automation_mode(),
        'battery_soc': (state.get('battery_soc', 0) or 0) * 100,
        'house_consumption_w': state.get('house_consumption', 0) or 0,
        'pv_production_w': state.get('pv_production', 0) or 0,
        'grid_power_w': state.get('grid_power', 0) or 0,
        'wattpilot_power_w': state.get('wattpilot_power', 0) or 0,
        'current_price': current_price,
        'weather': weather or {},
        'pv_forecast_kw': (pv_forecast.get('total_w', 0) or 0) / 1000,
        'learning_days': learning_days
    }
    
    return render_template(
        'dashboard.html',
        state=state,
        status=status,
        mode=status['mode'],
        expert_mode=expert_mode,
        readonly=is_readonly_user(),
        user_role=user_role(current_user.id) if current_user.is_authenticated else 'guest',
    )


@app.route('/')
def dashboard():
    if is_first_run() and not current_user.is_authenticated:
        return redirect(url_for('setup_wizard'))
    return _render_dashboard(expert_mode=False)


@app.route('/expert')
def expert_dashboard():
    """Expert view with full diagnostics and history chart."""
    return _render_dashboard(expert_mode=True)



_monthly_grid_cache = {'month': None, 'data': None, 'ts': None}

def _get_monthly_grid_kwh(now=None):
    """Monatlicher Netzbezug und Einspeisung in kWh aus energy_readings."""
    import time as _time
    now = now or now_berlin()
    month_key = now.strftime('%Y-%m')
    cache = _monthly_grid_cache
    # Cache: 5 Minuten oder neuer Monat
    if (cache['month'] == month_key and cache['ts']
            and (_time.time() - cache['ts']) < 300):
        return cache['data']
    try:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        db_path = Path(__file__).parent / 'energy_optimizer.db'
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN grid_power_w > 0 THEN grid_power_w ELSE 0 END) / 4000.0, 0),
              COALESCE(SUM(CASE WHEN grid_power_w < 0 THEN ABS(grid_power_w) ELSE 0 END) / 4000.0, 0)
            FROM energy_readings
            WHERE timestamp >= ?
        """, (month_start.strftime('%Y-%m-%d'),)).fetchone()
        conn.close()
        data = {
            'import_kwh': round(row[0], 1),
            'export_kwh': round(row[1], 1),
        }
    except Exception as e:
        logger.debug(f'monthly grid error: {e}')
        data = {'import_kwh': 0, 'export_kwh': 0}
    cache['month'] = month_key
    cache['data'] = data
    cache['ts'] = _time.time()
    return data


_monthly_rfid_cache = {'month': None, 'data': None, 'ts': None}

def _get_monthly_rfid_kwh(now=None):
    """Monatlicher Ladeverbrauch pro RFID-Alias."""
    import time as _time
    now = now or now_berlin()
    month_key = now.strftime('%Y-%m')
    cache = _monthly_rfid_cache
    if (cache['month'] == month_key and cache['ts']
            and (_time.time() - cache['ts']) < 300):
        return cache['data']
    try:
        db_path = Path(__file__).parent / 'energy_optimizer.db'
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT id_tag, COALESCE(SUM(energy_kwh), 0), COUNT(*)
            FROM charge_sessions
            WHERE started_at >= ?
            GROUP BY id_tag
        """, (now.replace(day=1).strftime('%Y-%m-%d'),)).fetchall()
        conn.close()
        aliases = load_rfid_aliases()
        result = {}
        for tag, kwh, cnt in rows:
            name = aliases.get(tag, tag)
            if name in result:
                result[name]['kwh'] += kwh
                result[name]['sessions'] += cnt
            else:
                result[name] = {'kwh': round(kwh, 2), 'sessions': cnt}
        # Sortiert nach kWh absteigend
        data = {k: v for k, v in sorted(result.items(), key=lambda x: -x[1]['kwh'])}
    except Exception as e:
        logger.debug(f'monthly rfid error: {e}')
        data = {}
    cache['month'] = month_key
    cache['data'] = data
    cache['ts'] = _time.time()
    return data

@app.route('/api/status')
def api_status():
    services = get_services()
    state = services['evcc'].get_current_state()
    
    prices = services['tibber'].get_current_prices()
    weather = services['weather'].get_current_weather()
    pv_forecast = services['forecast'].get_pv_forecast()
    
    # Find NEXT hour price (we pay for the next hour, not last hour)
    now = now_berlin()
    next_hour = (now.hour + 1) % 24
    current_price = None
    for p in prices:
        if p['timestamp'].hour == next_hour:
            current_price = p['price']
            break
    if current_price is None and prices:
        current_price = prices[0]['price']  # fallback
    
    # Monatliche Netz-Bilanz (Bezug + Einspeisung)
    monthly_grid = _get_monthly_grid_kwh(now)

    return jsonify({
        'program': read_program_state(),
        'readonly': is_readonly_user(),
        'role': user_role(current_user.id) if current_user.is_authenticated else None,
        'mode': read_automation_mode(),
        'battery_soc': state.get('battery_soc', 0) * 100,
        'house_consumption_w': state.get('house_consumption', 0),
        'pv_production_w': state.get('pv_production', 0),
        'grid_power_w': state.get('grid_power', 0),
        'wattpilot_power_w': state.get('wattpilot_power', 0),
        'current_price': current_price,
        'weather': weather,
        'pv_forecast_kw': pv_forecast.get('total_w', 0) / 1000,
        'learning_days': services['learning'].get_learning_days(),
        'license': services['license'].get_status(),
        'version': _get_current_version(),
        'monthly_grid_import_kwh': monthly_grid['import_kwh'],
        'monthly_grid_export_kwh': monthly_grid['export_kwh'],
    })


@app.route('/api/mode', methods=['POST'])
def api_set_mode():
    global current_mode, _last_battery_action
    data = request.get_json(silent=True) or {}
    mode = normalize_automation_mode(data.get('mode', 'auto'))
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    current_mode = write_automation_mode(mode)
    if mode in ('auto', 'ki'):
        try:
            # Vollautomatik nutzt die smarte Wallbox-Strategie.
            write_wallbox_mode('mix')
        except Exception as e:
            logger.debug(f'wallbox mode sync failed: {e}')
    elif mode == 'manual':
        try:
            write_wallbox_mode('manual')
        except Exception as e:
            logger.debug(f'wallbox manual sync failed: {e}')
        try:
            if BATTERY_MODBUS_OK and _battery_modbus and not _force_charge.get('active'):
                _battery_modbus.release()
                _last_battery_action = {'action': 'released', 'pct': 100}
                logger.info('Battery release because automation mode changed to manual')
        except Exception as e:
            logger.debug(f'battery release on manual failed: {e}')
    logger.info(f'Automation mode set to {current_mode}')
    return jsonify({'success': True, 'mode': current_mode, 'wallbox_mode': read_wallbox_mode()})


@app.route('/api/mode', methods=['GET'])
def api_get_mode():
    return jsonify({
        'mode': read_automation_mode(),
        'wallbox_mode': read_wallbox_mode(),
        'program': read_program_state(),
        'readonly': is_readonly_user(),
        'role': user_role(current_user.id) if current_user.is_authenticated else None,
    })


@app.route('/api/program', methods=['GET', 'POST'])
def api_program():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        enabled = _bool_value(data.get('enabled'), True)
        try:
            state = set_program_enabled(enabled)
            return jsonify({
                'success': True,
                'program': state,
                'mode': read_automation_mode(),
                'wallbox_mode': read_wallbox_mode(),
            })
        except Exception as e:
            logger.error(f'program switch failed: {e}')
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({
        'program': read_program_state(),
        'readonly': is_readonly_user(),
        'role': user_role(current_user.id) if current_user.is_authenticated else None,
        'mode': read_automation_mode(),
        'wallbox_mode': read_wallbox_mode(),
    })


@app.route('/api/automation/status')
def api_automation_status():
    services = get_services()
    try:
        learning = services['learning'].get_weekly_report()
    except Exception as e:
        learning = {'learning_complete': False, 'learning_days': 0, 'days_remaining': 7, 'message': str(e)}
    plan = {}
    try:
        if BATTERY_PLAN_FILE.exists():
            plan = json.loads(BATTERY_PLAN_FILE.read_text())
    except Exception:
        pass
    return jsonify({
        'program': read_program_state(),
        'readonly': is_readonly_user(),
        'role': user_role(current_user.id) if current_user.is_authenticated else None,
        'mode': read_automation_mode(),
        'wallbox_mode': read_wallbox_mode(),
        'learning': learning,
        'plan': {
            'target_soc_1830': plan.get('target_soc_1830'),
            'projected_soc_1830': plan.get('projected_soc_1830'),
            'current_price_ct': plan.get('current_price_ct'),
            'pv_tomorrow_kwh': plan.get('pv_tomorrow_kwh'),
            'weather_hint': plan.get('weather_hint'),
            'overshoot': plan.get('overshoot'),
            'learned_load_today_kwh': plan.get('learned_load_today_kwh'),
            'learned_evening_load_kwh': plan.get('learned_evening_load_kwh'),
            'pool_expected_kwh': plan.get('pool_expected_kwh'),
            'pool': plan.get('pool'),
            'grid_charge': plan.get('grid_charge'),
            'updated': plan.get('updated'),
        }
    })


@app.route('/api/update/status')
def api_update_status():
    state = read_update_state()
    state['role'] = user_role(current_user.id) if current_user.is_authenticated else None
    state['readonly'] = is_readonly_user()
    return jsonify(state)


@app.route('/api/update/check', methods=['POST'])
def api_update_check():
    try:
        return jsonify(check_update_job(auto_install=False))
    except Exception as e:
        logger.error(f'update check failed: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/update/install', methods=['POST'])
def api_update_install():
    state = read_update_state()
    if state.get('status') == 'installing':
        return jsonify(state)
    threading.Thread(target=install_update_job, daemon=True).start()
    return jsonify(write_update_state(status='installing', message='Update-Installation wurde gestartet.'))


# --- Lizenz-API ---

@app.route('/api/license/status')
def api_license_status():
    """Aktueller Lizenz-Status (Trial, aktiv, abgelaufen)."""
    try:
        lic = get_services()['license']
        return jsonify(lic.get_status())
    except Exception as e:
        logger.error(f'license status error: {e}')
        return jsonify({'active': True, 'mode': 'error', 'message': str(e)})


@app.route('/api/license/activate', methods=['POST'])
def api_license_activate():
    """Lizenzschluessel aktivieren."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    data = request.get_json() or {}
    key = data.get('key', '').strip()
    if not key:
        return jsonify({'success': False, 'message': 'Kein Schluessel angegeben'}), 400
    try:
        lic = get_services()['license']
        result = lic.activate_key(key)
        return jsonify(result)
    except Exception as e:
        logger.error(f'license activate error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/license/deactivate', methods=['POST'])
def api_license_deactivate():
    """Lizenz deaktivieren (fuer Geraetewechsel)."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    try:
        lic = get_services()['license']
        result = lic.deactivate()
        return jsonify(result)
    except Exception as e:
        logger.error(f'license deactivate error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


# --- Payment / PayPal API ---

@app.route('/api/payment/plans')
def api_payment_plans():
    """Verfuegbare Abo-Plaene und PayPal-Konfiguration."""
    try:
        pay = get_services()['payment']
        info = pay.get_plans_info()
        lic = get_services()['license']
        info['hardware_id'] = lic.get_hardware_id()
        return jsonify(info)
    except Exception as e:
        logger.error(f'payment plans error: {e}')
        return jsonify({'configured': False, 'message': str(e)}), 500


@app.route('/api/payment/create-subscription', methods=['POST'])
def api_payment_create_subscription():
    """PayPal-Abo starten (monatlich/jaehrlich)."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    data = request.get_json() or {}
    plan = data.get('plan', 'monthly')
    try:
        pay = get_services()['payment']
        lic = get_services()['license']
        hw_id = lic.get_hardware_id()
        base_url = request.host_url.rstrip('/')
        result = pay.create_subscription(
            plan=plan,
            hardware_id=hw_id,
            return_url=f"{base_url}/?payment=success",
            cancel_url=f"{base_url}/?payment=cancelled",
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f'create subscription error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/payment/create-order', methods=['POST'])
def api_payment_create_order():
    """PayPal-Order fuer Einmalkauf erstellen."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    data = request.get_json() or {}
    plan = data.get('plan', 'lifetime')
    try:
        pay = get_services()['payment']
        lic = get_services()['license']
        hw_id = lic.get_hardware_id()
        result = pay.create_order(plan=plan, hardware_id=hw_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f'create order error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/payment/capture-order', methods=['POST'])
def api_payment_capture_order():
    """PayPal-Order abschliessen und Lizenz aktivieren."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    data = request.get_json() or {}
    order_id = data.get('order_id', '')
    plan = data.get('plan', 'lifetime')
    if not order_id:
        return jsonify({'success': False, 'message': 'Keine Order-ID'}), 400
    try:
        pay = get_services()['payment']
        lic = get_services()['license']
        capture = pay.capture_order(order_id)
        if capture['success']:
            plan_info = pay.PLANS.get(plan, {})
            days = plan_info.get('license_days', 365)
            result = lic.activate_from_payment(plan=plan, days=days)
            result['transaction_id'] = capture.get('transaction_id')
            return jsonify(result)
        return jsonify(capture), 400
    except Exception as e:
        logger.error(f'capture order error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/payment/subscription-complete', methods=['POST'])
def api_payment_subscription_complete():
    """PayPal-Abo nach Kundengenehmigung aktivieren."""
    if is_readonly_user():
        return jsonify({'success': False, 'message': 'Keine Berechtigung'}), 403
    data = request.get_json() or {}
    subscription_id = data.get('subscription_id', '')
    plan = data.get('plan', 'monthly')
    if not subscription_id:
        return jsonify({'success': False, 'message': 'Keine Subscription-ID'}), 400
    try:
        pay = get_services()['payment']
        lic = get_services()['license']
        sub = pay.get_subscription_status(subscription_id)
        if sub.get('status') == 'ACTIVE':
            plan_info = pay.PLANS.get(plan, {})
            days = plan_info.get('license_days', 35)
            result = lic.activate_from_payment(
                plan=plan, days=days, subscription_id=subscription_id
            )
            return jsonify(result)
        return jsonify({
            'success': False,
            'message': f"Abo-Status: {sub.get('status', 'unbekannt')}"
        }), 400
    except Exception as e:
        logger.error(f'subscription complete error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/payment/webhook', methods=['POST'])
def api_payment_webhook():
    """PayPal Webhook — automatische Lizenzverlaengerung.

    Wird von PayPal aufgerufen bei:
      - PAYMENT.SALE.COMPLETED → Abo-Zahlung eingegangen
      - BILLING.SUBSCRIPTION.CANCELLED → Abo gekuendigt
      - BILLING.SUBSCRIPTION.ACTIVATED → Neues Abo aktiviert
    """
    try:
        pay = get_services()['payment']
        body = request.get_data()

        webhook_headers = {
            'PAYPAL-AUTH-ALGO': request.headers.get('PAYPAL-AUTH-ALGO', ''),
            'PAYPAL-CERT-URL': request.headers.get('PAYPAL-CERT-URL', ''),
            'PAYPAL-TRANSMISSION-ID': request.headers.get('PAYPAL-TRANSMISSION-ID', ''),
            'PAYPAL-TRANSMISSION-SIG': request.headers.get('PAYPAL-TRANSMISSION-SIG', ''),
            'PAYPAL-TRANSMISSION-TIME': request.headers.get('PAYPAL-TRANSMISSION-TIME', ''),
        }

        if not pay.verify_webhook(webhook_headers, body):
            logger.warning("PayPal Webhook: Signatur ungueltig")
            return jsonify({'status': 'invalid_signature'}), 401

        event = json.loads(body)
        event_type = event.get('event_type', '')
        resource = event.get('resource', {})

        result = pay.process_webhook(event_type, resource)
        action = result.get('action')

        if action in ('extend', 'activate'):
            lic = get_services()['license']
            lic.extend_license(
                days=result.get('days', 35),
                plan=result.get('plan'),
                subscription_id=result.get('subscription_id'),
            )
            logger.info(
                f"PayPal Webhook: Lizenz verlaengert "
                f"(Plan: {result.get('plan')}, Tage: {result.get('days')})"
            )
        elif action == 'cancel':
            logger.info(
                f"PayPal Webhook: Abo gekuendigt "
                f"(Sub: {result.get('subscription_id')}). "
                f"Lizenz laeuft zum Ende der Periode aus."
            )
        elif action == 'suspend':
            logger.warning(
                f"PayPal Webhook: Abo suspendiert "
                f"(Zahlungsproblem, Sub: {result.get('subscription_id')})"
            )

        return jsonify({'status': 'ok', 'action': action}), 200

    except Exception as e:
        logger.error(f'payment webhook error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route("/api/system/plugins")
def api_system_plugins():
    """Plugin-System Informationen — verfuegbare und aktive Plugins."""
    try:
        from services.plugin_registry import PluginRegistry
        import os
        app_dir = os.path.dirname(os.path.abspath(__file__))
        registry = PluginRegistry(app_dir)
        return jsonify({
            "active": registry.get_system_info(),
            "available": registry.list_available_plugins(),
        })
    except Exception as e:
        logger.error(f"plugin info error: {e}")
        return jsonify({"error": str(e)}), 500







@app.route("/api/wallbox/status")
def api_wallbox_status():
    """Wallbox-Status via Plugin."""
    try:
        services = get_services()
        wb = services.get('wallbox')
        if not wb:
            return jsonify({"error": "Keine Wallbox konfiguriert"}), 404
        status = wb.get_status()
        # datetime serialisieren
        if 'timestamp' in status:
            status['timestamp'] = str(status['timestamp'])
        return jsonify(status)
    except Exception as e:
        logger.error(f"wallbox status error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/wallbox/control", methods=["POST"])
def api_wallbox_control():
    """Wallbox steuern (start/stop/set_current)."""
    try:
        services = get_services()
        wb = services.get('wallbox')
        if not wb:
            return jsonify({"error": "Keine Wallbox konfiguriert"}), 404

        data = request.get_json() or {}
        action = data.get('action', '')

        if action == 'start':
            ok = wb.start_charging()
            return jsonify({"success": ok, "action": "start"})
        elif action == 'stop':
            ok = wb.stop_charging()
            return jsonify({"success": ok, "action": "stop"})
        elif action == 'set_current':
            amps = float(data.get('amps', 6))
            ok = wb.set_charge_current(amps)
            return jsonify({"success": ok, "action": "set_current", "amps": amps})
        else:
            return jsonify({"error": f"Unbekannte Aktion: {action}"}), 400
    except Exception as e:
        logger.error(f"wallbox control error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Setup-Wizard (Onboarding fuer neue Kunden)
# ---------------------------------------------------------------------------

SETUP_DONE_FILE = NEXUS_DIR / 'setup_complete'


def is_first_run() -> bool:
    """True wenn noch kein Setup durchgefuehrt wurde."""
    return not SETUP_DONE_FILE.exists()


@app.route('/setup')
def setup_wizard():
    """Setup-Wizard Seite (kein Login noetig)."""
    return render_template('setup.html')


@app.route('/api/setup/test-inverter', methods=['POST'])
def api_setup_test_inverter():
    """Wechselrichter-Verbindung testen (kein Login noetig fuer Setup)."""
    data = request.get_json(silent=True) or {}
    inv_type = data.get('type', 'fronius_gen24')
    ip = data.get('ip', '')

    if not ip:
        return jsonify({'connected': False, 'message': 'Keine IP-Adresse angegeben'})

    try:
        if inv_type == 'fronius_gen24':
            resp = requests.get(
                f'http://{ip}/solar_api/v1/GetPowerFlowRealtimeData.fcgi',
                params={'Scope': 'System'},
                timeout=5,
            )
            if resp.status_code == 200:
                data_body = resp.json().get('Body', {}).get('Data', {})
                site = data_body.get('Site', {})
                pv_w = abs(site.get('P_PV', 0) or 0)
                return jsonify({
                    'connected': True,
                    'message': f'Fronius GEN24 gefunden! Aktuelle PV-Leistung: {pv_w:.0f}W'
                })
            return jsonify({'connected': False, 'message': f'Geraet antwortet nicht korrekt (HTTP {resp.status_code})'})
        else:
            return jsonify({'connected': False, 'message': f'Plugin "{inv_type}" noch nicht verfuegbar'})
    except requests.exceptions.ConnectionError:
        return jsonify({'connected': False, 'message': f'Keine Verbindung zu {ip} — bitte IP pruefen'})
    except Exception as e:
        return jsonify({'connected': False, 'message': str(e)})


@app.route('/api/setup/complete', methods=['POST'])
def api_setup_complete():
    """Setup-Wizard abschliessen — config.yaml und .env schreiben."""
    data = request.get_json(silent=True) or {}

    try:
        import yaml
        app_dir = os.path.dirname(os.path.abspath(__file__))

        # --- 1. Account in .env schreiben ---
        account = data.get('account', {})
        env_path = os.path.join(app_dir, '.env')
        env_lines = []
        if os.path.exists(env_path):
            with open(env_path) as f:
                env_lines = f.readlines()

        def set_env(key, value):
            nonlocal env_lines
            found = False
            for i, line in enumerate(env_lines):
                if line.startswith(key + '='):
                    env_lines[i] = f'{key}={value}\n'
                    found = True
                    break
            if not found:
                env_lines.append(f'{key}={value}\n')

        if account.get('username'):
            set_env('WEB_USER', account['username'])
        if account.get('password'):
            set_env('WEB_PASSWORD', account['password'])

        # Tarif API-Key
        tariff = data.get('tariff', {})
        if tariff.get('api_key'):
            set_env('TIBBER_API_TOKEN', tariff['api_key'])

        # PV + Standort
        pv = data.get('pv', {})
        location = data.get('location', {})
        if pv.get('kwp'):
            set_env('FORECAST_SOLAR_KWP', str(pv['kwp']))
        if location.get('lat'):
            set_env('FORECAST_SOLAR_LAT', str(location['lat']))
            set_env('OPEN_METEO_LAT', str(location['lat']))
        if location.get('lon'):
            set_env('FORECAST_SOLAR_LON', str(location['lon']))
            set_env('OPEN_METEO_LON', str(location['lon']))
        if pv.get('battery_kwh'):
            set_env('BATTERY_CAPACITY_KWH', str(pv['battery_kwh']))
            usable = round(pv['battery_kwh'] * 0.91, 1)  # ~91% nutzbar
            set_env('BATTERY_USABLE_KWH', str(usable))

        # Inverter IP
        inverter = data.get('inverter', {})
        if inverter.get('ip'):
            set_env('WATTPLOT_IP', inverter['ip'])

        with open(env_path, 'w') as f:
            f.writelines(env_lines)

        # --- 2. config.yaml schreiben ---
        config = {}

        if inverter.get('type'):
            config['inverter'] = {'type': inverter['type']}
            if inverter.get('ip'):
                config['inverter']['ip'] = inverter['ip']

        if tariff.get('type'):
            config['tariff'] = {'type': tariff['type']}
            # Tarif-spezifische Parameter in config.yaml schreiben
            if tariff['type'] == 'fixed_price':
                if tariff.get('price_ct'):
                    config['tariff']['price_ct'] = tariff['price_ct']
                if tariff.get('feed_in_ct'):
                    config['tariff']['feed_in_ct'] = tariff['feed_in_ct']
            elif tariff['type'] == 'dual_rate':
                for k in ('ht_price_ct', 'nt_price_ct', 'nt_start', 'nt_end', 'feed_in_ct'):
                    if tariff.get(k):
                        config['tariff'][k] = tariff[k]

        if pv:
            config['pv'] = {}
            if pv.get('kwp'): config['pv']['kwp'] = pv['kwp']
            if pv.get('tilt'): config['pv']['tilt'] = pv['tilt']
            if 'azimuth' in pv: config['pv']['azimuth'] = pv['azimuth']

        if pv.get('battery_kwh'):
            config['battery'] = {
                'capacity_kwh': pv['battery_kwh'],
                'usable_kwh': round(pv['battery_kwh'] * 0.91, 1),
                'min_soc': 5,
            }

        if location:
            config['location'] = {}
            if location.get('lat'): config['location']['lat'] = location['lat']
            if location.get('lon'): config['location']['lon'] = location['lon']

        config_path = os.path.join(app_dir, 'config.yaml')
        with open(config_path, 'w') as f:
            f.write('# Vista-Energy — Konfiguration (erstellt vom Setup-Wizard)\n')
            f.write(f'# Erstellt: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n\n')
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        # --- 3. Lizenz aktivieren ---
        lic_data = data.get('license', {})
        if lic_data.get('mode') == 'key' and lic_data.get('key'):
            try:
                lic_svc = get_services().get('license')
                if lic_svc:
                    lic_svc.activate_key(lic_data['key'])
            except Exception as le:
                logger.warning(f'License activation during setup: {le}')

        # --- 4. Setup als erledigt markieren ---
        SETUP_DONE_FILE.write_text(datetime.now().isoformat())
        logger.info('Setup-Wizard abgeschlossen')

        return jsonify({'success': True, 'message': 'Einrichtung abgeschlossen!'})

    except Exception as e:
        logger.error(f'Setup error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/threshold', methods=['POST'])
def api_set_threshold():
    """Preis-Schwellen fuer Wallbox-Netzladung setzen.

    Parameter:
      threshold / price_high: Ab diesem Preis NUR PV laden (ct/kWh)
      price_low: Unter diesem Preis VOLLLAST aus Netz laden (ct/kWh)
    """
    global price_threshold
    data = request.get_json() or {}

    # Abwaertskompatibel: 'threshold' = price_high
    price_high = float(data.get('price_high', data.get('threshold', 28.0)))
    price_low = float(data.get('price_low', 18.0))

    try:
        if not (1 <= price_low <= 100 and 1 <= price_high <= 100):
            raise ValueError('Preise muessen zwischen 1 und 100 ct liegen')
        if price_low >= price_high:
            raise ValueError('Guenstig-Schwelle muss unter Teuer-Schwelle liegen')

        price_threshold = price_high

        # In Datei schreiben fuer pv_controller
        import json
        limits_file = NEXUS_DIR / 'wallbox_price_limits.json'
        limits_file.write_text(json.dumps({
            'price_low': round(price_low, 1),
            'price_high': round(price_high, 1),
            'updated': datetime.now().isoformat(),
        }, indent=2))

        logger.info(f"Wallbox Preis-Schwellen: guenstig<={price_low}ct, teuer>={price_high}ct")
        return jsonify({
            'success': True,
            'price_low': price_low,
            'price_high': price_high,
        })
    except (ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/threshold', methods=['GET'])
def api_get_threshold():
    """Aktuelle Preis-Schwellen abfragen."""
    import json
    limits_file = NEXUS_DIR / 'wallbox_price_limits.json'
    try:
        if limits_file.exists():
            data = json.loads(limits_file.read_text())
            return jsonify(data)
    except Exception:
        pass
    return jsonify({'price_low': 18.0, 'price_high': 28.0})


@app.route('/api/charge', methods=['POST'])
def api_charge():
    global current_mode
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    data = request.get_json()
    services = get_services()
    
    action = data.get('action', 'auto')  # start, stop, auto
    target_soc = data.get('target_soc', 0.8)
    max_power = data.get('max_power', 22000)

    if action in ('start', 'stop'):
        current_mode = write_automation_mode('manual')
        write_wallbox_mode('manual')
    
    services['evcc'].set_charge_mode(action, target_soc, max_power)
    
    return jsonify({'success': True, 'action': action})


# ---------------------------------------------------------------------------
# NEW: Wallbox Mode + Strategy + Battery Plan endpoints
# ---------------------------------------------------------------------------
@app.route('/api/wallbox/mode', methods=['GET', 'POST'])
def api_wallbox_mode():
    """Get/set wallbox operating mode: eco | mix | fast | off | manual."""
    if request.method == 'POST':
        if not is_program_enabled():
            return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
        data = request.get_json(silent=True) or {}
        mode = (data.get('mode') or '').lower()
        if mode == 'standard':
            mode = 'mix'  # legacy alias
        if mode not in VALID_MODES:
            return jsonify({'success': False, 'error': f'mode must be {"|".join(VALID_MODES)}'}), 400
        try:
            write_wallbox_mode(mode)
            logger.info(f'Wallbox mode set to {mode}')
            return jsonify({'success': True, 'mode': mode})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'mode': read_wallbox_mode(), 'program': read_program_state()})


@app.route('/api/wallbox/strategy')
def api_wallbox_strategy():
    """Returns the latest decision the pv_controller made."""
    cstate = read_controller_state()
    if not isinstance(cstate, dict):
        cstate = {}
    if not cstate.get('fronius'):
        flow = live_fronius_flow()
        if flow:
            cstate['fronius'] = flow
            cstate['live_flow_fallback'] = True
    plan = {}
    try:
        if BATTERY_PLAN_FILE.exists():
            plan = json.loads(BATTERY_PLAN_FILE.read_text())
    except Exception:
        pass
    monthly = _get_monthly_grid_kwh()
    return jsonify({
        'program': read_program_state(),
        'mode': read_wallbox_mode(),
        'controller': cstate,
        'battery_plan': plan,
        'monthly_grid_import_kwh': monthly['import_kwh'],
        'monthly_grid_export_kwh': monthly['export_kwh'],
        'monthly_rfid': _get_monthly_rfid_kwh(),
    })


@app.route('/api/battery/plan')
def api_battery_plan():
    """Returns the current battery plan (for 18:30/65% goal)."""
    try:
        if BATTERY_PLAN_FILE.exists():
            return jsonify(json.loads(BATTERY_PLAN_FILE.read_text()))
        # Fallback: berechne ad-hoc
        return jsonify(build_battery_plan(get_services()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/battery/plan/refresh', methods=['POST'])
def api_battery_plan_refresh():
    write_battery_plan_job()
    try:
        return jsonify(json.loads(BATTERY_PLAN_FILE.read_text()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/wallbox/set_current', methods=['POST'])
def api_wallbox_set_current():
    """Manuelles Setzen des Lade-Stroms (zum Debuggen)."""
    global current_mode
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    data = request.get_json(silent=True) or {}
    try:
        amps = int(data.get('amps', 6))
        phases = int(data.get('phases', 3))
    except Exception:
        return jsonify({'success': False, 'error': 'amps/phases must be int'}), 400
    current_mode = write_automation_mode('manual')
    write_wallbox_mode('manual')
    res = wallbox_send_cmd({'cmd': 'set_current', 'amps': amps, 'phases': phases})
    return jsonify(res)


@app.route('/api/wallbox/pause', methods=['POST'])
def api_wallbox_pause():
    global current_mode
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    current_mode = write_automation_mode('manual')
    write_wallbox_mode('manual')
    return jsonify(wallbox_send_cmd({'cmd': 'pause'}))


@app.route('/api/wallbox/resume', methods=['POST'])
def api_wallbox_resume():
    global current_mode
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    current_mode = write_automation_mode('manual')
    write_wallbox_mode('manual')
    return jsonify(wallbox_send_cmd({'cmd': 'resume'}))


# ---- Battery Modbus ----
@app.route('/api/battery/modbus')
def api_battery_modbus():
    """Liest aktuellen Storage-Status direkt aus dem Inverter (SunSpec 124)."""
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    try:
        return jsonify(_battery_modbus.read())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/battery/charge_limit', methods=['POST'])
def api_battery_charge_limit():
    """Setzt das Lade-Limit in % (0..100). 0 = sperrt das Laden komplett."""
    global current_mode, _last_battery_action
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    data = request.get_json(silent=True) or {}
    try:
        pct = float(data.get('pct', 100))
        revert = int(data.get('revert_seconds', 0))
        current_mode = write_automation_mode('manual')
        write_wallbox_mode('manual')
        _battery_modbus.set_charge_limit_pct(pct, revert_seconds=revert)
        _last_battery_action = {'action': 'manual', 'pct': pct}
        return jsonify({'success': True, 'pct': pct, 'revert_seconds': revert,
                        'state': _battery_modbus.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/battery/release', methods=['POST'])
def api_battery_release():
    """Akku-Steuerung wieder freigeben."""
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    try:
        _battery_modbus.release()
        global _last_battery_action
        _last_battery_action = {'action': 'released', 'pct': 100}
        return jsonify({'success': True, 'state': _battery_modbus.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500




@app.route('/api/battery/hold', methods=['POST'])
def api_battery_hold():
    """Akkustand halten - Entladung auf 0% setzen.
    
    Der Akku gibt keinen Strom mehr ab, behaelt den aktuellen SOC.
    PV-Laden geht weiter! Nur Entladung wird gesperrt.
    """
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    try:
        _battery_modbus.set_discharge_limit_pct(0, revert_seconds=0)
        global _last_battery_action
        _last_battery_action = {'action': 'hold', 'pct': 0}
        state = _battery_modbus.read()
        soc = state.get('soc', 0)
        logger.info(f"Akkustand halten: SOC={soc}%%, Entladung gesperrt")
        return jsonify({'success': True, 'mode': 'hold', 'state': state})
    except Exception as e:
        logger.error(f"Battery hold failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/battery/hold', methods=['DELETE'])
def api_battery_hold_release():
    """Akkustand halten aufheben - Entladung wieder freigeben."""
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    try:
        _battery_modbus.release()
        global _last_battery_action
        _last_battery_action = {'action': 'released', 'pct': 100}
        state = _battery_modbus.read()
        soc = state.get('soc', 0)
        logger.info(f"Akkustand halten aufgehoben: SOC={soc}%%")
        return jsonify({'success': True, 'mode': 'normal', 'state': state})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Persistenter Force-Charge-Status (damit der Plan-Job ihn nicht ueberschreibt)
_force_charge = {'active': False, 'target_soc': None, 'max_w': None, 'started': None}


@app.route('/api/battery/force_charge', methods=['POST'])
def api_battery_force_charge():
    """Erzwingt Akku-Nachladung aus dem Netz bis target_soc.
    Body: {"target_soc": 80, "max_w": 5000}
    """
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    data = request.get_json(silent=True) or {}
    try:
        target = float(data.get('target_soc', 80))
        max_w = int(data.get('max_w', 5000))
        _battery_modbus.force_charge(target, max_w)
        global _force_charge, _last_battery_action
        _force_charge = {
            'active': True, 'target_soc': target, 'max_w': max_w,
            'started': now_berlin().isoformat()
        }
        _last_battery_action = {'action': 'force_charge', 'pct': target}
        logger.info(f'FORCE-CHARGE gestartet: bis {target}% mit max {max_w}W')
        return jsonify({'success': True, 'force_charge': _force_charge,
                        'state': _battery_modbus.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/battery/force_charge', methods=['GET'])
def api_battery_force_charge_status():
    return jsonify(_force_charge)


@app.route('/api/battery/force_charge/stop', methods=['POST'])
def api_battery_force_charge_stop():
    if not is_program_enabled():
        return jsonify({'success': False, 'error': 'Programm ist ausgeschaltet'}), 409
    if not BATTERY_MODBUS_OK or _battery_modbus is None:
        return jsonify({'error': 'BatteryModbus nicht verfuegbar'}), 503
    try:
        _battery_modbus.release()
        global _force_charge, _last_battery_action
        _force_charge = {'active': False, 'target_soc': None, 'max_w': None, 'started': None}
        _last_battery_action = {'action': 'released', 'pct': 100}
        logger.info('FORCE-CHARGE beendet')
        return jsonify({'success': True, 'state': _battery_modbus.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# History-Endpoint mit Aggregation fuer Charts
# ---------------------------------------------------------------------------
@app.route('/api/chart')
def api_chart():
    """Aggregierte Zeitreihen fuer Chart.js.

    Query params:
      range = today | week | month | year | total  (default today)
    Returns:
      { labels:[ISO timestamps], pv:[W], load:[W], grid:[W], wattpilot:[W],
        to_grid:[W], to_battery:[W], direct_consumed:[W], soc:[%], price:[ct/kWh] }
    """
    import sqlite3
    rng = request.args.get('range', 'today').lower()
    date_param = request.args.get('date')

    # Range -> SQL-Filter + Bucket-Groesse (in Minuten)
    now = now_berlin()
    if rng == 'today' and date_param:
        try:
            selected = datetime.strptime(date_param, '%Y-%m-%d')
            cutoff = selected.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket_min = 15
    elif rng == 'today':
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket_min = 15
    elif rng == 'week':
        cutoff = now - timedelta(days=7)
        bucket_min = 60  # 1 Stunde
    elif rng == 'month':
        cutoff = now - timedelta(days=30)
        bucket_min = 360  # 6 Stunden
    elif rng == 'year':
        cutoff = now - timedelta(days=365)
        bucket_min = 1440  # 1 Tag
    elif rng == 'total':
        cutoff = datetime(2000, 1, 1, tzinfo=BERLIN_TZ)
        bucket_min = 1440  # 1 Tag
    else:
        return jsonify({'error': 'invalid range'}), 400

    cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
    is_historic_day = rng == 'today' and date_param and cutoff.date() < now.date()
    cutoff_end_str = (cutoff + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S') if is_historic_day else None
    db_path = Path(__file__).parent / 'energy_optimizer.db'

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        if cutoff_end_str:
            cursor.execute(f"""
                SELECT
                  datetime(strftime('%s', timestamp) / ({bucket_min}*60) * ({bucket_min}*60), 'unixepoch') AS bucket,
                  AVG(pv_production_w)            AS pv,
                  AVG(ABS(house_consumption_w))   AS load,
                  AVG(grid_power_w)               AS grid,
                  AVG(wattpilot_power_w)          AS wp,
                  AVG(battery_charge_w)           AS battery_w,
                  AVG(battery_soc * 100)          AS soc,
                  AVG(price_per_kwh)              AS price
                FROM energy_readings
                WHERE timestamp >= ? AND timestamp < ?
                GROUP BY bucket
                ORDER BY bucket ASC
            """, (cutoff_str, cutoff_end_str))
        else:
            cursor.execute(f"""
                SELECT
                  datetime(strftime('%s', timestamp) / ({bucket_min}*60) * ({bucket_min}*60), 'unixepoch') AS bucket,
                  AVG(pv_production_w)            AS pv,
                  AVG(ABS(house_consumption_w))   AS load,
                  AVG(grid_power_w)               AS grid,
                  AVG(wattpilot_power_w)          AS wp,
                  AVG(battery_charge_w)           AS battery_w,
                  AVG(battery_soc * 100)          AS soc,
                  AVG(price_per_kwh)              AS price
                FROM energy_readings
                WHERE timestamp >= ?
                GROUP BY bucket
                ORDER BY bucket ASC
            """, (cutoff_str,))
        rows = cursor.fetchall()
        conn.close()

        labels, pv, load, grid, wp, battery, to_grid, to_battery, direct_consumed, soc, price = [], [], [], [], [], [], [], [], [], [], []
        for r in rows:
            pv_w = max(0.0, float(r[1] or 0))
            load_w = max(0.0, float(r[2] or 0))
            grid_w = float(r[3] or 0)
            wp_w = max(0.0, float(r[4] or 0))
            battery_w = float(r[5] or 0)
            export_w = max(0.0, -grid_w)
            charge_w = max(0.0, -battery_w)
            direct_w = max(0.0, pv_w - export_w - charge_w)
            labels.append(r[0])
            pv.append(round(pv_w, 1))
            load.append(round(load_w, 1))
            grid.append(round(grid_w, 1))
            wp.append(round(wp_w, 1))
            battery.append(round(battery_w, 1))
            to_grid.append(round(export_w, 1))
            to_battery.append(round(charge_w, 1))
            direct_consumed.append(round(direct_w, 1))
            soc.append(round(r[6] or 0, 1))
            # price ist in EUR/kWh -> in ct/kWh
            p = (r[7] or 0)
            if p < 5:  # vermutlich EUR
                p = p * 100
            price.append(round(p, 2))

        actual_count = len(labels)
        expected_soc = []
        expected_load = []
        if rng == 'today':
            # Fuer TAG immer die volle 00:00-24:00 Achse liefern. Vergangene
            # Messpunkte bleiben echt, zukuenftige Werte werden als Prognose
            # in eigenen gestrichelten Reihen ausgegeben.
            bucket_count = int(24 * 60 / bucket_min) + 1
            day_start = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
            full_labels = [
                (day_start + timedelta(minutes=bucket_min * i)).strftime('%Y-%m-%d %H:%M:%S')
                for i in range(bucket_count)
            ]
            series = {
                'pv': dict(zip(labels, pv)),
                'load': dict(zip(labels, load)),
                'grid': dict(zip(labels, grid)),
                'wattpilot': dict(zip(labels, wp)),
                'battery': dict(zip(labels, battery)),
                'to_grid': dict(zip(labels, to_grid)),
                'to_battery': dict(zip(labels, to_battery)),
                'direct_consumed': dict(zip(labels, direct_consumed)),
                'soc': dict(zip(labels, soc)),
                'price': dict(zip(labels, price)),
            }

            last_idx = -1
            last_soc = None
            for i, label in enumerate(full_labels):
                if label in series['soc'] and series['soc'][label] is not None:
                    last_idx = i
                    last_soc = series['soc'][label]

            plan = {}
            try:
                if BATTERY_PLAN_FILE.exists():
                    plan = json.loads(BATTERY_PLAN_FILE.read_text())
            except Exception:
                plan = {}
            tibber_prices = {}
            try:
                for p in get_services()['tibber'].get_current_prices() or []:
                    ts = p.get('timestamp')
                    if not ts:
                        continue
                    if ts.tzinfo is not None:
                        ts = ts.astimezone(BERLIN_TZ).replace(tzinfo=None)
                    hour_key = ts.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
                    tibber_prices[hour_key] = round(float(p.get('price') or 0) * 100, 2)
            except Exception as e:
                logger.debug(f'tibber chart extension failed: {e}')
            target_soc = float(plan.get('target_soc_1830') or 65)
            expected_profile = {}
            try:
                expected_profile = get_services()['learning'].get_daily_profile()
            except Exception:
                expected_profile = {}

            # --- SOC-Vorhersage: Physik-Simulation ab letztem Messwert ---
            # Statt linearer Interpolation: echte PV-Last-Bilanz vorwaerts
            # simulieren, mit Min-SOC-Schutz + Tibber-Override-Logik.
            #
            # PV-Kurve (24 Stundenwerte in kWh)
            pv_today_kwh = float(plan.get('pv_total_today_kwh') or 0)
            if pv_today_kwh <= 0:
                try:
                    pv_f = get_services()['forecast'].get_pv_forecast() or {}
                    pv_today_kwh = pv_f.get('today_kwh') or (pv_f.get('total_w', 0) or 0) / 1000.0
                except Exception:
                    pv_today_kwh = 0
            pv_curve_h = hourly_pv_curve_kwh(pv_today_kwh, 0)

            # Last-Kurve (24 Stundenwerte in kWh, inkl. Pool wenn erwartet)
            load_curve_h = merged_load_curve(expected_profile)
            try:
                pool_learning_fc = get_services()['learning'].get_pool_learning_summary()
            except Exception:
                pool_learning_fc = get_pool_learning_summary()
            pool_plan_fc = build_pool_plan(
                get_services()['forecast'].get_pv_forecast(),
                pool_learning_fc, day_offset=0)
            pool_in_plan = bool(pool_plan_fc.get('active') or
                (pool_learning_fc.get('event_count', 0) >= 2 and pool_plan_fc.get('expected')))
            if pool_in_plan:
                pc = pool_curve_kwh(pool_plan_fc)
                load_curve_h = [load_curve_h[h] + pc[h] for h in range(24)]

            # Tibber-Preise fuer Steuerlogik (ct/kWh pro Stunde)
            tibber_by_hour = {}
            for hk, pct in tibber_prices.items():
                try:
                    tibber_by_hour[int(hk[11:13])] = pct
                except (ValueError, IndexError):
                    pass
            protect_max_ct = AUTO_GRID_CHARGE_MAX_CT

            # Vorwaerts-Simulation: startet beim letzten echten SOC-Messwert
            # und simuliert ab da die PV-Last-Bilanz bis Mitternacht.
            # Min-SOC: Fronius GEN24 entlaedt den Akku nicht unter diesen Wert;
            # bei Unterschreitung wird stattdessen aus dem Netz bezogen, d.h. der
            # Akku bleibt bei min_soc stehen und wird bei PV-Ueberschuss sofort
            # wieder geladen.
            MIN_SOC_SIM = 5.0  # Fronius GEN24 Minimum-SOC (typ. 5%)
            sim = last_soc if last_soc is not None else float(plan.get('current_soc') or 50)
            for i, label in enumerate(full_labels):
                h = int(label[11:13])

                if last_idx >= 0 and i > last_idx:
                    # Zukunft: PV-Last-Bilanz simulieren
                    pv_q = pv_curve_h[h] / 4.0
                    ld_q = load_curve_h[h] / 4.0
                    net = pv_q - ld_q

                    if net >= 0:
                        # PV-Ueberschuss -> Batterie laden (immer)
                        new_sim = sim + (net / BATTERY_USABLE_KWH) * 100.0
                    else:
                        # Verbrauch > PV -> Batterie entladen, aber nur bis MIN_SOC
                        if sim > MIN_SOC_SIM:
                            new_sim = sim + (net / BATTERY_USABLE_KWH) * 100.0
                            new_sim = max(MIN_SOC_SIM, new_sim)
                        else:
                            # Akku bereits bei/unter Minimum -> Netz deckt Defizit
                            new_sim = sim

                    # SOC-Schutz fuer 18:30 Ziel + Tibber-Override
                    if net < 0 and new_sim < TARGET_SOC_1830:
                        price_h = tibber_by_hour.get(h)
                        tibber_expensive = price_h is not None and price_h > protect_max_ct
                        if not tibber_expensive and sim >= (TARGET_SOC_1830 - 15.0):
                            new_sim = max(TARGET_SOC_1830, sim)

                    sim = max(MIN_SOC_SIM, min(100.0, new_sim))
                    expected_soc.append(round(sim, 1))
                elif last_idx >= 0 and i == last_idx:
                    # Uebergangspunkt: echten letzten Wert als Startwert
                    expected_soc.append(round(sim, 1))
                else:
                    # Vergangenheit: echte SOC-Linie zeigt die Realitaet
                    expected_soc.append(None)

                # Expected-Load: nur fuer Zukunft (Vergangenheit hat echte Werte)
                # Verwende load_curve_h (bereinigt um Pool/Wallbox) statt
                # das rohe Lernprofil, damit die Prognose-Last realistisch ist.
                if label in series['load']:
                    expected_load.append(None)
                else:
                    expected_load.append(round(load_curve_h[h] * 1000, 1))

            def expand(name):
                return [series[name].get(label) for label in full_labels]

            def expand_price():
                out = []
                last_p = None
                for label in full_labels:
                    p = series['price'].get(label)
                    if p is None:
                        hour_label = label[:14] + '00:00'
                        p = tibber_prices.get(hour_label)
                    if p is not None:
                        last_p = p
                    out.append(p if p is not None else last_p)
                return out

            labels = full_labels
            pv = expand('pv')
            load = expand('load')
            grid = expand('grid')
            wp = expand('wattpilot')
            battery = expand('battery')
            to_grid = expand('to_grid')
            to_battery = expand('to_battery')
            direct_consumed = expand('direct_consumed')
            soc = expand('soc')
            price = expand_price()

        try:
            expected_pool_plan = None
            if rng == 'today':
                services = get_services()
                try:
                    pool_learning = services['learning'].get_pool_learning_summary()
                except Exception:
                    pool_learning = get_pool_learning_summary()
                expected_pool_plan = build_pool_plan(services['forecast'].get_pv_forecast(), pool_learning, day_offset=0)
            pool_active, pool_expected = pool_series_for_labels(
                labels, bucket_min, include_expected=(rng == 'today'), expected_plan=expected_pool_plan
            )
        except Exception as e:
            logger.debug(f'pool chart series failed: {e}')
            pool_active = [None] * len(labels)
            pool_expected = [None] * len(labels)

        return jsonify({
            'range': rng,
            'bucket_min': bucket_min,
            'count': len(labels),
            'actual_count': actual_count,
            'labels': labels,
            'pv': pv,
            'load': load,
            'grid': grid,
            'wattpilot': wp,
            'battery': battery,
            'to_grid': to_grid,
            'to_battery': to_battery,
            'direct_consumed': direct_consumed,
            'soc': soc,
            'expected_soc': expected_soc,
            'expected_load': expected_load,
            'pool_active': pool_active,
            'pool_expected': pool_expected,
            'price': price,
        })
    except Exception as e:
        logger.error(f'api_chart failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/wattpilot/status')
def api_wattpilot_status():
    """Get real-time Wattpilot status from OCPP server."""
    try:
        import websocket
        ws = websocket.WebSocket()
        ws.connect('ws://localhost:8889', timeout=5)
        ws.send(json.dumps({'cmd': 'status'}))
        response = ws.recv()
        ws.close()
        status = json.loads(response)
        
        if status.get('status') == 'Charging' or status.get('power_w', 0) == 0:
            try:
                pf_response = requests.get(
                    'http://192.168.1.80/solar_api/v1/GetPowerFlowRealtimeData.fcgi?Scope=System',
                    timeout=10
                )
                pf_data = pf_response.json()
                site = pf_data.get('Body', {}).get('Data', {}).get('Site', {})
                load_w = abs(site.get('P_Load', 0) or 0)
                house_estimate = 500
                
                if load_w > house_estimate:
                    status['power_w'] = int(load_w - house_estimate)
                    status['load_w'] = int(load_w)
            except:
                pass
        
        return jsonify(status)
    except Exception as e:
        logger.warning(f'Wattpilot status query failed: {e}')
        return jsonify({
            'connected': False,
            'status': 'Unknown',
            'power_w': 0,
            'error': str(e)
        })


@app.route('/api/power-flow')
def api_power_flow():
    """Get real-time power flow showing where electricity comes from and goes to."""
    try:
        response = requests.get(
            'http://192.168.1.80/solar_api/v1/GetPowerFlowRealtimeData.fcgi?Scope=System',
            timeout=10
        )
        data = response.json()
        site = data.get('Body', {}).get('Data', {}).get('Site', {})
        
        pv_w = site.get('P_PV', 0) or 0
        battery_w = site.get('P_Akku', 0) or 0
        grid_w = site.get('P_Grid', 0) or 0
        load_w = abs(site.get('P_Load', 0) or 0)
        
        return jsonify({
            'pv_w': pv_w,
            'battery_w': battery_w,
            'grid_w': grid_w,
            'load_w': load_w,
            'autonomy': site.get('rel_Autonomy', 0),
            'self_consumption': site.get('rel_SelfConsumption', 0)
        })
    except Exception as e:
        logger.warning(f'Power flow query failed: {e}')
        return jsonify({
            'pv_w': 0, 'battery_w': 0, 'grid_w': 0, 'load_w': 0,
            'error': str(e)
        })


@app.route('/api/report')
def api_report():
    report = generate_morning_report()
    return jsonify({'report': report})


@app.route('/api/history')
def api_history():
    """Get energy history from database using direct SQL to avoid session issues."""
    days = request.args.get('days', 1, type=int)
    
    # Use direct SQL connection
    import sqlite3
    from datetime import datetime, timedelta
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Calculate cutoff - days=0 means today (midnight), days=1 means yesterday, etc.
        if days == 0:
            # Today from midnight
            cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute("""
            SELECT timestamp, house_consumption_w, pv_production_w, 
                   battery_soc * 100 as battery_soc, grid_power_w, wattpilot_power_w,
                   price_per_kwh
            FROM energy_readings
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (cutoff_str,))
        
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                'timestamp': row['timestamp'],
                'house_consumption_w': row['house_consumption_w'],
                'pv_production_w': row['pv_production_w'],
                'battery_soc': row['battery_soc'],
                'grid_power_w': row['grid_power_w'],
                'wattpilot_power_w': row['wattpilot_power_w'],
                'price_per_kwh': row['price_per_kwh'] or 0
            })
        
        conn.close()
        return jsonify(result)
    except Exception as e:
        logger.error(f"History query failed: {e}")
        return jsonify([])


@app.route('/api/profile')
def api_profile():
    services = get_services()
    profile = services['learning'].get_daily_profile()
    return jsonify(profile)


@app.route('/api/optimize')
def api_optimize():
    """
    Comprehensive optimization endpoint returning strategy and actions.
    
    Returns:
        target_18h: Target battery SOC at 18:00
        strategy: PREPARE, CHARGE, DISCHARGE, HOLD
        reason: Explanation
        actions: List of recommended actions
        battery_target: Target battery SOC
        ev_status: EV charging recommendation
    """
    services = get_services()
    now = now_berlin()
    hour = now.hour
    
    # Get data
    state = services['evcc'].get_current_state()
    current_soc = state.get('battery_soc', 0) * 100  # Convert to %
    prices = services['tibber'].get_current_prices()
    pv_forecast = services['forecast'].get_pv_forecast()
    profile = services['learning'].get_daily_profile()
    
    # Get current price
    current_price = 0
    current_price_ct = 0
    for p in prices:
        if p['timestamp'].hour == hour:
            current_price = p['price']
            current_price_ct = p['price'] * 100
            break
    if current_price == 0 and prices:
        current_price = prices[0]['price']
        current_price_ct = current_price * 100
    
    # Get tomorrow PV forecast (estimate from remaining today)
    pv_total_today = pv_forecast.get('total_w', 0) / 1000  # kWh
    
    # Analyze tomorrow weather - check Open-Meteo for weather code
    # Weather codes: 0=clear, 1=mainly clear, 2=partly cloudy, 3=overcast
    tomorrow_cloudy = True  # Default assume cloudy
    try:
        import requests
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude=48.086&longitude=11.024&daily=weathercode&timezone=Europe/Berlin&forecast_days=2"
        weather_resp = requests.get(weather_url, timeout=5)
        if weather_resp.status_code == 200:
            weather_data = weather_resp.json()
            tomorrow_code = weather_data.get('daily', {}).get('weathercode', [3])[1]  # Index 1 = tomorrow
            tomorrow_cloudy = tomorrow_code >= 3  # Overcast or worse
    except:
        pass
    
    # Fallback: if PV forecast available and > 30 kWh, tomorrow is sunny
    if pv_total_today > 25:
        tomorrow_cloudy = False
    
    # Get tomorrow prices (if available)
    tomorrow_prices = [p for p in prices if p['timestamp'].day != now.day] if prices else []
    tomorrow_avg_price = sum(p['price'] for p in tomorrow_prices) / len(tomorrow_prices) if tomorrow_prices else 0.25
    
    # Calculate target_18h based on strategy
    if tomorrow_cloudy:
        # Bad weather tomorrow - keep battery fuller
        target_18h = 95
        strategy = "PREPARE"
        reason = f"Morgen bewölkt/regnerisch - volle Batterie halten (PV heute: {pv_total_today:.1f} kWh)"
    else:
        # Good weather tomorrow - can discharge more today
        target_18h = 60
        strategy = "DISCHARGE"
        reason = f"Morgen sonnig erwartet - Batterie für Solar nutzen (PV heute: {pv_total_today:.1f} kWh)"
    
    # Price-based adjustments
    actions = []
    if current_price_ct > 28:
        actions.append(f"Strom teuer ({current_price_ct:.1f} ct/kWh) - Batterie entladen")
    elif current_price_ct < 18:
        actions.append(f"Strom günstig ({current_price_ct:.1f} ct/kWh) - Batterie laden")
    
    # Time-based actions
    if 6 <= hour <= 9:
        actions.append("Morgen: Sicherung angemessener Batteriestand")
    if 17 <= hour <= 21:
        actions.append("Abend: Aufladen für Nachtverbrauch vorbereiten")
    
    # EV charging recommendation - based on Werner's rule
    # Price < 28 ct → charge (grid OK), Price > 28 ct → wait for solar tomorrow
    if current_price_ct < 28:
        actions.append(f"EV: Laden! Preis günstig ({current_price_ct:.1f} ct/kWh)")
    else:
        actions.append(f"EV: Warten bis morgen - Preis zu hoch ({current_price_ct:.1f} ct/kWh)")
    
    # Battery recommendation
    battery_action = "hold"
    if current_soc < target_18h - 10 and current_price_ct < 22:
        battery_action = "charge"
    elif current_soc > 70 and current_price_ct > 26:
        battery_action = "discharge"
    
    result = {
        'timestamp': now.isoformat(),
        'target_18h': target_18h,
        'current_soc': round(current_soc, 1),
        'strategy': strategy,
        'reason': reason,
        'actions': actions,
        'battery': {
            'target': target_18h,
            'current': round(current_soc, 1),
            'action': battery_action,
            'price_ctkwh': round(current_price_ct, 2)
        },
        'pv_forecast_kwh': round(pv_total_today, 1),
        'tomorrow': {
            'cloudy': tomorrow_cloudy,
            'avg_price_ct': round(tomorrow_avg_price * 100, 2) if tomorrow_avg_price else None
        }
    }
    
    return jsonify(result)


# ---------------------------------------------------------------------------
# RFID / Charge-Sessions
# ---------------------------------------------------------------------------
def _ensure_sessions_table():
    """Stellt sicher dass die Tabelle existiert (falls OCPP-Server noch nicht lief)."""
    import sqlite3 as _sq
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    try:
        c = _sq.connect(str(db_path))
        c.execute("""
            CREATE TABLE IF NOT EXISTS charge_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL, stopped_at TEXT, id_tag TEXT,
              transaction_id INTEGER, meter_start_wh INTEGER, meter_stop_wh INTEGER,
              energy_kwh REAL, duration_min INTEGER, connector_id INTEGER, stop_reason TEXT
            )
        """)
        c.commit(); c.close()
    except Exception as e:
        logger.debug(f'_ensure_sessions_table: {e}')


_ensure_sessions_table()


@app.route('/api/rfid/aliases', methods=['GET', 'POST'])
def api_rfid_aliases():
    """GET: aktuelle Mapping-Tabelle.
       POST: setze ein Mapping {"id_tag":"ABC123","alias":"Werner"}
             oder loesche {"id_tag":"ABC123","alias":""} bzw. {"delete":"ABC123"}.
    """
    if request.method == 'GET':
        return jsonify(load_rfid_aliases())
    data = request.get_json(silent=True) or {}
    aliases = load_rfid_aliases()
    if 'delete' in data:
        aliases.pop(data['delete'], None)
    elif 'id_tag' in data:
        tag = (data['id_tag'] or '').strip()
        alias = (data.get('alias') or '').strip()
        if not tag:
            return jsonify({'success': False, 'error': 'id_tag empty'}), 400
        if alias:
            aliases[tag] = alias
        else:
            aliases.pop(tag, None)
    else:
        return jsonify({'success': False, 'error': 'missing id_tag or delete'}), 400
    save_rfid_aliases(aliases)
    return jsonify({'success': True, 'aliases': aliases})


def _attach_alias(rows, aliases):
    out = []
    for r in rows:
        d = dict(r)
        d['alias'] = aliases.get(d.get('id_tag') or '', '')
        out.append(d)
    return out


@app.route('/api/rfid/sessions')
def api_rfid_sessions():
    """Liste der Lade-Sessions.
       Query: month=YYYY-MM (optional), limit=int (default 200), tag=<id>
    """
    import sqlite3 as _sq
    month = request.args.get('month')
    tag = request.args.get('tag')
    limit = min(int(request.args.get('limit', 200)), 1000)
    db_path = Path(__file__).parent / 'energy_optimizer.db'

    where = []
    args = []
    if month:
        # month like 2026-04 -> alle started_at die mit "2026-04" beginnen
        where.append("substr(started_at, 1, 7) = ?")
        args.append(month)
    if tag:
        where.append("id_tag = ?")
        args.append(tag)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    try:
        c = _sq.connect(str(db_path))
        c.row_factory = _sq.Row
        rows = c.execute(f"""
            SELECT id, started_at, stopped_at, id_tag, transaction_id,
                   meter_start_wh, meter_stop_wh, energy_kwh, duration_min, stop_reason
            FROM charge_sessions {where_sql}
            ORDER BY id DESC LIMIT ?
        """, (*args, limit)).fetchall()
        c.close()
        aliases = load_rfid_aliases()
        return jsonify({'count': len(rows), 'sessions': _attach_alias(rows, aliases)})
    except Exception as e:
        logger.error(f'api_rfid_sessions: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/rfid/sessions.csv')
def api_rfid_sessions_csv():
    """CSV-Export aller (oder gefilterter) Sessions.
       Query-Params: month=YYYY-MM (optional), tag=<id> (optional), all=1 (alles)
    """
    import sqlite3 as _sq
    import csv as _csv
    import io as _io

    month = request.args.get('month')
    tag = request.args.get('tag')
    take_all = request.args.get('all') == '1'
    db_path = Path(__file__).parent / 'energy_optimizer.db'

    where = []
    args = []
    if not take_all:
        if month:
            where.append("substr(started_at, 1, 7) = ?")
            args.append(month)
        if tag:
            where.append("id_tag = ?")
            args.append(tag)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    try:
        c = _sq.connect(str(db_path))
        c.row_factory = _sq.Row
        rows = c.execute(f"""
            SELECT id, started_at, stopped_at, id_tag, transaction_id,
                   meter_start_wh, meter_stop_wh, energy_kwh, duration_min,
                   connector_id, stop_reason
            FROM charge_sessions {where_sql}
            ORDER BY started_at ASC
        """, args).fetchall()
        c.close()
        aliases = load_rfid_aliases()

        buf = _io.StringIO()
        # BOM damit Excel UTF-8 sofort erkennt
        buf.write('﻿')
        w = _csv.writer(buf, delimiter=';', quoting=_csv.QUOTE_MINIMAL)
        w.writerow([
            'ID', 'Datum', 'Start', 'Ende', 'Dauer (min)',
            'Chip-ID', 'Alias',
            'Meter-Start (Wh)', 'Meter-Stop (Wh)', 'Energie (kWh)',
            'Connector', 'Stop-Grund', 'Transaktion'
        ])
        total_kwh = 0.0
        for r in rows:
            d = dict(r)
            started = d.get('started_at') or ''
            stopped = d.get('stopped_at') or ''
            try:
                started_dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                # ggf. von UTC nach Berlin konvertieren
                started_local = started_dt.astimezone(BERLIN_TZ) if started_dt.tzinfo else started_dt
                date_str = started_local.strftime('%Y-%m-%d')
                start_str = started_local.strftime('%H:%M:%S')
            except Exception:
                date_str = started[:10]
                start_str = started[11:19]
            try:
                stopped_dt = datetime.fromisoformat(stopped.replace('Z', '+00:00')) if stopped else None
                stop_str = stopped_dt.astimezone(BERLIN_TZ).strftime('%H:%M:%S') if stopped_dt else ''
            except Exception:
                stop_str = stopped[11:19] if stopped else ''

            kwh = d.get('energy_kwh')
            if kwh is not None:
                total_kwh += float(kwh)
            tag_id = d.get('id_tag') or ''
            alias = aliases.get(tag_id, '')
            w.writerow([
                d.get('id'), date_str, start_str, stop_str, d.get('duration_min') or '',
                tag_id, alias,
                d.get('meter_start_wh') or '', d.get('meter_stop_wh') or '',
                f"{kwh:.3f}".replace('.', ',') if kwh is not None else '',
                d.get('connector_id') or '', d.get('stop_reason') or '', d.get('transaction_id') or '',
            ])
        # Summenzeile
        w.writerow([])
        w.writerow(['', '', '', '', '', '', 'GESAMT', '', '', f"{total_kwh:.3f}".replace('.', ','), '', '', ''])

        # Dateiname
        parts = []
        if tag:
            alias = load_rfid_aliases().get(tag, tag)
            parts.append(alias.replace(' ', '_'))
        if month:
            parts.append(month)
        if not parts:
            parts.append('alle')
        filename = 'ladungen_' + '_'.join(parts) + '.csv'

        return Response(
            buf.getvalue(),
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f'sessions.csv: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/wallbox/active_session')
def api_wallbox_active_session():
    """Liefert die aktuell laufende Lade-Session (falls vorhanden) inkl. Live-Energie.

    Quelle: charge_sessions WHERE stopped_at IS NULL ORDER BY id DESC LIMIT 1.
    Live-Energie = (aktueller Meter im OCPP-Server) - meter_start_wh.
    """
    import sqlite3 as _sq
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    aliases = load_rfid_aliases()
    out = {'active': False}
    try:
        c = _sq.connect(str(db_path))
        c.row_factory = _sq.Row
        row = c.execute("""
            SELECT id, started_at, id_tag, transaction_id, meter_start_wh, connector_id
            FROM charge_sessions WHERE stopped_at IS NULL
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        c.close()
        if row:
            d = dict(row)
            tag = d.get('id_tag') or ''
            d['alias'] = aliases.get(tag, '')
            # Live-Meter aus OCPP-Status holen
            try:
                wp = wallbox_send_cmd({'cmd': 'status'})
                state_file = NEXUS_DIR / 'state.json'
                if state_file.exists():
                    s = json.loads(state_file.read_text())
                    cur_meter = s.get('last_meter_wh')
                    if cur_meter is not None and d.get('meter_start_wh') is not None:
                        diff = (int(cur_meter) - int(d['meter_start_wh'])) / 1000.0
                        d['live_kwh'] = round(max(0.0, diff), 3)  # nie negativ
                        d['current_meter_wh'] = int(cur_meter)
                d['live_power_w'] = wp.get('meter_w', 0)
                d['connector_status'] = wp.get('connector_status')
            except Exception as e:
                logger.debug(f'live data: {e}')
            # Dauer in Minuten berechnen (started_at kommt als UTC ohne tz aus SQLite)
            try:
                from datetime import timezone as _tz
                ts = (d['started_at'] or '').replace('T', ' ').replace('Z', '')
                # Versuche ISO mit tz, fallback naive UTC
                try:
                    dt = datetime.fromisoformat(d['started_at'].replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                except Exception:
                    dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=_tz.utc)
                dt_local = dt.astimezone(BERLIN_TZ)
                elapsed = int((now_berlin() - dt_local).total_seconds() // 60)
                d['elapsed_min'] = max(0, elapsed)
                d['started_local'] = dt_local.strftime('%d.%m. %H:%M')
            except Exception as _e:
                logger.debug(f'elapsed parse: {_e}')
                d['elapsed_min'] = None
            # --- EV-SOC Schaetzung ---
            ev_vehicles = load_ev_vehicles()
            alias = d.get('alias', '')
            vehicle = ev_vehicles.get(alias)
            if vehicle and vehicle.get('battery_kwh'):
                batt = vehicle['battery_kwh']
                d['ev_vehicle_name'] = vehicle.get('name', '')
                d['ev_battery_kwh'] = batt
                if d.get('live_kwh') is not None:
                    # Geschaetzter SOC-Zuwachs durch diese Ladung
                    soc_added = round(d['live_kwh'] / batt * 100, 1)
                    d['ev_soc_added_pct'] = soc_added
            out = {'active': True, 'session': d}
        return jsonify(out)
    except Exception as e:
        logger.error(f'active_session: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/ev_vehicles', methods=['GET', 'POST'])
def api_ev_vehicles():
    """GET: Liste aller EV-Fahrzeuge.
       POST: Fahrzeug anlegen/aendern: {alias, name, battery_kwh}
             oder loeschen: {delete: alias}
    """
    if request.method == 'GET':
        return jsonify(load_ev_vehicles())
    data = request.get_json(force=True, silent=True) or {}
    vehicles = load_ev_vehicles()
    if 'delete' in data:
        vehicles.pop(data['delete'], None)
    elif 'alias' in data:
        alias = data['alias'].strip()
        if not alias:
            return jsonify({'success': False, 'error': 'alias empty'}), 400
        vehicles[alias] = {
            'name': data.get('name', '').strip(),
            'battery_kwh': float(data.get('battery_kwh', 0)),
        }
    else:
        return jsonify({'success': False, 'error': 'missing alias or delete'}), 400
    save_ev_vehicles(vehicles)
    return jsonify({'success': True, 'vehicles': vehicles})


@app.route('/api/rfid/summary')
def api_rfid_summary():
    """Aggregat: kWh pro Monat pro idTag/Alias.
       Query: months=12 (default 6) – wieviele Monate zurueck.
    """
    import sqlite3 as _sq
    months = int(request.args.get('months', 6))
    db_path = Path(__file__).parent / 'energy_optimizer.db'
    try:
        c = _sq.connect(str(db_path))
        c.row_factory = _sq.Row
        rows = c.execute("""
            SELECT substr(COALESCE(stopped_at, started_at), 1, 7) AS month,
                   COALESCE(id_tag, 'unknown') AS id_tag,
                   SUM(COALESCE(energy_kwh, 0)) AS kwh,
                   COUNT(*) AS sessions,
                   SUM(COALESCE(duration_min, 0)) AS minutes
            FROM charge_sessions
            WHERE started_at >= date('now', ?)
            GROUP BY month, id_tag
            ORDER BY month DESC, kwh DESC
        """, (f'-{months} months',)).fetchall()
        c.close()
        aliases = load_rfid_aliases()
        out = []
        for r in rows:
            d = dict(r)
            d['alias'] = aliases.get(d['id_tag'], '')
            d['kwh'] = round(d['kwh'] or 0, 2)
            out.append(d)
        # auch je Monat einen Total-Summary
        per_month = {}
        for d in out:
            per_month.setdefault(d['month'], 0.0)
            per_month[d['month']] += d['kwh']
        return jsonify({'rows': out, 'totals': {m: round(v, 2) for m, v in per_month.items()}})
    except Exception as e:
        logger.error(f'api_rfid_summary: {e}')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Schedules (geplante Lade-Sessions)
# ---------------------------------------------------------------------------
def _is_holiday_today() -> bool:
    if _HOLIDAYS is None:
        return False
    try:
        return now_berlin().date() in _HOLIDAYS
    except Exception:
        return False


def _active_schedule_now() -> dict | None:
    now = now_berlin()
    iso_dow = now.isoweekday()
    today = now.date()
    for s in load_schedules():
        try:
            if not s.get('active', True):
                continue
            if iso_dow not in (s.get('days') or []):
                continue
            if s.get('skip_holidays', True) and _HOLIDAYS and today in _HOLIDAYS:
                continue
            sh, sm = (s.get('start') or '00:00').split(':')
            eh, em = (s.get('end') or '23:59').split(':')
            sd = now.replace(hour=int(sh), minute=int(sm), second=0, microsecond=0)
            ed = now.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
            if sd <= now <= ed:
                return s
        except Exception:
            pass
    return None


def _next_schedule_today() -> dict | None:
    """Naechste heute noch ausstehende Schedule (zur Anzeige)."""
    now = now_berlin()
    iso_dow = now.isoweekday()
    today = now.date()
    candidates = []
    for s in load_schedules():
        try:
            if not s.get('active', True):
                continue
            if iso_dow not in (s.get('days') or []):
                continue
            if s.get('skip_holidays', True) and _HOLIDAYS and today in _HOLIDAYS:
                continue
            sh, sm = (s.get('start') or '00:00').split(':')
            sd = now.replace(hour=int(sh), minute=int(sm), second=0, microsecond=0)
            if sd >= now:
                candidates.append((sd, s))
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


@app.route('/api/schedules', methods=['GET', 'POST'])
def api_schedules():
    """GET: alle Schedules. POST: ersetzt die ganze Liste {schedules:[...]}."""
    if request.method == 'GET':
        return jsonify({
            'schedules': load_schedules(),
            'active_now': _active_schedule_now(),
            'next_today': _next_schedule_today(),
            'is_holiday_today': _is_holiday_today(),
        })
    data = request.get_json(silent=True) or {}
    schedules = data.get('schedules')
    if not isinstance(schedules, list):
        return jsonify({'success': False, 'error': 'schedules muss Liste sein'}), 400
    # validieren
    for s in schedules:
        if not s.get('name'):
            return jsonify({'success': False, 'error': 'name fehlt'}), 400
        if not isinstance(s.get('days'), list):
            return jsonify({'success': False, 'error': 'days fehlt'}), 400
    save_schedules(schedules)
    logger.info(f'Schedules aktualisiert ({len(schedules)} Eintraege)')
    return jsonify({'success': True, 'schedules': schedules})


@app.route('/api/schedules/<int:idx>', methods=['DELETE'])
def api_schedules_delete(idx):
    schedules = load_schedules()
    if idx < 0 or idx >= len(schedules):
        return jsonify({'success': False, 'error': 'index out of range'}), 404
    removed = schedules.pop(idx)
    save_schedules(schedules)
    return jsonify({'success': True, 'removed': removed})


# ---------------------------------------------------------------------------
# Tagesplan / Empfehlungs-Engine (regelbasiert, deterministisch)
# ---------------------------------------------------------------------------
@app.route('/api/plan/today')
def api_plan_today():
    """Tagesplan: Empfehlungen anhand Wetter, Tibber, Schedule, Akku."""
    services = get_services()
    now = now_berlin()
    try:
        pv = services['forecast'].get_pv_forecast()
    except Exception:
        pv = {}
    try:
        prices = services['tibber'].get_current_prices() or []
    except Exception:
        prices = []
    plan = {}
    try:
        if BATTERY_PLAN_FILE.exists():
            plan = json.loads(BATTERY_PLAN_FILE.read_text())
    except Exception:
        pass
    bm = {}
    if BATTERY_MODBUS_OK and _battery_modbus:
        try:
            bm = _battery_modbus.read()
        except Exception:
            bm = {}

    # Tibber: heute guenstigste / teuerste Stunden
    today_prices = [p for p in prices if p['timestamp'].day == now.day]
    cheap_hours = sorted(today_prices, key=lambda p: p['price'])[:3]
    expensive_hours = sorted(today_prices, key=lambda p: -p['price'])[:3]

    def fmt_h(p):
        return {'hour': p['timestamp'].hour, 'price_ct': round(p['price']*100, 2)}

    today_kwh = pv.get('today_kwh') or (pv.get('total_w', 0) or 0) / 1000.0
    tomorrow_kwh = pv.get('tomorrow_kwh') or 0
    soc = bm.get('soc') if bm.get('soc') is not None else (plan.get('current_soc') or 0)

    sched_now = _active_schedule_now()
    sched_next = _next_schedule_today()
    try:
        pool_learning = services['learning'].get_pool_learning_summary()
    except Exception:
        pool_learning = get_pool_learning_summary()
    pool_today = build_pool_plan(pv, pool_learning, day_offset=0)
    pool_tomorrow = build_pool_plan(pv, pool_learning, day_offset=1)

    # Empfehlungen formulieren
    recommendations = []
    weather_emoji = '☀'
    if tomorrow_kwh < 8:
        weather_emoji = '☁'
        recommendations.append({
            'icon': '🔋',
            'title': 'Akku heute Abend voller halten',
            'detail': f'Morgen wenig Sonne ({tomorrow_kwh:.1f} kWh erwartet) - Ziel 18:30 ist auf {plan.get("target_soc_1830", 90)}% gesetzt.'
        })
    elif tomorrow_kwh >= 32:
        recommendations.append({
            'icon': '☀',
            'title': 'Morgen viel Sonne',
            'detail': f'Akku darf heute Abend bis {plan.get("target_soc_1830", 65)}% entladen werden ({tomorrow_kwh:.1f} kWh PV erwartet).'
        })
    elif tomorrow_kwh >= 15:
        weather_emoji = '⛅'
        recommendations.append({
            'icon': '🌤',
            'title': 'Morgen wechselhaft',
            'detail': f'Mittlere Sonne ({tomorrow_kwh:.1f} kWh) - Akku-Ziel 18:30: {plan.get("target_soc_1830", 80)}%.'
        })
    else:
        weather_emoji = '⛅'
        recommendations.append({
            'icon': '🌤',
            'title': 'Morgen wenig Sonne',
            'detail': f'Wenig Sonne ({tomorrow_kwh:.1f} kWh) - Akku-Ziel 18:30: {plan.get("target_soc_1830", 80)}%.'
        })

    if pool_today.get('active'):
        recommendations.append({
            'icon': '🏊',
            'title': 'Pool läuft',
            'detail': f'Pool ist als Verbraucher aktiv: ca. {pool_today["power_w"]} W bis etwa {pool_today.get("end") or "offen"} Uhr, erwartet {pool_today["expected_kwh"]:.1f} kWh.'
        })
    elif pool_tomorrow.get('expected'):
        recommendations.append({
            'icon': '🏊',
            'title': 'Pool morgen einplanen',
            'detail': f'{pool_tomorrow["weather_reason"]}. Erwartet ab {pool_tomorrow["start"]} Uhr fuer {pool_tomorrow["duration_min"]} min, ca. {pool_tomorrow["expected_kwh"]:.1f} kWh.'
        })
    elif pool_tomorrow.get('weather_class') == 'bad':
        recommendations.append({
            'icon': '🏊',
            'title': 'Pool morgen eher auslassen',
            'detail': f'{pool_tomorrow["weather_reason"]}. VistaEnergy plant dafuer keine Zusatzlast ein.'
        })

    if sched_now:
        recommendations.append({
            'icon': '⚡',
            'title': f'JETZT: {sched_now["name"]}',
            'detail': f'Geplante Ladung {sched_now["start"]}-{sched_now["end"]} mit {sched_now["max_amps"]}A {sched_now["phases"]}-phasig laeuft gerade.'
        })
    elif sched_next:
        recommendations.append({
            'icon': '📅',
            'title': f'Heute geplant: {sched_next["name"]}',
            'detail': f'Ab {sched_next["start"]} bis {sched_next["end"]} - {sched_next["max_amps"]}A x {sched_next["phases"]}ph.'
        })

    if cheap_hours:
        h = cheap_hours[0]['timestamp'].hour
        p = cheap_hours[0]['price'] * 100
        recommendations.append({
            'icon': '💶',
            'title': f'Guenstigste Stunde: {h:02d}:00 Uhr',
            'detail': f'{p:.1f} ct/kWh - ideal fuer Waschmaschine / Trockner / Geschirrspueler / Force-Charge.'
        })

    if expensive_hours:
        h = expensive_hours[0]['timestamp'].hour
        p = expensive_hours[0]['price'] * 100
        recommendations.append({
            'icon': '⚠',
            'title': f'Teuerste Stunde: {h:02d}:00 Uhr',
            'detail': f'{p:.1f} ct/kWh - Akku-Entladung sinnvoll, keine grossen Verbraucher starten.'
        })

    if soc and soc < 30 and now.hour < 12:
        recommendations.append({
            'icon': '🪫',
            'title': 'Akku niedrig',
            'detail': f'Aktueller SOC {soc:.0f}% - tagsueber wird die PV ihn voraussichtlich auffuellen.'
        })

    return jsonify({
        'updated': now.isoformat(),
        'weather': {
            'emoji': weather_emoji,
            'today_kwh': round(today_kwh, 1),
            'tomorrow_kwh': round(tomorrow_kwh, 1),
            'sunshine_hours_tomorrow': pv.get('sunshine_hours_tomorrow'),
            'is_holiday_today': _is_holiday_today(),
        },
        'price': {
            'cheapest': [fmt_h(p) for p in cheap_hours],
            'most_expensive': [fmt_h(p) for p in expensive_hours],
        },
        'schedule': {
            'active_now': sched_now,
            'next_today': sched_next,
        },
        'pool': {
            'today': pool_today,
            'tomorrow': pool_tomorrow,
            'learning': pool_learning,
        },
        'battery': {
            'soc': round(soc, 1) if soc is not None else None,
            'target_1830': plan.get('target_soc_1830'),
            'projected_1830': plan.get('projected_soc_1830'),
        },
        'recommendations': recommendations,
    })


# Initialize
def initialize_app():
    """Initialize database and scheduler."""
    init_db()
    ensure_pool_schema()
    
    # Wrap jobs with app context for database access
    def run_with_context(fn):
        def wrapper():
            with app.app_context():
                fn()
        return wrapper

    # Einmal beim Start aufraeumen; danach taeglich nachts.
    maintenance_job()
    
    # Schedule data collection every 15 minutes
    scheduler.add_job(run_with_context(collect_data_job), 'interval', minutes=15, id='data_collection')

    # Schedule optimization every 15 minutes (offset by 5 min)
    scheduler.add_job(run_with_context(optimize_job), 'interval', minutes=5, id='optimization', next_run_time=datetime.now() + timedelta(minutes=2))

    # Schedule battery-plan write every 60s (consumed by pv_controller.py)
    scheduler.add_job(
        run_with_context(write_battery_plan_job), 'interval', seconds=60,
        id='battery_plan', next_run_time=datetime.now() + timedelta(seconds=5)
    )

    scheduler.add_job(run_with_context(maintenance_job), 'cron', hour=3, minute=15, id='maintenance')
    scheduler.add_job(run_with_context(forecast_log_job), 'cron', hour=23, minute=30, id='forecast_log')
    scheduler.add_job(
        run_with_context(lambda: check_update_job(auto_install=True)),
        'cron', hour=4, minute=20, id='update_check'
    )

    scheduler.start()
    logger.info("Energy Optimizer started successfully")


if __name__ == '__main__':
    initialize_app()
    
    port = int(os.getenv('WEB_PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
