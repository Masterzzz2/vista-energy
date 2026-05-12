"""
Vista-Energy — Abstrakte Basisklasse fuer Wechselrichter.

Jeder Wechselrichter-Hersteller implementiert dieses Interface.
Neue Hersteller = neue Datei in services/inverters/, kein Umbau am Kernsystem.

Beispiel:
    from services.inverters.fronius_gen24 import FroniusGen24Inverter
    inverter = FroniusGen24Inverter(ip='192.168.1.80')
    flow = inverter.get_power_flow()
    print(f"PV: {flow['pv_w']}W, Load: {flow['load_w']}W")
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional


class InverterBase(ABC):
    """Abstraktes Interface fuer alle Wechselrichter.

    Jede Implementierung MUSS mindestens get_power_flow() liefern.
    Batterie-Steuerung ist optional (has_battery_control).
    """

    # ------------------------------------------------------------------
    # Pflicht-Methoden (muessen implementiert werden)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_power_flow(self) -> Dict:
        """Aktuelle Leistungswerte lesen.

        Returns:
            {
                'pv_w':          float,  # PV-Erzeugung in Watt
                'load_w':        float,  # Hausverbrauch in Watt
                'grid_w':        float,  # Netz: + = Bezug, - = Einspeisung
                'battery_w':     float,  # Batterie: + = Laden, - = Entladen
                'battery_soc':   float,  # State of Charge 0.0 – 1.0
                'timestamp':     datetime,
            }
        """
        ...

    @abstractmethod
    def get_info(self) -> Dict:
        """Wechselrichter-Informationen.

        Returns:
            {
                'manufacturer':  str,    # z.B. 'Fronius', 'SMA', 'Huawei'
                'model':         str,    # z.B. 'Symo GEN24 6.0'
                'serial':        str,    # Seriennummer (falls verfuegbar)
                'firmware':      str,    # Firmware-Version
                'connected':     bool,   # Erreichbar?
            }
        """
        ...

    # ------------------------------------------------------------------
    # Batterie-Steuerung (optional — Standard: nicht unterstuetzt)
    # ------------------------------------------------------------------

    def has_battery_control(self) -> bool:
        """True wenn dieser Wechselrichter Batterie-Steuerung unterstuetzt."""
        return False

    def set_charge_limit(self, percent: int) -> bool:
        """Lade-Leistung begrenzen (0-100%).

        Args:
            percent: 0 = kein Laden, 100 = volle Ladeleistung

        Returns:
            True wenn erfolgreich, False wenn nicht unterstuetzt/Fehler.
        """
        return False

    def set_discharge_limit(self, percent: int) -> bool:
        """Entlade-Leistung begrenzen (0-100%).

        Args:
            percent: 0 = kein Entladen, 100 = volle Entladeleistung

        Returns:
            True wenn erfolgreich, False wenn nicht unterstuetzt/Fehler.
        """
        return False

    def get_charge_limit(self) -> Optional[int]:
        """Aktuelle Lade-Begrenzung lesen (0-100%)."""
        return None

    def get_discharge_limit(self) -> Optional[int]:
        """Aktuelle Entlade-Begrenzung lesen (0-100%)."""
        return None

    def lock_battery(self) -> bool:
        """Batterie sperren (kein Laden). Kurzform fuer set_charge_limit(0)."""
        return self.set_charge_limit(0)

    def unlock_battery(self) -> bool:
        """Batterie freigeben. Kurzform fuer set_charge_limit(100)."""
        return self.set_charge_limit(100)

    # ------------------------------------------------------------------
    # Batterie-Info (optional)
    # ------------------------------------------------------------------

    def get_battery_info(self) -> Dict:
        """Batterie-Informationen.

        Returns:
            {
                'capacity_kwh':  float,  # Nenn-Kapazitaet
                'usable_kwh':    float,  # Nutzbare Kapazitaet
                'min_soc':       float,  # Minimaler SOC (0.0–1.0)
                'max_soc':       float,  # Maximaler SOC (0.0–1.0)
                'cycles':        int,    # Ladezyklen (falls verfuegbar)
            }
        """
        return {
            'capacity_kwh': 0.0,
            'usable_kwh': 0.0,
            'min_soc': 0.0,
            'max_soc': 1.0,
            'cycles': 0,
        }

    # ------------------------------------------------------------------
    # Verbindung
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """True wenn der Wechselrichter erreichbar ist."""
        try:
            info = self.get_info()
            return info.get('connected', False)
        except Exception:
            return False

    def close(self):
        """Verbindung sauber schliessen (z.B. Modbus TCP)."""
        pass

    # ------------------------------------------------------------------
    # Plugin-Registrierung
    # ------------------------------------------------------------------

    @staticmethod
    def plugin_id() -> str:
        """Eindeutige ID fuer config.yaml (z.B. 'fronius_gen24', 'sma_tripower')."""
        raise NotImplementedError

    @staticmethod
    def plugin_name() -> str:
        """Anzeigename fuer Dashboard (z.B. 'Fronius Symo GEN24')."""
        raise NotImplementedError
