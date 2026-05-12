"""
Configuration utilities for Energy Optimizer
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration from environment variables."""
    
    # EVCC / Fronius
    EVCC_API_URL = os.getenv('EVCC_API_URL', 'http://192.168.1.x:7070')
    EVCC_API_TOKEN = os.getenv('EVCC_API_TOKEN', '')
    
    # Tibber
    TIBBER_API_TOKEN = os.getenv('TIBBER_API_TOKEN', '')
    
    # Forecast.Solar
    FORECAST_SOLAR_API_KEY = os.getenv('FORECAST_SOLAR_API_KEY', '')
    FORECAST_SOLAR_LAT = float(os.getenv('FORECAST_SOLAR_LAT', '52.52'))
    FORECAST_SOLAR_LON = float(os.getenv('FORECAST_SOLAR_LON', '13.405'))
    FORECAST_SOLAR_KWP = float(os.getenv('FORECAST_SOLAR_KWP', '6.0'))
    
    # Open-Meteo
    OPEN_METEO_LAT = float(os.getenv('OPEN_METEO_LAT', '52.52'))
    OPEN_METEO_LON = float(os.getenv('OPEN_METEO_LON', '13.405'))
    
    # Web UI
    WEB_PASSWORD = os.getenv('WEB_PASSWORD', 'admin')
    WEB_PORT = int(os.getenv('WEB_PORT', '8080'))
    
    # Battery settings
    BATTERY_CAPACITY_KWH = float(os.getenv('BATTERY_CAPACITY_KWH', '7.68'))
    BATTERY_USABLE_KWH = float(os.getenv('BATTERY_USABLE_KWH', '7.0'))
    BATTERY_MIN_SOC = float(os.getenv('BATTERY_MIN_SOC', '0.1'))
    BATTERY_MAX_SOC = float(os.getenv('BATTERY_MAX_SOC', '0.95'))
    
    # Wattpilot
    WATTPLOT_IP = os.getenv('WATTPLOT_IP', '192.168.1.x')
    WATTPLOT_MAX_KW = float(os.getenv('WATTPLOT_MAX_KW', '22'))
    
    @classmethod
    def validate(cls) -> list:
        """Check for missing required configuration."""
        missing = []
        
        if not cls.TIBBER_API_TOKEN:
            missing.append('TIBBER_API_TOKEN')
        
        if not cls.EVCC_API_URL or cls.EVCC_API_URL == 'http://192.168.1.x:7070':
            missing.append('EVCC_API_URL')
        
        return missing