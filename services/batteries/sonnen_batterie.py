"""
Vista-Energy — sonnenBatterie Plugin

Kommunikation via lokale HTTP API (JSON).
Unterstuetzt: sonnenBatterie eco/pro 8, 10, 10 performance.

Die sonnenBatterie hat eine lokale REST-API.
API-Key wird im Dashboard unter "Software-Integration" generiert.

API-Endpoints:
  - GET /api/v2/status              → Leistung, SOC, Status
  - GET /api/v2/battery             → Batterie-Details
  - GET /api/v2/inverter            → Wechselrichter-Daten
  - GET /api/v2/powermeter           → Zaehler-Daten
  - PUT /api/v2/configurations       → Konfiguration aendern
  - GET /api/v2/latestdata           → Letzte Messwerte

  Legacy (v1):
  - GET /api/v1/status               → Status (aeltere Firmware)

Konfiguration (config.yaml):
  battery:
    type: sonnen_batterie
    ip: 192.168.1.160
    api_key: YOUR_API_KEY        # Aus sonnenBatterie Dashboard
    capacity_kwh: 10.0
"""

import logging
from datetime import datetime
from typing import Dict, Optional

import requests

from services.batteries.base import BatteryBase

logger = logging.getLogger(__name__)


class SonnenBatterieBattery(BatteryBase):
    """sonnenBatterie via lokale REST-API."""

    def __init__(self, ip: str = '192.168.1.160',
                 api_key: str = '',
                 capacity_kwh: float = 10.0,
                 timeout: int = 10,
                 **kwargs):
        self.ip = ip
        self.base_url = f"http://{ip}"
        self.api_key = api_key
        self.capacity_kwh = capacity_kwh
        self.timeout = timeout

    def get_status(self) -> Dict:
        data = self._api_get('/api/v2/status')
        if not data:
            # v1 Fallback
            data = self._api_get('/api/v1/status')
        if not data:
            return self._fallback()

        soc = data.get('USOC', data.get('UserSOC', 0)) / 100.0
        # Leistung: Pac_total_W (+ Entladen, - Laden bei sonnen)
        pac = data.get('Pac_total_W', 0)
        # Invertieren: Vista-Energy Konvention + = Laden, - = Entladen
        power_w = -pac

        production_w = data.get('Production_W', 0)
        consumption_w = data.get('Consumption_W', 0)
        grid_feed_w = data.get('GridFeedIn_W', 0)

        # Temperatur (falls verfuegbar)
        bat_data = self._api_get('/api/v2/battery')
        temp = 0.0
        voltage = 0.0
        cycles = 0
        if bat_data:
            temp = bat_data.get('temperature', 0)
            voltage = bat_data.get('voltage', 0)
            cycles = bat_data.get('cyclecount', 0)

        return {
            'soc': min(soc, 1.0),
            'power_w': power_w,
            'voltage_v': voltage,
            'temperature_c': temp,
            'capacity_kwh': self.capacity_kwh,
            'usable_kwh': self.capacity_kwh * 0.9,
            'cycles': cycles,
            'health_pct': data.get('SOH', 100),
            'timestamp': datetime.now(),
            'production_w': production_w,
            'consumption_w': consumption_w,
            'grid_feed_w': grid_feed_w,
            'system_status': data.get('SystemStatus', ''),
            'operating_mode': data.get('OperatingMode', ''),
        }

    def get_info(self) -> Dict:
        data = self._api_get('/api/v2/battery')
        if not data:
            return {
                'manufacturer': 'sonnen',
                'model': 'sonnenBatterie',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

        # System-Info
        status = self._api_get('/api/v2/status') or {}

        return {
            'manufacturer': 'sonnen',
            'model': data.get('model_name', 'sonnenBatterie 10'),
            'serial': str(data.get('serial', '')),
            'firmware': status.get('SoftwareVersion', ''),
            'connected': True,
            'modules': data.get('module_count', 0),
        }

    def close(self):
        pass  # Kein persistenter State

    @staticmethod
    def plugin_id() -> str:
        return 'sonnen_batterie'

    @staticmethod
    def plugin_name() -> str:
        return 'sonnenBatterie'

    def _api_get(self, endpoint: str) -> Optional[dict]:
        """GET-Request an sonnenBatterie API."""
        headers = {}
        if self.api_key:
            headers['Auth-Token'] = self.api_key

        try:
            resp = requests.get(
                f"{self.base_url}{endpoint}",
                headers=headers,
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.debug(f"sonnen API {endpoint}: HTTP {resp.status_code}")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning(f"sonnenBatterie nicht erreichbar: {self.ip}")
            return None
        except Exception as e:
            logger.error(f"sonnen API {endpoint}: {e}")
            return None

    def _fallback(self) -> Dict:
        return {
            'soc': 0.0,
            'power_w': 0.0,
            'voltage_v': 0.0,
            'temperature_c': 0.0,
            'capacity_kwh': self.capacity_kwh,
            'usable_kwh': self.capacity_kwh * 0.9,
            'cycles': 0,
            'health_pct': 0.0,
            'timestamp': datetime.now(),
        }
