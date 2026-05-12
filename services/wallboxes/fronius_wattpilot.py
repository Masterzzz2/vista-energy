"""
Vista-Energy — Fronius WattPilot Plugin

Steuerung via OCPP 1.6 WebSocket (nexus_ocpp_server.py).
Unterstuetzt: WattPilot Go 11, WattPilot Home 11/22.

Die WattPilot wird ueber einen lokalen OCPP-Server gesteuert,
der als separater Prozess laeuft und einen Control-WebSocket
auf Port 8889 bereitstellt.

Konfiguration (config.yaml):
  wallbox:
    type: fronius_wattpilot
    ocpp_ws: ws://127.0.0.1:8889     # optional, default
"""

import json
import logging
from datetime import datetime
from typing import Dict

from services.wallboxes.base import WallboxBase

logger = logging.getLogger(__name__)


class FroniusWattPilotWallbox(WallboxBase):
    """Fronius WattPilot via OCPP WebSocket."""

    DEFAULT_WS = 'ws://127.0.0.1:8889'

    def __init__(self, ocpp_ws: str = None, **kwargs):
        self.ocpp_ws = ocpp_ws or self.DEFAULT_WS

    # ==================================================================
    # WallboxBase — Pflicht-Methoden
    # ==================================================================

    def get_status(self) -> Dict:
        """Aktueller WattPilot-Status via OCPP."""
        data = self._ocpp_command('status')
        connector = data.get('connector_status') or data.get('status') or 'Available'
        power = float(data.get('meter_w') or data.get('power_w') or 0)
        is_charging = power > 100 or connector == 'Charging'

        return {
            'connected': connector not in ('Available', 'Unavailable', ''),
            'charging': is_charging,
            'power_w': abs(power),
            'energy_kwh': float(data.get('energy_kwh') or data.get('meter_kwh') or 0),
            'current_a': float(data.get('current_a') or 0),
            'max_current_a': float(data.get('max_current_a') or 32),
            'phases': int(data.get('phases') or 3),
            'timestamp': datetime.now(),
            'connector_status': connector,
            'rfid': data.get('rfid_tag') or data.get('id_tag'),
            'vehicle': data.get('vehicle_alias'),
        }

    def set_charge_current(self, amps: float) -> bool:
        """Ladestrom setzen via OCPP."""
        if amps <= 0:
            return self.stop_charging()
        try:
            resp = self._ocpp_command('set_current', {'amps': amps})
            return resp.get('status') == 'ok' or resp.get('accepted', False)
        except Exception as e:
            logger.error(f"WattPilot set_charge_current({amps}A): {e}")
            return False

    # ==================================================================
    # Optionale Methoden
    # ==================================================================

    def start_charging(self) -> bool:
        """RemoteStartTransaction via OCPP."""
        try:
            resp = self._ocpp_command('start')
            return resp.get('status') in ('ok', 'Accepted')
        except Exception as e:
            logger.error(f"WattPilot start: {e}")
            return False

    def stop_charging(self) -> bool:
        """RemoteStopTransaction via OCPP."""
        try:
            resp = self._ocpp_command('stop')
            return resp.get('status') in ('ok', 'Accepted')
        except Exception as e:
            logger.error(f"WattPilot stop: {e}")
            return False

    def get_info(self) -> Dict:
        """WattPilot-Info."""
        data = self._ocpp_command('status')
        return {
            'manufacturer': 'Fronius',
            'model': 'WattPilot Go 11',
            'serial': data.get('serial') or data.get('charge_point_id') or '',
            'firmware': data.get('firmware') or '',
            'connected': bool(data),
        }

    def is_connected(self) -> bool:
        data = self._ocpp_command('status')
        return bool(data) and 'error' not in data

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'fronius_wattpilot'

    @staticmethod
    def plugin_name() -> str:
        return 'Fronius WattPilot (OCPP)'

    # ==================================================================
    # Interne Methoden
    # ==================================================================

    def _ocpp_command(self, cmd: str, params: dict = None) -> dict:
        """OCPP WebSocket Kommando senden."""
        try:
            import websocket
            ws = websocket.WebSocket()
            ws.connect(self.ocpp_ws, timeout=5)
            payload = {'cmd': cmd}
            if params:
                payload.update(params)
            ws.send(json.dumps(payload))
            response = ws.recv()
            ws.close()
            return json.loads(response)
        except ImportError:
            logger.debug("websocket-client nicht installiert")
            return {}
        except Exception as e:
            logger.debug(f"OCPP {cmd} fehlgeschlagen: {e}")
            return {}
