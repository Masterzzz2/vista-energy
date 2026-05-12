"""
Consumption Profile Model
Builds and manages daily consumption profiles from historical data.
"""

import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

from models.database import EnergyReading, ChargeLog


class ConsumptionProfile:
    """
    Manages daily consumption profiles.
    After 7 days of learning, provides hourly averages for optimization.
    """
    
    def __init__(self, db_session):
        self.db = db_session
        self.min_learning_days = 7
        
    def build_profile(self):
        """
        Build hourly consumption profile from last 7 days of data.
        Returns dict: {hour: {'avg': w, 'std': w, 'ev_probability': 0-1}}
        """
        seven_days_ago = datetime.now() - timedelta(days=7)
        
        readings = EnergyReading.query.filter(
            EnergyReading.timestamp >= seven_days_ago
        ).all()
        
        if len(readings) < 50:  # Not enough data
            return None
        
        # Group by hour
        hourly_data = defaultdict(list)
        for r in readings:
            hour = r.timestamp.hour
            hourly_data[hour].append({
                'consumption': r.house_consumption_w,
                'pv': r.pv_production_w,
                'wattpilot': r.wattpilot_power_w
            })
        
        profile = {}
        for hour in range(24):
            if hour in hourly_data:
                data = hourly_data[hour]
                consumptions = [d['consumption'] for d in data]
                wattpilot_usage = [d['wattpilot'] for d in data]
                
                # EV charging probability = % of readings with significant wattpilot usage
                ev_prob = sum(1 for w in wattpilot_usage if w > 1000) / len(data)
                
                profile[hour] = {
                    'avg_consumption_w': sum(consumptions) / len(consumptions),
                    'std_consumption_w': self._std(consumptions),
                    'typical_ev_charging': ev_prob
                }
            else:
                profile[hour] = {
                    'avg_consumption_w': 500,  # Default assumption
                    'std_consumption_w': 200,
                    'typical_ev_charging': 0.1
                }
                
        return profile
    
    def get_learning_days(self):
        """Return how many days of data we have."""
        first_reading = EnergyReading.query.order_by(EnergyReading.timestamp.asc()).first()
        if not first_reading:
            return 0
        
        age = datetime.now() - first_reading.timestamp
        return min(age.days, 7)
    
    def is_learning_complete(self):
        """Return True if we have 7+ days of data."""
        return self.get_learning_days() >= self.min_learning_days
    
    def _std(self, values):
        """Calculate standard deviation."""
        if len(values) < 2:
            return 0
        avg = sum(values) / len(values)
        variance = sum((v - avg) ** 2 for v in values) / len(values)
        return variance ** 0.5