"""
Vista-Energy — Huawei SUN2000 Plugin

Kommunikation via Modbus TCP (SDongle / SmartLogger).
Unterstuetzt: SUN2000-3KTL-L1 bis SUN2000-12KTL-M5,
              SUN2000-LUNA2000 (mit Batterie).

Der Huawei-Wechselrichter wird ueber den SDongle oder SmartLogger angesprochen.
Modbus TCP muss im SDongle-Web-Interface aktiviert werden.

Modbus-Register (Huawei-spezifisch):
  - 32064: PV1-Leistung (I32, kW * 1000)
  - 32080: AC-Leistung (I32, kW * 1000)
  - 37113: Netz-Leistung (I32, W, + Bezug, - Einspeisung)
  - 37004: SOC (U16, 0.1%)
  - 37001: Batterie-Leistung (I32, W, + Laden, - Entladen)
  - 47075: Max. Ladeleistung (U32, W)
  - 47077: Max. Entladeleistung (U32, W)
  - 47086: Arbeitsmod (0=Maximum, 1=Vollst. Eigenverbrauch, 2=Zeitabh.)

Konfiguration (config.yaml):
  inverter:
    type: huawei_sun2000
    ip: 192.168.1.90          # SDongle / SmartLogger IP
    modbus_port: 502           # optional
    modbus_unit: 1             # optional
"""

import logging
from datetime import datetime
from typing import Dict, Optional

from services.inverters.base import InverterBase

logger = logging.getLogger(__name__)


class HuaweiSun2000Inverter(InverterBase):
    """Huawei SUN2000 via Modbus TCP (SDongle / SmartLogger)."""

    # Modbus Register
    REG_PV_POWER = 32080       # I32, kW * 1000 (AC Active Power)
    REG_INPUT_POWER = 32064    # I32, kW * 1000 (PV Input)
    REG_GRID_POWER = 37113     # I32, W (+ Bezug, - Einspeisung)
    REG_BAT_SOC = 37004        # U16, 0.1%
    REG_BAT_POWER = 37001      # I32, W (+ Laden, - Entladen)

    # Batterie-Steuerung
    REG_MAX_CHARGE_POWER = 47075     # U32, W
    REG_MAX_DISCHARGE_POWER = 47077  # U32, W
    REG_WORKING_MODE = 47086         # U16
    REG_CHARGE_FROM_GRID = 47087     # U16 (0=Nein, 1=Ja)

    # Geraete-Info
    REG_MODEL = 30000        # String (15 Register)
    REG_SERIAL = 30015       # String (10 Register)
    REG_MODEL_ID = 30070     # U16

    def __init__(self, ip: str = '192.168.1.90',
                 modbus_port: int = 502,
                 modbus_unit: int = 1,
                 timeout: int = 10,
                 max_charge_w: int = 5000,
                 **kwargs):
        self.ip = ip
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.timeout = timeout
        self.max_charge_w = max_charge_w
        self._client = None

    # ==================================================================
    # InverterBase — Pflicht-Methoden
    # ==================================================================

    def get_power_flow(self) -> Dict:
        """Aktuelle Leistungswerte via Modbus TCP."""
        client = self._get_modbus()
        if not client:
            return self._fallback()

        try:
            pv_w = abs(self._read_i32(client, self.REG_INPUT_POWER))
            grid_w = self._read_i32(client, self.REG_GRID_POWER)
            bat_w = self._read_i32(client, self.REG_BAT_POWER)

            # SOC lesen (U16, 0.1%)
            rr = client.read_holding_registers(
                self.REG_BAT_SOC, count=1, slave=self.modbus_unit
            )
            soc = 0.0
            if not rr.isError() and rr.registers[0] != 0xFFFF:
                soc = rr.registers[0] / 1000.0  # 0.1% → 0.0-1.0

            load_w = abs(pv_w) + grid_w - bat_w

            return {
                'pv_w': pv_w,
                'load_w': max(0, load_w),
                'grid_w': grid_w,
                'battery_w': bat_w,
                'battery_soc': min(soc, 1.0),
                'timestamp': datetime.now(),
            }
        except Exception as e:
            logger.error(f"Huawei Modbus Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        """Wechselrichter-Infos via Modbus."""
        client = self._get_modbus()
        if not client:
            return {
                'manufacturer': 'Huawei',
                'model': 'SUN2000',
                'serial': '',
                'firmware': '',
                'connected': False,
            }
        try:
            model = self._read_string(client, self.REG_MODEL, 15)
            serial = self._read_string(client, self.REG_SERIAL, 10)

            return {
                'manufacturer': 'Huawei',
                'model': model or 'SUN2000',
                'serial': serial or '',
                'firmware': '',
                'connected': True,
            }
        except Exception as e:
            logger.debug(f"Huawei Info-Abfrage fehlgeschlagen: {e}")
            return {
                'manufacturer': 'Huawei',
                'model': 'SUN2000',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

    # ==================================================================
    # Batterie-Steuerung (LUNA2000)
    # ==================================================================

    def has_battery_control(self) -> bool:
        return True

    def set_charge_limit(self, percent: int) -> bool:
        """Lade-Limit via max. Ladeleistung (Watt)."""
        client = self._get_modbus()
        if not client:
            return False
        try:
            watts = int(self.max_charge_w * min(100, max(0, percent)) / 100)
            rr = client.write_registers(
                self.REG_MAX_CHARGE_POWER, [0, watts],
                slave=self.modbus_unit
            )
            if not rr.isError():
                logger.info(f"Huawei Lade-Limit: {percent}% ({watts}W)")
                return True
            return False
        except Exception as e:
            logger.error(f"Huawei set_charge_limit: {e}")
            return False

    def set_discharge_limit(self, percent: int) -> bool:
        """Entlade-Limit via max. Entladeleistung (Watt)."""
        client = self._get_modbus()
        if not client:
            return False
        try:
            watts = int(self.max_charge_w * min(100, max(0, percent)) / 100)
            rr = client.write_registers(
                self.REG_MAX_DISCHARGE_POWER, [0, watts],
                slave=self.modbus_unit
            )
            if not rr.isError():
                logger.info(f"Huawei Entlade-Limit: {percent}% ({watts}W)")
                return True
            return False
        except Exception as e:
            logger.error(f"Huawei set_discharge_limit: {e}")
            return False

    def get_battery_info(self) -> Dict:
        flow = self.get_power_flow()
        return {
            'capacity_kwh': 0.0,
            'usable_kwh': 0.0,
            'min_soc': 0.10,
            'max_soc': 1.0,
            'cycles': 0,
            'soc': flow.get('battery_soc', 0.0),
        }

    # ==================================================================
    # Verbindung
    # ==================================================================

    def is_connected(self) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            rr = client.read_holding_registers(
                self.REG_MODEL_ID, count=1, slave=self.modbus_unit
            )
            return not rr.isError()
        except Exception:
            return False

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'huawei_sun2000'

    @staticmethod
    def plugin_name() -> str:
        return 'Huawei SUN2000'

    # ==================================================================
    # Interne Hilfsmethoden
    # ==================================================================

    def _get_modbus(self):
        if self._client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                self._client = ModbusTcpClient(
                    host=self.ip,
                    port=self.modbus_port,
                    timeout=self.timeout,
                )
            except ImportError:
                logger.error("pymodbus nicht installiert: pip install pymodbus")
                return None
        if self._client.connect():
            return self._client
        logger.warning(f"Huawei Modbus Verbindung fehlgeschlagen: {self.ip}")
        return None

    def _read_i32(self, client, register: int) -> float:
        """Signed 32-bit Register lesen (2 Register, Big-Endian)."""
        rr = client.read_holding_registers(register, count=2, slave=self.modbus_unit)
        if rr.isError():
            return 0.0
        raw = (rr.registers[0] << 16) | rr.registers[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        return float(raw)

    def _read_string(self, client, register: int, count: int) -> str:
        """String-Register lesen (ASCII in 16-bit Registern)."""
        rr = client.read_holding_registers(register, count=count, slave=self.modbus_unit)
        if rr.isError():
            return ''
        chars = []
        for reg in rr.registers:
            hi = (reg >> 8) & 0xFF
            lo = reg & 0xFF
            if hi > 0:
                chars.append(chr(hi))
            if lo > 0:
                chars.append(chr(lo))
        return ''.join(chars).strip('\x00 ')

    @staticmethod
    def _fallback() -> Dict:
        return {
            'pv_w': 0.0,
            'load_w': 0.0,
            'grid_w': 0.0,
            'battery_w': 0.0,
            'battery_soc': 0.0,
            'timestamp': datetime.now(),
        }
