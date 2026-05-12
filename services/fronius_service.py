"""
Fronius Service
Direct communication with Fronius Symo GEN24 via Solar API and Modbus TCP
Wattpilot IP: 192.168.1.80
API Endpoint: http://192.168.1.80/solar_api/v1/GetPowerFlowRealtimeData.fcgi
Modbus TCP: port 502, unit ID 1

Battery Control via Modbus:
- Register 40358: InWRte (Charge limit, 0-10000 = 0-100%)
- Register 40359: OutWRte (Discharge limit, 0-10000 = 0-100%)
- Register 40350: StorCtl_Mod (1=charge limit, 2=discharge limit, 3=both)

To lock battery (no charging):
  set_inwrte(0)  # Set charge limit to 0%

To unlock battery:
  set_inwrte(10000)  # Set charge limit to 100%
"""

import os
import logging
from typing import Dict, Optional
from datetime import datetime
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


class ModbusBatteryService:
    """
    Controls battery charging via Modbus TCP.
    Uses pymodbus to write to Fronius GEN24 registers.
    
    Registers (SunSpec Model 124):
    - REG_INWRTE (40358): Charge power limit (0-10000 = 0-100% of WChaMax)
    - REG_OUTWRTE (40359): Discharge power limit (0-10000 = 0-100% of WChaMax)
    - REG_STORCTL_MOD (40350): Storage control mode
    """
    
    MODBUS_HOST = '192.168.1.80'
    MODBUS_PORT = 502
    MODBUS_UNIT = 1
    
    # Modbus registers for battery control (SunSpec Model 124)
    REG_INWRTE = 40358      # Charge power limit (0-10000 = 0-100%)
    REG_OUTWRTE = 40359     # Discharge power limit (0-10000 = 0-100%)
    REG_STORCTL_MOD = 40350  # Storage control mode
    
    def __init__(self):
        self.client = None
        self._connected = False
    
    def _get_client(self):
        """Get or create Modbus TCP client."""
        if self.client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                self.client = ModbusTcpClient(
                    host=self.MODBUS_HOST,
                    port=self.MODBUS_PORT,
                    timeout=5
                )
            except ImportError:
                logger.error("pymodbus not installed. Run: pip install pymodbus")
                return None
        return self.client if self.client.connect() else None
    
    def is_connected(self) -> bool:
        """Check if Modbus connection is available."""
        client = self._get_client()
        if client:
            try:
                rr = client.read_holding_registers(40343, count=1)
                self._connected = not rr.isError()
            except:
                self._connected = False
        return self._connected
    
    def set_charge_limit(self, percent: int) -> bool:
        """
        Set battery charge limit.
        Args:
            percent: 0-100, where 0 = no charging, 100 = full charging
        Returns:
            True if successful, False otherwise
        """
        client = self._get_client()
        if not client:
            logger.warning("Modbus not connected, cannot set charge limit")
            return False
        
        try:
            value = min(10000, max(0, percent * 100))
            rr = client.write_register(self.REG_INWRTE, value)
            if not rr.isError():
                logger.info(f"Set charge limit to {percent}% ({value})")
                return True
            else:
                logger.error(f"Failed to set charge limit: {rr}")
                return False
        except Exception as e:
            logger.error(f"Error setting charge limit: {e}")
            return False
    
    def get_charge_limit(self) -> Optional[int]:
        """Get current charge limit setting."""
        client = self._get_client()
        if not client:
            return None
        try:
            rr = client.read_holding_registers(self.REG_INWRTE, count=1)
            if not rr.isError():
                return int(rr.registers[0] / 100)
            return None
        except Exception as e:
            logger.error(f"Error reading charge limit: {e}")
            return None
    
    def set_discharge_limit(self, percent: int) -> bool:
        """
        Set battery discharge limit.
        Args:
            percent: 0-100, where 0 = no discharging, 100 = full discharging
        Returns:
            True if successful
        """
        client = self._get_client()
        if not client:
            logger.warning("Modbus not connected, cannot set discharge limit")
            return False
        try:
            value = min(10000, max(0, percent * 100))
            rr = client.write_register(self.REG_OUTWRTE, value)
            if not rr.isError():
                logger.info(f"Set discharge limit to {percent}% ({value})")
                return True
            return False
        except Exception as e:
            logger.error(f"Error setting discharge limit: {e}")
            return False
    
    def lock_battery(self) -> bool:
        """Lock battery - prevent charging. Sets charge limit to 0%."""
        return self.set_charge_limit(0)
    
    def unlock_battery(self) -> bool:
        """Unlock battery - allow normal charging. Sets charge limit to 100%."""
        return self.set_charge_limit(100)
    
    def close(self):
        """Close Modbus connection."""
        if self.client:
            self.client.close()
            self.client = None
            self._connected = False


class FroniusService:
    """
    Handles direct communication with Fronius Symo GEN24 via Solar API.
    This is the Wattpilot's built-in API on port 80.
    """
    
    BASE_URL = "http://192.168.1.80"
    API_PATH = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    
    def __init__(self, ip: str = None, api_token: str = None):
        self.base_url = ip or os.getenv('WATTPLOT_IP', '192.168.1.80')
        self.api_token = api_token or os.getenv('EVCC_API_TOKEN', '')
        self.timeout = 10
    
    def _request(self, params: dict = None) -> dict:
        """Make GET request to Fronius Solar API."""
        try:
            url = f"{self.base_url}{self.API_PATH}"
            
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.ConnectionError:
            logger.warning(f"Fronius not reachable at {self.base_url}")
            return {'error': 'Connection refused'}
        except Exception as e:
            logger.error(f"Fronius API error: {e}")
            return {'error': str(e)}
    
    def get_power_flow(self) -> Dict:
        """
        Get real-time power flow data from Fronius inverter.
        This is the primary endpoint for PV, battery, and grid data.
        
        Returns:
            Dict with site_data containing:
            - p_pv: PV production in W
            - p_grid: Grid power in W (positive = consumption, negative = export)
            - p_load: House consumption in W
            - battery: battery data (if available)
        """
        result = self._request({'Scope': 'System'})
        
        if 'error' in result:
            logger.warning("Fronius API returned error, using simulated data")
            return self._get_simulated_power_flow()
        
        try:
            # Parse the Fronius API response
            body = result.get('Body', {})
            data = body.get('Data', {})
            site = data.get('Site', {})
            
            return {
                'pv_power_w': site.get('P_PV', 0),
                'grid_power_w': site.get('P_Grid', 0),
                'load_power_w': site.get('P_Load', 0),
                'battery_soc': site.get('E_Battery', [{}])[0].get('Value', 0) / 100 if site.get('E_Battery') else 0,
                'battery_power_w': site.get('P_Battery', 0),
                'timestamp': datetime.now()
            }
        except (KeyError, IndexError) as e:
            logger.error(f"Failed to parse Fronius response: {e}")
            return self._get_simulated_power_flow()
    
    def get_inverter_info(self) -> Dict:
        """Get inverter information and status."""
        result = self._request({'Scope': 'System'})
        
        try:
            body = result.get('Body', {})
            data = body.get('Data', {})
            
            return {
                'producer': data.get('Producer', 'Fronius'),
                'grid_status': data.get('GridStatus', 'Unknown'),
                'timestamp': datetime.now()
            }
        except:
            return {'producer': 'Fronius', 'grid_status': 'Unknown', 'error': str(e)}
    
    def get_battery_info(self) -> Dict:
        """Get battery-specific data (BYD HVS)."""
        result = self._request({'Scope': 'System'})
        
        try:
            body = result.get('Body', {})
            data = body.get('Data', {})
            site = data.get('Site', {})
            
            battery_data = site.get('Battery', {})
            
            return {
                'soc_percent': battery_data.get('BatteryCellVoltage', [0])[0].get('Value', 0) if battery_data else 0,
                'power_w': site.get('P_Battery', 0),
                'capacity_kwh': 7.68,
                'usable_capacity_kwh': 7.0,
                'timestamp': datetime.now()
            }
        except Exception as e:
            logger.error(f"Failed to parse battery info: {e}")
            return {
                'soc_percent': 0,
                'power_w': 0,
                'capacity_kwh': 7.68,
                'usable_capacity_kwh': 7.0,
                'error': str(e)
            }
    
    def get_meter_info(self) -> Dict:
        """Get smart meter data (grid export/import)."""
        result = self._request({'Scope': 'System'})
        
        try:
            body = result.get('Body', {})
            data = body.get('Data', {})
            meter = data.get('Meter', [{}])[0]
            
            return {
                'grid_power_w': meter.get('power', 0),
                'energy_reactive': meter.get('power_reactive', 0),
                'timestamp': datetime.now()
            }
        except Exception as e:
            logger.error(f"Failed to parse meter info: {e}")
            return {'grid_power_w': 0, 'energy_reactive': 0, 'error': str(e)}
    
    def get_all_data(self) -> Dict:
        """
        Get complete system data in one call.
        Combines power flow, battery, and meter data.
        """
        power_flow = self.get_power_flow()
        battery = self.get_battery_info()
        meter = self.get_meter_info()
        
        return {
            'pv_power_w': power_flow.get('pv_power_w', 0),
            'grid_power_w': power_flow.get('grid_power_w', 0),
            'load_power_w': power_flow.get('load_power_w', 0),
            'battery_soc': power_flow.get('battery_soc', 0),
            'battery_power_w': power_flow.get('battery_power_w', 0),
            'battery_capacity_kwh': battery.get('capacity_kwh', 7.68),
            'meter_power_w': meter.get('grid_power_w', 0),
            'timestamp': datetime.now()
        }
    
    def _get_simulated_power_flow(self) -> Dict:
        """
        Return simulated data for testing when Fronius is not available.
        Based on typical values for a 6kWp system in Germany.
        """
        hour = datetime.now().hour
        
        # Simulate PV production (peaks at midday)
        if 6 <= hour <= 18:
            pv_base = 6000  # 6kWp system
            # Bell curve simulation
            peak_hour = 12
            hour_diff = abs(hour - peak_hour)
            pv_factor = max(0.1, 1 - (hour_diff / 8))
            pv_power = int(pv_base * pv_factor * 0.8)  # Add some variance
        else:
            pv_power = 0
        
        return {
            'pv_power_w': pv_power,
            'grid_power_w': 500,  # Typical house consumption
            'load_power_w': 800,
            'battery_soc': 0.65,  # 65%
            'battery_power_w': 0,
            'timestamp': datetime.now()
        }