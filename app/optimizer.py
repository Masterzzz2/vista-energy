"""
Energy Optimizer - Optimierungslogik
3 Modi: Manuell, Automatisch, KI-unterstützt
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


class EnergyOptimizer:
    """Optimiert Energieflüsse basierend auf Tibber Preisen und PV Prognose"""
    
    # Preis-Schwellenwerte in EUR/kWh
    PRICE_CHEAP = 0.15
    PRICE_NORMAL = 0.25
    PRICE_EXPENSIVE = 0.35
    
    # Batterie Schwellen
    BATTERY_SOC_LOW = 20
    BATTERY_SOC_OK = 50
    BATTERY_SOC_GOOD = 80
    
    def __init__(self, db, tibber_client, forecast_client):
        self.db = db
        self.tibber = tibber_client
        self.forecast = forecast_client
    
    # ==================== MANUELLER MODUS ====================
    
    def apply_manual_settings(self, settings: dict) -> dict:
        """Wendet manuelle Einstellungen an"""
        result = {
            "success": True,
            "actions": [],
            "battery_soc_target": settings.get('battery_soc_target', 100),
            "charge_power_w": settings.get('charge_power_w', 0),
            "discharge_lock": settings.get('discharge_lock', False)
        }
        
        # Hier würden später API-Calls zu GEN24 und Wattpilot gehen
        # z.B. über self.fronius und self.wattpilot
        
        logger.info(f"Manuelle Einstellungen angewendet: {settings}")
        return result
    
    # ==================== AUTOMATISCHER MODUS ====================
    
    def optimize_automatic(self) -> dict:
        """Regelbasierte Optimierung"""
        logger.info("Starte automatische Optimierung...")
        
        result = {
            "success": True,
            "actions": [],
            "reasoning": [],
            "recommendations": []
        }
        
        # Hole aktuelle Daten
        battery = self.db.get_latest_battery()
        tibber_price = self.db.get_current_tibber_price()
        pv_forecast = self.forecast.get_forecast()
        weather = self._get_current_weather()
        
        if not battery:
            result["success"] = False
            result["reasoning"].append("Keine Batteriedaten verfügbar")
            return result
        
        soc = battery.get('soc_percent', 50)
        battery_power = battery.get('power_w', 0)
        
        # Regel 1: PV-Überschuss vorhanden -> Batterie laden
        pv_power = self.db.get_latest_pv().get('power_w', 0) if self.db.get_latest_pv() else 0
        house_power = self.db.get_latest_pv().get('power_w', 0) if self.db.get_latest_pv() else 0
        
        # Hole Hausverbrauch
        # PV-Überschuss = PV - Hausverbrauch
        pv_excess = pv_power - house_power
        
        if pv_excess > 1000 and soc < 95:  # >1kW Überschuss
            result["actions"].append({
                "target": "battery",
                "action": "charge",
                "power_w": min(pv_excess, 5000),
                "reason": f"PV-Überschuss {pv_excess}W, SoC {soc}%"
            })
            result["reasoning"].append(f"Batterie wird geladen: PV-Überschuss {pv_excess}W")
        
        # Regel 2: Niedriger Preis und Batterie nicht voll -> Laden
        if tibber_price:
            price = tibber_price.get('price', 0.25)
            
            if price < self.PRICE_CHEAP and soc < 90:
                # Sehr günstiger Strom -> Batterie voll machen
                result["actions"].append({
                    "target": "battery",
                    "action": "charge",
                    "power_w": 5000,
                    "reason": f"Sehr günstiger Strom ({price:.3f} €/kWh)"
                })
                result["reasoning"].append(f"Batterie laden: Preis günstig ({price:.3f} €/kWh)")
            
            elif price > self.PRICE_EXPENSIVE and soc > self.BATTERY_SOC_OK:
                # Teurer Strom -> Batterie entladen
                result["actions"].append({
                    "target": "battery",
                    "action": "discharge",
                    "power_w": 3000,
                    "reason": f"Teurer Strom ({price:.3f} €/kWh)"
                })
                result["reasoning"].append(f"Batterie entladen: Preis hoch ({price:.3f} €/kWh)")
        
        # Regel 3: Auto laden nur wenn günstig oder Batterie voll
        wattpilot = self.db.get_latest_wattpilot()
        if wattpilot and wattpilot.get('plugged_in'):
            car_needs_charge = wattpilot.get('charge_limit', 100) > 20
            
            if car_needs_charge:
                if price < self.PRICE_CHEAP or soc > 90:
                    result["actions"].append({
                        "target": "wattpilot",
                        "action": "charge",
                        "power_w": 22000,
                        "reason": f"Auto laden: Preis={price:.3f}, SoC={soc}%"
                    })
                    result["reasoning"].append("Auto wird geladen (günstig oder SoC hoch)")
                else:
                    result["actions"].append({
                        "target": "wattpilot",
                        "action": "wait",
                        "reason": f"Warte auf günstigeren Strom (aktuell {price:.3f} €/kWh)"
                    })
                    result["reasoning"].append(f"Auto warten: Preis zu hoch ({price:.3f} €/kWh)")
        
        # Morgen-Empfehlungen basierend auf Prognose
        tomorrow_prices = self.db.get_hourly_prices_tomorrow()
        if tomorrow_prices:
            cheap_hours = [p['timestamp'] for p in tomorrow_prices if p['price'] < self.PRICE_CHEAP]
            if cheap_hours:
                result["recommendations"].append({
                    "type": "charging",
                    "message": f"Günstige Stunden morgen: {', '.join(cheap_hours[:3])}",
                    "hours": cheap_hours[:3]
                })
        
        logger.info(f"Optimierung abgeschlossen: {len(result['actions'])} Aktionen")
        return result
    
    # ==================== KI MODUS ====================
    
    def optimize_ai(self) -> dict:
        """KI-gestützte Optimierung mit Lernprofil"""
        logger.info("Starte KI-Optimierung...")
        
        result = {
            "success": True,
            "actions": [],
            "reasoning": [],
            "confidence": 0.0,
            "learning_days": self.db.get_learning_days()
        }
        
        # Prüfe ob genug Daten vorhanden (min 7 Tage)
        if self.db.get_learning_days() < 7:
            result["reasoning"].append(f"Noch {7 - self.db.get_learning_days()} Tage bis KI bereit")
            result["confidence"] = 0.3
            # Fallback zu automatisch
            return {**result, **self.optimize_automatic()}
        
        # Lade Verbrauchsprofil
        profile = self.db.get_consumption_profile()
        
        if profile.empty:
            result["confidence"] = 0.5
            return {**result, **self.optimize_automatic()}
        
        # Hole Kontext
        current_hour = datetime.now().hour
        current_dow = datetime.now().weekday()
        
        # Finde typischen Verbrauch für diese Stunde
        profile_match = profile[(profile['hour'] == current_hour) & (profile['day_of_week'] == current_dow)]
        
        typical_consumption = 500  # Default 500W
        if not profile_match.empty:
            typical_consumption = profile_match.iloc[0]['avg_power_w']
        
        # PV Prognose für heute
        pv_today_estimate = self.forecast.estimate_pv_today()
        
        # Tibber Preis jetzt
        tibber_price = self.db.get_current_tibber_price()
        price_now = tibber_price['price'] if tibber_price else 0.25
        
        # Batterie Status
        battery = self.db.get_latest_battery()
        soc = battery.get('soc_percent', 50) if battery else 50
        
        # KI Entscheidungslogik
        # -------------------
        
        # Score berechnen (höher = besser zum Laden)
        charge_score = 0
        discharge_score = 0
        
        # Preis-Komponente
        if price_now < self.PRICE_CHEAP:
            charge_score += 40
        elif price_now < self.PRICE_NORMAL:
            charge_score += 20
        elif price_now > self.PRICE_EXPENSIVE:
            discharge_score += 30
        
        # PV-Komponente
        if pv_today_estimate > 15:  # >15kWh erwartet
            charge_score += 20
        
        # SoC-Komponente
        if soc < 30:
            charge_score += 25
        elif soc > 80:
            discharge_score += 20
        
        # Verbrauchs-Komponente
        if typical_consumption > 2000:  # Hoher Verbrauch erwartet
            discharge_score += 15
        
        # Auto laden?
        wattpilot = self.db.get_latest_wattpilot()
        car_plugged = wattpilot and wattpilot.get('plugged_in', 0) == 1
        
        if car_plugged:
            if charge_score > discharge_score + 20:
                result["actions"].append({
                    "target": "wattpilot",
                    "action": "charge",
                    "power_w": min(22000, 3 * 230),  # 3-phasig
                    "reason": f"KІ entschieden: Laden (Score {charge_score})"
                })
                result["confidence"] = min(0.9, charge_score / 100)
            else:
                result["actions"].append({
                    "target": "wattpilot",
                    "action": "wait",
                    "reason": f"KІ entschieden: Warten (Score Lad={charge_score}, Entl={discharge_score})"
                })
                result["confidence"] = 0.6
        
        # Batterie Aktion
        if charge_score > discharge_score + 10:
            result["actions"].append({
                "target": "battery",
                "action": "charge",
                "power_w": 5000,
                "reason": f"KІ: Laden (Score {charge_score})"
            })
        elif discharge_score > charge_score + 10:
            result["actions"].append({
                "target": "battery",
                "action": "discharge",
                "power_w": 3000,
                "reason": f"KІ: Entladen (Score {discharge_score})"
            })
        
        result["reasoning"].append(f"Scores: Laden={charge_score}, Entladen={discharge_score}")
        result["reasoning"].append(f"Typischer Verbrauch jetzt: {typical_consumption:.0f}W")
        result["reasoning"].append(f"PV-Prognose heute: {pv_today_estimate:.1f}kWh")
        
        logger.info(f"КІ-Optimierung: Confidence={result['confidence']:.2f}")
        return result
    
    # ==================== TÄGLICHER BERICHT ====================
    
    def generate_daily_report(self) -> dict:
        """Erstellt täglichen Bericht"""
        logger.info("Erstelle täglichen Bericht...")
        
        # Hole Statistiken
        stats = self.db.get_daily_stats(days=1)
        
        savings = self.db.get_today_savings()
        pv_today = savings['pv_kwh']
        
        # Prognose für morgen
        pv_tomorrow = self.forecast.estimate_pv_today()
        tomorrow_prices = self.db.get_hourly_prices_tomorrow()
        
        # Finde beste Ladezeiten
        best_charge_hours = []
        if tomorrow_prices:
            sorted_prices = sorted(tomorrow_prices, key=lambda x: x['price'])[:3]
            best_charge_hours = [p['timestamp'] for p in sorted_prices]
        
        # hole Tibber-Preis jetzt
        tibber_price = self.db.get_current_tibber_price()
        price_now = tibber_price['price'] if tibber_price else 0
        
        report = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "pv_today_kwh": pv_today,
            "savings_eur": savings['savings_eur'],
            "price_now_ct": round(price_now * 100, 2),
            "pv_tomorrow_kwh": pv_tomorrow,
            "best_charge_hours": best_charge_hours,
            "battery_soc": self.db.get_latest_battery().get('soc_percent') if self.db.get_latest_battery() else 0,
            "wattpilot_status": "laden" if self.db.get_latest_wattpilot().get('power_w', 0) > 100 else "warten" if self.db.get_latest_wattpilot() else "unbekannt"
        }
        
        # Text-Report
        report_text = f"""
╔══════════════════════════════════════════════════════════════╗
║          🌞 ENERGY OPTIMIZER - TAGESBERICHT                  ║
║          {report['date']}                                     ║
╠══════════════════════════════════════════════════════════════╣
║  PV Heute:        {report['pv_today_kwh']:.1f} kWh                         ║
║  Ersparnis:       {report['savings_eur']:.2f} €                          ║
║  Aktueller Preis: {report['price_now_ct']:.1f} ct/kWh                     ║
║  Batterie SoC:    {report['battery_soc']:.0f}%                             ║
║  Wattpilot:       {report['wattpilot_status']}                       ║
╠══════════════════════════════════════════════════════════════╣
║  📅 MORGEN PROGNOSE                                        ║
║  PV-Ertrag:       {report['pv_tomorrow_kwh']:.1f} kWh                         ║
║  Beste Ladezeiten:                                        ║
"""
        
        for i, hour in enumerate(best_charge_hours[:3]):
            try:
                dt = datetime.fromisoformat(hour.replace('Z', '+00:00'))
                report_text += f"║    {i+1}. {dt.strftime('%H:%M')} Uhr                                    ║\n"
            except:
                report_text += f"║    {i+1}. {hour[:16]}                            ║\n"
        
        report_text += """╚══════════════════════════════════════════════════════════════╝"""
        
        report['text'] = report_text
        logger.info("Tagesbericht erstellt")
        return report
    
    def _get_current_weather(self) -> dict:
        """Hilfsmethode für Wetter"""
        # Würde WeatherClient verwenden
        return {}
