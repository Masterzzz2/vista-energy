"""
Vista-Energy — Tibber Plugin

Dynamischer Stromtarif via Tibber GraphQL API.
Liefert stuendliche Preise fuer heute und morgen (ab ~13 Uhr).

Konfiguration (config.yaml):
  tariff:
    type: tibber
    api_key: xxxxx-xxxx-xxxx-xxxx-xxxxxxxxx
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from services.tariffs.base import TariffBase

logger = logging.getLogger(__name__)


class TibberTariff(TariffBase):
    """Tibber dynamischer Stromtarif via GraphQL API."""

    GRAPHQL_URL = "https://api.tibber.com/v1-beta/gql"

    PRICE_QUERY = """
    query {
        viewer {
            homes {
                currentSubscription {
                    priceInfo {
                        today {
                            total
                            startsAt
                        }
                        tomorrow {
                            total
                            startsAt
                        }
                    }
                }
            }
        }
    }
    """

    def __init__(self, api_key: str = None, **kwargs):
        import os
        self.api_key = api_key or os.getenv('TIBBER_API_TOKEN', '')
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self._price_cache = []
        self._cache_time = None
        self._cache_ttl = 300  # 5 Minuten Cache

    # ==================================================================
    # TariffBase — Pflicht-Methoden
    # ==================================================================

    def get_prices(self, hours_ahead: int = 48) -> List[Dict]:
        """Strompreise von Tibber (heute + morgen)."""
        # Cache pruefen
        if self._price_cache and self._cache_time:
            elapsed = (datetime.now() - self._cache_time).total_seconds()
            if elapsed < self._cache_ttl:
                return self._price_cache

        result = self._graphql_query(self.PRICE_QUERY)

        if not result or 'errors' in result:
            logger.warning(f"Tibber API Fehler: {result}")
            return self._price_cache  # Alten Cache zurueckgeben

        try:
            homes = result['data']['viewer']['homes']
            if not homes:
                return []

            price_info = homes[0]['currentSubscription']['priceInfo']
            prices = []

            for period in ['today', 'tomorrow']:
                entries = price_info.get(period, []) or []
                for p in entries:
                    ts = datetime.fromisoformat(
                        p['startsAt'].replace('Z', '+00:00')
                    )
                    price = p['total']
                    prices.append({
                        'timestamp': ts,
                        'price': price,
                        'level': self._classify_price(price),
                    })

            self._price_cache = prices
            self._cache_time = datetime.now()
            return prices

        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Tibber-Antwort parsen fehlgeschlagen: {e}")
            return self._price_cache

    def get_current_price(self) -> float:
        """Aktueller Tibber-Preis in EUR/kWh."""
        prices = self.get_prices()
        if not prices:
            return 0.0

        now = datetime.now()
        current = None
        for p in prices:
            if p['timestamp'] <= now:
                current = p
        return current['price'] if current else prices[0]['price']

    # ==================================================================
    # Zusatz-Methoden
    # ==================================================================

    def get_info(self) -> Dict:
        return {
            'provider': 'Tibber',
            'plan': 'Pulse',
            'dynamic': True,
            'connected': self._test_connection(),
        }

    def get_home_id(self) -> Optional[str]:
        """Tibber Home-ID abrufen."""
        query = """
        query {
            viewer {
                homes {
                    id
                }
            }
        }
        """
        result = self._graphql_query(query)
        try:
            return result['data']['viewer']['homes'][0]['id']
        except (KeyError, IndexError, TypeError):
            return None

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'tibber'

    @staticmethod
    def plugin_name() -> str:
        return 'Tibber (dynamisch)'

    # ==================================================================
    # Interne Methoden
    # ==================================================================

    def _graphql_query(self, query: str, variables: dict = None) -> dict:
        """GraphQL-Query an Tibber senden."""
        try:
            resp = requests.post(
                self.GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Tibber GraphQL Fehler: {e}")
            return {"data": None, "errors": [str(e)]}

    def _test_connection(self) -> bool:
        """API-Verbindung testen."""
        result = self._graphql_query("{ viewer { name } }")
        return bool(result.get('data'))
