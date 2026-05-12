"""
Datenbank-Modul für Energy Optimizer
SQLite Datenbank für Historien- und Profildaten
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class Database:
    """SQLite Datenbank für Energy Optimizer"""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_database()
    
    def get_connection(self):
        """Gibt eine Datenbankverbindung zurück"""
        return sqlite3.connect(self.db_path)
    
    def init_database(self):
        """Initialisiert die Datenbank mit allen Tabellen"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Tibber Preise
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tibber_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT DEFAULT 'EUR',
                level TEXT,
                UNIQUE(timestamp)
            )
        """)
        
        # PV Produktion
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pv_production (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL UNIQUE,
                power_w REAL NOT NULL,
                energy_wh REAL DEFAULT 0
            )
        """)
        
        # Batterie Status
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS battery_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL UNIQUE,
                soc_percent REAL NOT NULL,
                capacity_kwh REAL,
                power_w REAL,
                status TEXT
            )
        """)
        
        # Hausverbrauch
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS house_consumption (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL UNIQUE,
                power_w REAL NOT NULL,
                energy_wh REAL DEFAULT 0
            )
        """)
        
        # Wattpilot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wattpilot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL UNIQUE,
                power_w REAL DEFAULT 0,
                energy_wh REAL DEFAULT 0,
                status TEXT,
                charge_limit REAL,
                plugged_in INTEGER DEFAULT 0
            )
        """)
        
        # Wetter
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weather (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL UNIQUE,
                temperature_c REAL,
                cloud_percent INTEGER,
                radiation_wm2 INTEGER,
                precipitation_mm REAL,
                weather_code INTEGER
            )
        """)
        
        # PV Prognose
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pv_forecast (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT,
                power_w INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Verbrauchsprofil (für KI/Lernmodus)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS consumption_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                avg_power_w REAL NOT NULL,
                std_power_w REAL DEFAULT 0,
                sample_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(hour, day_of_week)
            )
        """)
        
        # Ladehistorie
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS charge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_start TEXT,
                timestamp_end TEXT,
                energy_kwh REAL,
                price_eur REAL,
                source TEXT,
                reason TEXT
            )
        """)
        
        # Konfiguration
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Manuellen Einstellungen
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS manual_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                battery_soc_target REAL,
                charge_power_w REAL,
                discharge_lock INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Indexe für bessere Performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tibber_time ON tibber_prices(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pv_time ON pv_production(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_battery_time ON battery_status(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_consumption_time ON house_consumption(timestamp)")
        
        conn.commit()
        conn.close()
        logger.info("Datenbank initialisiert")
    
    # ==================== INSERT METHODEN ====================
    
    def insert_tibber_prices(self, prices: list):
        """Fügt Tibber Preise ein"""
        conn = self.get_connection()
        for p in prices:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO tibber_prices (timestamp, price, currency, level)
                    VALUES (?, ?, ?, ?)
                """, (p['timestamp'], p['price'], p.get('currency', 'EUR'), p.get('level', 'NORMAL')))
            except Exception as e:
                pass
        conn.commit()
        conn.close()
    
    def insert_pv_data(self, data: dict):
        """Fügt PV Produktionsdaten ein"""
        conn = self.get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO pv_production (timestamp, power_w, energy_wh)
                VALUES (?, ?, ?)
            """, (data['timestamp'], data['power_w'], data.get('energy_wh', 0)))
            conn.commit()
        except Exception as e:
            logger.error(f"PV Daten Fehler: {e}")
        finally:
            conn.close()
    
    def insert_battery_data(self, data: dict):
        """Fügt Batterie Daten ein"""
        conn = self.get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO battery_status (timestamp, soc_percent, capacity_kwh, power_w, status)
                VALUES (?, ?, ?, ?, ?)
            """, (data['timestamp'], data['soc_percent'], data.get('capacity_kwh'), 
                  data.get('power_w'), data.get('status')))
            conn.commit()
        except Exception as e:
            logger.error(f"Batterie Daten Fehler: {e}")
        finally:
            conn.close()
    
    def insert_consumption_data(self, data: dict):
        """Fügt Verbrauchsdaten ein"""
        conn = self.get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO house_consumption (timestamp, power_w, energy_wh)
                VALUES (?, ?, ?)
            """, (data['timestamp'], data['power_w'], data.get('energy_wh', 0)))
            conn.commit()
        except Exception as e:
            logger.error(f"Verbrauchsdaten Fehler: {e}")
        finally:
            conn.close()
    
    def insert_wattpilot_data(self, data: dict):
        """Fügt Wattpilot Daten ein"""
        conn = self.get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO wattpilot (timestamp, power_w, energy_wh, status, charge_limit, plugged_in)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (data['timestamp'], data.get('power_w', 0), data.get('energy_wh', 0),
                  data.get('status'), data.get('charge_limit'), data.get('plugged_in', 0)))
            conn.commit()
        except Exception as e:
            logger.error(f"Wattpilot Daten Fehler: {e}")
        finally:
            conn.close()
    
    def insert_weather_data(self, data: dict):
        """Fügt Wetterdaten ein"""
        conn = self.get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO weather (timestamp, temperature_c, cloud_percent, radiation_wm2, precipitation_mm, weather_code)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (data['timestamp'], data.get('temperature_c'), data.get('cloud_percent'),
                  data.get('radiation_wm2'), data.get('precipitation_mm'), data.get('weather_code')))
            conn.commit()
        except Exception as e:
            logger.error(f"Wetter Daten Fehler: {e}")
        finally:
            conn.close()
    
    def insert_pv_forecast(self, forecasts: list):
        """Fügt PV Prognose ein"""
        conn = self.get_connection()
        try:
            for f in forecasts:
                conn.execute("""
                    INSERT OR REPLACE INTO pv_forecast (timestamp, source, power_w)
                    VALUES (?, ?, ?)
                """, (f['timestamp'], f.get('source', 'forecast.solar'), f.get('power_w', 0)))
            conn.commit()
        except Exception as e:
            logger.error(f"PV Prognose Fehler: {e}")
        finally:
            conn.close()
    
    # ==================== GET METHODEN ====================
    
    def get_current_tibber_price(self) -> dict:
        """Gibt aktuellen Tibber Preis zurück"""
        conn = self.get_connection()
        cursor = conn.execute("""
            SELECT price, currency, level, timestamp FROM tibber_prices 
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"price": row[0], "currency": row[1], "level": row[2], "timestamp": row[3]}
        return None
    
    def get_latest_battery(self) -> dict:
        """Gibt neuesten Batterie Status zurück"""
        conn = self.get_connection()
        cursor = conn.execute("""
            SELECT soc_percent, capacity_kwh, power_w, status, timestamp FROM battery_status 
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "soc_percent": row[0], "capacity_kwh": row[1], 
                "power_w": row[2], "status": row[3], "timestamp": row[4]
            }
        return None
    
    def get_latest_pv(self) -> dict:
        """Gibt neueste PV Produktion zurück"""
        conn = self.get_connection()
        cursor = conn.execute("""
            SELECT power_w, energy_wh, timestamp FROM pv_production 
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"power_w": row[0], "energy_wh": row[1], "timestamp": row[2]}
        return None
    
    def get_latest_wattpilot(self) -> dict:
        """Gibt neuesten Wattpilot Status zurück"""
        conn = self.get_connection()
        cursor = conn.execute("""
            SELECT power_w, status, charge_limit, plugged_in, timestamp FROM wattpilot 
            ORDER BY timestamp DESC LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "power_w": row[0], "status": row[1], 
                "charge_limit": row[2], "plugged_in": row[3], "timestamp": row[4]
            }
        return None
    
    def get_today_prices(self) -> list:
        """Gibt alle Tibber Preise für heute zurück"""
        conn = self.get_connection()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT timestamp, price FROM tibber_prices 
            WHERE timestamp LIKE ? ORDER BY timestamp
        """, (f"{today}%",))
        rows = cursor.fetchall()
        conn.close()
        return [{"timestamp": r[0], "price": r[1]} for r in rows]
    
    def get_pv_production_today(self) -> float:
        """Gibt PV Produktion für heute in Wh zurück"""
        conn = self.get_connection()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT SUM(energy_wh) FROM pv_production WHERE timestamp LIKE ?
        """, (f"{today}%",))
        result = cursor.fetchone()[0]
        conn.close()
        return result or 0
    
    def get_consumption_today(self) -> float:
        """Gibt Verbrauch für heute in Wh zurück"""
        conn = self.get_connection()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT SUM(energy_wh) FROM house_consumption WHERE timestamp LIKE ?
        """, (f"{today}%",))
        result = cursor.fetchone()[0]
        conn.close()
        return result or 0
    
    def get_charging_today(self) -> float:
        """Gibt geladene Energie am Wattpilot heute in Wh zurück"""
        conn = self.get_connection()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT SUM(energy_wh) FROM wattpilot WHERE timestamp LIKE ? AND power_w > 0
        """, (f"{today}%",))
        result = cursor.fetchone()[0]
        conn.close()
        return result or 0
    
    def get_today_savings(self) -> dict:
        """Berechnet heutige Ersparnis"""
        pv_today = self.get_pv_production_today() / 1000  # kWh
        grid_today = self.get_grid_import_today() / 1000  # kWh
        avg_price = self.get_average_price_today()
        
        # Ersparnis = PV Eigenverbrauch * Preis
        # Vereinfacht: 70% Eigenverbrauch bei PV, 30% ins Netz
        self_consumption = pv_today * 0.7
        savings = self_consumption * avg_price
        
        return {
            "pv_kwh": round(pv_today, 2),
            "grid_kwh": round(grid_today, 2),
            "avg_price_ct": round(avg_price * 100, 2),
            "savings_eur": round(savings, 2)
        }
    
    def get_grid_import_today(self) -> float:
        """Gibt Grid Import für heute zurück"""
        # Grid Import = Hausverbrauch + Batterieladung - PV
        consumption = self.get_consumption_today()
        pv = self.get_pv_production_today()
        return max(0, consumption - pv)
    
    def get_average_price_today(self) -> float:
        """Durchschnittspreis heute"""
        prices = self.get_today_prices()
        if prices:
            return sum(p['price'] for p in prices) / len(prices)
        return 0.25  # Default 25 Cent
    
    def get_learning_days(self) -> int:
        """Anzahl der Tage mit Daten für Lernmodus"""
        conn = self.get_connection()
        cursor = conn.execute("""
            SELECT COUNT(DISTINCT DATE(timestamp)) FROM house_consumption
        """)
        days = cursor.fetchone()[0]
        conn.close()
        return days or 0
    
    def get_consumption_profile(self) -> pd.DataFrame:
        """Gibt Verbrauchsprofil zurück"""
        conn = self.get_connection()
        df = pd.read_sql("SELECT * FROM consumption_profile", conn)
        conn.close()
        return df
    
    def get_hourly_prices_tomorrow(self) -> list:
        """Gibt stündliche Preise für morgen zurück"""
        conn = self.get_connection()
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT timestamp, price FROM tibber_prices 
            WHERE timestamp LIKE ? ORDER BY timestamp
        """, (f"{tomorrow}%",))
        rows = cursor.fetchall()
        conn.close()
        return [{"timestamp": r[0], "price": r[1]} for r in rows]
    
    # ==================== PROFIL UPDATE ====================
    
    def update_consumption_profile(self):
        """Aktualisiert das stündliche Verbrauchsprofil aus historischen Daten"""
        conn = self.get_connection()
        
        # Hole mindestens 7 Tage Daten
        cursor = conn.execute("""
            SELECT 
                strftime('%H', timestamp) as hour,
                strftime('%w', timestamp) as dow,
                AVG(power_w) as avg_power,
                STDDEV(power_w) as std_power,
                COUNT(*) as samples
            FROM house_consumption
            WHERE timestamp > datetime('now', '-14 days')
            GROUP BY hour, dow
        """)
        
        rows = cursor.fetchall()
        
        for row in rows:
            conn.execute("""
                INSERT OR REPLACE INTO consumption_profile 
                (hour, day_of_week, avg_power_w, std_power_w, sample_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (int(row[0]), int(row[1]), row[2], row[3] or 0, row[4], datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        logger.info("Verbrauchsprofil aktualisiert")
    
    # ==================== CONFIG ====================
    
    def set_config(self, key: str, value: str):
        """Speichert Konfigurationswert"""
        conn = self.get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO config (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    
    def get_config(self, key: str, default=None) -> str:
        """Liest Konfigurationswert"""
        conn = self.get_connection()
        cursor = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    
    def get_manual_settings(self) -> dict:
        """Liest manuelle Einstellungen"""
        conn = self.get_connection()
        cursor = conn.execute("SELECT * FROM manual_settings ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "battery_soc_target": row[1],
                "charge_power_w": row[2],
                "discharge_lock": row[3],
                "enabled": row[4]
            }
        return {"enabled": False}
    
    def save_manual_settings(self, settings: dict):
        """Speichert manuelle Einstellungen"""
        conn = self.get_connection()
        conn.execute("""
            INSERT INTO manual_settings 
            (battery_soc_target, charge_power_w, discharge_lock, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            settings.get('battery_soc_target'),
            settings.get('charge_power_w'),
            settings.get('discharge_lock', 0),
            settings.get('enabled', 1),
            datetime.now().isoformat()
        ))
        conn.commit()
        conn.close()
    
    # ==================== STATISTIKEN ====================
    
    def get_daily_stats(self, days: int = 7) -> pd.DataFrame:
        """Gibt tägliche Statistiken zurück"""
        conn = self.get_connection()
        query = """
            SELECT 
                DATE(timestamp) as date,
                AVG(pv.power_w) as avg_pv_w,
                SUM(pv.energy_wh) as total_pv_wh,
                AVG(bat.soc_percent) as avg_soc,
                AVG(wp.power_w) as avg_wattpilot_w,
                SUM(wp.energy_wh) as total_wattpilot_wh,
                AVG(tp.price) as avg_price
            FROM pv_production pv
            LEFT JOIN battery_status bat ON DATE(pv.timestamp) = DATE(bat.timestamp)
            LEFT JOIN wattpilot wp ON DATE(pv.timestamp) = DATE(wp.timestamp)
            LEFT JOIN tibber_prices tp ON DATE(pv.timestamp) = DATE(tp.timestamp)
            WHERE pv.timestamp > datetime('now', '-{} days')
            GROUP BY DATE(pv.timestamp)
            ORDER BY date
        """.format(days)
        
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    
    def get_chart_data(self, hours: int = 48) -> dict:
        """Daten für Charts"""
        conn = self.get_connection()
        
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        
        pv_df = pd.read_sql(f"SELECT timestamp, power_w FROM pv_production WHERE timestamp > '{since}'", conn)
        bat_df = pd.read_sql(f"SELECT timestamp, soc_percent FROM battery_status WHERE timestamp > '{since}'", conn)
        cons_df = pd.read_sql(f"SELECT timestamp, power_w FROM house_consumption WHERE timestamp > '{since}'", conn)
        price_df = pd.read_sql(f"SELECT timestamp, price FROM tibber_prices WHERE timestamp > '{since}'", conn)
        
        conn.close()
        
        return {
            "pv": pv_df.to_dict('records'),
            "battery": bat_df.to_dict('records'),
            "consumption": cons_df.to_dict('records'),
            "prices": price_df.to_dict('records')
        }
