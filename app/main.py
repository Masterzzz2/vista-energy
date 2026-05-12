#!/usr/bin/env python3
"""
Energy Optimizer - Main Application
Steuert Fronius GEN24, BYD Battery, Wattpilot mit Tibber-Tarif
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Pfad zur .env Datei
BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

# Logging konfigurieren
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "logs" / "app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Importiere Module
from app.database import Database
from app.api_clients import TibberClient, FroniusClient, WattpilotClient, WeatherClient, ForecastSolarClient
from app.optimizer import EnergyOptimizer
from app.scheduler import Scheduler
from app.web import create_app


class EnergyOptimizerApp:
    """Hauptklasse für den Energy Optimizer"""
    
    def __init__(self):
        self.base_dir = BASE_DIR
        self.db = Database(self.base_dir / "data" / "energy.db")
        self.mode = "automatic"  # manual, automatic, ai
        
        # API Clients initialisieren
        self.tibber = TibberClient(
            api_token=os.getenv("TIBBER_TOKEN")
        )
        self.fronius = FroniusClient(
            ip=os.getenv("FRONIUS_GEN24_IP", "192.168.1.80")
        )
        self.wattpilot = WattpilotClient(
            ip=os.getenv("WATTPILOT_IP", "192.168.1.80")
        )
        self.weather = WeatherClient(
            latitude=float(os.getenv("LATITUDE", "47.91")),
            longitude=float(os.getenv("LONGITUDE", "11.09"))
        )
        self.forecast_solar = ForecastSolarClient(
            api_key=os.getenv("FORECAST_SOLAR_API_KEY"),
            latitude=float(os.getenv("LATITUDE", "47.91")),
            longitude=float(os.getenv("LONGITUDE", "11.09")),
            power_kwp=float(os.getenv("PV_POWER_KWP", "6.0"))
        )
        
        # Optimizer
        self.optimizer = EnergyOptimizer(self.db, self.tibber, self.forecast_solar)
        
        # Scheduler
        self.scheduler = Scheduler(
            db=self.db,
            tibber=self.tibber,
            fronius=self.fronius,
            wattpilot=self.wattpilot,
            weather=self.weather,
            forecast_solar=self.forecast_solar,
            optimizer=self.optimizer
        )
        
        logger.info("Energy Optimizer gestartet")
    
    def collect_all_data(self):
        """Sammelt alle Daten von allen Quellen"""
        logger.info("Sammle Daten von allen Quellen...")
        
        try:
            # Tibber Preise
            prices = self.tibber.get_current_prices()
            self.db.insert_tibber_prices(prices)
            
            # PV Erzeugung (von Fronius)
            pv_data = self.fronius.get_pv_production()
            self.db.insert_pv_data(pv_data)
            
            # Batterie Status
            battery_data = self.fronius.get_battery_status()
            self.db.insert_battery_data(battery_data)
            
            # Hausverbrauch
            consumption = self.fronius.get_house_consumption()
            self.db.insert_consumption_data(consumption)
            
            # Wattpilot Status
            wattpilot_data = self.wattpilot.get_status()
            self.db.insert_wattpilot_data(wattpilot_data)
            
            # Wetter
            weather = self.weather.get_current_weather()
            self.db.insert_weather_data(weather)
            
            # PV Prognose
            forecast = self.forecast_solar.get_forecast()
            self.db.insert_pv_forecast(forecast)
            
            logger.info("Alle Daten erfolgreich gesammelt")
            return True
            
        except Exception as e:
            logger.error(f"Fehler beim Sammeln der Daten: {e}")
            return False
    
    def run_optimization(self):
        """Führt die Optimierung basierend auf dem aktuellen Modus aus"""
        logger.info(f"Optimierung läuft im Modus: {self.mode}")
        
        try:
            if self.mode == "manual":
                # Manueller Modus - Einstellungen aus DB lesen
                settings = self.db.get_manual_settings()
                return self.optimizer.apply_manual_settings(settings)
                
            elif self.mode == "automatic":
                # Automatischer Modus - Regelbasiert
                return self.optimizer.optimize_automatic()
                
            elif self.mode == "ai":
                # KI Modus - Lernbasiert
                return self.optimizer.optimize_ai()
                
        except Exception as e:
            logger.error(f"Fehler bei der Optimierung: {e}")
            return False
    
    def set_mode(self, mode):
        """Setzt den Betriebsmodus"""
        valid_modes = ["manual", "automatic", "ai"]
        if mode in valid_modes:
            self.mode = mode
            self.db.set_config("mode", mode)
            logger.info(f"Modus geändert zu: {mode}")
            return True
        return False
    
    def get_status(self):
        """Gibt den aktuellen Status zurück"""
        return {
            "mode": self.mode,
            "battery": self.db.get_latest_battery(),
            "pv_production": self.db.get_latest_pv(),
            "tibber_price": self.db.get_current_tibber_price(),
            "wattpilot": self.db.get_latest_wattpilot(),
            "learning_days": self.db.get_learning_days(),
            "today_savings": self.db.get_today_savings()
        }
    
    def get_daily_report(self):
        """Erstellt den täglichen Bericht"""
        return self.optimizer.generate_daily_report()
    
    def start(self):
        """Startet die Anwendung"""
        logger.info("Starte Energy Optimizer...")
        
        # Initiale Datensammlung
        self.collect_all_data()
        
        # Scheduler starten
        self.scheduler.start()
        
        # Web UI starten
        app = create_app(self)
        app.run(
            host="0.0.0.0",
            port=int(os.getenv("WEB_PORT", 8080)),
            debug=False,
            use_reloader=False
        )


def main():
    """Main Entry Point"""
    try:
        app = EnergyOptimizerApp()
        app.start()
    except KeyboardInterrupt:
        logger.info("Energy Optimizer gestoppt")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Kritischer Fehler: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
