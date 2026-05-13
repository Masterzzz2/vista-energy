"""
Vista-Energy — LG RESU (ESS) Plugin

Kommunikation via Modbus TCP (ueber kompatiblen Wechselrichter).
Unterstuetzt: LG RESU 6.5, 10, 10H, 12, 16H Prime.

LG RESU Batterien werden typischerweise ueber den Wechselrichter
angesprochen (Fronius, SMA, SolarEdge, Kostal). Fuer direkte
Kommunikation nutzt das BMS einen CAN-Bus.

Dieses Plugin liest die Batterie-Daten via Modbus TCP,
wobei die Register vom Wechselrichter abhaengen.
Fuer eigenstaendige Konfigurationen mit EMS.

Konfiguration (config.yaml):
  battery:
    type: lg_resu
    ip: 192.168.1.80          # Wechselrichter/EMS IP
    modbus_port: 502
    modbus_unit: 1
    capacity_kwh: 9.8          # RESU 10H
    usable_kwh: 9.3
    min_soc: 5
"""

import logging
from datetime import datetime
from typing import Dict

from services.batteries.base import BatteryBase

logger = logging.getLogger(__name__)


class LGResuBattery(BatteryBase):
    """LG RESU via Modbus TCP."""

    # SunSpec-kompatible Register (ueber Wechselrichter)
    # Anpassbar je nach Wechselrichter-Hersteller
    REG_SOC = 62852          # U16, 1%
    REG_SOH = 62854          # U16, 1%
    REG_VOLTAGE = 62856      # U16, 0.1V
    REG_CURRENT = 62858      # S16, 0.1A
    REG_TEMP = 62860         # S16, 0.1°C
    REG_CYCLES = 62862       # U16
    REG_STATUS = 62864       # U16 (0=Idle, 1=Charge, 2=Discharge, 3=Error)

    def __init__(self, ip: str = '192.168.1.80',
                 modbus_port: int = 502,
                 modbus_unit: int = 1,
                 capacity_kwh: float = 9.8,
                 usable_kwh: float = 9.3,
                 min_soc: int = 5,
                 timeout: int = 5,
                 # Benutzerdefinierte Register (Wechselrichter-abhaengig)
                 reg_soc: int = None,
                 reg_voltage: int = None,
                 reg_current: int = None,
                 reg_temp: int = None,
                 **kwargs):
        self.ip = ip
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.capacity_kwh = capacity_kwh
        self.usable_kwh = usable_kwh
        self.min_soc = min_soc
        self.timeout = timeout
        self._client = None

        # Register ueberschreiben falls konfiguriert
        if reg_soc is not None:
            self.REG_SOC = reg_soc
        if reg_voltage is not None:
            self.REG_VOLTAGE = reg_voltage
        if reg_current is not None:
            self.REG_CURRENT = reg_current
        if reg_temp is not None:
            self.REG_TEMP = reg_temp

    def get_status(self) -> Dict:
        client = self._get_modbus()
        if not client:
            return self._fallback()

        try:
            # SOC
            rr = client.read_holding_registers(
                self.REG_SOC, count=1, slave=self.modbus_unit
            )
            soc = (rr.registers[0] / 100.0) if not rr.isError() else 0.0

            # SOH
            rr = client.read_holding_registers(
                self.REG_SOH, count=1, slave=self.modbus_unit
            )
            soh = rr.registers[0] if not rr.isError() else 100

            # Spannung
            rr = client.read_holding_registers(
                self.REG_VOLTAGE, count=1, slave=self.modbus_unit
            )
            voltage = (rr.registers[0] / 10.0) if not rr.isError() else 0.0

            # Strom (signed)
            rr = client.read_holding_registers(
                self.REG_CURRENT, count=1, slave=self.modbus_unit
            )
            current = 0.0
            if not rr.isError():
                raw = rr.registers[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                current = raw / 10.0

            # Temperatur (signed)
            rr = client.read_holding_registers(
                self.REG_TEMP, count=1, slave=self.modbus_unit
            )
            temp = 0.0
            if not rr.isError():
                raw = rr.registers[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                temp = raw / 10.0

            # Zyklen
            rr = client.read_holding_registers(
                self.REG_CYCLES, count=1, slave=self.modbus_unit
            )
            cycles = rr.registers[0] if not rr.isError() else 0

            power_w = voltage * current

            return {
                'soc': min(soc, 1.0),
                'power_w': power_w,
                'voltage_v': voltage,
                'temperature_c': temp,
                'capacity_kwh': self.capacity_kwh,
                'usable_kwh': self.usable_kwh,
                'cycles': cycles,
                'health_pct': soh,
                'timestamp': datetime.now(),
                'current_a': current,
            }
        except Exception as e:
            logger.error(f"LG RESU Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        connected = self._check_connection()
        kwh_str = str(self.capacity_kwh).replace('.0', '')
        return {
            'manufacturer': 'LG Energy Solution',
            'model': f'RESU {kwh_str}',
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
        return 'lg_resu'

    @staticmethod
    def plugin_name() -> str:
        return 'LG RESU'

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

    def _check_connection(self) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            rr = client.read_holding_registers(
                self.REG_SOC, count=1, slave=self.modbus_unit
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
            'usable_kwh': self.usable_kwh,
            'cycles': 0,
            'health_pct': 0.0,
            'timestamp': datetime.now(),
        }
