"""
Vista-Energy — HT/NT Doppeltarif Plugin (Tag/Nacht-Strom)

Fuer Kunden mit Hochtarif (HT) und Niedertarif (NT).
Typisch: guenstiger Nachtstrom zum EV-Laden oder Akku-Laden.

Beispiele:
  - E.ON NaturStrom HT/NT
  - Stadtwerke Doppeltarif
  - Waermepumpen-Tarif (guenstiger Nachtstrom)

Konfiguration (config.yaml):
  tariff:
    type: dual_rate
    ht_price_ct: 32.5        # Hochtarif in ct/kWh
    nt_price_ct: 22.0         # Niedertarif in ct/kWh
    nt_start: "22:00"         # NT-Beginn
    nt_end: "06:00"           # NT-Ende
    feed_in_ct: 8.2           # Einspeiseverguetung
    weekend_nt: true          # Ganzes Wochenende NT? (optional)
"""

import logging
from datetime import datetime, timedelta, time
from typing import Dict, List

from services.tariffs.base import TariffBase

logger = logging.getLogger(__name__)


class DualRateTariff(TariffBase):
    """HT/NT Doppeltarif — guenstiger Nachtstrom."""

    def __init__(self, ht_price_ct: float = 32.5, nt_price_ct: float = 22.0,
                 nt_start: str = "22:00", nt_end: str = "06:00",
                 feed_in_ct: float = 8.2, weekend_nt: bool = False,
                 **kwargs):
        self.ht_eur = ht_price_ct / 100.0
        self.nt_eur = nt_price_ct / 100.0
        self.ht_ct = ht_price_ct
        self.nt_ct = nt_price_ct
        self.feed_in_ct = feed_in_ct
        self.weekend_nt = weekend_nt

        # NT-Zeiten parsen
        h, m = nt_start.split(':')
        self.nt_start = time(int(h), int(m))
        h, m = nt_end.split(':')
        self.nt_end = time(int(h), int(m))

    def _is_nt(self, dt: datetime) -> bool:
        """True wenn der Zeitpunkt im Niedertarif liegt."""
        # Wochenende komplett NT?
        if self.weekend_nt and dt.weekday() >= 5:
            return True

        t = dt.time()
        # NT geht ueber Mitternacht (z.B. 22:00 - 06:00)
        if self.nt_start > self.nt_end:
            return t >= self.nt_start or t < self.nt_end
        else:
            return self.nt_start <= t < self.nt_end

    def get_prices(self, hours_ahead: int = 48) -> List[Dict]:
        """Preise mit HT/NT-Unterscheidung."""
        prices = []
        now = datetime.now().replace(minute=0, second=0, microsecond=0)

        for h in range(hours_ahead):
            ts = now + timedelta(hours=h)
            is_nt = self._is_nt(ts)
            price = self.nt_eur if is_nt else self.ht_eur

            prices.append({
                'timestamp': ts,
                'price': price,
                'level': 'cheap' if is_nt else 'normal',
            })

        return prices

    def get_current_price(self) -> float:
        now = datetime.now()
        return self.nt_eur if self._is_nt(now) else self.ht_eur

    def get_cheapest_hours(self, count: int = 4, hours_ahead: int = 24) -> List[Dict]:
        """NT-Stunden sind die guenstigsten."""
        prices = self.get_prices(hours_ahead)
        now = datetime.now()
        upcoming = [p for p in prices if p['timestamp'] >= now]
        upcoming.sort(key=lambda x: x['price'])
        return upcoming[:count]

    def get_price_level(self) -> str:
        return 'cheap' if self._is_nt(datetime.now()) else 'normal'

    def is_dynamic(self) -> bool:
        return True  # Hat Zeitfenster → teilweise dynamisch

    def get_info(self) -> Dict:
        return {
            'provider': 'HT/NT Doppeltarif',
            'plan': f'HT {self.ht_ct:.1f}ct / NT {self.nt_ct:.1f}ct',
            'dynamic': True,
            'connected': True,
            'nt_start': self.nt_start.strftime('%H:%M'),
            'nt_end': self.nt_end.strftime('%H:%M'),
            'feed_in_ct': self.feed_in_ct,
        }

    @staticmethod
    def plugin_id() -> str:
        return 'dual_rate'

    @staticmethod
    def plugin_name() -> str:
        return 'HT/NT Doppeltarif (Tag/Nacht)'
