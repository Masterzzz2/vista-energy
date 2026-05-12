"""
Weather Service
Open-Meteo API for weather data (free, no API key needed)
"""

import os
import logging
from datetime import datetime
from typing import Dict

import requests

logger = logging.getLogger(__name__)


class WeatherService:
    """Handles Open-Meteo API for weather data."""

    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    CACHE_TTL_SECONDS = 600  # 10 Minuten — Wetter aendert sich langsam

    def __init__(self, lat: float, lon: float):
        self.lat = lat
        self.lon = lon
        self._cache_current = None
        self._cache_current_time = None
        self._cache_forecast = None
        self._cache_forecast_time = None

    def get_current_weather(self) -> Dict:
        """Get current weather conditions (cached for 10 min)."""
        now = datetime.now()
        if (self._cache_current is not None
                and self._cache_current_time is not None
                and (now - self._cache_current_time).total_seconds() < self.CACHE_TTL_SECONDS
                and 'error' not in self._cache_current):
            return self._cache_current
        try:
            params = {
                'latitude': self.lat,
                'longitude': self.lon,
                'current': 'temperature_2m,weather_code,cloud_cover',
                'timezone': 'Europe/Berlin'
            }
            
            response = requests.get(self.BASE_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            current = data.get('current', {})
            
            result = {
                'temperature': current.get('temperature_2m', 0),
                'condition': self._weather_code_to_condition(current.get('weather_code', 0)),
                'cloud_cover': current.get('cloud_cover', 0),
                'timestamp': datetime.now()
            }
            self._cache_current = result
            self._cache_current_time = datetime.now()
            return result

        except Exception as e:
            logger.error(f"Open-Meteo API error: {e}")
            if self._cache_current is not None:
                logger.warning("Using stale weather cache after API error")
                return self._cache_current
            return {
                'temperature': 0,
                'condition': 'unknown',
                'cloud_cover': 0,
                'error': str(e)
            }
    
    def get_forecast(self, days: int = 2) -> Dict:
        """Get weather forecast for next N days (cached for 10 min)."""
        now = datetime.now()
        if (self._cache_forecast is not None
                and self._cache_forecast_time is not None
                and (now - self._cache_forecast_time).total_seconds() < self.CACHE_TTL_SECONDS
                and 'error' not in self._cache_forecast):
            return self._cache_forecast
        try:
            params = {
                'latitude': self.lat,
                'longitude': self.lon,
                'hourly': 'temperature_2m,weather_code,cloud_cover,solar_radiation',
                'forecast_days': days,
                'timezone': 'Europe/Berlin'
            }
            
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            hourly = data.get('hourly', {})
            
            forecast = []
            for i, time in enumerate(hourly.get('time', [])):
                forecast.append({
                    'timestamp': datetime.fromisoformat(time),
                    'temperature': hourly.get('temperature_2m', [0] * (i + 1))[i],
                    'condition': self._weather_code_to_condition(
                        hourly.get('weather_code', [0] * (i + 1))[i]
                    ),
                    'cloud_cover': hourly.get('cloud_cover', [0] * (i + 1))[i],
                    'solar_radiation': hourly.get('solar_radiation', [0] * (i + 1))[i]
                })
            
            result = {'forecast': forecast}
            self._cache_forecast = result
            self._cache_forecast_time = datetime.now()
            return result

        except Exception as e:
            logger.error(f"Open-Meteo forecast error: {e}")
            if self._cache_forecast is not None:
                logger.warning("Using stale forecast cache after API error")
                return self._cache_forecast
            return {'forecast': [], 'error': str(e)}
    
    def _weather_code_to_condition(self, code: int) -> str:
        """Convert WMO weather code to simple condition string."""
        mapping = {
            0: 'clear',
            1: 'clear',
            2: 'partly_cloudy',
            3: 'cloudy',
            45: 'fog',
            48: 'fog',
            51: 'drizzle',
            53: 'drizzle',
            55: 'drizzle',
            61: 'rain',
            63: 'rain',
            65: 'rain',
            71: 'snow',
            73: 'snow',
            75: 'snow',
            80: 'showers',
            81: 'showers',
            82: 'showers',
            95: 'thunderstorm',
            96: 'thunderstorm',
            99: 'thunderstorm'
        }
        return mapping.get(code, 'unknown')