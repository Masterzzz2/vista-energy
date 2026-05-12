#!/usr/bin/env python3
"""
NEXUS OCPP Server v6.0
======================
- Reines OCPP 1.6 für Fronius Wattpilot Go 11
- Persistenter Zustand in /home/werner/.nexus/state.json
- SetChargingProfile zur dynamischen Strom-Steuerung (PV-Überschuss)
- ChangeAvailability fuer Pause
- Robuste Recovery bei Reconnect
- WebSocket-Control auf Port 8889 fuer pv_controller.py und app.py

Befehle ueber ws://localhost:8889 (JSON-Frame):
  {"cmd":"status"}                         -> aktueller Zustand
  {"cmd":"start"}                          -> RemoteStartTransaction
  {"cmd":"stop"}                           -> RemoteStopTransaction
  {"cmd":"set_current","amps":<int>,"phases":<1|3>}  -> SetChargingProfile
  {"cmd":"pause"}                          -> ChangeAvailability Inoperative
  {"cmd":"resume"}                         -> ChangeAvailability Operative
  {"cmd":"trigger","msg":"StatusNotification|MeterValues"}
  {"cmd":"reset","type":"Soft|Hard"}
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path

import websockets

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
STATE_DIR = Path('/home/werner/.nexus')
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / 'state.json'
LOG_FILE = STATE_DIR / 'ocpp.log'
DB_FILE = '/home/werner/energy-optimizer/energy_optimizer.db'

OCPP_PORT = 8888           # Wattpilot connectet hier her (ws://192.168.1.169:8888/<stationId>)
CONTROL_PORT = 8889        # lokales Steuer-Interface
HEARTBEAT_INTERVAL = 60    # Sekunden, wird Wattpilot mitgeteilt
METER_INTERVAL = 30        # Sekunden, wird Wattpilot mitgeteilt
CHARGING_PROFILE_ID = 1    # interne ID
DEFAULT_PHASES = 3
DEFAULT_VOLTAGE = 230
MIN_AMPS = 6               # OCPP / Wattpilot: 6 A
MAX_AMPS = 16              # 16 A * 3 Phasen = 11 kW (Wattpilot Go 11)

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
log = logging.getLogger('ocpp')

# ---------------------------------------------------------------------------
# Persistenter State
# ---------------------------------------------------------------------------
DEFAULT_STATE = {
    'transaction_id': None,
    'last_meter_wh': 0,
    'connector_status': 'Unknown',
    'last_status_at': None,
    'last_seen_at': None,
    'current_amps': MIN_AMPS,
    'current_phases': DEFAULT_PHASES,
    'profile_purpose': None,        # was zuletzt gesetzt wurde
    'mode_hint': 'standard',        # informativ - eigentliche Mode liegt bei pv_controller
    'station_id': None,
    'vendor': None,
    'model': None,
    'firmware': None,
    'pending_profile': None,        # wenn gesetzt, beim naechsten Connect senden
    'last_id_tag': None,
    'last_id_tag_at': None,
    'last_remote_start_status': None,
    'last_remote_start_at': None,
    'last_profile_status': None,
    'last_profile_at': None,
    'last_availability_status': None,
    'last_availability_at': None,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                # mit Defaults mergen
                for k, v in DEFAULT_STATE.items():
                    data.setdefault(k, v)
                return data
        except Exception as e:
            log.warning(f'state.json korrupt ({e}) - reset')
    return dict(DEFAULT_STATE)


def save_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(STATE, f, indent=2, default=str)
    except Exception as e:
        log.warning(f'save_state failed: {e}')


STATE = load_state()


MANUAL_ID_TAG = 'manual'
MISSING_ID_TAGS = ('', 'unknown', 'manual', None)
AUTHORIZE_TAG_MAX_AGE_S = 10 * 60


def _clean_id_tag(tag):
    tag = str(tag or '').strip()
    return tag or None


def _remember_id_tag(tag):
    tag = _clean_id_tag(tag)
    if not tag or tag in ('unknown', MANUAL_ID_TAG):
        return
    STATE['last_id_tag'] = tag
    STATE['last_id_tag_at'] = now_iso()


def _recent_authorized_id_tag():
    tag = _clean_id_tag(STATE.get('last_id_tag'))
    if not tag:
        return None
    try:
        raw = STATE.get('last_id_tag_at')
        if not raw:
            return tag
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if age_s <= AUTHORIZE_TAG_MAX_AGE_S:
            return tag
    except Exception:
        return tag
    return None


def resolve_start_id_tag(payload):
    """Bestimme die RFID-ID fuer StartTransaction.

    Manche Wallboxen schicken Authorize mit idTag, lassen das idTag aber im
    anschliessenden StartTransaction weg. In dem Fall verwenden wir den kurz
    zuvor gemerkten Chip. Nur wenn wirklich kein Chip bekannt ist, markieren
    wir die Session als manuell.
    """
    return _clean_id_tag(payload.get('idTag')) or _recent_authorized_id_tag() or MANUAL_ID_TAG


# ---------------------------------------------------------------------------
# Charge-Session DB (RFID Tracking)
# ---------------------------------------------------------------------------
def db_conn():
    c = sqlite3.connect(DB_FILE, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    return c


def db_init():
    try:
        c = db_conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS charge_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at      TEXT NOT NULL,
              stopped_at      TEXT,
              id_tag          TEXT,
              transaction_id  INTEGER,
              meter_start_wh  INTEGER,
              meter_stop_wh   INTEGER,
              energy_kwh      REAL,
              duration_min    INTEGER,
              connector_id    INTEGER,
              stop_reason     TEXT
            )
        """)
        c.execute('CREATE INDEX IF NOT EXISTS idx_cs_started ON charge_sessions(started_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_cs_idtag   ON charge_sessions(id_tag)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_cs_tx      ON charge_sessions(transaction_id)')
        c.commit()
        c.close()
    except Exception as e:
        log.warning(f'db_init: {e}')


def db_log_start(transaction_id, id_tag, meter_start_wh, connector_id):
    try:
        c = db_conn()
        c.execute("""
            INSERT INTO charge_sessions
              (started_at, id_tag, transaction_id, meter_start_wh, connector_id)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), id_tag, transaction_id,
              meter_start_wh, connector_id))
        c.commit()
        c.close()
        log.info(f'   DB session START tx={transaction_id} idTag={id_tag} meter={meter_start_wh}Wh')
    except Exception as e:
        log.warning(f'db_log_start: {e}')


def db_log_stop(transaction_id, meter_stop_wh, reason=None, id_tag=None):
    try:
        c = db_conn()
        # Letzte offene Session zur transaction_id
        cur = c.execute("""
            SELECT id, started_at, meter_start_wh, id_tag
            FROM charge_sessions
            WHERE transaction_id = ? AND stopped_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (transaction_id,))
        row = cur.fetchone()
        if not row:
            # Manche Wallboxen senden keinen StartTransaction nach Power-Loss.
            # Falls keine offene Session existiert, neu anlegen mit Schaetzung.
            log.warning(f'db_log_stop: keine offene Session fuer tx={transaction_id}, lege Stub an')
            c.execute("""
                INSERT INTO charge_sessions
                  (started_at, stopped_at, id_tag, transaction_id, meter_stop_wh)
                VALUES (?, ?, ?, ?, ?)
            """, (datetime.now(timezone.utc).isoformat(),
                  datetime.now(timezone.utc).isoformat(),
                  id_tag, transaction_id, meter_stop_wh))
            c.commit(); c.close(); return
        sid, started_at, meter_start, prev_tag = row
        stopped_at = datetime.now(timezone.utc).isoformat()
        try:
            d_started = datetime.fromisoformat(started_at.replace('Z','+00:00'))
            d_stopped = datetime.fromisoformat(stopped_at.replace('Z','+00:00'))
            duration_min = int((d_stopped - d_started).total_seconds() // 60)
        except Exception:
            duration_min = None
        energy_kwh = None
        if meter_stop_wh is not None and meter_start is not None:
            try:
                energy_kwh = round((int(meter_stop_wh) - int(meter_start)) / 1000.0, 3)
            except Exception:
                pass
        # Falls idTag vorher fehlte/unknown/manual war aber jetzt vorhanden ist,
        # nachtragen. COALESCE alleine ersetzt "unknown" nicht.
        new_tag = prev_tag
        if prev_tag in MISSING_ID_TAGS and id_tag not in MISSING_ID_TAGS:
            new_tag = id_tag
        c.execute("""
            UPDATE charge_sessions SET
              stopped_at = ?, meter_stop_wh = ?, energy_kwh = ?,
              duration_min = ?, stop_reason = ?, id_tag = ?
            WHERE id = ?
        """, (stopped_at, meter_stop_wh, energy_kwh, duration_min, reason, new_tag, sid))
        c.commit(); c.close()
        log.info(f'   DB session STOP id={sid} tx={transaction_id} energy={energy_kwh}kWh dur={duration_min}min')
    except Exception as e:
        log.warning(f'db_log_stop: {e}')


db_init()

# Live-Zustand (nicht persistent)
LIVE = {
    'connected': False,
    'wattpilot_ws': None,
    'pending': {},          # msg_id -> action
    'meter_w': 0,
    'last_meter_at': None,
}


# ---------------------------------------------------------------------------
# OCPP Helfer
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def msg_id() -> str:
    return str(int(time.time() * 1000))


async def send(ws, payload):
    """Sende OCPP-Nachricht und logge."""
    try:
        await ws.send(json.dumps(payload))
        if payload[0] == 2:
            log.info(f'-> {payload[2]} id={payload[1]}')
        else:
            log.debug(f'-> Ack id={payload[1]}')
    except Exception as e:
        log.warning(f'send failed: {e}')


def build_charging_profile(amps: int, phases: int = 3) -> dict:
    """Baue ein OCPP 1.6 ChargingProfile fuer SetChargingProfile."""
    amps = max(MIN_AMPS, min(MAX_AMPS, int(amps)))
    return {
        'connectorId': 1,
        'csChargingProfiles': {
            'chargingProfileId': CHARGING_PROFILE_ID,
            'stackLevel': 0,
            'chargingProfilePurpose': 'TxDefaultProfile',
            'chargingProfileKind': 'Relative',
            'chargingSchedule': {
                'chargingRateUnit': 'A',
                'chargingSchedulePeriod': [
                    {'startPeriod': 0, 'limit': float(amps), 'numberPhases': phases}
                ]
            }
        }
    }


# ---------------------------------------------------------------------------
# OCPP Server (Wattpilot connectet hier hinein)
# ---------------------------------------------------------------------------
async def ocpp_handler(ws, path=None):
    """Behandelt eingehende Wattpilot OCPP-Verbindung."""
    # Path enthält die Station-ID, z.B. /91091581
    # In neueren websockets-Versionen ist `path` optional - dann via ws.path lesen
    if path is None:
        path = getattr(ws, 'path', '/')
    station_id = path.strip('/').split('/')[-1] if path else 'unknown'
    LIVE['connected'] = True
    LIVE['wattpilot_ws'] = ws
    STATE['station_id'] = station_id
    log.info(f'Wattpilot CONNECTED  station={station_id}  protocol={ws.subprotocol}')

    # Wenn bei letzter Trennung ein Profile pending war -> jetzt nachholen
    pending = STATE.get('pending_profile')
    if pending:
        try:
            await send(ws, [2, msg_id(), 'SetChargingProfile', build_charging_profile(pending['amps'], pending['phases'])])
            STATE['pending_profile'] = None
            save_state()
        except Exception as e:
            log.warning(f'pending profile resend failed: {e}')

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f'invalid JSON: {raw!r}')
                continue
            await handle_message(ws, data)
    except websockets.ConnectionClosed as e:
        log.info(f'Wattpilot DISCONNECTED  code={e.code} reason={e.reason}')
    except Exception as e:
        log.exception(f'ocpp_handler error: {e}')
    finally:
        LIVE['connected'] = False
        LIVE['wattpilot_ws'] = None


async def handle_message(ws, data):
    """Verarbeite eine OCPP-Nachricht."""
    if not isinstance(data, list) or len(data) < 3:
        log.warning(f'malformed: {data}')
        return

    msg_type = data[0]
    mid = data[1]

    if msg_type == 2:
        action = data[2]
        payload = data[3] if len(data) > 3 else {}
        await handle_call(ws, mid, action, payload)
    elif msg_type == 3:
        payload = data[2] if len(data) > 2 else {}
        action = LIVE['pending'].pop(mid, 'unknown')
        await handle_call_result(action, payload)
    elif msg_type == 4:
        log.warning(f'CallError id={mid}: {data[2:]}')
        LIVE['pending'].pop(mid, None)


async def handle_call(ws, mid, action, payload):
    """Wattpilot -> uns."""
    STATE['last_seen_at'] = now_iso()
    log.info(f'<- {action} id={mid}  {payload if action != "Heartbeat" else ""}')

    if action == 'BootNotification':
        STATE['vendor'] = payload.get('chargePointVendor')
        STATE['model'] = payload.get('chargePointModel')
        STATE['firmware'] = payload.get('firmwareVersion')
        save_state()
        await send(ws, [3, mid, {
            'status': 'Accepted',
            'currentTime': now_iso(),
            'interval': HEARTBEAT_INTERVAL,
        }])
        # Direkt nach Boot: aktuellen Status erfragen
        asyncio.create_task(_after_boot(ws))

    elif action == 'Heartbeat':
        await send(ws, [3, mid, {'currentTime': now_iso()}])

    elif action == 'StatusNotification':
        status = payload.get('status', 'Unknown')
        STATE['connector_status'] = status
        STATE['last_status_at'] = now_iso()
        log.info(f'   STATUS connector={payload.get("connectorId")} -> {status}  errorCode={payload.get("errorCode")}')
        save_state()
        await send(ws, [3, mid, {}])

    elif action == 'StartTransaction':
        tid = int(time.time())
        STATE['transaction_id'] = tid
        meter_start = payload.get('meterStart', 0) or 0
        STATE['last_meter_wh'] = meter_start
        id_tag = resolve_start_id_tag(payload)
        connector_id = payload.get('connectorId') or 1
        _remember_id_tag(id_tag)
        save_state()
        log.info(f'   START transaction={tid}  idTag={id_tag}  meterStart={meter_start} Wh')
        db_log_start(tid, id_tag, meter_start, connector_id)
        await send(ws, [3, mid, {
            'transactionId': tid,
            'idTagInfo': {'status': 'Accepted'},
        }])

    elif action == 'StopTransaction':
        tid_in = payload.get('transactionId')
        meter_stop = payload.get('meterStop')
        reason = payload.get('reason')
        id_tag = _clean_id_tag(payload.get('idTag')) or _recent_authorized_id_tag()
        _remember_id_tag(id_tag)
        log.info(f'   STOP  transaction={tid_in}  meterStop={meter_stop} Wh reason={reason}')
        db_log_stop(tid_in, meter_stop, reason=reason, id_tag=id_tag)
        STATE['transaction_id'] = None
        save_state()
        await send(ws, [3, mid, {'idTagInfo': {'status': 'Accepted'}}])

    elif action == 'Authorize':
        # idTag kommt hier - wir merken ihn fuer den naechsten StartTransaction
        _remember_id_tag(payload.get('idTag'))
        save_state()
        log.info(f'   AUTH idTag={STATE["last_id_tag"]}')
        await send(ws, [3, mid, {'idTagInfo': {'status': 'Accepted'}}])

    elif action == 'MeterValues':
        try:
            samples = payload.get('meterValue', [])
            for s in samples:
                for v in s.get('sampledValue', []):
                    measurand = v.get('measurand', 'Energy.Active.Import.Register')
                    val = v.get('value')
                    unit = v.get('unit', 'Wh')
                    if measurand in ('Power.Active.Import', 'Power.Active'):
                        # Power kommt manchmal als kW
                        try:
                            p = float(val)
                            if unit and unit.lower().startswith('k'):
                                p *= 1000
                            LIVE['meter_w'] = int(p)
                            LIVE['last_meter_at'] = now_iso()
                        except Exception:
                            pass
                    elif measurand in ('Energy.Active.Import.Register',):
                        try:
                            STATE['last_meter_wh'] = int(float(val))
                        except Exception:
                            pass
            log.info(f'   METER  {LIVE["meter_w"]} W  total={STATE["last_meter_wh"]} Wh')
            save_state()
        except Exception as e:
            log.warning(f'MeterValues parse: {e}')
        await send(ws, [3, mid, {}])

    elif action == 'DataTransfer':
        await send(ws, [3, mid, {'status': 'Accepted'}])

    elif action == 'TransactionEvent':
        # OCPP 2.0.1 - falls Wattpilot doch 2.x spricht
        tid = payload.get('transactionInfo', {}).get('transactionId')
        if tid:
            STATE['transaction_id'] = tid
            save_state()
        await send(ws, [3, mid, {}])

    else:
        log.info(f'   unhandled action={action}, generic ack')
        await send(ws, [3, mid, {}])


async def handle_call_result(action, payload):
    """Antwort der Wattpilot auf unsere Anfragen."""
    log.info(f'<- RESULT for {action}: {payload}')
    if action == 'SetChargingProfile':
        STATE['last_profile_status'] = payload.get('status')
        STATE['last_profile_at'] = now_iso()
        if payload.get('status') == 'Accepted':
            log.info('   SetChargingProfile ACCEPTED')
        else:
            log.warning(f'   SetChargingProfile REJECTED: {payload}')
        save_state()
    elif action == 'RemoteStartTransaction':
        STATE['last_remote_start_status'] = payload.get('status')
        STATE['last_remote_start_at'] = now_iso()
        if payload.get('status') == 'Accepted':
            log.info('   RemoteStartTransaction ACCEPTED')
        else:
            log.warning(f'   RemoteStartTransaction REJECTED: {payload}')
        save_state()
    elif action == 'ChangeAvailability':
        STATE['last_availability_status'] = payload.get('status')
        STATE['last_availability_at'] = now_iso()
        save_state()


async def _after_boot(ws):
    """Nach Boot kurz warten und dann Status + Meter triggern."""
    await asyncio.sleep(2)
    if LIVE['wattpilot_ws'] is not ws:
        return
    for trig in ('StatusNotification', 'MeterValues'):
        m = msg_id()
        LIVE['pending'][m] = 'TriggerMessage'
        await send(ws, [2, m, 'TriggerMessage', {'requestedMessage': trig, 'connectorId': 1}])
    # Wenn TxDefaultProfile gesetzt war, neu setzen damit Wattpilot nicht zurueckfaellt
    if STATE.get('current_amps'):
        m = msg_id()
        LIVE['pending'][m] = 'SetChargingProfile'
        await send(ws, [2, m, 'SetChargingProfile',
                        build_charging_profile(STATE['current_amps'], STATE.get('current_phases', 3))])


# ---------------------------------------------------------------------------
# Control Server (Port 8889) fuer pv_controller / app.py
# ---------------------------------------------------------------------------
async def _send_to_wattpilot(action, payload):
    """Sende OCPP Call an Wattpilot, falls verbunden."""
    ws = LIVE['wattpilot_ws']
    if ws is None:
        return {'status': 'error', 'reason': 'Wattpilot not connected'}
    m = msg_id()
    LIVE['pending'][m] = action
    await send(ws, [2, m, action, payload])
    return {'status': 'ok', 'action': action, 'request_id': m}


async def control_handler(ws, path=None):
    log.info('Control client connected')
    try:
        async for raw in ws:
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({'status': 'error', 'reason': 'invalid JSON'}))
                continue

            cmd = req.get('cmd')
            try:
                if cmd == 'status':
                    out = {
                        'connected': LIVE['connected'],
                        'station_id': STATE.get('station_id'),
                        'vendor': STATE.get('vendor'),
                        'model': STATE.get('model'),
                        'firmware': STATE.get('firmware'),
                        'connector_status': STATE.get('connector_status'),
                        'transaction_id': STATE.get('transaction_id'),
                        'meter_w': LIVE['meter_w'],
                        'last_meter_at': LIVE['last_meter_at'],
                        'last_status_at': STATE.get('last_status_at'),
                        'last_seen_at': STATE.get('last_seen_at'),
                        'current_amps': STATE.get('current_amps'),
                        'current_phases': STATE.get('current_phases'),
                        'mode_hint': STATE.get('mode_hint'),
                        'last_id_tag': STATE.get('last_id_tag'),
                        'last_id_tag_at': STATE.get('last_id_tag_at'),
                        'last_remote_start_status': STATE.get('last_remote_start_status'),
                        'last_remote_start_at': STATE.get('last_remote_start_at'),
                        'last_profile_status': STATE.get('last_profile_status'),
                        'last_profile_at': STATE.get('last_profile_at'),
                        'last_availability_status': STATE.get('last_availability_status'),
                        'last_availability_at': STATE.get('last_availability_at'),
                    }
                    await ws.send(json.dumps(out))

                elif cmd == 'start':
                    id_tag = (
                        _clean_id_tag(req.get('idTag') or req.get('id_tag'))
                        or _recent_authorized_id_tag()
                        or 'NEXUS'
                    )
                    _remember_id_tag(id_tag)
                    out = await _send_to_wattpilot('RemoteStartTransaction',
                                                  {'connectorId': 1, 'idTag': id_tag})
                    out.update({'idTag': id_tag})
                    await ws.send(json.dumps(out))

                elif cmd == 'stop':
                    tid = STATE.get('transaction_id') or req.get('transaction_id') or 1
                    out = await _send_to_wattpilot('RemoteStopTransaction', {'transactionId': int(tid)})
                    await ws.send(json.dumps(out))

                elif cmd == 'set_current':
                    amps = int(req.get('amps', MIN_AMPS))
                    phases = int(req.get('phases', STATE.get('current_phases', DEFAULT_PHASES)))
                    if phases not in (1, 3):
                        phases = 3
                    amps_clamped = max(MIN_AMPS, min(MAX_AMPS, amps))
                    profile = build_charging_profile(amps_clamped, phases)
                    out = await _send_to_wattpilot('SetChargingProfile', profile)
                    STATE['current_amps'] = amps_clamped
                    STATE['current_phases'] = phases
                    STATE['pending_profile'] = {'amps': amps_clamped, 'phases': phases}
                    save_state()
                    out.update({'amps': amps_clamped, 'phases': phases})
                    await ws.send(json.dumps(out))

                elif cmd == 'pause':
                    out = await _send_to_wattpilot('ChangeAvailability',
                                                  {'connectorId': 1, 'type': 'Inoperative'})
                    await ws.send(json.dumps(out))

                elif cmd == 'resume':
                    out = await _send_to_wattpilot('ChangeAvailability',
                                                  {'connectorId': 1, 'type': 'Operative'})
                    await ws.send(json.dumps(out))

                elif cmd == 'trigger':
                    which = req.get('msg', 'StatusNotification')
                    out = await _send_to_wattpilot('TriggerMessage',
                                                  {'requestedMessage': which, 'connectorId': 1})
                    await ws.send(json.dumps(out))

                elif cmd == 'reset':
                    rt = req.get('type', 'Soft')
                    out = await _send_to_wattpilot('Reset', {'type': rt})
                    await ws.send(json.dumps(out))

                elif cmd == 'mode_hint':
                    STATE['mode_hint'] = str(req.get('mode', 'standard'))
                    save_state()
                    await ws.send(json.dumps({'status': 'ok', 'mode_hint': STATE['mode_hint']}))

                elif cmd == 'clear_profile':
                    out = await _send_to_wattpilot('ClearChargingProfile',
                                                  {'id': CHARGING_PROFILE_ID})
                    STATE['pending_profile'] = None
                    save_state()
                    await ws.send(json.dumps(out))

                else:
                    await ws.send(json.dumps({'status': 'error', 'reason': f'unknown cmd: {cmd}'}))

            except Exception as e:
                log.exception(f'control cmd {cmd} error: {e}')
                await ws.send(json.dumps({'status': 'error', 'reason': str(e)}))

    except websockets.ConnectionClosed:
        pass
    log.debug('Control client disconnected')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    log.info('=' * 60)
    log.info('NEXUS OCPP Server v6.0 starting')
    log.info(f'OCPP    -> ws://0.0.0.0:{OCPP_PORT}/<stationId>  (subprotocol ocpp1.6)')
    log.info(f'Control -> ws://0.0.0.0:{CONTROL_PORT}')
    log.info(f'State   -> {STATE_FILE}')

    ocpp_srv = await websockets.serve(
        ocpp_handler, '0.0.0.0', OCPP_PORT,
        subprotocols=['ocpp1.6'],
        ping_interval=20, ping_timeout=20,
    )
    ctrl_srv = await websockets.serve(
        control_handler, '0.0.0.0', CONTROL_PORT,
        ping_interval=20, ping_timeout=20,
    )

    try:
        await asyncio.Future()
    finally:
        ocpp_srv.close()
        ctrl_srv.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('shutdown')
