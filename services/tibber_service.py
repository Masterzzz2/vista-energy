"""
Tibber Service
GraphQL API client for Tibber
"""

import os
import logging
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)


class TibberService:
    """Handles Tibber API interactions."""
    
    GRAPHQL_URL = "https://api.tibber.com/v1-beta/gql"
    
    def __init__(self, api_token: str = None):
        self.api_token = api_token or os.getenv('TIBBER_API_TOKEN')
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
    
    def query(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query."""
        try:
            response = requests.post(
                self.GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Tibber query failed: {e}")
            return {"data": None, "errors": [str(e)]}
    
    def get_current_prices(self) -> list:
        """
        Get current and upcoming electricity prices.
        Returns list of dicts with timestamp, price (EUR/kWh), and level.
        """
        query = """
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
        
        result = self.query(query)
        
        if 'errors' in result:
            logger.warning(f"Tibber API errors: {result['errors']}")
            return []
        
        try:
            homes = result['data']['viewer']['homes']
            if not homes:
                return []
            
            prices = []
            
            # Today's prices
            today = homes[0]['currentSubscription']['priceInfo']['today']
            for p in today:
                prices.append({
                    'timestamp': datetime.fromisoformat(p['startsAt'].replace('Z', '+00:00')),
                    'price': p['total'],  # Already in EUR/kWh
                    'level': self._price_level(p['total'])
                })
            
            # Tomorrow's prices
            tomorrow = homes[0]['currentSubscription']['priceInfo'].get('tomorrow', [])
            for p in tomorrow:
                prices.append({
                    'timestamp': datetime.fromisoformat(p['startsAt'].replace('Z', '+00:00')),
                    'price': p['total'],
                    'level': self._price_level(p['total'])
                })
            
            return prices
            
        except (KeyError, IndexError) as e:
            logger.error(f"Failed to parse Tibber response: {e}")
            return []
    
    def get_current_price(self) -> float:
        """Get current electricity price in EUR/kWh."""
        prices = self.get_current_prices()
        if not prices:
            return 0
        
        now = datetime.now()
        for p in prices:
            if p['timestamp'] <= now:
                return p['price']
        
        return prices[0]['price']
    
    def get_cheapest_hours(self, count: int = 4, hours_ahead: int = 24) -> list:
        """Return the cheapest hours in the upcoming period."""
        prices = self.get_current_prices()
        
        now = datetime.now()
        cutoff = now + timedelta(hours=hours_ahead)
        
        upcoming = [
            p for p in prices
            if now <= p['timestamp'] <= cutoff
        ]
        
        # Sort by price
        upcoming.sort(key=lambda x: x['price'])
        
        return upcoming[:count]
    
    def get_price_level(self) -> str:
        """Return current price level: cheap, normal, expensive."""
        price = self.get_current_price()
        return self._price_level(price)
    
    def _price_level(self, price: float) -> str:
        """Determine price level based on EUR/kWh."""
        if price < 0.15:
            return 'cheap'
        elif price < 0.25:
            return 'normal'
        else:
            return 'expensive'
    
    def get_home_id(self) -> str:
        """Get the home ID for the user."""
        query = """
        query {
            viewer {
                homes {
                    id
                }
            }
        }
        """
        result = self.query(query)
        try:
            homes = result['data']['viewer']['homes']
            if homes:
                return homes[0]['id']
        except (KeyError, IndexError):
            pass
        return None