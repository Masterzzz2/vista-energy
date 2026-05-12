"""
Learning Service
Builds and maintains consumption profiles from historical data
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import EnergyReading, db

logger = logging.getLogger(__name__)
POOL_POWER_W = float(os.getenv('POOL_POWER_W', 1800))


class LearningService:
    """
    Manages the learning phase and profile building.
    After 7 days of data, provides consumption profiles for optimization.

    Lern-Strategie:
      - Basis-Verbrauch = house_consumption MINUS wattpilot MINUS erkannte Pool-Events
      - P25 (unteres Quartil) statt Durchschnitt: filtert Grossverbraucher-Spitzen
        automatisch raus (Poolpumpe, Heizstab, etc.)
      - Lernfenster 30 Tage: seltene Events (z.B. Abend-Ladung Mercedes) verfaelschen
        die Statistik nicht, da sie im P25 untergehen
      - Aggregiertes Profil (learned_profile Tabelle): taegliches Update mit
        exponentiellem gleitendem Mittel (EMA, alpha=0.1). Wird ueber Monate
        immer praeziser, ohne dass die DB waechst.
    """

    PROFILE_WINDOW_DAYS = 30     # Tage fuer Rohdaten-Fenster
    EMA_ALPHA = 0.1              # Glaettungsfaktor fuer Langzeit-Profil (0.1 = langsam)

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.min_learning_days = 7
        self.engine = create_engine(f'sqlite:///{db_path}')
        self.Session = sessionmaker(bind=self.engine)
        self._ensure_profile_table()
    
    # day_type Konstanten
    DAY_WEEKDAY = 0
    DAY_WEEKEND = 1

    @staticmethod
    def _day_type_for_date(dt: datetime) -> int:
        """0 = Werktag (Mo-Fr), 1 = Wochenende (Sa-So)."""
        return 1 if dt.weekday() >= 5 else 0

    def _ensure_profile_table(self):
        """Erstelle learned_profile Tabelle falls nicht vorhanden.

        Schema v2: Primaerschluessel (hour, day_type) statt nur hour.
        day_type 0 = Werktag, 1 = Wochenende.
        Migration: falls alte Tabelle ohne day_type existiert, wird sie
        umgebaut und die bestehenden EMA-Werte fuer beide Tagestypen kopiert.
        """
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            # Pruefen ob day_type Spalte bereits existiert
            cols = [r[1] for r in conn.execute("PRAGMA table_info(learned_profile)").fetchall()]
            if 'day_type' not in cols and cols:
                # Migration: alte Tabelle -> neue Tabelle
                logger.info("Migrating learned_profile: adding day_type column")
                conn.execute("ALTER TABLE learned_profile RENAME TO learned_profile_old")
                conn.execute("""
                    CREATE TABLE learned_profile (
                        hour INTEGER NOT NULL,
                        day_type INTEGER NOT NULL DEFAULT 0,
                        base_w REAL DEFAULT 300,
                        sample_days INTEGER DEFAULT 0,
                        last_updated TEXT,
                        PRIMARY KEY (hour, day_type)
                    )
                """)
                # Alte Werte fuer beide Tagestypen uebernehmen
                conn.execute("""
                    INSERT INTO learned_profile (hour, day_type, base_w, sample_days, last_updated)
                    SELECT hour, 0, base_w, sample_days, last_updated FROM learned_profile_old
                """)
                conn.execute("""
                    INSERT INTO learned_profile (hour, day_type, base_w, sample_days, last_updated)
                    SELECT hour, 1, base_w, sample_days, last_updated FROM learned_profile_old
                """)
                conn.execute("DROP TABLE learned_profile_old")
                conn.commit()
                logger.info("learned_profile migration complete")
            elif not cols:
                # Tabelle existiert noch nicht — neu anlegen
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS learned_profile (
                        hour INTEGER NOT NULL,
                        day_type INTEGER NOT NULL DEFAULT 0,
                        base_w REAL DEFAULT 300,
                        sample_days INTEGER DEFAULT 0,
                        last_updated TEXT,
                        PRIMARY KEY (hour, day_type)
                    )
                """)

            # Sicherstellen, dass alle 48 Eintraege vorhanden sind (24h × 2 Tagestypen)
            for dt in (0, 1):
                for h in range(24):
                    conn.execute(
                        "INSERT OR IGNORE INTO learned_profile (hour, day_type, base_w, sample_days) VALUES (?, ?, 300, 0)",
                        (h, dt)
                    )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f'ensure profile table: {e}')

    def update_aggregated_profile(self):
        """Taegliches Update des Langzeit-Profils via EMA.

        Wird vom maintenance_job aufgerufen (einmal taeglich um 03:15).
        Berechnet aus den gestrigen Rohdaten den bereinigten Hausverbrauch
        pro Stunde und mischt ihn per EMA in das bestehende Profil ein.
        So wird das Profil ueber Wochen/Monate immer genauer.

        Seit v2: getrennte Profile fuer Werktag und Wochenende. Gestern wird
        automatisch dem richtigen Tagestyp zugeordnet.
        """
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            yesterday_dt = datetime.now() - timedelta(days=1)
            yesterday = yesterday_dt.strftime('%Y-%m-%d')
            today = datetime.now().strftime('%Y-%m-%d')
            day_type = self._day_type_for_date(yesterday_dt)
            day_label = "Wochenende" if day_type == 1 else "Werktag"

            rows = conn.execute("""
                SELECT
                    CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                    ABS(house_consumption_w) AS total_w,
                    ABS(COALESCE(wattpilot_power_w, 0)) AS wp_w
                FROM energy_readings
                WHERE timestamp >= ? AND timestamp < ?
            """, (yesterday, today)).fetchall()

            if not rows:
                conn.close()
                return

            from collections import defaultdict
            by_hour = defaultdict(list)
            for r in rows:
                hour_val, total_w, wp_w = r[0], r[1] or 0, r[2] or 0
                # Nur Readings OHNE aktive Wallbox fuer Basis-Verbrauch verwenden.
                # Bei aktiver WB (>500W) sind Timing-Differenzen zwischen
                # SmartMeter und WattPilot zu gross fuer zuverlaessige Subtraktion.
                if wp_w < 500:
                    net_w = max(100.0, total_w - wp_w)
                    by_hour[hour_val].append(net_w)

            alpha = self.EMA_ALPHA
            updated = 0
            for h in range(24):
                vals = sorted(by_hour.get(h, []))
                if len(vals) < 2:
                    continue
                # P25 als robuster Schaetzer fuer reinen Hausverbrauch
                p25 = vals[len(vals) // 4]

                # Aktuellen EMA-Wert fuer den richtigen Tagestyp lesen
                cur = conn.execute(
                    "SELECT base_w, sample_days FROM learned_profile WHERE hour = ? AND day_type = ?",
                    (h, day_type)
                ).fetchone()
                old_w = cur[0] if cur else 300
                old_days = cur[1] if cur else 0

                # EMA: neuer Wert = alpha * gestern + (1-alpha) * alter Wert
                # Bei wenigen Samples staerker gewichten (schnelleres Lernen am Anfang)
                effective_alpha = min(0.5, alpha * max(1, 10 / max(1, old_days)))
                new_w = effective_alpha * p25 + (1 - effective_alpha) * old_w

                conn.execute("""
                    UPDATE learned_profile
                    SET base_w = ?, sample_days = ?, last_updated = ?
                    WHERE hour = ? AND day_type = ?
                """, (round(new_w, 1), old_days + 1, today, h, day_type))
                updated += 1

            conn.commit()
            conn.close()
            if updated:
                logger.info(
                    f"Learned profile updated ({day_label}): {updated} hours from {yesterday}"
                )
        except Exception as e:
            logger.error(f'update_aggregated_profile: {e}')

    def get_aggregated_profile(self, day_type: int = None) -> dict:
        """Lese das Langzeit-Profil (EMA-basiert).

        Args:
            day_type: 0=Werktag, 1=Wochenende. None=automatisch (heute).
        """
        if day_type is None:
            day_type = self._day_type_for_date(datetime.now())
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            rows = conn.execute(
                "SELECT hour, base_w, sample_days, last_updated "
                "FROM learned_profile WHERE day_type = ? ORDER BY hour",
                (day_type,)
            ).fetchall()
            conn.close()
            return {
                r[0]: {'base_w': r[1], 'sample_days': r[2], 'last_updated': r[3]}
                for r in rows
            }
        except Exception:
            return {}

    def get_session(self):
        """Get a database session."""
        return self.Session()
    
    def get_learning_days(self) -> int:
        """Return how many days of data we have."""
        session = self.get_session()
        try:
            first_reading = session.query(EnergyReading).order_by(
                EnergyReading.timestamp.asc()
            ).first()
            
            if not first_reading:
                return 0
            
            age = datetime.now() - first_reading.timestamp
            return int(age.days)
        finally:
            session.close()

    def get_reading_count(self) -> int:
        """Return total number of stored readings."""
        session = self.get_session()
        try:
            return session.query(EnergyReading).count()
        finally:
            session.close()

    def _parse_dt(self, value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        try:
            text = str(value).strip().replace('Z', '+00:00')
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

    def _pool_events(self, start: datetime, end: datetime) -> list:
        """Return pool runs overlapping the requested window."""
        if not self.db_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT *
                FROM pool_events
                WHERE COALESCE(stopped_at, datetime('now')) >= ?
                  AND started_at <= ?
                ORDER BY started_at ASC
            """, (
                start.strftime('%Y-%m-%d %H:%M:%S'),
                end.strftime('%Y-%m-%d %H:%M:%S'),
            )).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
        except Exception as e:
            logger.debug(f'pool events unavailable: {e}')
            return []

    def _pool_power_at(self, ts: datetime, pool_events: list) -> float:
        ts = ts.replace(tzinfo=None)
        for ev in pool_events:
            started = self._parse_dt(ev.get('started_at'))
            stopped = self._parse_dt(ev.get('stopped_at')) or datetime.now()
            if started and started <= ts <= stopped:
                return float(ev.get('power_w') or POOL_POWER_W)
        return 0.0

    def get_pool_learning_summary(self, days: int = 90) -> Dict:
        """Summarize learned pool heating behavior from manual Pool AN/AUS events."""
        end = datetime.now()
        start = end - timedelta(days=days)
        events = self._pool_events(start, end)
        completed = []
        hour_hits = {h: 0 for h in range(24)}
        weekday_hits = {d: 0 for d in range(7)}

        for ev in events:
            started = self._parse_dt(ev.get('started_at'))
            stopped = self._parse_dt(ev.get('stopped_at'))
            if not started or not stopped or stopped <= started:
                continue
            duration = int(ev.get('duration_min') or ((stopped - started).total_seconds() / 60))
            if duration < 10:
                continue
            power = float(ev.get('power_w') or POOL_POWER_W)
            energy = float(ev.get('energy_kwh') or (duration / 60.0 * power / 1000.0))
            completed.append({
                'started_at': ev.get('started_at'),
                'stopped_at': ev.get('stopped_at'),
                'start_min': started.hour * 60 + started.minute,
                'duration_min': duration,
                'energy_kwh': energy,
                'power_w': power,
                'temp_start_c': ev.get('temp_start_c'),
                'temp_end_c': ev.get('temp_end_c'),
                'pv_forecast_kwh': ev.get('pv_forecast_kwh'),
                'sunshine_hours': ev.get('sunshine_hours'),
            })
            weekday_hits[started.weekday()] += 1
            for hour in range(24):
                h_start = started.replace(hour=hour, minute=0, second=0, microsecond=0)
                h_end = h_start + timedelta(hours=1)
                if max(started, h_start) < min(stopped, h_end):
                    hour_hits[hour] += 1

        if not completed:
            return {
                'event_count': 0,
                'typical_start_min': None,
                'typical_start': None,
                'avg_duration_min': None,
                'avg_energy_kwh': None,
                'avg_power_w': POOL_POWER_W,
                'hour_probability': {h: 0 for h in range(24)},
                'hour_expected_w': {h: 0 for h in range(24)},
            }

        count = len(completed)
        avg_start = sum(e['start_min'] for e in completed) / count
        avg_duration = sum(e['duration_min'] for e in completed) / count
        avg_energy = sum(e['energy_kwh'] for e in completed) / count
        avg_power = sum(e['power_w'] for e in completed) / count
        hour_probability = {h: round(hour_hits[h] / count, 3) for h in range(24)}
        hour_expected_w = {h: round(avg_power * hour_probability[h], 1) for h in range(24)}

        return {
            'event_count': count,
            'typical_start_min': round(avg_start),
            'typical_start': f'{int(avg_start) // 60:02d}:{int(avg_start) % 60:02d}',
            'avg_duration_min': round(avg_duration),
            'avg_energy_kwh': round(avg_energy, 2),
            'avg_power_w': round(avg_power),
            'hour_probability': hour_probability,
            'hour_expected_w': hour_expected_w,
            'weekday_hits': weekday_hits,
            'last_event': completed[-1],
        }
    
    def is_learning_complete(self) -> bool:
        """Return True if we have 7+ days of data."""
        return self.get_learning_days() >= self.min_learning_days
    
    def get_daily_profile(self, day_offset: int = 0) -> Optional[Dict]:
        """
        Get consumption profile for a specific day.

        Verwendet ein 30-Tage-Fenster fuer robuste Statistik.
        Seltene Events (z.B. einmalige Abend-Ladung) werden durch P25 rausgefiltert.
        Falls ein Langzeit-Profil (EMA) existiert, wird es als Fallback/Blend verwendet.

        Seit v2: Getrennte Profile fuer Werktag und Wochenende.
        Es werden nur Readings vom gleichen Tagestyp (Mo-Fr / Sa-So) verwendet,
        damit z.B. Wochenend-Kochen nicht die Werktagsprognose verfaelscht.

        Args:
            day_offset: 0 = today, -1 = yesterday, etc.

        Returns:
            Dict with hourly data or None if not enough data
        """
        session = self.get_session()
        try:
            # Zieldatum und Tagestyp bestimmen
            target_date = datetime.now() + timedelta(days=day_offset)
            target_day_type = self._day_type_for_date(target_date)

            # 30-Tage-Fenster fuer mehr Datenpunkte = stabilere Statistik
            end_date = target_date
            start_date = end_date - timedelta(days=self.PROFILE_WINDOW_DAYS)

            readings = session.query(EnergyReading).filter(
                EnergyReading.timestamp >= start_date,
                EnergyReading.timestamp < end_date
            ).all()

            # Build hourly profile — NUR Readings vom gleichen Tagestyp verwenden
            pool_events = self._pool_events(start_date, end_date)
            default_profile = self._get_default_profile()
            hourly_data = {
                h: {'consumption': [], 'base_consumption': [], 'wattpilot': [], 'pool_power': []}
                for h in range(24)
            }

            # Schwelle ab der die Wallbox als "aktiv ladend" gilt.
            # Readings mit aktiver WB sind fuer Basis-Verbrauch unbrauchbar,
            # weil Timing-Differenzen zwischen SmartMeter und WattPilot zu
            # negativen Differenzen fuehren (WP meldet z.B. 3600 W, aber
            # SmartMeter hat 2 Sekunden spaeter gemessen und zeigt nur 300 W).
            WP_ACTIVE_THRESHOLD_W = 500

            for r in readings:
                # Nur gleichen Tagestyp (Werktag/Wochenende) einbeziehen
                if self._day_type_for_date(r.timestamp) != target_day_type:
                    continue

                hour = r.timestamp.hour
                raw_consumption = abs(r.house_consumption_w or 0)
                wp_power = abs(r.wattpilot_power_w or 0)
                pool_power = self._pool_power_at(r.timestamp, pool_events)

                hourly_data[hour]['consumption'].append(raw_consumption)
                hourly_data[hour]['wattpilot'].append(wp_power)
                hourly_data[hour]['pool_power'].append(pool_power)

                # Basis-Verbrauch NUR berechnen wenn Wallbox NICHT aktiv laedt.
                # Wenn die WB laedt, dominiert sie den SmartMeter-Wert und die
                # Subtraktion ist unzuverlaessig (Timing). Solche Readings
                # wuerden den Basisverbrauch kuenstlich auf 100 W druecken.
                if wp_power < WP_ACTIVE_THRESHOLD_W:
                    base_consumption = max(100.0, raw_consumption - wp_power - pool_power)
                    hourly_data[hour]['base_consumption'].append(base_consumption)

            # Langzeit-EMA-Profil laden — passend zum Tagestyp
            ema_profile = self.get_aggregated_profile(day_type=target_day_type)

            profile = {}
            for hour, data in hourly_data.items():
                consumptions = data['consumption']
                base_consumptions = data['base_consumption']
                wattpilot = data['wattpilot']
                pool_powers = data['pool_power']
                default_entry = default_profile[hour]
                default_consumption = default_entry['avg_consumption_w']

                if consumptions:
                    learned_consumption = sum(consumptions) / len(consumptions)
                    # P25 als robuster Schaetzer fuer reinen Hausverbrauch.
                    # base_consumptions enthaelt NUR Readings ohne aktive Wallbox,
                    # daher keine kuenstlichen 100-W-Werte durch Timing-Differenzen.
                    sorted_base = sorted(base_consumptions)
                    n_base = len(sorted_base)
                    if n_base >= 3:
                        # Genug WB-freie Readings: P25/P10-Blend
                        p25_base = sorted_base[n_base // 4]
                        p10_base = sorted_base[max(0, n_base // 10)]
                        learned_base_consumption = p25_base * 0.8 + p10_base * 0.2
                    elif n_base > 0:
                        # Wenige WB-freie Readings: Median nehmen
                        learned_base_consumption = sorted_base[n_base // 2]
                    else:
                        # Keine WB-freien Readings (WB laedt immer zu dieser Stunde).
                        # Fallback: EMA oder benachbarte Stunde oder Default.
                        learned_base_consumption = None
                    learned_pool_expected = (
                        sum(pool_powers) / len(pool_powers)
                        if pool_events and pool_powers else 0.0
                    )
                    learned_wp_avg = (
                        sum(wattpilot) / len(wattpilot)
                        if wattpilot else 0.0
                    )
                    confidence = min(1.0, len(consumptions) / 12.0)

                    # Blend-Quelle bestimmen: P25 aus 30-Tage-Fenster
                    short_term = learned_base_consumption if learned_base_consumption is not None else learned_consumption

                    # Falls Langzeit-EMA vorhanden (>7 Tage gesammelt): mische ein.
                    # EMA glaettet ueber Monate und ist noch stabiler als P25.
                    ema_entry = ema_profile.get(hour)
                    if ema_entry and (ema_entry.get('sample_days') or 0) >= 7:
                        ema_w = ema_entry['base_w']
                        # 60% Kurzzeit (aktuell), 40% Langzeit (EMA) — so reagiert
                        # das System auf Aenderungen, bleibt aber stabil.
                        blend_source = short_term * 0.6 + ema_w * 0.4
                    else:
                        blend_source = short_term

                    avg_consumption = (
                        default_consumption * (1.0 - confidence)
                        + blend_source * confidence
                    )
                else:
                    learned_consumption = None
                    learned_base_consumption = None
                    learned_pool_expected = 0.0
                    learned_wp_avg = 0.0
                    confidence = 0.0
                    # Kein 30-Tage-Daten: EMA oder Default
                    ema_entry = ema_profile.get(hour)
                    if ema_entry and (ema_entry.get('sample_days') or 0) >= 3:
                        avg_consumption = ema_entry['base_w']
                    else:
                        avg_consumption = default_consumption
                
                # EV charging probability
                ev_count = sum(1 for w in wattpilot if w > 500) if wattpilot else 0
                learned_ev_probability = ev_count / len(wattpilot) if wattpilot else None
                ev_probability = (
                    default_entry['typical_ev_charging'] if learned_ev_probability is None
                    else default_entry['typical_ev_charging'] * (1.0 - confidence) + learned_ev_probability * confidence
                )
                
                # Standard deviation
                std = self._calculate_std(consumptions) if len(consumptions) > 1 else default_entry['std_consumption_w']
                
                profile[hour] = {
                    'avg_consumption_w': avg_consumption,
                    'raw_consumption_w': learned_consumption,
                    'base_consumption_w': learned_base_consumption,
                    'learned_consumption_w': learned_consumption,
                    'default_consumption_w': default_consumption,
                    'std_consumption_w': std,
                    'typical_ev_charging': ev_probability,
                    'wattpilot_avg_w': round(learned_wp_avg, 1),
                    'pool_expected_w': learned_pool_expected,
                    'pool_active_probability': (
                        round(sum(1 for p in pool_powers if p > 0) / len(pool_powers), 3)
                        if pool_powers else 0
                    ),
                    'pool_sample_count': sum(1 for p in pool_powers if p > 0),
                    'sample_count': len(consumptions),
                    'confidence': round(confidence, 3),
                    'source': 'learned_clean' if consumptions else 'default'
                }
            
            return profile
            
        finally:
            session.close()
    
    def get_weekly_report(self) -> Dict:
        """Generate a weekly summary report."""
        days = self.get_learning_days()
        now = datetime.now()
        day_type = self._day_type_for_date(now)

        return {
            'learning_complete': self.is_learning_complete(),
            'profile_active': self.get_reading_count() > 0,
            'learning_days': days,
            'days_remaining': max(0, self.min_learning_days - days),
            'reading_count': self.get_reading_count(),
            'pool': self.get_pool_learning_summary(),
            'day_type': 'weekend' if day_type == 1 else 'weekday',
            'message': self._get_learning_message(days)
        }
    
    def _get_learning_message(self, days: int) -> str:
        """Get human-readable learning status message."""
        if days == 0:
            return "No data collected yet. Learning will begin on first data point."
        elif days < 7:
            return f"Learning active: {days}/7 days. Existing readings are already blended into the profile."
        else:
            return f"Learning complete. Profile based on {days} days of data."
    
    def _calculate_std(self, values: list) -> float:
        """Calculate standard deviation."""
        if len(values) < 2:
            return 0
        
        avg = sum(values) / len(values)
        variance = sum((v - avg) ** 2 for v in values) / len(values)
        return variance ** 0.5
    
    def _get_default_profile(self) -> Dict:
        """Return default profile when not enough data."""
        profile = {}
        for hour in range(24):
            # Default assumptions for a typical household
            if 6 <= hour <= 9:
                consumption = 800  # Morning peak
            elif 17 <= hour <= 22:
                consumption = 1000  # Evening peak
            elif 0 <= hour <= 5:
                consumption = 200  # Night low
            else:
                consumption = 400  # Daytime average
            
            profile[hour] = {
                'avg_consumption_w': consumption,
                'std_consumption_w': 200,
                'typical_ev_charging': 0.1 if 22 <= hour or hour <= 6 else 0.3,
                'sample_count': 0
            }
        
        return profile
    
    def predict_tomorrow_consumption(self) -> Dict:
        """
        Predict tomorrow's consumption pattern based on learned profile.
        Returns hourly predictions in Watt.
        Uses day_offset=1 so the correct weekday/weekend profile is used.
        """
        profile = self.get_daily_profile(day_offset=1)
        if not profile:
            profile = self._get_default_profile()
        
        predictions = []
        for hour in range(24):
            if hour in profile:
                pred = profile[hour]['avg_consumption_w']
            else:
                pred = 500
            
            predictions.append({
                'hour': hour,
                'predicted_consumption_w': pred,
                'confidence': profile.get(hour, {}).get('confidence', 0)
            })
        
        return {'predictions': predictions}
    
    def get_optimal_charge_windows(self, hours_needed: int = 4) -> list:
        """
        Get the best hours to charge based on learned patterns.
        
        Returns list of hours (0-23) when charging is most efficient.
        """
        profile = self.get_daily_profile()
        if not profile:
            return list(range(0, 4))  # Default: night hours
        
        # Score each hour
        hour_scores = []
        for hour in range(24):
            if hour in profile:
                ev_prob = profile[hour].get('typical_ev_charging', 0.1)
                consumption = profile[hour].get('avg_consumption_w', 500)
                
                # Lower consumption + lower EV probability = better for charging
                score = (consumption / 1000) * (1 - ev_prob)
            else:
                score = 0.5
            
            hour_scores.append((hour, score))
        
        # Sort by score (ascending = best for charging)
        hour_scores.sort(key=lambda x: x[1])
        
        return [h[0] for h in hour_scores[:hours_needed]]
