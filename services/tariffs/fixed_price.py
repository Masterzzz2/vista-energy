"""
Vista-Energy — Festpreis-Tarif Plugin

Fuer Kunden mit normalem Grundversorger-Tarif (fester ct/kWh Preis).
Keine dynamische Optimierung moeglich, aber Eigenverbrauch-Maximierung
funktioniert trotzdem (PV → Akku → Haus → Netz).

Konfiguration (config.yaml):
  tariff:
    type: fixed_price
    price_ct: 32.5          # Arbeitspreis in ct/kWh (brutto)
    feed_in_ct: 8.2          # Einspeiseverguetung in ct/kWh
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List

from services.tariffs.base import TariffBase

logger = logging.getLogger(__name__)


class FixedPriceTariff(TariffBase):
    """Fester Strompreis — keine stuendliche Schwankung."""

    def __init__(self, price_ct: float = 32.5, feed_in_ct: float = 8.2,
                 **kwargs):
        """
        Args:
            price_ct: Arbeitspreis in ct/kWh (brutto), z.B. 32.5
            feed_in_ct: Einspeiseverguetung in ct/kWh, z.B. 8.2
        """
        self.price_eur = price_ct / 100.0  # intern EUR/kWh
        self.feed_in_eur = feed_in_ct / 100.0
        self.price_ct = price_ct
        self.feed_in_ct = feed_in_ct

    def get_prices(self, hours_ahead: int = 48) -> List[Dict]:
        """Gibt feste Preise fuer alle Stunden zurueck.

        Bei Festpreis ist jede Stunde gleich teuer.
        """
        prices = []
        now = datetime.now().replace(minute=0, second=0, microsecond=0)

        for h in range(hours_ahead):
            ts = now + timedelta(hours=h)
            prices.append({
                'timestamp': ts,
                'price': self.price_eur,
                'level': 'normal',  # Festpreis ist immer 'normal'
            })

        return prices

    def get_current_price(self) -> float:
        return self.price_eur

    def get_cheapest_hours(self, count: int = 4, hours_ahead: int = 24) -> List[Dict]:
        """Bei Festpreis sind alle Stunden gleich — keine Optimierung."""
        return self.get_prices(count)

    def get_price_level(self) -> str:
        return 'normal'

    def is_dynamic(self) -> bool:
        return False

    def get_info(self) -> Dict:
        return {
            'provider': 'Festpreis',
            'plan': f'{self.price_ct:.1f} ct/kWh',
            'dynamic': False,
            'connected': True,  # Braucht kein API
            'feed_in_ct': self.feed_in_ct,
        }

    @staticmethod
    def plugin_id() -> str:
        return 'fixed_price'

    @staticmethod
    def plugin_name() -> str:
        return 'Festpreis (Grundversorger)'
