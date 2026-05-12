"""
Vista-Energy — Abstrakte Basisklasse fuer Stromtarife.

Unterstuetzt dynamische Tarife (Tibber, aWATTar, etc.)
und feste Tarife (Grundversorger).
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, List, Optional


class TariffBase(ABC):
    """Abstraktes Interface fuer Stromtarif-Anbieter."""

    # ------------------------------------------------------------------
    # Pflicht-Methoden
    # ------------------------------------------------------------------

    @abstractmethod
    def get_prices(self, hours_ahead: int = 48) -> List[Dict]:
        """Strompreise abrufen.

        Args:
            hours_ahead: Wie viele Stunden voraus (max. abhaengig vom Anbieter).

        Returns:
            Liste sortiert nach Zeitstempel:
            [
                {
                    'timestamp': datetime,  # Beginn der Stunde
                    'price':     float,     # EUR/kWh (brutto)
                    'level':     str,       # 'cheap' | 'normal' | 'expensive'
                },
                ...
            ]
        """
        ...

    @abstractmethod
    def get_current_price(self) -> float:
        """Aktueller Strompreis in EUR/kWh."""
        ...

    # ------------------------------------------------------------------
    # Komfort-Methoden (Standard-Implementierung vorhanden)
    # ------------------------------------------------------------------

    def get_cheapest_hours(self, count: int = 4, hours_ahead: int = 24) -> List[Dict]:
        """Die guenstigsten Stunden im angegebenen Zeitraum.

        Args:
            count: Anzahl gewuenschter Stunden
            hours_ahead: Suchfenster in Stunden

        Returns:
            Liste der guenstigsten Stunden, sortiert nach Preis.
        """
        prices = self.get_prices(hours_ahead)
        now = datetime.now()
        cutoff = now + timedelta(hours=hours_ahead)

        upcoming = [
            p for p in prices
            if now <= p['timestamp'] <= cutoff
        ]
        upcoming.sort(key=lambda x: x['price'])
        return upcoming[:count]

    def get_price_level(self) -> str:
        """Aktuelles Preisniveau: 'cheap', 'normal', 'expensive'."""
        price = self.get_current_price()
        return self._classify_price(price)

    def is_dynamic(self) -> bool:
        """True wenn dynamischer Tarif (stuendlich wechselnd)."""
        return True

    def get_info(self) -> Dict:
        """Tarif-Informationen.

        Returns:
            {
                'provider':  str,   # z.B. 'Tibber', 'aWATTar'
                'plan':      str,   # z.B. 'Pulse', 'HOURLY'
                'dynamic':   bool,  # Dynamischer Tarif?
                'connected': bool,  # API erreichbar?
            }
        """
        return {
            'provider': 'Unknown',
            'plan': 'Unknown',
            'dynamic': self.is_dynamic(),
            'connected': False,
        }

    def close(self):
        """Verbindung/Session schliessen."""
        pass

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_price(price: float) -> str:
        """Preis-Level bestimmen basierend auf EUR/kWh.

        Standard-Schwellen fuer deutsche Strompreise.
        Kann in Unterklassen ueberschrieben werden.
        """
        if price < 0.15:
            return 'cheap'
        elif price < 0.25:
            return 'normal'
        else:
            return 'expensive'

    @staticmethod
    def plugin_id() -> str:
        raise NotImplementedError

    @staticmethod
    def plugin_name() -> str:
        raise NotImplementedError
