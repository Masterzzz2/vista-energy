"""
Vista-Energy — go-e Charger Plugin

Kommunikation via HTTP API v2 (go-e Charger Gemini / HOME+).
Unterstuetzt: go-e Charger Gemini, go-e Charger Gemini flex,
              go-e Charger HOME+ (mit API v2).

Die go-e Wallbox bietet eine lokale HTTP API (kein Cloud noetig).
API v2 Dokumentation: https://github.com/goecharger/go-eCharger-API-v2

Wichtige API-Keys:
  - alw:  Allow charging (0/1)
  - amp:  Ampere (6-32)
  - car:  Fahrzeug-Status (1=bereit, 2=laedt, 3=Warten, 4=fertig)
  - nrg:  Array mit 16 Werten (Spannung, Strom, Leistung pro Phase)
  - wh:   Energie dieser Session (Wh)
  - psm:  Phasen-Modus (1=1-phasig, 2=3-phasig)
  - frc:  Force-State (0=neutral, 1=off, 2=on)

Konfiguration (config.yaml):
  wallbox:
    type: goe_charger
    ip: 192.168.1.100
"""

import logging
from datetime import datetime
from typing import Dict

import requests

from services.wallboxes.base import WallboxBase

logger = logging.getLogger(__name__)


class GoEChargerWallbox(WallboxBase):
    """go-e Charger Gemini / HOME+ via HTTP API v2."""

    def __init__(self, ip: str = '192.168.1.100',
                 timeout: int = 5,
                 **kwargs):
        self.ip = ip
        self.base_url = f"http://{ip}"
        self.timeout = timeout

    # ==================================================================
    # WallboxBase — Pflicht-Methoden
    # ==================================================================

    def get_status(self) -> Dict:
        """Aktueller Wallbox-Status via API v2."""
        data = self._api_get('status')
        if not data:
            return self._fallback_status()

        car = data.get('car', 0)
        is_connected = car in (2, 3, 4)
        is_charging = car == 2

        # Leistung aus nrg-Array (Index 11 = Gesamtleistung in 0.01 kW)
        nrg = data.get('nrg', [0] * 16)
        power_w = 0
        if len(nrg) > 11:
            power_w = nrg[11] * 10  # 0.01 kW → W

        # Strom pro Phase (nrg[4-6] in 0.1A)
        current_a = 0
        if len(nrg) > 6:
            current_a = max(nrg[4], nrg[5], nrg[6]) / 10.0

        return {
            'connected': is_connected,
            'charging': is_charging,
            'power_w': power_w,
            'energy_kwh': data.get('wh', 0) / 1000.0,
            'current_a': current_a,
            'max_current_a': float(data.get('amp', 16)),
            'phases': 3 if data.get('psm', 2) == 2 else 1,
            'timestamp': datetime.now(),
            'car_status': car,
            'allow_charging': data.get('alw', False),
            'temperature': data.get('tma', [0])[0] if data.get('tma') else 0,
        }

    def set_charge_current(self, amps: float) -> bool:
        """Ladestrom setzen (6-32A oder 0 fuer Stop)."""
        if amps <= 0:
            return self.stop_charging()

        amps = int(min(32, max(6, amps)))
        try:
            result = self._api_set('amp', amps)
            if result is not None:
                # Auch Laden erlauben
                self._api_set('frc', 2)  # Force ON
                logger.info(f"go-e Ladestrom: {amps}A")
                return True
            return False
        except Exception as e:
            logger.error(f"go-e set_charge_current({amps}A): {e}")
            return False

    # ==================================================================
    # Optionale Methoden
    # ==================================================================

    def start_charging(self) -> bool:
        """Laden starten (Force ON)."""
        try:
            result = self._api_set('frc', 2)
            return result is not None
        except Exception as e:
            logger.error(f"go-e start: {e}")
            return False

    def stop_charging(self) -> bool:
        """Laden stoppen (Force OFF)."""
        try:
            result = self._api_set('frc', 1)
            return result is not None
        except Exception as e:
            logger.error(f"go-e stop: {e}")
            return False

    def set_phases(self, phases: int) -> bool:
        """Phasen umschalten (1 oder 3).

        Hinweis: Nur moeglich wenn kein Fahrzeug laedt (Auto muss kurz
        getrennt werden fuer Phasenumschaltung).
        """
        if phases not in (1, 3):
            return False
        try:
            psm = 1 if phases == 1 else 2
            result = self._api_set('psm', psm)
            if result is not None:
                logger.info(f"go-e Phasen: {phases}-phasig")
                return True
            return False
        except Exception as e:
            logger.error(f"go-e set_phases: {e}")
            return False

    def get_info(self) -> Dict:
        """go-e Wallbox-Info."""
        data = self._api_get('status')
        if not data:
            return {
                'manufacturer': 'go-e',
                'model': 'Charger Gemini',
                'serial': '',
                'firmware': '',
                'connected': False,
            }
        return {
            'manufacturer': 'go-e',
            'model': data.get('typ', 'Charger Gemini'),
            'serial': data.get('sse', ''),
            'firmware': data.get('fwv', ''),
            'connected': True,
        }

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'goe_charger'

    @staticmethod
    def plugin_name() -> str:
        return 'go-e Charger'

    # ==================================================================
    # Interne Methoden
    # ==================================================================

    def _api_get(self, endpoint: str) -> dict:
        """HTTP GET an go-e API v2."""
        try:
            url = f"{self.base_url}/api/{endpoint}"
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            logger.warning(f"go-e nicht erreichbar: {self.base_url}")
            return {}
        except Exception as e:
            logger.error(f"go-e API GET {endpoint}: {e}")
            return {}

    def _api_set(self, key: str, value) -> dict:
        """Wert setzen via API v2 (GET mit Query-Parameter)."""
        try:
            url = f"{self.base_url}/api/set"
            resp = requests.get(
                url, params={key: value}, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get(key) is not None:
                return data
            return data
        except Exception as e:
            logger.error(f"go-e API SET {key}={value}: {e}")
            return None

    @staticmethod
    def _fallback_status() -> Dict:
        return {
            'connected': False,
            'charging': False,
            'power_w': 0,
            'energy_kwh': 0,
            'current_a': 0,
            'max_current_a': 16,
            'phases': 3,
            'timestamp': datetime.now(),
        }
