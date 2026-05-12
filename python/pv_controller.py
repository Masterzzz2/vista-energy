#!/usr/bin/env python3
"""
NEXUS PV-Controller v1.0
========================
Eigenstaendiger Daemon, der alle 30 Sekunden die Wattpilot-Strom-Stellgroesse
basierend auf dem aktuellen PV-Ueberschuss UND dem 18:30/65%-Akku-Plan
nachregelt.

Datenfluss:
  Fronius PowerFlow -> hier rechnen -> ws://localhost:8889 (OCPP-Control)

Modes (gelesen aus /home/werner/.nexus/wallbox_mode):
  eco       -> Nur PV-Ueberschuss laden, sonst Pause
  standard  -> Smart-Steuerung:
                 - bei niedrigem Strompreis (Tibber) volle Power
                 - bei hohem Preis: PV-Ueberschuss UND erzwingen wenn Akku
                   sonst zu schnell voll waere (18:30/65% Ziel)
  off       -> Wallbox auf Inoperative

Anti-Schwingung:
  - Aenderungen erst, wenn delta >= 1A oder Pause/Resume noetig
  - mind. 60 s zwischen Pause/Resume
"""
import asyncio
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import websockets

BERLIN_TZ = ZoneInfo('Europe/Berlin')

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
STATE_DIR = Path('/home/werner/.nexus')
STATE_DIR.mkdir(parents=True, exist_ok=True)
MODE_FILE = STATE_DIR / 'wallbox_mode'         # eco|mix|fast|off|manual
PLAN_FILE = STATE_DIR / 'battery_plan.json'    # vom Optimizer geschrieben
SCHEDULES_FILE = STATE_DIR / 'schedules.json'  # geplante Lade-Sessions
LOG_FILE = STATE_DIR / 'pv_controller.log'

# Optionale Bayern-Feiertage (falls library da)
try:
    import holidays as _holidays
    _HOL = _holidays.DE(prov='BY')
except Exception:
    _HOL = None

FRONIUS_HOST = '192.168.1.80'
CONTROL_WS = 'ws://127.0.0.1:8889'

LOOP_SECONDS = 30
MIN_AMPS = 6
MAX_AMPS = 16
PHASES_3 = 3
VOLT = 230
HEADROOM_W = 100            # Sicherheits-Reserve damit nichts aus dem Netz gezogen wird
MIN_3PH_W = MIN_AMPS * VOLT * PHASES_3   # 4140 W minimum fuer 3-phasig
MIN_1PH_W = MIN_AMPS * VOLT              # 1380 W minimum fuer 1-phasig
PAUSE_DEBOUNCE_S = 60       # mind. 60 s zwischen Pause / Resume

# Ziel-SOC um 18:30 (kann ueber battery_plan.json ueberschrieben werden)
TARGET_SOC_AT_1830 = 65.0

# Tibber-Preis Schwelle fuer Standard-Modus (ct/kWh)
PRICE_LOW = 18.0   # Strom guenstig: laden auch ohne PV
PRICE_HIGH = 28.0  # Strom teuer:  nur PV

# Wenn ein EV steckt und der Wattpilot zwar Stromprofil bekommt, aber keine
# Transaktion startet, einmalig RemoteStart ausloesen.
AUTO_REMOTE_START = os.getenv('WATTPILOT_AUTO_REMOTE_START', 'true').lower() in ('1', 'true', 'yes', 'on')
REMOTE_START_DEBOUNCE_S = int(os.getenv('WATTPILOT_REMOTE_START_DEBOUNCE_S', '180'))
REMOTE_START_REJECT_COOLDOWN_S = int(os.getenv('WATTPILOT_REMOTE_START_REJECT_COOLDOWN_S', '900'))
START_ON_1PH_UNTIL_CHARGING = os.getenv('WATTPILOT_START_ON_1PH', 'true').lower() in ('1', 'true', 'yes', 'on')
CHARGING_ACTIVE_W = int(os.getenv('WATTPILOT_CHARGING_ACTIVE_W', '500'))

# ECO-Akku-Puffer: kurze Wolken/Lastspitzen sollen eine laufende
# Ueberschussladung nicht sofort abbrechen. Vista haelt dann fuer einige
# Minuten die 1-phasige Mindestladung und laesst den Hausakku puffern.
ECO_BATTERY_BUFFER_S = int(os.getenv('WATTPILOT_ECO_BATTERY_BUFFER_S', '300'))
ECO_BATTERY_BUFFER_MIN_SOC = float(os.getenv('WATTPILOT_ECO_BATTERY_BUFFER_MIN_SOC', '65'))
ECO_BATTERY_BUFFER_MAX_GRID_IMPORT_W = int(os.getenv('WATTPILOT_ECO_BATTERY_BUFFER_MAX_GRID_IMPORT_W', '400'))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=5),
    ],
)
log = logging.getLogger('pvc')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_berlin():
    return datetime.now(BERLIN_TZ)


def iso_ts_to_epoch(raw) -> float | None:
    try:
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def read_mode() -> str:
    """Lies den gewuenschten Modus aus der Datei.
       Werte:
         'eco'      = nur PV-Ueberschuss
         'mix'      = Netz-Strom + PV (smart, frueher 'standard')
         'fast'     = volle Ladung, alle Quellen, Akku darf entladen
         'off'      = Wallbox aus
         'manual'   = keine automatische Regelung; Dashboard-Tasten bleiben frei
       Backwards-Compat: 'standard' -> 'mix'
    """
    try:
        if MODE_FILE.exists():
            m = MODE_FILE.read_text().strip().lower()
            if m == 'standard':  # legacy
                m = 'mix'
            if m in ('eco', 'mix', 'fast', 'off', 'manual'):
                return m
    except Exception as e:
        log.warning(f'read_mode: {e}')
    return 'mix'


def read_plan() -> dict:
    """Lies den Battery-Plan vom Optimizer."""
    try:
        if PLAN_FILE.exists():
            return json.loads(PLAN_FILE.read_text())
    except Exception as e:
        log.warning(f'read_plan: {e}')
    return {}


def read_schedules() -> list:
    """Lies geplante Lade-Sessions."""
    try:
        if SCHEDULES_FILE.exists():
            data = json.loads(SCHEDULES_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f'read_schedules: {e}')
    return []


def is_holiday(d) -> bool:
    if _HOL is None:
        return False
    try:
        return d in _HOL
    except Exception:
        return False


def active_schedule(now: datetime) -> dict | None:
    """Liefert die aktive Schedule, falls eine zur jetzigen Zeit zaehlt.

    Schedule-Schema:
      {
        "name": "Moni Mercedes",
        "alias": "Moni",            # informativ
        "days": [2,3,5],            # ISO-weekday: 1=Mo .. 7=So
        "start": "13:30",
        "end":   "17:00",
        "max_amps": 16,
        "phases": 3,
        "skip_holidays": true,
        "active": true
      }
    """
    schedules = read_schedules()
    iso_dow = now.isoweekday()
    today = now.date()
    for s in schedules:
        try:
            if not s.get('active', True):
                continue
            if iso_dow not in (s.get('days') or []):
                continue
            if s.get('skip_holidays', True) and is_holiday(today):
                continue
            sh, sm = (s.get('start') or '00:00').split(':')
            eh, em = (s.get('end') or '23:59').split(':')
            start_dt = now.replace(hour=int(sh), minute=int(sm), second=0, microsecond=0)
            end_dt = now.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
            if start_dt <= now <= end_dt:
                return s
        except Exception as e:
            log.warning(f'schedule {s.get("name")}: {e}')
    return None


def fetch_fronius() -> dict:
    """Live-Daten vom Fronius Hybrid GEN24."""
    out = {'pv_w': 0, 'load_w': 0, 'grid_w': 0, 'akku_w': 0, 'soc': 0.0}
    try:
        r = requests.get(
            f'http://{FRONIUS_HOST}/solar_api/v1/GetPowerFlowRealtimeData.fcgi?Scope=System',
            timeout=5,
        )
        r.raise_for_status()
        site = r.json()['Body']['Data']['Site']
        out['pv_w'] = float(site.get('P_PV') or 0)
        out['load_w'] = abs(float(site.get('P_Load') or 0))
        out['grid_w'] = float(site.get('P_Grid') or 0)
        out['akku_w'] = float(site.get('P_Akku') or 0)
    except Exception as e:
        log.warning(f'fronius site: {e}')
    try:
        r = requests.get(
            f'http://{FRONIUS_HOST}/solar_api/v1/GetPowerFlowRealtimeData.fcgi?Scope=Device',
            timeout=5,
        )
        r.raise_for_status()
        inv = r.json()['Body']['Data']['Inverters']
        if '1' in inv:
            out['soc'] = float(inv['1'].get('SOC') or 0)
    except Exception as e:
        log.warning(f'fronius device: {e}')
    return out


async def control_call(req: dict) -> dict:
    """Schickt einen Befehl an den OCPP-Control-Server."""
    try:
        async with websockets.connect(CONTROL_WS, ping_interval=None, open_timeout=5) as ws:
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            return json.loads(raw)
    except Exception as e:
        log.warning(f'control_call({req}): {e}')
        return {'status': 'error', 'reason': str(e)}


# ---------------------------------------------------------------------------
# Steuerlogik
# ---------------------------------------------------------------------------
def amps_for_surplus(surplus_w: float, phases: int = PHASES_3) -> int:
    """Berechne Ampere fuer einen gegebenen Ueberschuss (Watt)."""
    if surplus_w <= 0:
        return 0
    a = int(surplus_w // (VOLT * phases))
    return max(0, min(MAX_AMPS, a))


def projected_soc_at_1830(curr_soc: float, fronius: dict, plan: dict) -> float:
    """Sehr einfache Projektion. Kann der Optimizer durch genauere
    Werte in plan['projected_soc_1830'] ueberschreiben."""
    if 'projected_soc_1830' in plan:
        try:
            return float(plan['projected_soc_1830'])
        except Exception:
            pass

    # Heuristik: aktueller Akku-Trend in W * verbleibende Stunden bis 18:30
    now = now_berlin()
    target = now.replace(hour=18, minute=30, second=0, microsecond=0)
    if now > target:
        return curr_soc
    hours_left = (target - now).total_seconds() / 3600.0
    # Akku-Power positiv = Entladung, negativ = Ladung
    akku_w = fronius.get('akku_w', 0.0)
    delta_kwh = (-akku_w / 1000.0) * hours_left
    # Akku nutzbare Kapazitaet ~ 7.0 kWh
    cap_kwh = 7.0
    delta_pct = (delta_kwh / cap_kwh) * 100.0
    return max(0.0, min(100.0, curr_soc + delta_pct))


def decide(mode: str, fronius: dict, plan: dict, now: datetime) -> dict:
    """Bestimme Ziel-Stromstaerke und Aktion."""
    pv = fronius['pv_w']
    load = fronius['load_w']
    grid = fronius['grid_w']
    soc = fronius['soc']

    # Wattpilot-Verbrauch separat bekommen wir aus dem letzten OCPP-Status (s. main loop).
    # Fuer die Bilanz nehmen wir an, dass der Wattpilot Teil der "load" ist; der
    # ECHTE Hausverbrauch ist also load - wattpilot_w. Wir bekommen wattpilot_w
    # vom OCPP-Server. Wenn nicht verfuegbar, nutzen wir 0.
    wp_w = plan.get('_wattpilot_w', 0)
    house_only_w = max(0.0, load - wp_w)
    # PV-Ueberschuss = was die Anlage haette uebrig wenn Wattpilot nichts
    # zoege und kein Akku-Lade-Bedarf da waere.
    surplus_w = pv - house_only_w - HEADROOM_W

    target_soc = float(plan.get('target_soc_1830', TARGET_SOC_AT_1830))
    proj_soc = projected_soc_at_1830(soc, fronius, plan)
    overshoot = proj_soc > target_soc + 2.0   # 2% Hysterese

    decision = {
        'mode': mode,
        'pv_w': int(pv),
        'load_w': int(load),
        'house_only_w': int(house_only_w),
        'wattpilot_w': int(wp_w),
        'surplus_w': int(surplus_w),
        'soc': round(soc, 1),
        'projected_soc_1830': round(proj_soc, 1),
        'target_soc_1830': target_soc,
        'overshoot_1830': overshoot,
        'price_ct': plan.get('current_price_ct'),
    }

    if mode == 'off':
        decision.update({'action': 'pause', 'amps': 0, 'reason': 'mode=off'})
        return decision

    if mode == 'manual':
        decision.update({'action': 'idle', 'amps': None, 'phases': None,
                         'reason': 'MANUELL: automatische Wallbox-Regelung pausiert'})
        return decision

    if mode == 'fast':
        # SCHNELL: Maximale Ladung, alle Quellen, Akku darf liefern
        decision.update({
            'action': 'set', 'amps': MAX_AMPS, 'phases': PHASES_3,
            'reason': f'FAST Volllast {MAX_AMPS}A {PHASES_3}ph (Netz+PV+Akku, max Speed)',
        })
        return decision

    if mode == 'eco':
        # ECO: Reines PV-Ueberschuss laden, Mindeststrom 6A nicht unterschreiten
        a = amps_for_surplus(surplus_w, PHASES_3)
        if a >= MIN_AMPS:
            decision.update({'action': 'set', 'amps': a, 'phases': PHASES_3,
                             'reason': f'ECO PV-Ueberschuss {surplus_w:.0f}W -> {a}A 3ph'})
        elif surplus_w >= MIN_1PH_W:
            a1 = amps_for_surplus(surplus_w, 1)
            a1 = max(MIN_AMPS, a1)
            decision.update({'action': 'set', 'amps': a1, 'phases': 1,
                             'reason': f'ECO PV-Ueberschuss {surplus_w:.0f}W -> {a1}A 1ph'})
        else:
            decision.update({'action': 'pause', 'amps': 0,
                             'reason': f'ECO Ueberschuss zu klein ({surplus_w:.0f}W)'})
        return decision

    # mode == 'mix' (frueher 'standard')
    price = plan.get('current_price_ct')
    if price is not None:
        try:
            price = float(price)
        except Exception:
            price = None

    # Strompreis sehr guenstig ODER Akku wuerde Ziel ueberschreiten -> Volllast
    if (price is not None and price <= PRICE_LOW) or overshoot:
        decision.update({
            'action': 'set', 'amps': MAX_AMPS, 'phases': PHASES_3,
            'reason': (f'MIX Volllast: price={price}<= {PRICE_LOW} ' if price is not None and price <= PRICE_LOW
                       else f'MIX Volllast: Akku-Overshoot proj={proj_soc:.1f}>{target_soc}'),
        })
        return decision

    # Strompreis hoch -> nur PV-Ueberschuss
    if price is not None and price >= PRICE_HIGH:
        a = amps_for_surplus(surplus_w, PHASES_3)
        if a >= MIN_AMPS:
            decision.update({'action': 'set', 'amps': a, 'phases': PHASES_3,
                             'reason': f'MIX teuer ({price}ct), nur PV {a}A 3ph'})
        else:
            decision.update({'action': 'pause', 'amps': 0,
                             'reason': f'MIX teuer ({price}ct), PV reicht nicht'})
        return decision

    # Mittlerer Preis -> PV-Ueberschuss + 1-phasige Mindestladung wenn Akku ausreichend voll
    a = amps_for_surplus(surplus_w, PHASES_3)
    if a >= MIN_AMPS:
        decision.update({'action': 'set', 'amps': a, 'phases': PHASES_3,
                         'reason': f'MIX mittel, PV {a}A 3ph'})
    elif soc > 50:
        # Akku ist gut, etwas aus Akku/Netz mit-versorgen
        a1 = max(MIN_AMPS, amps_for_surplus(surplus_w, 1))
        decision.update({'action': 'set', 'amps': a1, 'phases': 1,
                         'reason': f'MIX mittel, Akku {soc:.0f}% -> {a1}A 1ph'})
    else:
        decision.update({'action': 'pause', 'amps': 0,
                         'reason': f'MIX mittel, Akku zu leer ({soc:.0f}%) und kein PV-Ueberschuss'})
    return decision


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.last_action = None      # 'set' / 'pause' / 'resume'
        self.last_amps = None
        self.last_phases = None
        self.last_pause_at = 0
        self.last_resume_at = 0
        self.last_remote_start_at = 0
        self.eco_buffer_started_at = 0
        self.paused = False


async def step(s: State):
    mode = read_mode()
    plan = read_plan()
    fronius = fetch_fronius()

    # Wattpilot-Power vom OCPP-Server holen (informativ)
    wp = await control_call({'cmd': 'status'})
    wp_w = wp.get('meter_w', 0) or 0
    plan['_wattpilot_w'] = wp_w

    # Wenn der Wattpilot nicht verbunden ist, koennen wir nichts tun
    if not wp.get('connected'):
        log.info(f'Wattpilot nicht verbunden (mode={mode}). Skip.')
        # State-Datei dennoch aktualisieren
        STATE_DIR.joinpath('controller_state.json').write_text(
            json.dumps({'mode': mode, 'note': 'Wattpilot offline', 'time': now_berlin().isoformat()}, indent=2)
        )
        return

    # Wenn der Wattpilot per ChangeAvailability pausiert wurde, meldet der
    # Connector "Unavailable". In diesem Zustand kann ein gestecktes Auto nicht
    # erkannt werden; bei sinnvoller Ladeentscheidung muessen wir zuerst wieder
    # auf Operative schalten.
    conn_status = wp.get('connector_status', '')
    ev_connected = conn_status in ('Charging', 'SuspendedEV', 'SuspendedEVSE', 'Preparing', 'Finishing')
    connector_unavailable = conn_status == 'Unavailable'

    # Schedule-Override: nur wenn die Wallbox nicht bewusst aus/manuell steht.
    sched = None if mode in ('manual', 'off') else active_schedule(now_berlin())
    if sched and (ev_connected or connector_unavailable) and mode == 'eco':
        decision = decide(mode, fronius, plan, now_berlin())
        decision['ev_connected'] = ev_connected
        decision['connector_status'] = conn_status
        decision['schedule'] = sched.get('name')
        if decision.get('action') == 'set':
            max_amps = max(MIN_AMPS, min(MAX_AMPS, int(sched.get('max_amps', MAX_AMPS))))
            decision['amps'] = min(int(decision.get('amps') or max_amps), max_amps)
            if int(sched.get('phases', PHASES_3)) == 1:
                decision['phases'] = 1
            decision['reason'] += f' | SCHEDULE "{sched.get("name", "?")}" aktiv, ECO bleibt PV-Ueberschuss'
        else:
            decision['reason'] += f' | SCHEDULE "{sched.get("name", "?")}" aktiv, wartet auf PV-Ueberschuss'
    elif sched and (ev_connected or connector_unavailable):
        amps = max(MIN_AMPS, min(MAX_AMPS, int(sched.get('max_amps', MAX_AMPS))))
        phases = int(sched.get('phases', PHASES_3))
        if phases not in (1, 3):
            phases = 3
        decision = {
            'mode': mode,
            'pv_w': int(fronius['pv_w']),
            'load_w': int(fronius['load_w']),
            'house_only_w': int(max(0.0, fronius['load_w'] - wp_w)),
            'wattpilot_w': int(wp_w),
            'surplus_w': int(fronius['pv_w'] - max(0.0, fronius['load_w'] - wp_w) - HEADROOM_W),
            'soc': round(fronius['soc'], 1),
            'projected_soc_1830': float(plan.get('projected_soc_1830') or 0),
            'target_soc_1830': float(plan.get('target_soc_1830') or TARGET_SOC_AT_1830),
            'overshoot_1830': bool(plan.get('overshoot')),
            'price_ct': plan.get('current_price_ct'),
            'action': 'set',
            'amps': amps,
            'phases': phases,
            'reason': f'SCHEDULE "{sched.get("name", "?")}" {sched.get("start","")}-{sched.get("end","")} -> {amps}A {phases}ph',
            'schedule': sched.get('name'),
            'ev_connected': ev_connected,
            'connector_status': conn_status,
        }
    else:
        decision = decide(mode, fronius, plan, now_berlin())
        decision['ev_connected'] = ev_connected
        decision['connector_status'] = conn_status

    should_resume_unavailable = (
        connector_unavailable
        and mode not in ('off', 'manual')
        and decision.get('action') == 'set'
    )
    if should_resume_unavailable:
        decision['action'] = 'resume'
        decision['reason'] = (
            f"{decision.get('reason', '')} | Wattpilot war Unavailable/Inoperative; "
            "bei Ladefreigabe wieder Operative setzen"
        ).strip()
    elif not ev_connected:
        decision['action'] = 'idle'
        decision['reason'] = f'kein EV (status={conn_status})'

    now_ts = time.time()
    charging_active = conn_status == 'Charging' or wp_w >= CHARGING_ACTIVE_W
    surplus_w = float(decision.get('surplus_w') or 0)
    grid_import_w = max(0.0, float(fronius.get('grid_w') or 0))

    # ECO-Puffer: wenn die Ladung schon laeuft und der Ueberschuss kurz unter
    # die 1-phasige Mindestleistung faellt, halten wir max. X Minuten mit dem
    # Hausakku durch. Sobald wieder echter Ueberschuss da ist, startet der
    # Puffer-Timer neu. Ist der Akku unter dem Tagesziel, wird nicht gepuffert.
    buffer_candidate = (
        mode == 'eco'
        and ev_connected
        and charging_active
        and decision.get('action') == 'pause'
    )
    if buffer_candidate and ECO_BATTERY_BUFFER_S > 0:
        if not s.eco_buffer_started_at:
            s.eco_buffer_started_at = now_ts

        elapsed_s = max(0, int(now_ts - s.eco_buffer_started_at))
        remaining_s = max(0, ECO_BATTERY_BUFFER_S - elapsed_s)
        shortage_w = max(0, MIN_1PH_W - surplus_w)
        target_soc = float(decision.get('target_soc_1830') or TARGET_SOC_AT_1830)
        min_buffer_soc = max(ECO_BATTERY_BUFFER_MIN_SOC, target_soc)
        soc_ok = float(fronius.get('soc') or 0) >= min_buffer_soc
        grid_ok = grid_import_w <= ECO_BATTERY_BUFFER_MAX_GRID_IMPORT_W
        time_ok = elapsed_s <= ECO_BATTERY_BUFFER_S

        decision['battery_buffer'] = {
            'active': False,
            'elapsed_s': elapsed_s,
            'remaining_s': remaining_s,
            'limit_s': ECO_BATTERY_BUFFER_S,
            'shortage_w': int(shortage_w),
            'min_soc': round(min_buffer_soc, 1),
            'grid_import_w': int(grid_import_w),
        }
        if time_ok and soc_ok and grid_ok:
            decision.update({
                'action': 'set',
                'amps': MIN_AMPS,
                'phases': 1,
            })
            decision['battery_buffer']['active'] = True
            decision['reason'] += (
                f' | Akku-Puffer {elapsed_s // 60}:{elapsed_s % 60:02d}/'
                f'{ECO_BATTERY_BUFFER_S // 60}min, fehlend ca. {shortage_w:.0f}W'
            )
        else:
            if not soc_ok:
                decision['reason'] += (
                    f' | Akku-Puffer aus: SOC {fronius.get("soc", 0):.1f}% '
                    f'< Ziel {min_buffer_soc:.1f}%'
                )
            elif not grid_ok:
                decision['reason'] += (
                    f' | Akku-Puffer aus: Netzbezug {grid_import_w:.0f}W '
                    f'> {ECO_BATTERY_BUFFER_MAX_GRID_IMPORT_W}W'
                )
            else:
                decision['reason'] += f' | Akku-Puffer nach {ECO_BATTERY_BUFFER_S // 60}min abgelaufen'
    else:
        s.eco_buffer_started_at = 0

    if (
        START_ON_1PH_UNTIL_CHARGING
        and ev_connected
        and not charging_active
        and mode in ('eco', 'mix')
        and decision.get('action') == 'set'
        and decision.get('phases') == PHASES_3
    ):
        start_surplus_w = max(0, float(decision.get('surplus_w') or 0))
        start_amps = max(MIN_AMPS, amps_for_surplus(start_surplus_w, 1))
        decision['amps'] = min(MAX_AMPS, start_amps)
        decision['phases'] = 1
        decision['reason'] += ' | Startmodus: 1ph bis EV Strom annimmt'

    log.info(
        f"mode={mode} status={conn_status} pv={int(fronius['pv_w'])}W load={int(fronius['load_w'])}W "
        f"wp={wp_w}W surplus={decision['surplus_w']}W soc={fronius['soc']:.1f}% "
        f"-> {decision['action']} amps={decision.get('amps')} ({decision['reason']})"
    )

    # Persistiere Entscheidung fuer das Dashboard
    STATE_DIR.joinpath('controller_state.json').write_text(
        json.dumps({
            'time': now_berlin().isoformat(),
            'fronius': fronius,
            'wattpilot': {
                'connected': wp.get('connected'),
                'status': conn_status,
                'meter_w': wp_w,
                'transaction_id': wp.get('transaction_id'),
                'current_amps': wp.get('current_amps'),
                'current_phases': wp.get('current_phases'),
                'charging_active': charging_active,
                'last_remote_start_status': wp.get('last_remote_start_status'),
                'last_remote_start_at': wp.get('last_remote_start_at'),
                'last_profile_status': wp.get('last_profile_status'),
                'last_profile_at': wp.get('last_profile_at'),
            },
            'mode': mode,
            'decision': decision,
        }, indent=2, default=str)
    )

    if decision['action'] == 'resume':
        if (now_ts - s.last_resume_at) > PAUSE_DEBOUNCE_S:
            log.info('-> resume Wallbox (Operative; connector was Unavailable)')
            await control_call({'cmd': 'resume'})
            s.paused = False
            s.last_resume_at = now_ts
        return

    if not ev_connected:
        return

    if decision['action'] == 'idle':
        return

    if decision['action'] == 'pause':
        if not s.paused and (now_ts - s.last_pause_at) > PAUSE_DEBOUNCE_S:
            log.info('-> pause Wallbox (Inoperative)')
            await control_call({'cmd': 'pause'})
            s.paused = True
            s.last_pause_at = now_ts
        return

    # action == set
    amps = decision['amps']
    phases = decision.get('phases', PHASES_3)

    # ggf. erst aufwecken
    if s.paused and (now_ts - s.last_resume_at) > PAUSE_DEBOUNCE_S:
        log.info('-> resume Wallbox (Operative)')
        await control_call({'cmd': 'resume'})
        s.paused = False
        s.last_resume_at = now_ts
        await asyncio.sleep(2)

    # nur senden wenn Aenderung
    if s.last_amps != amps or s.last_phases != phases:
        log.info(f'-> set_current amps={amps} phases={phases}')
        res = await control_call({'cmd': 'set_current', 'amps': amps, 'phases': phases})
        if res.get('status') == 'ok':
            s.last_amps = amps
            s.last_phases = phases

    remote_start_cooldown_s = (
        REMOTE_START_REJECT_COOLDOWN_S
        if wp.get('last_remote_start_status') == 'Rejected'
        else REMOTE_START_DEBOUNCE_S
    )
    persisted_remote_start_ts = iso_ts_to_epoch(wp.get('last_remote_start_at'))
    last_remote_start_ts = max(s.last_remote_start_at, persisted_remote_start_ts or 0)
    if (
        AUTO_REMOTE_START
        and not wp.get('transaction_id')
        and conn_status in ('Preparing', 'SuspendedEV', 'SuspendedEVSE')
        and (now_ts - last_remote_start_ts) > remote_start_cooldown_s
    ):
        tag = wp.get('last_id_tag')
        log.info(f'-> remote_start transaction (status={conn_status}, idTag={tag or "auto"})')
        res = await control_call({'cmd': 'start', 'idTag': tag})
        s.last_remote_start_at = now_ts
        log.info(f'   remote_start request={res}')


async def main():
    log.info('PV-Controller v1.0 starting')
    s = State()
    while True:
        try:
            await step(s)
        except Exception as e:
            log.exception(f'step failed: {e}')
        await asyncio.sleep(LOOP_SECONDS)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('shutdown')
