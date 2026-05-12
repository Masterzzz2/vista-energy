"""
Vista-Energy — Abstrakte Basisklasse fuer Wallboxen.

Jede Wallbox-Implementierung liefert Status und erlaubt Ladesteuerung.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Optional


class WallboxBase(ABC):
    """Abstraktes Interface fuer alle Wallboxen."""

    # ------------------------------------------------------------------
    # Pflicht-Methoden
    # ------------------------------------------------------------------

    @abstractmethod
    def get_status(self) -> Dict:
        """Aktueller Wallbox-Status.

        Returns:
            {
                'connected':     bool,   # Fahrzeug angesteckt?
                'charging':      bool,   # Laedt gerade?
                'power_w':       float,  # Aktuelle Ladeleistung in Watt
                'energy_kwh':    float,  # Geladene Energie dieser Session
                'current_a':     float,  # Aktueller Ladestrom in Ampere
                'max_current_a': float,  # Max. erlaubter Strom
                'phases':        int,    # 1 oder 3
                'timestamp':     datetime,
            }
        """
        ...

    @abstractmethod
    def set_charge_current(self, amps: float) -> bool:
        """Ladestrom setzen (in Ampere).

        Args:
            amps: Gewuenschter Ladestrom (6-32A typisch).
                  0 = Laden pausieren.

        Returns:
            True wenn erfolgreich.
        """
        ...

    # ------------------------------------------------------------------
    # Optionale Methoden
    # ------------------------------------------------------------------

    def start_charging(self) -> bool:
        """Laden starten."""
        return False

    def stop_charging(self) -> bool:
        """Laden stoppen."""
        return self.set_charge_current(0)

    def set_phases(self, phases: int) -> bool:
        """Phasen umschalten (1-phasig / 3-phasig).

        Nicht alle Wallboxen unterstuetzen das.
        """
        return False

    def get_info(self) -> Dict:
        """Wallbox-Informationen.

        Returns:
            {
                'manufacturer':  str,
                'model':         str,
                'serial':        str,
                'firmware':      str,
                'connected':     bool,  # Wallbox erreichbar?
            }
        """
        return {
            'manufacturer': 'Unknown',
            'model': 'Unknown',
            'serial': '',
            'firmware': '',
            'connected': False,
        }

    def is_connected(self) -> bool:
        """True wenn die Wallbox erreichbar ist."""
        try:
            status = self.get_status()
            return True
        except Exception:
            return False

    def close(self):
        """Verbindung schliessen."""
        pass

    # ------------------------------------------------------------------
    # Plugin-Registrierung
    # ------------------------------------------------------------------

    @staticmethod
    def plugin_id() -> str:
        """Eindeutige ID (z.B. 'fronius_wattpilot', 'goe_charger')."""
        raise NotImplementedError

    @staticmethod
    def plugin_name() -> str:
        """Anzeigename (z.B. 'Fronius WattPilot Go 11')."""
        raise NotImplementedError
