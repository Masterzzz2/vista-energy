"""
Optimizer Service
Core optimization logic for battery and EV charging
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class Optimizer:
    """
    Main optimization engine.
    Decides when to charge/discharge battery and EV based on prices and forecasts.
    """
    
    def __init__(self, battery_capacity: float, max_charge_power: float):
        """
        Initialize optimizer.
        
        Args:
            battery_capacity: Usable battery capacity in kWh
            max_charge_power: Max charge/discharge power in W
        """
        self.battery_capacity_kwh = battery_capacity
        self.max_charge_power_w = max_charge_power
        
        # Thresholds (can be made configurable)
        self.cheap_price_threshold = 0.18  # EUR/kWh - below this, consider charging
        self.expensive_price_threshold = 0.28  # EUR/kWh - above this, prefer discharging
        self.min_battery_soc_charge = 0.2  # Never discharge below 20%
        self.target_morning_soc = 0.8  # Want 80% SOC by morning
    
    def optimize(
        self,
        current_soc: float,
        current_prices: List[Dict],
        pv_forecast: Dict,
        consumption_profile: Dict,
        mode: str = 'auto'
    ) -> Dict:
        """
        Generate optimization recommendation.
        
        Args:
            current_soc: Current battery state of charge (0.0 - 1.0)
            current_prices: List of upcoming prices with timestamps
            pv_forecast: PV production forecast
            consumption_profile: Hourly consumption averages
            mode: 'manual', 'auto', or 'ki'
        
        Returns:
            Dict with battery_action, charge_action, target_soc, etc.
        """
        now = datetime.now()
        hour = now.hour
        
        recommendation = {
            'timestamp': now,
            'current_soc': current_soc,
            'mode': mode,
            'battery_mode': 'normal',
            'target_soc': None,
            'charge_action': 'auto',
            'max_power_w': self.max_charge_power_w,
            'reason': []
        }
        
        if mode == 'manual':
            # In manual mode, don't change anything
            recommendation['reason'].append('Manual mode - no changes')
            return recommendation
        
        elif mode == 'auto':
            recommendation = self._auto_optimize(
                recommendation, current_soc, current_prices, hour
            )
        
        elif mode == 'ki':
            recommendation = self._ki_optimize(
                recommendation, current_soc, current_prices, pv_forecast, consumption_profile, hour
            )
        
        return recommendation
    
    def _auto_optimize(
        self,
        recommendation: Dict,
        current_soc: float,
        current_prices: List[Dict],
        hour: int
    ) -> Dict:
        """Rule-based automatic optimization."""
        
        # Get current price
        current_price = 0
        for p in current_prices:
            if p['timestamp'] <= datetime.now():
                current_price = p['price']
                break
        
        # Rule 1: If price is cheap (< threshold) and battery not full, charge
        if current_price < self.cheap_price_threshold and current_soc < 0.95:
            recommendation['battery_mode'] = 'charge'
            recommendation['target_soc'] = 0.95
            recommendation['reason'].append(f'Price cheap ({current_price:.3f EUR/kWh}) - charging')
        
        # Rule 2: If price is expensive (> threshold) and battery has charge, discharge
        elif current_price > self.expensive_price_threshold and current_soc > self.min_battery_soc_charge:
            recommendation['battery_mode'] = 'discharge'
            recommendation['reason'].append(f'Price expensive ({current_price:.3f EUR/kWh}) - discharging')
        
        # Rule 3: Evening optimization - ensure enough for night consumption
        if 17 <= hour <= 21 and current_soc < self.target_morning_soc:
            recommendation['battery_mode'] = 'charge'
            recommendation['target_soc'] = max(current_soc, self.target_morning_soc)
            recommendation['reason'].append('Evening: charging for night')
        
        # Rule 4: Night - minimal discharge unless necessary
        if 0 <= hour <= 5:
            if current_soc < self.min_battery_soc_charge + 0.1:
                recommendation['battery_mode'] = 'hold'
                recommendation['reason'].append('Night: holding minimum SOC')
        
        # Rule 5: Morning - ensure enough for daily consumption
        if 6 <= hour <= 9 and current_soc < 0.5:
            recommendation['battery_mode'] = 'charge'
            recommendation['reason'].append('Morning: ensuring adequate SOC')
        
        # EV Charging rules
        cheap_hours = [p for p in current_prices if p['price'] < self.cheap_price_threshold]
        if cheap_hours and current_soc > 0.6:
            recommendation['charge_action'] = 'start'
            recommendation['reason'].append('EV: charging during cheap hours')
        elif current_price > self.expensive_price_threshold and current_soc < 0.4:
            recommendation['charge_action'] = 'stop'
            recommendation['reason'].append('EV: stopping - expensive grid power')
        
        return recommendation
    
    def _ki_optimize(
        self,
        recommendation: Dict,
        current_soc: float,
        current_prices: List[Dict],
        pv_forecast: Dict,
        consumption_profile: Dict,
        hour: int
    ) -> Dict:
        """AI-assisted optimization using learned profile."""
        
        # Use consumption profile if available
        if consumption_profile and hour in consumption_profile:
            profile = consumption_profile[hour]
            expected_consumption = profile.get('avg_consumption_w', 500)
            ev_probability = profile.get('typical_ev_charging', 0.1)
        else:
            expected_consumption = 500
            ev_probability = 0.1
        
        # Calculate expected tonight/tomorrow consumption
        remaining_hours = 24 - hour
        expected_total_consumption = expected_consumption * remaining_hours / 1000  # kWh
        
        # Calculate expected PV production
        pv_estimate_kwh = pv_forecast.get('total_w', 0) / 1000 * remaining_hours / 2  # Rough estimate
        
        # Determine optimal SOC target
        net_demand = expected_total_consumption - pv_estimate_kwh
        optimal_soc = min(0.95, max(self.min_battery_soc_charge, net_demand / self.battery_capacity_kwh))
        
        # Price-based adjustments
        current_price = self._get_current_price(current_prices)
        
        if current_price < self.cheap_price_threshold:
            # Good price - consider charging more
            recommendation['target_soc'] = min(0.95, optimal_soc + 0.2)
            recommendation['battery_mode'] = 'charge'
            recommendation['reason'].append(f'KI: Cheap price + high PV expected, target SOC {recommendation["target_soc"]:.0%}')
        
        elif current_price > self.expensive_price_threshold:
            # Expensive - discharge if we have surplus
            if current_soc > optimal_soc + 0.1:
                recommendation['battery_mode'] = 'discharge'
                recommendation['reason'].append('KI: Expensive grid - using battery')
        
        # EV charging decision
        if ev_probability > 0.5 and current_soc > 0.7:
            # High probability of EV charging tonight
            recommendation['charge_action'] = 'start'
            recommendation['max_power_w'] = min(self.max_charge_power_w, 22000)
            recommendation['reason'].append(f'KI: High EV probability ({ev_probability:.0%})')
        elif current_price < self.cheap_price_threshold and current_soc > 0.5:
            recommendation['charge_action'] = 'start'
            recommendation['reason'].append('KI: Cheap price - EV charging')
        
        # Add learned profile info
        recommendation['expected_consumption_kwh'] = expected_total_consumption
        recommendation['pv_estimate_kwh'] = pv_estimate_kwh
        
        return recommendation
    
    def _get_current_price(self, prices: List[Dict]) -> float:
        """Get current electricity price."""
        now = datetime.now()
        for p in prices:
            if p['timestamp'] <= now:
                return p['price']
        return prices[0]['price'] if prices else 0
    
    def calculate_optimal_charge_times(self, prices: List[Dict], hours_needed: int = 4) -> List[Dict]:
        """
        Find the best hours to charge based on prices.
        
        Args:
            prices: Upcoming prices
            hours_needed: How many hours of charging needed
        
        Returns:
            List of best hours to charge
        """
        if not prices:
            return []
        
        # Sort by price (ascending)
        sorted_prices = sorted(prices, key=lambda x: x['price'])
        
        # Return the cheapest hours
        return sorted_prices[:hours_needed]
    
    def estimate_savings(self, current_prices: List[Dict], profile: Dict = None) -> Dict:
        """
        Estimate potential savings from optimization.
        
        Returns:
            Dict with estimated_savings_eur, cost_without_optimization, etc.
        """
        if not current_prices:
            return {'estimated_savings': 0}
        
        # Calculate average price
        avg_price = sum(p['price'] for p in current_prices) / len(current_prices)
        
        # Estimate daily consumption
        daily_consumption_kwh = 15  # Rough estimate for household
        
        # Without optimization: pay average price for all consumption
        cost_without = daily_consumption_kwh * avg_price
        
        # With optimization: use cheap hours when possible
        cheap_prices = [p['price'] for p in current_prices if p['price'] < self.cheap_price_threshold]
        
        if cheap_prices:
            avg_cheap_price = sum(cheap_prices) / len(cheap_prices)
            # Assume 60% of consumption can be shifted to cheap hours
            cost_with = (daily_consumption_kwh * 0.4 * avg_price) + (daily_consumption_kwh * 0.6 * avg_cheap_price)
        else:
            cost_with = cost_without
        
        savings = cost_without - cost_with
        
        return {
            'estimated_savings_daily_eur': max(0, savings),
            'cost_without_optimization_eur': cost_without,
            'cost_with_optimization_eur': cost_with,
            'avg_price_eur_kwh': avg_price
        }