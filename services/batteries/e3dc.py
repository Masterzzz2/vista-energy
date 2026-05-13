"""
Vista-Energy — E3/DC Hauskraftwerk Plugin

Kommunikation via RSCP-Protokoll (E3/DC eigenes Protokoll) oder Modbus TCP.
Unterstuetzt: E3/DC S10 E, S10 E PRO, S10 X, S10 Mini.

E3/DC bietet zwei Schnittstellen:
1. RSCP (Remote Storage Control Protocol) — proprietaer, voller Zugriff
2. Modbus TCP — einfacher, muss im Portal aktiviert werden

Dieses Plugin nutzt Modbus TCP (einfacher zu integrieren).
Modbus muss im E3/DC-Portal unter "Modbus" aktiviert werden.

Modbus-Register (E3/DC Simple Mode):
  - 40001: PV-Leistung (I32, W)
  - 40003: Batterie-Leistung (I32, W, + Laden, - Entladen)
  - 40005: Hausverbrauch (I32, W)
  - 40007: Netz-Leistung (I32, W, + Bezug, - Einspeisung)
  - 40009: Wallbox-Leistung (I32, W)
  - 40033: Batterie SOC (U16, 1%)
  - 40035: Notstrom-Status (U16)
  - 40037: Batterie-Zyklen (U16)
  - 40039: Betriebsart (U16)

Konfiguration (config.yaml):
  battery:
    type: e3dc
    ip: 192.168.1.170
    modbus_port: 502
    modbus_unit: 1
    capacity_kwh: 13.0
"""

import logging
from datetime import datetime
from typing import Dict

from services.batteries.base import BatteryBase

logger = logging.getLogger(__name__)


class E3DCBattery(BatteryBase):
    """E3/DC Hauskraftwerk via Modbus TCP."""

    # Modbus Register (Simple Mode)
    REG_PV_POWER = 40001         # I32, W
    REG_BAT_POWER = 40003        # I32, W
    REG_CONSUMPTION = 40005      # I32, W
    REG_GRID_POWER = 40007       # I32, W
    REG_WALLBOX_POWER = 40009    # I32, W
    REG_BAT_SOC = 40033          # U16, 1%
    REG_EMERGENCY_STATUS = 40035  # U16
    REG_BAT_CYCLES = 40037       # U16
    REG_OPERATING_MODE = 40039   # U16

    # Zustaende
    OPERATING_MODES = {
        0: 'Idle',
        1: 'Eigenverbrauch',
        2: 'Zwangsladen',
        3: 'Zwangsentladen',
        4: 'Notstrom',
    }

    def __init__(self, ip: str = '192.168.1.170',
                 modbus_port: int = 502,
                 modbus_unit: int = 1,
                 capacity_kwh: float = 13.0,
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
            bat_power = self._read_i32(client, self.REG_BAT_POWER)
            pv_power = self._read_i32(client, self.REG_PV_POWER)
            grid_power = self._read_i32(client, self.REG_GRID_POWER)
            consumption = self._read_i32(client, self.REG_CONSUMPTION)
            wallbox_power = self._read_i32(client, self.REG_WALLBOX_POWER)

            # SOC
            rr = client.read_holding_registers(
                self.REG_BAT_SOC, count=1, slave=self.modbus_unit
            )
            soc = (rr.registers[0] / 100.0) if not rr.isError() else 0.0

            # Zyklen
            rr = client.read_holding_registers(
                self.REG_BAT_CYCLES, count=1, slave=self.modbus_unit
            )
            cycles = rr.registers[0] if not rr.isError() else 0

            # Betriebsart
            rr = client.read_holding_registers(
                self.REG_OPERATING_MODE, count=1, slave=self.modbus_unit
            )
            mode = rr.registers[0] if not rr.isError() else 0

            # Notstrom-Status
            rr = client.read_holding_registers(
                self.REG_EMERGENCY_STATUS, count=1, slave=self.modbus_unit
            )
            emergency = rr.registers[0] if not rr.isError() else 0

            return {
                'soc': min(soc, 1.0),
                'power_w': bat_power,
                'voltage_v': 0.0,     # Nicht ueber Simple Mode
                'temperature_c': 0.0,  # Nicht ueber Simple Mode
                'capacity_kwh': self.capacity_kwh,
                'usable_kwh': self.capacity_kwh * 0.9,
                'cycles': cycles,
                'health_pct': 100.0,   # Nicht ueber Simple Mode
                'timestamp': datetime.now(),
                'pv_power_w': pv_power,
                'grid_power_w': grid_power,
                'consumption_w': consumption,
                'wallbox_power_w': wallbox_power,
                'operating_mode': self.OPERATING_MODES.get(mode, f'Unknown ({mode})'),
                'emergency_power': emergency > 0,
            }
        except Exception as e:
            logger.error(f"E3/DC Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        connected = self._check_connection()
        return {
            'manufacturer': 'E3/DC (HagerEnergy)',
            'model': f'S10 ({self.capacity_kwh} kWh)',
            'serial': '',
            'firmware': '',
            'connected': connected,
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
        return 'e3dc'

    @staticmethod
    def plugin_name() -> str:
        return 'E3/DC Hauskraftwerk'

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

    def _check_connection(self) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            rr = client.read_holding_registers(
                self.REG_BAT_SOC, count=1, slave=self.modbus_unit
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
