"""
Vista-Energy — Plugin-Adapter (Kompatibilitaetsschicht)

Macht die neuen Plugins rueckwaertskompatibel mit dem bestehenden
services['evcc'] und services['tibber'] Interface in app.py.

Statt 20+ Stellen in app.py umzuschreiben, delegieren diese Adapter
an die echten Plugins. Der Kernsystem-Code bleibt stabil.

Verwendung in get_services():
    from services.plugin_adapters import InverterAdapter, TariffAdapter
    registry = PluginRegistry(app_dir)
    services['evcc'] = InverterAdapter(registry.get_inverter())
    services['tibber'] = TariffAdapter(registry.get_tariff())
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class InverterAdapter:
    """Adapter: Macht InverterBase kompatibel mit dem alten EVCCService-Interface.

    Alle bestehenden Aufrufe wie services['evcc'].get_current_state()
    funktionieren weiter, werden aber an das Plugin delegiert.
    """

    def __init__(self, inverter_plugin, ocpp_ws_url: str = 'ws://127.0.0.1:8889'):
        """
        Args:
            inverter_plugin: InverterBase-Instanz (z.B. FroniusGen24Inverter)
            ocpp_ws_url: WebSocket-URL fuer Wallbox/OCPP (WattPilot)
        """
        self._inverter = inverter_plugin
        self._ocpp_ws = ocpp_ws_url

    # ------------------------------------------------------------------
    # Alte EVCCService-API (unveraendert fuer Rueckwaertskompatibilitaet)
    # ------------------------------------------------------------------

    def get_current_state(self) -> Dict:
        """Kompatibel mit EVCCService.get_current_state().

        Delegiert an InverterBase.get_power_flow() und mappt Feldnamen.
        """
        try:
            flow = self._inverter.get_power_flow()
            ocpp = self._read_ocpp_status()
            wp_power = float(ocpp.get('meter_w') or ocpp.get('power_w') or 0)

            return {
                'house_consumption': abs(flow.get('load_w', 0)),
                'pv_production': abs(flow.get('pv_w', 0)),
                'battery_soc': flow.get('battery_soc', 0),
                'battery_charge': flow.get('battery_w', 0),
                'grid_power': flow.get('grid_w', 0),
                'wattpilot_power': abs(wp_power),
                'timestamp': flow.get('timestamp', datetime.now()),
            }
        except Exception as e:
            logger.error(f"InverterAdapter.get_current_state: {e}")
            return self._get_simulated_state()

    def get_battery_state(self) -> Dict:
        """Kompatibel mit EVCCService.get_battery_state()."""
        try:
            flow = self._inverter.get_power_flow()
            info = self._inverter.get_battery_info()
            return {
                'soc': flow.get('battery_soc', 0),
                'power_w': flow.get('battery_w', 0),
                'capacity_kwh': info.get('capacity_kwh', 7.68),
                'usable_kwh': info.get('usable_kwh', 7.0),
            }
        except Exception as e:
            logger.error(f"InverterAdapter.get_battery_state: {e}")
            return {'soc': 0, 'power_w': 0, 'capacity_kwh': 7.68, 'usable_kwh': 7.0}

    def get_pv_state(self) -> Dict:
        """Kompatibel mit EVCCService.get_pv_state()."""
        try:
            flow = self._inverter.get_power_flow()
            return {
                'power_w': abs(flow.get('pv_w', 0)),
                'today_kwh': 0,
                'timestamp': flow.get('timestamp', datetime.now()),
            }
        except Exception as e:
            return {'power_w': 0, 'today_kwh': 0, 'timestamp': datetime.now()}

    def get_grid_state(self) -> Dict:
        """Kompatibel mit EVCCService.get_grid_state()."""
        try:
            flow = self._inverter.get_power_flow()
            return {
                'power_w': flow.get('grid_w', 0),
                'import_kwh_today': 0,
                'export_kwh_today': 0,
            }
        except Exception as e:
            return {'power_w': 0, 'import_kwh_today': 0, 'export_kwh_today': 0}

    def get_wattpilot_status(self) -> Dict:
        """Kompatibel mit EVCCService.get_wattpilot_status()."""
        try:
            data = self._read_ocpp_status()
            connector_status = data.get('connector_status') or data.get('status') or 'Available'
            return {
                'power_w': data.get('meter_w') or data.get('power_w') or 0,
                'status': connector_status,
                'energy_kwh_today': data.get('energy_kwh', 0),
                'connected': bool(data.get('connected')),
                'timestamp': datetime.now(),
            }
        except Exception as e:
            logger.error(f"InverterAdapter.get_wattpilot_status: {e}")
            flow = self._inverter.get_power_flow()
            return {
                'power_w': 0,
                'status': 'Available',
                'energy_kwh_today': 0,
                'timestamp': datetime.now(),
            }

    def set_charge_mode(self, action: str, target_soc: float = 0.8,
                        max_power_w: int = 22000):
        """Kompatibel mit EVCCService.set_charge_mode()."""
        logger.info(f"Charge mode: {action}, target_soc={target_soc}")
        if action == 'start':
            return self.start_charging()
        elif action == 'stop':
            return self.stop_charging()
        return {'status': 'ok', 'action': action}

    def start_charging(self) -> Dict:
        """EV-Laden starten via OCPP WebSocket."""
        try:
            import websocket
            ws = websocket.WebSocket()
            ws.connect(self._ocpp_ws, timeout=5)
            ws.send(json.dumps({'cmd': 'start'}))
            response = ws.recv()
            ws.close()
            return {'status': 'ok', 'action': 'start', 'response': json.loads(response)}
        except Exception as e:
            logger.error(f"Start charging failed: {e}")
            return {'status': 'error', 'action': 'start', 'error': str(e)}

    def stop_charging(self) -> Dict:
        """EV-Laden stoppen via OCPP WebSocket."""
        try:
            import websocket
            ws = websocket.WebSocket()
            ws.connect(self._ocpp_ws, timeout=5)
            ws.send(json.dumps({'cmd': 'stop'}))
            response = ws.recv()
            ws.close()
            return {'status': 'ok', 'action': 'stop', 'response': json.loads(response)}
        except Exception as e:
            logger.error(f"Stop charging failed: {e}")
            return {'status': 'error', 'action': 'stop', 'error': str(e)}

    def set_battery_mode(self, mode: str, target_soc: float = None):
        """Kompatibel mit EVCCService.set_battery_mode()."""
        logger.info(f"Battery mode: {mode}, target_soc={target_soc}")
        return {'status': 'ok', 'mode': mode, 'logged': True}

    def apply_recommendation(self, recommendation: Dict):
        """Kompatibel mit EVCCService.apply_recommendation()."""
        logger.info(f"Optimization recommendation: {recommendation}")
        return {'status': 'applied', 'logged': True}

    # ------------------------------------------------------------------
    # Neues Plugin-Interface (direkt nutzbar)
    # ------------------------------------------------------------------

    @property
    def plugin(self):
        """Zugriff auf das echte Plugin (InverterBase)."""
        return self._inverter

    # ------------------------------------------------------------------
    # Interne Methoden
    # ------------------------------------------------------------------

    def _read_ocpp_status(self) -> Dict:
        """WattPilot-Status via OCPP WebSocket lesen."""
        try:
            import websocket
            ws = websocket.WebSocket()
            ws.connect(self._ocpp_ws, timeout=3)
            ws.send(json.dumps({'cmd': 'status'}))
            response = ws.recv()
            ws.close()
            return json.loads(response)
        except Exception as e:
            logger.debug(f"OCPP status unavailable: {e}")
            return {}

    @staticmethod
    def _get_simulated_state() -> Dict:
        """Fallback-Daten wenn Wechselrichter offline."""
        now = datetime.now()
        hour = now.hour
        if 6 <= hour <= 20:
            pv_factor = max(0.1, 1 - abs(hour - 12) / 8)
            pv_power = int(6000 * pv_factor * 0.85)
        else:
            pv_power = 0
        consumption = 600
        return {
            'house_consumption': consumption,
            'pv_production': pv_power,
            'battery_soc': 0.65,
            'battery_charge': 0,
            'grid_power': consumption - pv_power,
            'wattpilot_power': 0,
            'timestamp': now,
        }


class TariffAdapter:
    """Adapter: Macht TariffBase kompatibel mit dem alten TibberService-Interface.

    Alle bestehenden Aufrufe wie services['tibber'].get_current_prices()
    funktionieren weiter, werden aber an das Plugin delegiert.
    """

    def __init__(self, tariff_plugin):
        """
        Args:
            tariff_plugin: TariffBase-Instanz (z.B. TibberTariff)
        """
        self._tariff = tariff_plugin

    # ------------------------------------------------------------------
    # Alte TibberService-API
    # ------------------------------------------------------------------

    def get_current_prices(self) -> List[Dict]:
        """Kompatibel mit TibberService.get_current_prices().

        Mappt TariffBase.get_prices() auf das alte Format.
        """
        try:
            return self._tariff.get_prices()
        except Exception as e:
            logger.error(f"TariffAdapter.get_current_prices: {e}")
            return []

    def get_current_price(self) -> float:
        """Kompatibel mit TibberService.get_current_price()."""
        try:
            return self._tariff.get_current_price()
        except Exception as e:
            logger.error(f"TariffAdapter.get_current_price: {e}")
            return 0.0

    def get_cheapest_hours(self, count: int = 4, hours_ahead: int = 24) -> List[Dict]:
        """Kompatibel mit TibberService.get_cheapest_hours()."""
        try:
            return self._tariff.get_cheapest_hours(count, hours_ahead)
        except Exception as e:
            logger.error(f"TariffAdapter.get_cheapest_hours: {e}")
            return []

    def get_price_level(self) -> str:
        """Kompatibel mit TibberService.get_price_level()."""
        try:
            return self._tariff.get_price_level()
        except Exception as e:
            return 'normal'

    def query(self, query_str: str, variables: dict = None) -> dict:
        """Kompatibel mit TibberService.query() — Tibber-spezifisch.

        Nur Tibber-Plugin unterstuetzt direkte GraphQL-Queries.
        Andere Tarif-Plugins geben leeres Ergebnis zurueck.
        """
        if hasattr(self._tariff, '_graphql_query'):
            return self._tariff._graphql_query(query_str, variables)
        logger.debug("query() nur fuer Tibber-Plugin verfuegbar")
        return {"data": None}

    def get_home_id(self) -> Optional[str]:
        """Kompatibel mit TibberService.get_home_id() — Tibber-spezifisch."""
        if hasattr(self._tariff, 'get_home_id'):
            return self._tariff.get_home_id()
        return None

    # ------------------------------------------------------------------
    # Neues Plugin-Interface
    # ------------------------------------------------------------------

    @property
    def plugin(self):
        """Zugriff auf das echte Plugin (TariffBase)."""
        return self._tariff




class WallboxAdapter:
    """Adapter: Macht WallboxBase kompatibel mit bestehenden Aufrufen.

    Bietet sowohl das alte Interface (get_wattpilot_status etc.)
    als auch das neue Plugin-Interface.
    """

    def __init__(self, wallbox_plugin):
        """
        Args:
            wallbox_plugin: WallboxBase-Instanz (z.B. FroniusWattPilotWallbox)
        """
        self._wallbox = wallbox_plugin

    # ------------------------------------------------------------------
    # Status-Abfragen
    # ------------------------------------------------------------------

    def get_status(self):
        """Wallbox-Status (Plugin-Interface)."""
        try:
            return self._wallbox.get_status()
        except Exception as e:
            logger.error(f'WallboxAdapter.get_status: {e}')
            return {
                'connected': False, 'charging': False,
                'power_w': 0, 'energy_kwh': 0,
                'current_a': 0, 'max_current_a': 32,
                'phases': 3, 'timestamp': datetime.now(),
            }

    def get_wattpilot_status(self):
        """Kompatibel mit altem EVCCService.get_wattpilot_status()."""
        try:
            status = self._wallbox.get_status()
            return {
                'power_w': status.get('power_w', 0),
                'status': status.get('connector_status', 'Available'),
                'energy_kwh_today': status.get('energy_kwh', 0),
                'connected': status.get('connected', False),
                'charging': status.get('charging', False),
                'current_a': status.get('current_a', 0),
                'phases': status.get('phases', 3),
                'timestamp': datetime.now(),
            }
        except Exception as e:
            logger.error(f'WallboxAdapter.get_wattpilot_status: {e}')
            return {
                'power_w': 0, 'status': 'Available',
                'energy_kwh_today': 0, 'connected': False,
                'timestamp': datetime.now(),
            }

    def get_info(self):
        """Wallbox-Info (Hersteller, Modell etc.)."""
        try:
            return self._wallbox.get_info()
        except Exception as e:
            logger.error(f'WallboxAdapter.get_info: {e}')
            return {'manufacturer': 'Unknown', 'model': 'Unknown', 'connected': False}

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------

    def set_charge_current(self, amps):
        """Ladestrom setzen."""
        try:
            return self._wallbox.set_charge_current(amps)
        except Exception as e:
            logger.error(f'WallboxAdapter.set_charge_current({amps}A): {e}')
            return False

    def start_charging(self):
        """Laden starten."""
        try:
            return self._wallbox.start_charging()
        except Exception as e:
            logger.error(f'WallboxAdapter.start_charging: {e}')
            return False

    def stop_charging(self):
        """Laden stoppen."""
        try:
            return self._wallbox.stop_charging()
        except Exception as e:
            logger.error(f'WallboxAdapter.stop_charging: {e}')
            return False

    def set_phases(self, phases):
        """Phasen umschalten (1/3)."""
        try:
            return self._wallbox.set_phases(phases)
        except Exception as e:
            logger.error(f'WallboxAdapter.set_phases({phases}): {e}')
            return False

    # ------------------------------------------------------------------
    # Plugin-Zugriff
    # ------------------------------------------------------------------

    @property
    def plugin(self):
        """Zugriff auf das echte Plugin (WallboxBase)."""
        return self._wallbox

    def is_connected(self):
        """Wallbox erreichbar?"""
        try:
            return self._wallbox.is_connected()
        except Exception:
            return False
