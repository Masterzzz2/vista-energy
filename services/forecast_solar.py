"""
Forecast.Solar + Open-Meteo Service
PV production forecast using Open-Meteo (free, no rate limits)
Fallback to Forecast.Solar if available.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Dict

import requests

logger = logging.getLogger(__name__)


class ForecastSolarService:
    """Handles PV forecast using Open-Meteo (primary) and Forecast.Solar (backup)."""

    BASE_URL = "https://api.forecast.solar/estimate"
    # Cache TTL: PV-Forecast aendert sich kaum oefter als stuendlich
    CACHE_TTL_SECONDS = 900  # 15 Minuten

    def __init__(self, api_key: str, lat: float, lon: float, kwp: float):
        self.api_key = api_key
        self.lat = lat
        self.lon = lon
        self.kwp = kwp
        self._cache = None
        self._cache_time = None

    def get_pv_forecast(self) -> Dict:
        """
        Get PV forecast for today and tomorrow.
        Tries Open-Meteo first (free, unlimited), falls back to Forecast.Solar.
        Returns dict with hourly estimates in Watt.
        Results are cached for 15 minutes to avoid API rate limits.
        """
        # Cache pruefen
        now = datetime.now()
        if (self._cache is not None
                and self._cache_time is not None
                and (now - self._cache_time).total_seconds() < self.CACHE_TTL_SECONDS
                and self._cache.get('total_w', 0) > 0):
            return self._cache

        # Try Open-Meteo first (free, unlimited)
        result = self._get_open_meteo_forecast()
        if result.get('total_w', 0) > 0:
            self._cache = result
            self._cache_time = now
            return result

        # Fallback to Forecast.Solar
        logger.info("Open-Meteo failed, trying Forecast.Solar")
        result = self._get_forecast_solar_estimate()
        if result.get('total_w', 0) > 0:
            self._cache = result
            self._cache_time = now
        elif self._cache is not None:
            # API-Fehler: alten Cache zurueckgeben statt leere Daten
            logger.warning("Forecast APIs failed, using stale cache")
            return self._cache
        return result
    
    def _get_open_meteo_forecast(self) -> Dict:
        """Get PV forecast from Open-Meteo (free, unlimited).
        Liefert heute UND morgen kWh getrennt + Sonnenstunden + Wettercodes."""
        try:
            url = f"https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": self.lat,
                "longitude": self.lon,
                "hourly": "shortwave_radiation,cloud_cover",
                "daily": "weather_code,sunshine_duration",
                "timezone": "Europe/Berlin",
                "forecast_days": 2
            }
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            times = data['hourly']['time']
            radiation = data['hourly']['shortwave_radiation']
            clouds = data['hourly'].get('cloud_cover') or [None]*len(times)
            daily = data.get('daily', {})

            today = datetime.now().strftime('%Y-%m-%d')
            tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

            day_wh = {today: 0.0, tomorrow: 0.0}
            day_peak = {today: 0, tomorrow: 0}
            hourly = []
            tomorrow_hourly = []
            efficiency = 0.8

            for t, rad, cc in zip(times, radiation, clouds):
                hour = int(t[11:13])
                day = t[:10]
                if 6 <= hour <= 20 and day in day_wh:
                    day_wh[day] += rad or 0
                    day_peak[day] = max(day_peak[day], rad or 0)
                    entry = {
                        'hour': hour,
                        'w': (rad or 0) * self.kwp * efficiency / 1000,
                        'timestamp': datetime.fromisoformat(t),
                        'cloud_cover': cc,
                    }
                    if day == today:
                        hourly.append(entry)
                    else:
                        tomorrow_hourly.append(entry)

            total_kwh_today = (day_wh[today] / 1000.0) * self.kwp * efficiency
            total_kwh_tomorrow = (day_wh[tomorrow] / 1000.0) * self.kwp * efficiency

            wcodes = daily.get('weather_code') or []
            sunh = daily.get('sunshine_duration') or []
            wc_today = wcodes[0] if len(wcodes) > 0 else None
            wc_tomorrow = wcodes[1] if len(wcodes) > 1 else None
            sunh_today = (sunh[0] / 3600.0) if len(sunh) > 0 else 0
            sunh_tomorrow = (sunh[1] / 3600.0) if len(sunh) > 1 else 0

            result = {
                'total_w': total_kwh_today * 1000,    # legacy
                'today_kwh': round(total_kwh_today, 2),
                'tomorrow_kwh': round(total_kwh_tomorrow, 2),
                'hourly': hourly,
                'tomorrow_hourly': tomorrow_hourly,
                'peak_w': day_peak[today] * self.kwp * efficiency / 1000,
                'weather_today': wc_today,
                'weather_tomorrow': wc_tomorrow,
                'sunshine_hours_today': round(sunh_today, 1),
                'sunshine_hours_tomorrow': round(sunh_tomorrow, 1),
                'timestamp': datetime.now(),
                'source': 'open-meteo'
            }
            logger.info(
                f"Open-Meteo PV: heute {total_kwh_today:.1f} kWh, morgen {total_kwh_tomorrow:.1f} kWh, "
                f"Sonne morgen {sunh_tomorrow:.1f} h"
            )
            return result

        except Exception as e:
            logger.error(f"Open-Meteo error: {e}")
            return {'total_w': 0, 'today_kwh': 0, 'tomorrow_kwh': 0,
                    'hourly': [], 'tomorrow_hourly': [],
                    'peak_w': 0, 'error': str(e)}
    
    def _get_forecast_solar_estimate(self) -> Dict:
        """Get PV forecast from Forecast.Solar API."""
        try:
            # Path parameters: lat, lon, declination (35°), azimuth (0°=South), kwp
            url = f"{self.BASE_URL}/{self.lat}/{self.lon}/35/0/{self.kwp}"
            
            headers = {}
            if self.api_key:
                headers['X-API-Key'] = self.api_key
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            result = {
                'total_w': 0,
                'hourly': [],
                'peak_w': 0,
                'timestamp': datetime.now()
            }
            
            if 'result' in data:
                result_data = data['result']
                wh_days = result_data.get('watt_hours', {})
                
                if 'day' in wh_days:
                    day_data = wh_days['day']
                    total_wh = 0
                    peak = 0
                    hourly = []
                    
                    for ts_str, wh_value in day_data.items():
                        try:
                            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                            hour = ts.hour
                            
                            total_wh += wh_value
                            if wh_value > peak:
                                peak = wh_value
                            
                            hourly.append({
                                'hour': hour,
                                'w': wh_value,
                                'timestamp': ts
                            })
                        except:
                            continue
                    
                    result['total_w'] = total_wh
                    result['peak_w'] = peak
                    result['hourly'] = hourly
                    result['source'] = 'forecast.solar'
            
            return result
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"Forecast.Solar HTTP error: {e}")
            return {'total_w': 0, 'hourly': [], 'peak_w': 0, 'error': str(e)}
        except Exception as e:
            logger.error(f"Forecast.Solar API error: {e}")
            return {'total_w': 0, 'hourly': [], 'peak_w': 0, 'error': str(e)}
    
    def get_today_peak_kwh(self) -> float:
        """Get estimated kWh production for today."""
        forecast = self.get_pv_forecast()
        return forecast.get('total_w', 0) / 1000
    
    def get_current_production_estimate(self) -> float:
        """Get current estimated PV production in W."""
        forecast = self.get_pv_forecast()
        
        now = datetime.now().hour
        for h in forecast.get('hourly', []):
            if h.get('hour') == now:
                return h.get('w', 0)
        
        return forecast.get('peak_w', 0) / 24  # Rough estimate if nothing else
    
    def get_48h_forecast(self) -> list:
        """Get 48-hour forecast with hourly breakdown."""
        forecast = self.get_pv_forecast()
        return forecast.get('hourly', [])