"""
Vista-Energy — Huawei LUNA2000 Plugin

Kommunikation via Modbus TCP (ueber Huawei SUN2000 Wechselrichter).
Unterstuetzt: LUNA2000-5-S0, LUNA2000-10-S0, LUNA2000-15-S0.

Die LUNA2000 wird ueber den SUN2000-Wechselrichter angesprochen.
Batterie-spezifische Register im Modbus des Wechselrichters.

Modbus-Register (Huawei SUN2000 → Batterie):
  - 37000: Laufzustand (U16, 0=Offline, 1=Standby, 2=Running, 3=Fault)
  - 37001: Lade-/Entladeleistung (I32, W)
  - 37003: Bus-Spannung (U16, 0.1V)
  - 37004: SOC (U16, 0.1%)
  - 37006: Lade-/Entladekapazitaet taeglich (U32, 0.01 kWh)
  - 37014: Max. Ladeleistung (U32, W)
  - 37016: Max. Entladeleistung (U32, W)

Konfiguration (config.yaml):
  battery:
    type: huawei_luna2000
    ip: 192.168.1.90          # SUN2000 Wechselrichter IP
    modbus_port: 502
    modbus_unit: 1
    capacity_kwh: 10.0         # 5, 10 oder 15 kWh
"""

import logging
from datetime import datetime
from typing import Dict

from services.batteries.base import BatteryBase

logger = logging.getLogger(__name__)


class HuaweiLuna2000Battery(BatteryBase):
    """Huawei LUNA2000 via Modbus TCP (SUN2000 Wechselrichter)."""

    REG_STATUS = 37000         # U16
    REG_POWER = 37001          # I32, W
    REG_BUS_VOLTAGE = 37003    # U16, 0.1V
    REG_SOC = 37004            # U16, 0.1%
    REG_DAILY_CHARGE = 37006   # U32, 0.01 kWh
    REG_DAILY_DISCHARGE = 37008  # U32, 0.01 kWh
    REG_MAX_CHARGE_W = 37014   # U32, W
    REG_MAX_DISCHARGE_W = 37016  # U32, W
    REG_BAT_TEMP = 37022       # S16, 0.1°C

    STATUS_MAP = {0: 'Offline', 1: 'Standby', 2: 'Running', 3: 'Fault', 4: 'Sleep'}

    def __init__(self, ip: str = '192.168.1.90',
                 modbus_port: int = 502,
                 modbus_unit: int = 1,
                 capacity_kwh: float = 10.0,
                 timeout: int = 10,
                 **kwargs):
        self.ip = ip
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.capacity_kwh = capacity_kwh
        self.timeout = timeout
        self._client = None

    def get_status(self) -> Dict:
        client = self._get_modbus()
        if not client:
            return self._fallback()

        try:
            # Status
            rr = client.read_holding_registers(
                self.REG_STATUS, count=1, slave=self.modbus_unit
            )
            status = rr.registers[0] if not rr.isError() else 0

            # Leistung (I32)
            power_w = self._read_i32(client, self.REG_POWER)

            # Spannung
            rr = client.read_holding_registers(
                self.REG_BUS_VOLTAGE, count=1, slave=self.modbus_unit
            )
            voltage = (rr.registers[0] / 10.0) if not rr.isError() else 0.0

            # SOC
            rr = client.read_holding_registers(
                self.REG_SOC, count=1, slave=self.modbus_unit
            )
            soc = 0.0
            if not rr.isError() and rr.registers[0] != 0xFFFF:
                soc = rr.registers[0] / 1000.0

            # Temperatur
            rr = client.read_holding_registers(
                self.REG_BAT_TEMP, count=1, slave=self.modbus_unit
            )
            temp = 0.0
            if not rr.isError():
                raw = rr.registers[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                temp = raw / 10.0

            # Taegliche Lade-/Entladeenergie
            daily_charge = self._read_u32(client, self.REG_DAILY_CHARGE) / 100.0
            daily_discharge = self._read_u32(client, self.REG_DAILY_DISCHARGE) / 100.0

            return {
                'soc': min(soc, 1.0),
                'power_w': power_w,
                'voltage_v': voltage,
                'temperature_c': temp,
                'capacity_kwh': self.capacity_kwh,
                'usable_kwh': self.capacity_kwh * 0.9,
                'cycles': 0,  # Nicht direkt ueber Modbus verfuegbar
                'health_pct': 100.0,
                'timestamp': datetime.now(),
                'status': self.STATUS_MAP.get(status, f'Unknown ({status})'),
                'daily_charge_kwh': daily_charge,
                'daily_discharge_kwh': daily_discharge,
            }
        except Exception as e:
            logger.error(f"LUNA2000 Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        connected = self._check_connection()
        modules = max(1, int(self.capacity_kwh / 5))
        return {
            'manufacturer': 'Huawei',
            'model': f'LUNA2000-{int(self.capacity_kwh)}-S0',
            'serial': '',
            'firmware': '',
            'connected': connected,
            'modules': modules,
        }

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @staticmethod
    def plugin_id() -> str:
        return 'huawei_luna2000'

    @staticmethod
    def plugin_name() -> str:
        return 'Huawei LUNA2000'

    def _get_modbus(self):
        if self._client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                self._client = ModbusTcpClient(
                    host=self.ip, port=self.modbus_port, timeout=self.timeout
                )
            except ImportError:
                logger.error("pymodbus nicht installiert")
                return None
        if self._client.connect():
            return self._client
        return None

    def _read_i32(self, client, register: int) -> float:
        rr = client.read_holding_registers(register, count=2, slave=self.modbus_unit)
        if rr.isError():
            return 0.0
        raw = (rr.registers[0] << 16) | rr.registers[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        return float(raw)

    def _read_u32(self, client, register: int) -> int:
        rr = client.read_holding_registers(register, count=2, slave=self.modbus_unit)
        if rr.isError():
            return 0
        return (rr.registers[0] << 16) | rr.registers[1]

    def _check_connection(self) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            rr = client.read_holding_registers(
                self.REG_STATUS, count=1, slave=self.modbus_unit
            )
            return not rr.isError()
        except Exception:
            return False

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
