"""
Vista-Energy — Tesla Powerwall Plugin

Kommunikation via lokale HTTP API (Tesla Gateway).
Unterstuetzt: Powerwall 2, Powerwall+, Powerwall 3.

Die Tesla Powerwall hat ein lokales Gateway mit REST-API.
Authentifizierung erfolgt ueber Login (email + password) → Auth-Token.

API-Endpoints (Gateway):
  - GET /api/meters/aggregates     → Leistungswerte (site, battery, solar, load)
  - GET /api/system_status/soe     → SOC (percentage)
  - GET /api/system_status         → Systemstatus, Zyklen, Firmware
  - GET /api/powerwalls             → Powerwall-Info, Seriennummern
  - GET /api/operation              → Betriebsmodus
  - POST /api/login/Basic          → Auth-Token holen

Konfiguration (config.yaml):
  battery:
    type: tesla_powerwall
    ip: 192.168.1.150           # Tesla Gateway IP
    email: user@example.com      # Tesla-Account Email
    password: GATEWAY_PASSWORD   # Gateway-Passwort (auf Geraet aufgedruckt)
"""

import logging
from datetime import datetime
from typing import Dict, Optional

import requests
import urllib3

from services.batteries.base import BatteryBase

# Tesla Gateway nutzt selbstsigniertes Zertifikat
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class TeslaPowerwallBattery(BatteryBase):
    """Tesla Powerwall via lokale Gateway API."""

    def __init__(self, ip: str = '192.168.1.150',
                 email: str = '',
                 password: str = '',
                 capacity_kwh: float = 13.5,
                 timeout: int = 10,
                 **kwargs):
        self.ip = ip
        self.base_url = f"https://{ip}"
        self.email = email
        self.password = password
        self.capacity_kwh = capacity_kwh
        self.timeout = timeout
        self._token = None

    def get_status(self) -> Dict:
        # SOC
        soe_data = self._api_get('/api/system_status/soe')
        soc = (soe_data.get('percentage', 0) / 100.0) if soe_data else 0.0

        # Leistungswerte
        agg = self._api_get('/api/meters/aggregates')
        if not agg:
            return self._fallback()

        battery = agg.get('battery', {})
        power_w = battery.get('instant_power', 0)  # + Laden, - Entladen

        # System-Status (Zyklen, Firmware)
        sys_status = self._api_get('/api/system_status')
        cycles = 0
        health_pct = 100.0
        if sys_status:
            # Energiezaehler basierter Zyklen-Schaetzung
            energy_charged = sys_status.get('nominal_full_pack_energy', 0)
            if energy_charged > 0 and self.capacity_kwh > 0:
                total_energy = sys_status.get('lifetime_energy_charged', 0)
                cycles = int(total_energy / (self.capacity_kwh * 1000)) if total_energy else 0

        return {
            'soc': min(soc, 1.0),
            'power_w': power_w,
            'voltage_v': battery.get('instant_average_voltage', 0),
            'temperature_c': 0.0,  # Nicht ueber API verfuegbar
            'capacity_kwh': self.capacity_kwh,
            'usable_kwh': self.capacity_kwh * 0.95,
            'cycles': cycles,
            'health_pct': health_pct,
            'timestamp': datetime.now(),
            'grid_w': agg.get('site', {}).get('instant_power', 0),
            'solar_w': agg.get('solar', {}).get('instant_power', 0),
            'load_w': agg.get('load', {}).get('instant_power', 0),
        }

    def get_info(self) -> Dict:
        pw_data = self._api_get('/api/powerwalls')
        if not pw_data:
            return {
                'manufacturer': 'Tesla',
                'model': 'Powerwall',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

        powerwalls = pw_data.get('powerwalls', [])
        serial = ''
        model = 'Powerwall 2'
        if powerwalls:
            pw = powerwalls[0]
            serial = pw.get('PackageSerialNumber', '')
            part = pw.get('PackagePartNumber', '')
            if '3' in part:
                model = 'Powerwall 3'
            elif '+' in part or 'Plus' in part.lower():
                model = 'Powerwall+'

        # Firmware
        sys_status = self._api_get('/api/system_status')
        firmware = ''
        if sys_status:
            firmware = sys_status.get('version', '')

        return {
            'manufacturer': 'Tesla',
            'model': model,
            'serial': serial,
            'firmware': firmware,
            'connected': True,
            'count': len(powerwalls),
        }

    def close(self):
        self._token = None

    @staticmethod
    def plugin_id() -> str:
        return 'tesla_powerwall'

    @staticmethod
    def plugin_name() -> str:
        return 'Tesla Powerwall'

    # ==================================================================
    # Interne Methoden
    # ==================================================================

    def _api_get(self, endpoint: str) -> Optional[dict]:
        """GET-Request an Tesla Gateway."""
        headers = {}
        if self._token:
            headers['Authorization'] = f'Bearer {self._token}'

        try:
            resp = requests.get(
                f"{self.base_url}{endpoint}",
                headers=headers,
                timeout=self.timeout,
                verify=False,  # Selbstsigniertes Zertifikat
            )

            # 401/403 → Token erneuern
            if resp.status_code in (401, 403):
                if self._login():
                    headers['Authorization'] = f'Bearer {self._token}'
                    resp = requests.get(
                        f"{self.base_url}{endpoint}",
                        headers=headers,
                        timeout=self.timeout,
                        verify=False,
                    )

            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Tesla API {endpoint}: HTTP {resp.status_code}")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning(f"Tesla Gateway nicht erreichbar: {self.ip}")
            return None
        except Exception as e:
            logger.error(f"Tesla API {endpoint}: {e}")
            return None

    def _login(self) -> bool:
        """Login am Tesla Gateway fuer Auth-Token."""
        if not self.email or not self.password:
            logger.warning("Tesla Login: email/password nicht konfiguriert")
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/api/login/Basic",
                json={
                    'username': 'customer',
                    'password': self.password,
                    'email': self.email,
                    'force_sm_off': False,
                },
                timeout=self.timeout,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get('token')
                logger.info("Tesla Gateway Login erfolgreich")
                return True
            logger.error(f"Tesla Login fehlgeschlagen: HTTP {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"Tesla Login: {e}")
            return False

    def _fallback(self) -> Dict:
        return {
            'soc': 0.0,
            'power_w': 0.0,
            'voltage_v': 0.0,
            'temperature_c': 0.0,
            'capacity_kwh': self.capacity_kwh,
            'usable_kwh': self.capacity_kwh * 0.95,
            'cycles': 0,
            'health_pct': 0.0,
            'timestamp': datetime.now(),
        }
