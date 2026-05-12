"""
Scheduler-Modul für Energy Optimizer
APScheduler für 15-Minuten Datenakquise und stündliche Optimierung
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class Scheduler:
    """APScheduler für Energy Optimizer"""
    
    def __init__(self, db, tibber, fronius, wattpilot, weather, forecast_solar, optimizer):
        self.db = db
        self.tibber = tibber
        self.fronius = fronius
        self.wattpilot = wattpilot
        self.weather = weather
        self.forecast = forecast_solar
        self.optimizer = optimizer
        self.scheduler = BackgroundScheduler()
    
    def collect_data(self):
        """Sammelt alle 15 Minuten Daten"""
        logger.info("Scheduler: Sammle Daten...")
        
        try:
            now_str = datetime.now().isoformat()
            
            # Tibber Preise
            try:
                prices = self.tibber.get_current_prices()
                self.db.insert_tibber_prices(prices)
            except Exception as e:
                logger.error(f"Tibber Fehler: {e}")
            
            # PV + Batterie + Verbrauch von Fronius
            try:
                pv = self.fronius.get_pv_production()
                self.db.insert_pv_data(pv)
                
                battery = self.fronius.get_battery_status()
                self.db.insert_battery_data(battery)
                
                consumption = self.fronius.get_house_consumption()
                self.db.insert_consumption_data(consumption)
            except Exception as e:
                logger.error(f"Fronius Fehler: {e}")
            
            # Wattpilot
            try:
                wattpilot = self.wattpilot.get_status()
                self.db.insert_wattpilot_data(wattpilot)
            except Exception as e:
                logger.error(f"Wattpilot Fehler: {e}")
            
            # Wetter
            try:
                weather = self.weather.get_current_weather()
                self.db.insert_weather_data(weather)
            except Exception as e:
                logger.error(f"Wetter Fehler: {e}")
            
            # PV Prognose (nur stündlich)
            if datetime.now().minute < 5:
                try:
                    forecast = self.forecast.get_forecast()
                    self.db.insert_pv_forecast(forecast)
                except Exception as e:
                    logger.error(f"Forecast Fehler: {e}")
            
            logger.info("Daten erfolgreich gesammelt")
            
        except Exception as e:
            logger.error(f"Scheduler Datenfehler: {e}")
    
    def run_optimization(self):
        """Führt stündlich Optimierung aus"""
        logger.info("Scheduler: Optimierung...")
        
        try:
            mode = self.db.get_config("mode", "automatic")
            
            if mode == "manual":
                settings = self.db.get_manual_settings()
                if settings.get('enabled'):
                    self.optimizer.apply_manual_settings(settings)
            
            elif mode == "automatic":
                self.optimizer.optimize_automatic()
            
            elif mode == "ai":
                self.optimizer.optimize_ai()
            
            logger.info("Optimierung abgeschlossen")
            
        except Exception as e:
            logger.error(f"Optimierung Fehler: {e}")
    
    def update_profile(self):
        """Tägliches Profil-Update (Lernmodus)"""
        logger.info("Scheduler: Aktualisiere Verbrauchsprofil...")
        
        try:
            self.db.update_consumption_profile()
            logger.info("Profil aktualisiert")
        except Exception as e:
            logger.error(f"Profil Update Fehler: {e}")
    
    def daily_report(self):
        """Täglicher Bericht um 6 Uhr"""
        logger.info("Scheduler: Erstelle Tagesbericht...")
        
        try:
            report = self.optimizer.generate_daily_report()
            print(report['text'])
            
            # Speichere Bericht
            self.db.set_config("last_report", report['text'])
            
        except Exception as e:
            logger.error(f"Bericht Fehler: {e}")
    
    def start(self):
        """Startet den Scheduler"""
        logger.info("Starte Scheduler...")
        
        # Alle 15 Minuten: Daten sammeln
        self.scheduler.add_job(
            self.collect_data,
            IntervalTrigger(minutes=15),
            id='collect_data',
            name='Daten sammeln alle 15min',
            replace_existing=True
        )
        
        # Alle Stunden: Optimierung
        self.scheduler.add_job(
            self.run_optimization,
            CronTrigger(minute=0),
            id='optimization',
            name='Stündliche Optimierung',
            replace_existing=True
        )
        
        # Täglich um 6 Uhr: Profil updaten + Bericht
        self.scheduler.add_job(
            self.update_profile,
            CronTrigger(hour=6, minute=5),
            id='update_profile',
            name='Tägliches Profil-Update',
            replace_existing=True
        )
        
        self.scheduler.add_job(
            self.daily_report,
            CronTrigger(hour=6, minute=10),
            id='daily_report',
            name='Tagesbericht',
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info("Scheduler gestartet")
    
    def stop(self):
        """Stoppt den Scheduler"""
        self.scheduler.shutdown()
        logger.info("Scheduler gestoppt")


from datetime import datetime
