"""
Database models for Energy Optimizer
SQLite with SQLAlchemy ORM
"""

from datetime import datetime
from pathlib import Path

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Integer, Float, String, DateTime, JSON

db = SQLAlchemy()


class EnergyReading(db.Model):
    """Energy readings collected every 15 minutes."""
    __tablename__ = 'energy_readings'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)
    
    # Consumption & Production
    house_consumption_w = Column(Float, default=0)  # Watt
    pv_production_w = Column(Float, default=0)  # Watt
    battery_charge_w = Column(Float, default=0)  # Watt (positive = charging)
    
    # Battery
    battery_soc = Column(Float, default=0)  # 0.0 - 1.0
    
    # Grid
    grid_power_w = Column(Float, default=0)  # Watt (positive = from grid, negative = to grid)
    
    # EV Charging
    wattpilot_power_w = Column(Float, default=0)  # Watt
    
    # Weather
    temperature = Column(Float, default=0)  # Celsius
    weather_condition = Column(String(50), default='unknown')
    pv_forecast_w = Column(Float, default=0)  # Forecasted PV production
    
    # Price
    price_per_kwh = Column(Float, default=0)  # EUR/kWh from Tibber
    
    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'house_consumption_w': self.house_consumption_w,
            'pv_production_w': self.pv_production_w,
            'battery_soc': self.battery_soc,
            'battery_charge_w': self.battery_charge_w,
            'grid_power_w': self.grid_power_w,
            'wattpilot_power_w': self.wattpilot_power_w,
            'temperature': self.temperature,
            'weather_condition': self.weather_condition,
            'pv_forecast_w': self.pv_forecast_w,
            'price_per_kwh': self.price_per_kwh
        }


class PriceData(db.Model):
    """Tibber price data."""
    __tablename__ = 'price_data'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    price_per_kwh = Column(Float, nullable=False)  # Euro per kWh
    level = Column(String(20), default='normal')  # cheap, normal, expensive
    
    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat(),
            'price_per_kwh': self.price_per_kwh,
            'level': self.level
        }


class ChargeLog(db.Model):
    """EV charging logs."""
    __tablename__ = 'charge_logs'
    
    id = Column(Integer, primary_key=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)
    energy_kwh = Column(Float, default=0)  # kWh charged
    max_power_w = Column(Float, default=0)  # Max power used
    source = Column(String(20), default='grid')  # grid, battery, pv
    reason = Column(String(100), default='manual')  # cheap_rate, pv_surplus, manual
    
    def to_dict(self):
        return {
            'id': self.id,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'energy_kwh': self.energy_kwh,
            'max_power_w': self.max_power_w,
            'source': self.source,
            'reason': self.reason
        }


class Config(db.Model):
    """Configuration storage."""
    __tablename__ = 'config'
    
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    def to_dict(self):
        return {
            'key': self.key,
            'value': self.value,
            'updated_at': self.updated_at.isoformat()
        }


class ConsumptionProfile(db.Model):
    """Daily consumption profile (learned over 7 days)."""
    __tablename__ = 'consumption_profiles'
    
    id = Column(Integer, primary_key=True)
    hour = Column(Integer, nullable=False)  # 0-23
    avg_consumption_w = Column(Float, default=0)  # Average consumption this hour
    std_consumption_w = Column(Float, default=0)  # Standard deviation
    typical_ev_charging = Column(Float, default=0)  # 0-1 probability of EV charging
    sample_count = Column(Integer, default=0)  # How many samples this is based on
    
    def to_dict(self):
        return {
            'hour': self.hour,
            'avg_consumption_w': self.avg_consumption_w,
            'std_consumption_w': self.std_consumption_w,
            'typical_ev_charging': self.typical_ev_charging,
            'sample_count': self.sample_count
        }


def init_db(app=None):
    """Initialize database."""
    db_path = Path(__file__).parent.parent / 'energy_optimizer.db'
    
    if app:
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        with app.app_context():
            db.create_all()  # Create all tables
    
    return db_path