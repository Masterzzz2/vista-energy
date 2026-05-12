"""
Vista-Energy — Abstrakte Basisklasse fuer Batteriesysteme.

Fuer Systeme bei denen die Batterie separat angesprochen wird
(nicht ueber den Wechselrichter). Bei Fronius + BYD laeuft alles
ueber den Wechselrichter, daher ist dieses Interface optional.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Optional


class BatteryBase(ABC):
    """Abstraktes Interface fuer eigenstaendige Batteriesysteme."""

    @abstractmethod
    def get_status(self) -> Dict:
        """Batterie-Status.

        Returns:
            {
                'soc':           float,  # State of Charge 0.0–1.0
                'power_w':       float,  # + = Laden, - = Entladen
                'voltage_v':     float,  # Batterie-Spannung
                'temperature_c': float,  # Temperatur
                'capacity_kwh':  float,  # Nenn-Kapazitaet
                'usable_kwh':    float,  # Nutzbare Kapazitaet
                'cycles':        int,    # Ladezyklen
                'health_pct':    float,  # State of Health (0-100%)
                'timestamp':     datetime,
            }
        """
        ...

    def get_info(self) -> Dict:
        """Batterie-Informationen.

        Returns:
            {
                'manufacturer':  str,   # z.B. 'BYD', 'LG', 'Tesla'
                'model':         str,   # z.B. 'HVS 7.7'
                'serial':        str,
                'firmware':      str,
                'connected':     bool,
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
        """True wenn die Batterie erreichbar ist."""
        try:
            self.get_status()
            return True
        except Exception:
            return False

    def close(self):
        """Verbindung schliessen."""
        pass

    @staticmethod
    def plugin_id() -> str:
        raise NotImplementedError

    @staticmethod
    def plugin_name() -> str:
        raise NotImplementedError
