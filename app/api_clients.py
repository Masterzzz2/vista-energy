"""
API Clients für Energy Optimizer
Fronius GEN24, Wattpilot, Tibber, Wetter, PV-Prognose
"""

import requests
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ==================== TIBBER ====================

class TibberClient:
    """Tibber API Client für dynamische Strompreise"""
    
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.endpoint = "https://api.tibber.com/v1-beta/gql"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
    
    def _query(self, query: str, variables: dict = None) -> dict:
        """Führt eine GraphQL Anfrage aus"""
        try:
            response = requests.post(
                self.endpoint,
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"Tibber API HTTP Fehler: {e.response.status_code} - {e.response.text}")
            return {"data": None, "error": str(e)}
        except Exception as e:
            logger.error(f"Tibber API Fehler: {e}")
            return {"data": None}
    
    def get_current_prices(self) -> list:
        """Holt aktuelle und kommende Strompreise"""
        query = """
        {
            viewer {
                homes {
                    currentSubscription {
                        priceInfo {
                            today {
                                total
                                currency
                                startsAt
                            }
                            tomorrow {
                                total
                                currency
                                startsAt
                            }
                        }
                    }
                }
            }
        }
        """
        
        result = self._query(query)
        prices = []
        
        try:
            data = result.get("data", {}).get("viewer", {}).get("homes", [{}])
            if data:
                price_info = data[0].get("currentSubscription", {}).get("priceInfo", {})
                
                # Heute
                for p in price_info.get("today", []):
                    prices.append({
                        "timestamp": p["startsAt"],
                        "price": p["total"],
                        "currency": p.get("currency", "EUR"),
                        "level": self._get_price_level(p["total"])
                    })
                
                # Morgen (falls verfügbar)
                for p in price_info.get("tomorrow", []):
                    prices.append({
                        "timestamp": p["startsAt"],
                        "price": p["total"],
                        "currency": p.get("currency", "EUR"),
                        "level": self._get_price_level(p["total"])
                    })
        except Exception as e:
            logger.error(f"Tibber Preise Fehler: {e}")
        
        return prices
    
    def _get_price_level(self, price: float) -> str:
        """Bestimmt Preislevel"""
        if price < 0.15:
            return "CHEAP"
        elif price < 0.25:
            return "NORMAL"
        elif price < 0.35:
            return "EXPENSIVE"
        else:
            return "VERY_EXPENSIVE"
    
    def get_homes(self) -> list:
        """Holt Tibber Homes"""
        query = "{ viewer { homes { id address { address1 city } } } }"
        result = self._query(query)
        return result.get("data", {}).get("viewer", {}).get("homes", [])


# ==================== FRONIUS GEN24 ====================

class FroniusClient:
    """Fronius Symo GEN24 Wechselrichter API Client"""
    
    def __init__(self, ip: str):
        self.ip = ip
        self.base_url = f"http://{ip}"
    
    def _get(self, endpoint: str) -> dict:
        """Führt GET Request aus"""
        try:
            response = requests.get(f"{self.base_url}{endpoint}", timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Fronius API Fehler ({endpoint}): {e}")
            return {}
    
    def get_power_flow(self) -> dict:
        """Holt Echtzeit-Leistungsfluss"""
        data = self._get("/solar_api/v1/GetPowerFlowRealtimeData.fcgi")
        
        if not data:
            return {}
        
        try:
            site = data.get("Body", {}).get("Data", {}).get("Site", {})
            return {
                "pv_power": site.get("P_PV", 0),
                "grid_power": site.get("P_Grid", 0),
                "battery_power": site.get("P_Akku", 0),
                "house_power": site.get("P_Load", 0),
                "autonomy": site.get("rel_SelfConsumption", 0),
                "self_consumption": site.get("rel_Autonomy", 0)
            }
        except Exception as e:
            logger.error(f"PowerFlow Parse Fehler: {e}")
            return {}
    
    def get_pv_production(self) -> dict:
        """Holt PV Produktionsdaten"""
        now = datetime.now().isoformat()
        power_flow = self.get_power_flow()
        
        return {
            "timestamp": now,
            "power_w": power_flow.get("pv_power", 0)
        }
    
    def get_battery_status(self) -> dict:
        """Holt Batterie Status"""
        now = datetime.now().isoformat()
        power_flow = self.get_power_flow()
        
        # Batteriedaten aus PowerFlow
        battery_power = power_flow.get("battery_power", 0)
        
        # Versuche detaillierte Batteriedaten
        data = self._get("/solar_api/v1/GetBatteryStorageStatData.fcgi")
        
        soc = 50  # Default
        status = "unknown"
        
        try:
            if data.get("Body", {}).get("Data", {}):
                battery_data = data["Body"]["Data"]
                soc = battery_data.get("ModuleType", {}).get("Value", 50)
                status = "discharging" if battery_power < 0 else "charging"
        except:
            pass
        
        return {
            "timestamp": now,
            "soc_percent": soc,
            "power_w": battery_power,
            "status": status
        }
    
    def get_house_consumption(self) -> dict:
        """Holt Hausverbrauch"""
        now = datetime.now().isoformat()
        power_flow = self.get_power_flow()
        
        return {
            "timestamp": now,
            "power_w": abs(power_flow.get("house_power", 0))
        }
    
    def get_meter_data(self) -> dict:
        """Holt Zählerdaten"""
        data = self._get("/solar_api/v1/GetMeterRealtimeData.cgi")
        
        try:
            meters = data.get("Body", {}).get("Data", {})
            if meters:
                meter = list(meters.values())[0]
                return {
                    "power_active": meter.get("power_real", 0),
                    "energy_real": meter.get("energy_real", 0),
                    "voltage": meter.get("voltage", [0])[0]
                }
        except Exception as e:
            logger.error(f"Meter Parse Fehler: {e}")
        
        return {}


# ==================== WATTPILOT ====================

class WattpilotClient:
    """Fronius Wattpilot Go EV Charger API Client"""
    
    def __init__(self, ip: str):
        self.ip = ip
        self.base_url = f"http://{ip}"
    
    def _get(self, endpoint: str) -> dict:
        """Führt GET Request aus"""
        try:
            response = requests.get(f"{self.base_url}{endpoint}", timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Wattpilot API Fehler ({endpoint}): {e}")
            return {}
    
    def _post(self, endpoint: str, data: dict = None) -> bool:
        """Führt POST Request aus"""
        try:
            response = requests.post(f"{self.base_url}{endpoint}", json=data or {}, timeout=5)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Wattpilot POST Fehler ({endpoint}): {e}")
            return False
    
    def get_status(self) -> dict:
        """Holt Wattpilot Status"""
        now = datetime.now().isoformat()
        data = self._get("/api/status")
        
        if not data:
            return {
                "timestamp": now,
                "power_w": 0,
                "status": "offline",
                "plugged_in": 0
            }
        
        return {
            "timestamp": now,
            "power_w": data.get("power", 0) * 1000,  # kW -> W
            "energy_wh": data.get("energy", 0) * 1000,  # kWh -> Wh
            "status": data.get("status", "unknown"),
            "charge_limit": data.get("set_current", 0),
            "plugged_in": 1 if data.get("plugs", [{}])[0].get("cp", 0) == 1 else 0,
            "car_connected": data.get("car", 0) == 1,
            "current_a": data.get("actual_current", 0),
            "voltage": data.get("voltage", [0, 0, 0]),
            "temperature": data.get("temp", 0)
        }
    
    def set_charge_power(self, power_kw: float) -> bool:
        """Setzt Ladeleistung in kW"""
        power_w = int(power_kw * 1000)
        return self._post("/api/set", {"power": power_w})
    
    def set_current(self, current_ma: int) -> bool:
        """Setzt Ladestrom in mA"""
        return self._post("/api/set", {"current": current_ma})
    
    def start_charging(self) -> bool:
        """Startet Ladung"""
        return self._post("/api/set", {"frena": 0, "iona": 0})
    
    def stop_charging(self) -> bool:
        """Stoppt Ladung"""
        return self._post("/api/set", {"frena": 1})
    
    def get_available_current(self) -> int:
        """Max verfügbarer Strom in A"""
        status = self.get_status()
        return min(32, 22)  # Wattpilot max 22kW = 32A


# ==================== OPEN-METEO WETTER ====================

class WeatherClient:
    """Open-Meteo Wetter API Client (kostenlos)"""
    
    def __init__(self, latitude: float, longitude: float):
        self.lat = latitude
        self.lon = longitude
        self.base_url = "https://api.open-meteo.com/v1/forecast"
    
    def get_current_weather(self) -> dict:
        """Holt aktuelles Wetter"""
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,cloud_cover",
            "hourly": "shortwave_radiation",
            "timezone": "Europe/Berlin",
            "forecast_days": 2
        }
        
        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            current = data.get("current", {})
            hourly = data.get("hourly", {})
            
            # Radiation für PV-Schätzung
            radiation = hourly.get("shortwave_radiation", [0])[0] if hourly.get("shortwave_radiation") else 0
            
            return {
                "timestamp": datetime.now().isoformat(),
                "temperature_c": current.get("temperature_2m"),
                "humidity_percent": current.get("relative_humidity_2m"),
                "precipitation_mm": current.get("precipitation"),
                "weather_code": current.get("weather_code"),
                "cloud_percent": current.get("cloud_cover"),
                "radiation_wm2": radiation
            }
        except Exception as e:
            logger.error(f"Weather API Fehler: {e}")
            return {"timestamp": datetime.now().isoformat()}
    
    def get_forecast(self, hours: int = 48) -> list:
        """Holt Wetterprognose"""
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "hourly": "temperature_2m,precipitation_probability,weather_code,cloud_cover",
            "timezone": "Europe/Berlin",
            "forecast_days": 2
        }
        
        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            
            forecast = []
            for i, t in enumerate(times[:hours]):
                forecast.append({
                    "timestamp": t,
                    "temperature_c": hourly.get("temperature_2m", [0])[i],
                    "precipitation_prob": hourly.get("precipitation_probability", [0])[i],
                    "weather_code": hourly.get("weather_code", [0])[i],
                    "cloud_percent": hourly.get("cloud_cover", [0])[i]
                })
            
            return forecast
        except Exception as e:
            logger.error(f"Weather Forecast Fehler: {e}")
            return []


# ==================== FORECAST.SOLAR ====================

class ForecastSolarClient:
    """forecast.solar PV Prognose API"""
    
    def __init__(self, api_key: str, latitude: float, longitude: float, power_kwp: float):
        self.api_key = api_key
        self.lat = latitude
        self.lon = longitude
        self.power_kwp = power_kwp
        self.base_url = "https://api.forecast.solar"
    
    def get_estimate(self) -> dict:
        """Holt PV Schätzung"""
        params = {
            "key": self.api_key,
            "lat": self.lat,
            "lon": self.lon,
            "kwp": self.power_kwp
        }
        
        try:
            response = requests.get(f"{self.base_url}/estimate", params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Forecast.Solar Fehler: {e}")
            return {}
    
    def get_forecast(self) -> list:
        """Holt stündliche PV Prognose für heute und morgen"""
        estimate = self.get_estimate()
        
        if not estimate:
            return []
        
        forecasts = []
        now = datetime.now()
        
        # Verarbeite heutige und morgige Daten
        for day_key in ["today", "tomorrow"]:
            day_data = estimate.get(day_key, {})
            for hour, watt in day_data.items():
                try:
                    hour_int = int(hour)
                    if day_key == "today":
                        ts = now.replace(hour=hour_int, minute=0, second=0, microsecond=0)
                    else:
                        ts = (now + timedelta(days=1)).replace(hour=hour_int, minute=0, second=0, microsecond=0)
                    
                    forecasts.append({
                        "timestamp": ts.isoformat(),
                        "source": "forecast.solar",
                        "power_w": int(watt)
                    })
                except:
                    pass
        
        return forecasts
    
    def estimate_pv_today(self) -> float:
        """Schätzt PV Ertrag heute in kWh"""
        estimate = self.get_estimate()
        
        if not estimate:
            return 0.0
        
        today = estimate.get("today", {})
        total = sum(today.values()) if today else 0
        
        return total / 1000  # Wh -> kWh
