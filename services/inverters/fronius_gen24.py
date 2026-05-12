"""
Vista-Energy — Fronius Symo GEN24 Plugin

Kommunikation via Solar API (HTTP) und Modbus TCP (Batterie-Steuerung).
Unterstuetzt: Fronius GEN24, GEN24 Plus, Primo GEN24 (alle mit Solar API v1).

Batterie-Steuerung via Modbus TCP (SunSpec Model 124):
  - Register 40358: InWRte  (Lade-Limit, 0-10000 = 0-100%)
  - Register 40359: OutWRte (Entlade-Limit, 0-10000 = 0-100%)
  - Register 40350: StorCtl_Mod (1=charge, 2=discharge, 3=both)

Konfiguration (config.yaml):
  inverter:
    type: fronius_gen24
    ip: 192.168.1.80
    modbus_port: 502      # optional, default 502
    modbus_unit: 1         # optional, default 1
"""

import logging
from datetime import datetime
from typing import Dict, Optional

import requests

from services.inverters.base import InverterBase

logger = logging.getLogger(__name__)


class FroniusGen24Inverter(InverterBase):
    """Fronius Symo GEN24 via Solar API + Modbus TCP."""

    # Solar API
    API_PATH = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"

    # Modbus Register (SunSpec Model 124 — Storage)
    REG_INWRTE = 40358       # Lade-Limit 0-10000
    REG_OUTWRTE = 40359      # Entlade-Limit 0-10000
    REG_STORCTL_MOD = 40350  # Control-Mode

    def __init__(self, ip: str = '192.168.1.80',
                 modbus_port: int = 502,
                 modbus_unit: int = 1,
                 timeout: int = 10,
                 **kwargs):
        self.ip = ip
        self.base_url = f"http://{ip}"
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.timeout = timeout
        self._modbus_client = None

    # ==================================================================
    # InverterBase — Pflicht-Methoden
    # ==================================================================

    def get_power_flow(self) -> Dict:
        """Aktuelle Leistungswerte via Solar API."""
        data = self._solar_api_request()
        if 'error' in data:
            logger.warning("Fronius nicht erreichbar, Fallback-Daten")
            return self._fallback_power_flow()

        try:
            body = data.get('Body', {})
            site = body.get('Data', {}).get('Site', {})

            pv_w = abs(site.get('P_PV', 0) or 0)
            grid_w = site.get('P_Grid', 0) or 0
            load_w = abs(site.get('P_Load', 0) or 0)
            bat_w = site.get('P_Akku', 0) or 0

            # SOC aus Inverters > 1 > SOC
            inverters = body.get('Data', {}).get('Inverters', {})
            soc = 0.0
            for inv_data in inverters.values():
                if 'SOC' in inv_data:
                    soc = inv_data['SOC'] / 100.0
                    break

            return {
                'pv_w': pv_w,
                'load_w': load_w,
                'grid_w': grid_w,
                'battery_w': bat_w,
                'battery_soc': soc,
                'timestamp': datetime.now(),
            }
        except (KeyError, TypeError, IndexError) as e:
            logger.error(f"Fronius-Antwort konnte nicht geparst werden: {e}")
            return self._fallback_power_flow()

    def get_info(self) -> Dict:
        """Wechselrichter-Infos via Solar API."""
        try:
            url = f"{self.base_url}/solar_api/v1/GetInverterInfo.cgi"
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            body = data.get('Body', {}).get('Data', {})

            # Erstes Inverter-Objekt nehmen
            inv = {}
            for v in body.values():
                inv = v
                break

            return {
                'manufacturer': 'Fronius',
                'model': inv.get('DT', 'GEN24'),
                'serial': str(inv.get('UniqueID', '')),
                'firmware': inv.get('SWVersion', ''),
                'connected': True,
            }
        except Exception as e:
            logger.debug(f"Fronius Info-Abfrage fehlgeschlagen: {e}")
            return {
                'manufacturer': 'Fronius',
                'model': 'GEN24',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

    # ==================================================================
    # Batterie-Steuerung via Modbus TCP
    # ==================================================================

    def has_battery_control(self) -> bool:
        return True

    def set_charge_limit(self, percent: int) -> bool:
        """Lade-Limit setzen (0-100% → Modbus 0-10000)."""
        client = self._get_modbus()
        if not client:
            return False
        try:
            value = min(10000, max(0, percent * 100))
            rr = client.write_register(self.REG_INWRTE, value)
            if not rr.isError():
                logger.info(f"Fronius Lade-Limit: {percent}% ({value})")
                return True
            logger.error(f"Modbus write InWRte fehlgeschlagen: {rr}")
            return False
        except Exception as e:
            logger.error(f"Modbus Fehler set_charge_limit: {e}")
            return False

    def set_discharge_limit(self, percent: int) -> bool:
        """Entlade-Limit setzen (0-100% → Modbus 0-10000)."""
        client = self._get_modbus()
        if not client:
            return False
        try:
            value = min(10000, max(0, percent * 100))
            rr = client.write_register(self.REG_OUTWRTE, value)
            if not rr.isError():
                logger.info(f"Fronius Entlade-Limit: {percent}% ({value})")
                return True
            return False
        except Exception as e:
            logger.error(f"Modbus Fehler set_discharge_limit: {e}")
            return False

    def get_charge_limit(self) -> Optional[int]:
        client = self._get_modbus()
        if not client:
            return None
        try:
            rr = client.read_holding_registers(self.REG_INWRTE, count=1)
            if not rr.isError():
                return int(rr.registers[0] / 100)
        except Exception as e:
            logger.error(f"Modbus read InWRte: {e}")
        return None

    def get_discharge_limit(self) -> Optional[int]:
        client = self._get_modbus()
        if not client:
            return None
        try:
            rr = client.read_holding_registers(self.REG_OUTWRTE, count=1)
            if not rr.isError():
                return int(rr.registers[0] / 100)
        except Exception as e:
            logger.error(f"Modbus read OutWRte: {e}")
        return None

    def get_battery_info(self) -> Dict:
        """BYD HVS Batterie-Info (statisch konfiguriert + dynamischer SOC)."""
        flow = self.get_power_flow()
        return {
            'capacity_kwh': 7.68,
            'usable_kwh': 7.0,
            'min_soc': 0.05,
            'max_soc': 1.0,
            'cycles': 0,
            'soc': flow.get('battery_soc', 0.0),
        }

    # ==================================================================
    # Verbindung
    # ==================================================================

    def is_connected(self) -> bool:
        try:
            resp = requests.get(
                f"{self.base_url}{self.API_PATH}",
                params={'Scope': 'System'},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def close(self):
        if self._modbus_client:
            try:
                self._modbus_client.close()
            except Exception:
                pass
            self._modbus_client = None

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'fronius_gen24'

    @staticmethod
    def plugin_name() -> str:
        return 'Fronius Symo GEN24'

    # ==================================================================
    # Interne Hilfsmethoden
    # ==================================================================

    def _solar_api_request(self, params: dict = None) -> dict:
        """GET-Request an Fronius Solar API."""
        if params is None:
            params = {'Scope': 'System'}
        try:
            url = f"{self.base_url}{self.API_PATH}"
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            logger.warning(f"Fronius nicht erreichbar: {self.base_url}")
            return {'error': 'connection_refused'}
        except Exception as e:
            logger.error(f"Fronius API Fehler: {e}")
            return {'error': str(e)}

    def _get_modbus(self):
        """Modbus TCP Client holen/erstellen."""
        if self._modbus_client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                self._modbus_client = ModbusTcpClient(
                    host=self.ip,
                    port=self.modbus_port,
                    timeout=5,
                )
            except ImportError:
                logger.error("pymodbus nicht installiert: pip install pymodbus")
                return None
        if self._modbus_client.connect():
            return self._modbus_client
        logger.warning("Modbus TCP Verbindung fehlgeschlagen")
        return None

    @staticmethod
    def _fallback_power_flow() -> Dict:
        """Fallback-Daten wenn Fronius offline ist."""
        return {
            'pv_w': 0.0,
            'load_w': 0.0,
            'grid_w': 0.0,
            'battery_w': 0.0,
            'battery_soc': 0.0,
            'timestamp': datetime.now(),
        }
