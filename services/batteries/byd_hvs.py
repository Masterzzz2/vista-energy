"""
Vista-Energy — BYD Battery-Box HVS / HVM Plugin

Kommunikation via Modbus TCP (ueber den Wechselrichter oder BMS direkt).
Unterstuetzt: BYD Battery-Box Premium HVS 5.1–12.8 kWh,
              BYD Battery-Box Premium HVM 8.3–22.1 kWh.

Bei Fronius/SMA/Kostal wird die BYD meist ueber den Wechselrichter
angesprochen. Dieses Plugin liest direkt vom BYD Battery Management
System (BMS) via Modbus TCP, falls separat zugaenglich.

Modbus-Register (BYD BMS):
  - 0x0000: SOC (U16, 1%)
  - 0x0001: SOH (U16, 1%)
  - 0x0002: Batterie-Spannung (U16, 0.1V)
  - 0x0003: Batterie-Strom (S16, 0.1A, + Laden, - Entladen)
  - 0x0004: Temperatur (S16, 0.1°C)
  - 0x0005: Max. Ladestrom (U16, 0.1A)
  - 0x0006: Max. Entladestrom (U16, 0.1A)
  - 0x0010: Ladezyklen (U16)
  - 0x0020: Seriennummer (String, 8 Register)

Konfiguration (config.yaml):
  battery:
    type: byd_hvs
    ip: 192.168.1.80          # BMS IP oder Wechselrichter IP
    modbus_port: 8080          # BYD BMS Port (oft 8080)
    modbus_unit: 1
    capacity_kwh: 10.24        # HVS 10.2 oder HVM 11.0 etc.
    usable_kwh: 9.7
    min_soc: 5                 # Min. SOC in %
"""

import logging
from datetime import datetime
from typing import Dict

from services.batteries.base import BatteryBase

logger = logging.getLogger(__name__)


class BYDHVSBattery(BatteryBase):
    """BYD Battery-Box Premium HVS/HVM via Modbus TCP (BMS)."""

    # Modbus Register
    REG_SOC = 0x0000
    REG_SOH = 0x0001
    REG_VOLTAGE = 0x0002
    REG_CURRENT = 0x0003
    REG_TEMP = 0x0004
    REG_MAX_CHARGE_A = 0x0005
    REG_MAX_DISCHARGE_A = 0x0006
    REG_CYCLES = 0x0010
    REG_SERIAL = 0x0020

    def __init__(self, ip: str = '192.168.1.80',
                 modbus_port: int = 8080,
                 modbus_unit: int = 1,
                 capacity_kwh: float = 10.24,
                 usable_kwh: float = 9.7,
                 min_soc: int = 5,
                 timeout: int = 5,
                 **kwargs):
        self.ip = ip
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.capacity_kwh = capacity_kwh
        self.usable_kwh = usable_kwh
        self.min_soc = min_soc
        self.timeout = timeout
        self._client = None

    def get_status(self) -> Dict:
        client = self._get_modbus()
        if not client:
            return self._fallback()

        try:
            rr = client.read_holding_registers(
                self.REG_SOC, count=7, slave=self.modbus_unit
            )
            if rr.isError():
                return self._fallback()

            soc = rr.registers[0] / 100.0
            soh = rr.registers[1] / 100.0
            voltage = rr.registers[2] / 10.0
            # Strom: signed
            current_raw = rr.registers[3]
            if current_raw >= 0x8000:
                current_raw -= 0x10000
            current = current_raw / 10.0
            temp_raw = rr.registers[4]
            if temp_raw >= 0x8000:
                temp_raw -= 0x10000
            temp = temp_raw / 10.0

            power_w = voltage * current  # W

            # Zyklen lesen
            rr2 = client.read_holding_registers(
                self.REG_CYCLES, count=1, slave=self.modbus_unit
            )
            cycles = rr2.registers[0] if not rr2.isError() else 0

            return {
                'soc': soc,
                'power_w': power_w,
                'voltage_v': voltage,
                'temperature_c': temp,
                'capacity_kwh': self.capacity_kwh,
                'usable_kwh': self.usable_kwh,
                'cycles': cycles,
                'health_pct': soh * 100,
                'timestamp': datetime.now(),
                'current_a': current,
                'max_charge_a': rr.registers[5] / 10.0,
                'max_discharge_a': rr.registers[6] / 10.0,
            }
        except Exception as e:
            logger.error(f"BYD Modbus Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        client = self._get_modbus()
        if not client:
            return {
                'manufacturer': 'BYD',
                'model': 'Battery-Box Premium HVS',
                'serial': '',
                'firmware': '',
                'connected': False,
            }
        try:
            rr = client.read_holding_registers(
                self.REG_SERIAL, count=8, slave=self.modbus_unit
            )
            serial = ''
            if not rr.isError():
                chars = []
                for reg in rr.registers:
                    hi = (reg >> 8) & 0xFF
                    lo = reg & 0xFF
                    if hi > 0:
                        chars.append(chr(hi))
                    if lo > 0:
                        chars.append(chr(lo))
                serial = ''.join(chars).strip('\x00 ')

            model = 'HVS' if self.capacity_kwh <= 12.8 else 'HVM'
            return {
                'manufacturer': 'BYD',
                'model': f'Battery-Box Premium {model} {self.capacity_kwh}',
                'serial': serial,
                'firmware': '',
                'connected': True,
            }
        except Exception:
            return {
                'manufacturer': 'BYD',
                'model': 'Battery-Box Premium HVS',
                'serial': '',
                'firmware': '',
                'connected': False,
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
        return 'byd_hvs'

    @staticmethod
    def plugin_name() -> str:
        return 'BYD Battery-Box Premium HVS/HVM'

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

    def _fallback(self) -> Dict:
        return {
            'soc': 0.0,
            'power_w': 0.0,
            'voltage_v': 0.0,
            'temperature_c': 0.0,
            'capacity_kwh': self.capacity_kwh,
            'usable_kwh': self.usable_kwh,
            'cycles': 0,
            'health_pct': 0.0,
            'timestamp': datetime.now(),
        }
