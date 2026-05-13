"""
Vista-Energy — SMA Sunny Tripower / Sunny Boy Plugin

Kommunikation via Modbus TCP (SunSpec-konform).
Unterstuetzt: SMA Sunny Tripower X (STP 15-25), Sunny Boy (SB 3.0-6.0),
              Sunny Tripower Smart Energy (STP SE), Sunny Highpower Peak.

Batterie-Steuerung fuer Hybrid-Modelle (z.B. STP SE) via Modbus.

Modbus-Register (SunSpec):
  - 30775: AC-Leistung gesamt (S32, 1W)
  - 30769: DC-Leistung gesamt (S32, 1W)
  - 30865: Netzbezug / -einspeisung (S32, 1W)
  - 31393: Batterie-Ladezustand SOC (U32, 0-100%)
  - 31395: Batterie-Leistung (S32, W, + = Laden, - = Entladen)
  - 40149: Batterie Lade-Limit (U32, 0-100%)
  - 40151: Batterie Entlade-Limit (U32, 0-100%)
  - 40236: Betriebsart (2=Eigenverbrauch, 1803=Manuell)

Konfiguration (config.yaml):
  inverter:
    type: sma_tripower
    ip: 192.168.1.85
    modbus_port: 502       # optional, default 502
    modbus_unit: 3         # optional, default 3 (SMA Standard)
"""

import logging
from datetime import datetime
from typing import Dict, Optional

from services.inverters.base import InverterBase

logger = logging.getLogger(__name__)


class SMATriPowerInverter(InverterBase):
    """SMA Sunny Tripower / Boy via Modbus TCP (SunSpec)."""

    # Modbus Register (SMA-spezifisch, SunSpec-konform)
    REG_AC_POWER = 30775         # S32, Watt
    REG_DC_POWER = 30769         # S32, Watt (PV-Erzeugung)
    REG_GRID_POWER = 30865       # S32, Watt (+ Bezug, - Einspeisung)
    REG_BAT_SOC = 31393          # U32, 0-100%
    REG_BAT_POWER = 31395        # S32, Watt (+ Laden, - Entladen)
    REG_BAT_CHARGE_LIMIT = 40149   # U32, 0-100%
    REG_BAT_DISCHARGE_LIMIT = 40151  # U32, 0-100%
    REG_OPERATING_MODE = 40236   # U32

    # Geraete-Info Register
    REG_DEVICE_CLASS = 30051     # U32
    REG_SERIAL = 30057           # U32
    REG_SW_VERSION = 30059       # U32

    def __init__(self, ip: str = '192.168.1.85',
                 modbus_port: int = 502,
                 modbus_unit: int = 3,
                 timeout: int = 10,
                 **kwargs):
        self.ip = ip
        self.modbus_port = modbus_port
        self.modbus_unit = modbus_unit
        self.timeout = timeout
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
            pv_w = self._read_s32(client, self.REG_DC_POWER)
            grid_w = self._read_s32(client, self.REG_GRID_POWER)
            bat_w = self._read_s32(client, self.REG_BAT_POWER)
            bat_soc = self._read_u32(client, self.REG_BAT_SOC)

            # Hausverbrauch berechnen: PV + Netzbezug + Batterie-Entladung
            load_w = abs(pv_w) + grid_w - bat_w

            return {
                'pv_w': abs(pv_w),
                'load_w': max(0, load_w),
                'grid_w': grid_w,
                'battery_w': bat_w,
                'battery_soc': min(bat_soc, 100) / 100.0,
                'timestamp': datetime.now(),
            }
        except Exception as e:
            logger.error(f"SMA Modbus Lesefehler: {e}")
            return self._fallback()

    def get_info(self) -> Dict:
        """Wechselrichter-Infos via Modbus."""
        client = self._get_modbus()
        if not client:
            return {
                'manufacturer': 'SMA',
                'model': 'Sunny Tripower',
                'serial': '',
                'firmware': '',
                'connected': False,
            }
        try:
            serial = self._read_u32(client, self.REG_SERIAL)
            sw = self._read_u32(client, self.REG_SW_VERSION)
            dev_class = self._read_u32(client, self.REG_DEVICE_CLASS)

            model_map = {
                9074: 'Sunny Tripower X 15',
                9075: 'Sunny Tripower X 20',
                9076: 'Sunny Tripower X 25',
                9344: 'Sunny Tripower Smart Energy',
                9302: 'Sunny Boy 3.0',
                9303: 'Sunny Boy 4.0',
                9304: 'Sunny Boy 5.0',
                9305: 'Sunny Boy 6.0',
            }
            model = model_map.get(dev_class, f'SMA ({dev_class})')

            return {
                'manufacturer': 'SMA',
                'model': model,
                'serial': str(serial),
                'firmware': f'{sw >> 24}.{(sw >> 16) & 0xFF}.{(sw >> 8) & 0xFF}.{sw & 0xFF}',
                'connected': True,
            }
        except Exception as e:
            logger.debug(f"SMA Info-Abfrage fehlgeschlagen: {e}")
            return {
                'manufacturer': 'SMA',
                'model': 'Sunny Tripower',
                'serial': '',
                'firmware': '',
                'connected': False,
            }

    # ==================================================================
    # Batterie-Steuerung via Modbus
    # ==================================================================

    def has_battery_control(self) -> bool:
        """Hybrid-Modelle (STP SE) unterstuetzen Batterie-Steuerung."""
        return True

    def set_charge_limit(self, percent: int) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            value = min(100, max(0, percent))
            rr = client.write_registers(
                self.REG_BAT_CHARGE_LIMIT, [0, value],
                slave=self.modbus_unit
            )
            if not rr.isError():
                logger.info(f"SMA Lade-Limit: {percent}%")
                return True
            logger.error(f"SMA write charge limit: {rr}")
            return False
        except Exception as e:
            logger.error(f"SMA set_charge_limit: {e}")
            return False

    def set_discharge_limit(self, percent: int) -> bool:
        client = self._get_modbus()
        if not client:
            return False
        try:
            value = min(100, max(0, percent))
            rr = client.write_registers(
                self.REG_BAT_DISCHARGE_LIMIT, [0, value],
                slave=self.modbus_unit
            )
            if not rr.isError():
                logger.info(f"SMA Entlade-Limit: {percent}%")
                return True
            return False
        except Exception as e:
            logger.error(f"SMA set_discharge_limit: {e}")
            return False

    def get_charge_limit(self) -> Optional[int]:
        client = self._get_modbus()
        if not client:
            return None
        try:
            return self._read_u32(client, self.REG_BAT_CHARGE_LIMIT)
        except Exception:
            return None

    def get_discharge_limit(self) -> Optional[int]:
        client = self._get_modbus()
        if not client:
            return None
        try:
            return self._read_u32(client, self.REG_BAT_DISCHARGE_LIMIT)
        except Exception:
            return None

    def get_battery_info(self) -> Dict:
        flow = self.get_power_flow()
        return {
            'capacity_kwh': 0.0,    # Wird ueber config.yaml gesetzt
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
                self.REG_AC_POWER, count=2, slave=self.modbus_unit
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
        return 'sma_tripower'

    @staticmethod
    def plugin_name() -> str:
        return 'SMA Sunny Tripower'

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
        logger.warning(f"SMA Modbus Verbindung fehlgeschlagen: {self.ip}")
        return None

    def _read_s32(self, client, register: int) -> float:
        """Signed 32-bit Register lesen (2 Register, Big-Endian)."""
        rr = client.read_holding_registers(register, count=2, slave=self.modbus_unit)
        if rr.isError():
            return 0.0
        raw = (rr.registers[0] << 16) | rr.registers[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        # SMA NaN-Wert = 0x80000000
        if raw == -2147483648:
            return 0.0
        return float(raw)

    def _read_u32(self, client, register: int) -> int:
        """Unsigned 32-bit Register lesen."""
        rr = client.read_holding_registers(register, count=2, slave=self.modbus_unit)
        if rr.isError():
            return 0
        raw = (rr.registers[0] << 16) | rr.registers[1]
        # SMA NaN = 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return 0
        return raw

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
