"""
Vista-Energy — OpenWB Plugin

Kommunikation via MQTT (lokaler Broker auf der OpenWB).
Unterstuetzt: OpenWB 1.x und 2.x (openWB series 2).

OpenWB ist eine Open-Source Wallbox-Steuerung mit eigenem MQTT-Broker.
Die Ladeleistung wird ueber MQTT-Topics gesteuert.

MQTT-Topics (openWB 2.x):
  Lesen:
  - openWB/chargepoint/0/get/power         → Ladeleistung W
  - openWB/chargepoint/0/get/currents       → [I1, I2, I3] in A
  - openWB/chargepoint/0/get/energy_counter → kWh gesamt
  - openWB/chargepoint/0/get/plug_state     → True/False
  - openWB/chargepoint/0/get/charge_state   → True/False
  - openWB/chargepoint/0/get/phases_in_use  → 1 oder 3

  Schreiben:
  - openWB/set/chargepoint/0/set/current    → Sollstrom in A
  - openWB/set/chargepoint/0/set/manual_lock → 0/1

  MQTT-Topics (openWB 1.x - Legacy):
  - openWB/lp/1/W                           → Ladeleistung W
  - openWB/lp/1/%Soc                        → Fahrzeug SOC
  - openWB/set/lp1/DirectChargeAmps         → Sollstrom

Konfiguration (config.yaml):
  wallbox:
    type: openwb
    ip: 192.168.1.110       # OpenWB IP
    mqtt_port: 1883          # optional
    chargepoint: 0           # optional, default 0
    version: 2               # 1 oder 2, default 2
"""

import json
import logging
import time
from datetime import datetime
from typing import Dict

from services.wallboxes.base import WallboxBase

logger = logging.getLogger(__name__)


class OpenWBWallbox(WallboxBase):
    """OpenWB Wallbox via MQTT."""

    def __init__(self, ip: str = '192.168.1.110',
                 mqtt_port: int = 1883,
                 chargepoint: int = 0,
                 version: int = 2,
                 timeout: int = 5,
                 **kwargs):
        self.ip = ip
        self.mqtt_port = mqtt_port
        self.chargepoint = chargepoint
        self.version = version
        self.timeout = timeout
        self._mqtt_client = None
        self._last_data = {}
        self._data_received = False

    # ==================================================================
    # WallboxBase — Pflicht-Methoden
    # ==================================================================

    def get_status(self) -> Dict:
        """Aktueller OpenWB-Status via MQTT oder HTTP-API Fallback."""
        data = self._read_mqtt_data()
        if not data:
            # HTTP-API Fallback (openWB 1.x & 2.x haben /api/ Endpoint)
            data = self._http_fallback()

        if not data:
            return self._fallback_status()

        return {
            'connected': bool(data.get('plug_state', False)),
            'charging': bool(data.get('charge_state', False)),
            'power_w': float(data.get('power', 0)),
            'energy_kwh': float(data.get('energy_counter', 0)),
            'current_a': float(data.get('current', 0)),
            'max_current_a': float(data.get('max_current', 32)),
            'phases': int(data.get('phases_in_use', 3)),
            'timestamp': datetime.now(),
            'vehicle_soc': data.get('vehicle_soc'),
        }

    def set_charge_current(self, amps: float) -> bool:
        """Ladestrom setzen via MQTT."""
        if amps <= 0:
            return self.stop_charging()

        amps = int(min(32, max(6, amps)))

        # Zuerst MQTT versuchen
        if self._mqtt_publish(self._topic_set('current'), amps):
            logger.info(f"OpenWB Ladestrom: {amps}A")
            return True

        # HTTP-API Fallback
        return self._http_set_current(amps)

    # ==================================================================
    # Optionale Methoden
    # ==================================================================

    def start_charging(self) -> bool:
        """Laden freigeben (Manual Lock = 0)."""
        if self._mqtt_publish(self._topic_set('manual_lock'), 0):
            return True
        return self._http_command('start')

    def stop_charging(self) -> bool:
        """Laden sperren (Manual Lock = 1)."""
        if self._mqtt_publish(self._topic_set('manual_lock'), 1):
            return True
        return self._http_command('stop')

    def set_phases(self, phases: int) -> bool:
        """Phasen umschalten (1 oder 3)."""
        if phases not in (1, 3):
            return False
        topic = self._topic_set('phases_to_use')
        return self._mqtt_publish(topic, phases)

    def get_info(self) -> Dict:
        """OpenWB-Info."""
        connected = self._check_connection()
        return {
            'manufacturer': 'OpenWB',
            'model': f'openWB {"series 2" if self.version == 2 else "1.x"}',
            'serial': '',
            'firmware': f'v{self.version}.x',
            'connected': connected,
        }

    # ==================================================================
    # Plugin-Registrierung
    # ==================================================================

    @staticmethod
    def plugin_id() -> str:
        return 'openwb'

    @staticmethod
    def plugin_name() -> str:
        return 'OpenWB'

    # ==================================================================
    # Interne Methoden — MQTT
    # ==================================================================

    def _topic_get(self, key: str) -> str:
        """MQTT-Topic fuer Lesen (v2)."""
        if self.version == 2:
            return f"openWB/chargepoint/{self.chargepoint}/get/{key}"
        # v1 Legacy
        lp = self.chargepoint + 1
        v1_map = {
            'power': f'openWB/lp/{lp}/W',
            'plug_state': f'openWB/lp/{lp}/boolPlugStat',
            'charge_state': f'openWB/lp/{lp}/boolChargeStat',
            'energy_counter': f'openWB/lp/{lp}/kWhCounter',
        }
        return v1_map.get(key, f'openWB/lp/{lp}/{key}')

    def _topic_set(self, key: str) -> str:
        """MQTT-Topic fuer Schreiben (v2)."""
        if self.version == 2:
            return f"openWB/set/chargepoint/{self.chargepoint}/set/{key}"
        # v1 Legacy
        lp = self.chargepoint + 1
        v1_map = {
            'current': f'openWB/set/lp{lp}/DirectChargeAmps',
            'manual_lock': f'openWB/set/lp{lp}/ChargePointEnabled',
        }
        return v1_map.get(key, f'openWB/set/lp{lp}/{key}')

    def _get_mqtt_client(self):
        """MQTT-Client erstellen."""
        if self._mqtt_client is None:
            try:
                import paho.mqtt.client as mqtt
                self._mqtt_client = mqtt.Client(
                    client_id=f"vista-energy-openwb-{self.chargepoint}"
                )

                def on_message(client, userdata, msg):
                    try:
                        value = json.loads(msg.payload.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        value = msg.payload.decode()
                    self._last_data[msg.topic] = value
                    self._data_received = True

                self._mqtt_client.on_message = on_message
            except ImportError:
                logger.error("paho-mqtt nicht installiert: pip install paho-mqtt")
                return None
        return self._mqtt_client

    def _read_mqtt_data(self) -> dict:
        """Alle relevanten Topics lesen via MQTT."""
        client = self._get_mqtt_client()
        if not client:
            return {}

        try:
            client.connect(self.ip, self.mqtt_port, keepalive=10)

            # Alle Chargepoint-Topics abonnieren
            if self.version == 2:
                topic = f"openWB/chargepoint/{self.chargepoint}/get/#"
            else:
                lp = self.chargepoint + 1
                topic = f"openWB/lp/{lp}/#"

            client.subscribe(topic)
            self._data_received = False

            # Kurz warten auf Nachrichten (retained messages kommen sofort)
            deadline = time.time() + 2.0
            client.loop_start()
            while not self._data_received and time.time() < deadline:
                time.sleep(0.1)
            client.loop_stop()
            client.disconnect()

            # Daten extrahieren
            result = {}
            for key in ['power', 'plug_state', 'charge_state',
                        'energy_counter', 'currents', 'phases_in_use']:
                full_topic = self._topic_get(key)
                if full_topic in self._last_data:
                    result[key] = self._last_data[full_topic]

            # Strom aus currents-Array
            if 'currents' in result and isinstance(result['currents'], list):
                result['current'] = max(result['currents'])

            return result if result else {}

        except Exception as e:
            logger.debug(f"OpenWB MQTT fehlgeschlagen: {e}")
            return {}

    def _mqtt_publish(self, topic: str, value) -> bool:
        """Wert via MQTT publishen."""
        client = self._get_mqtt_client()
        if not client:
            return False
        try:
            client.connect(self.ip, self.mqtt_port, keepalive=5)
            payload = json.dumps(value) if not isinstance(value, str) else value
            result = client.publish(topic, payload, qos=1)
            result.wait_for_publish(timeout=3)
            client.disconnect()
            return result.is_published()
        except Exception as e:
            logger.error(f"OpenWB MQTT publish {topic}: {e}")
            return False

    # ==================================================================
    # Interne Methoden — HTTP Fallback
    # ==================================================================

    def _http_fallback(self) -> dict:
        """HTTP-API als Fallback (openWB hat auch HTTP-Endpoints)."""
        try:
            import requests
            url = f"http://{self.ip}/api/get/chargepoint"
            resp = requests.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                cp = data.get(str(self.chargepoint), data)
                return cp.get('get', cp) if isinstance(cp, dict) else {}
        except Exception as e:
            logger.debug(f"OpenWB HTTP Fallback: {e}")
        return {}

    def _http_set_current(self, amps: int) -> bool:
        try:
            import requests
            url = f"http://{self.ip}/api/set/chargepoint/{self.chargepoint}/set/current"
            resp = requests.post(url, json=amps, timeout=self.timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def _http_command(self, cmd: str) -> bool:
        try:
            import requests
            lock = 0 if cmd == 'start' else 1
            url = f"http://{self.ip}/api/set/chargepoint/{self.chargepoint}/set/manual_lock"
            resp = requests.post(url, json=lock, timeout=self.timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def _check_connection(self) -> bool:
        """Pruefen ob OpenWB erreichbar ist."""
        try:
            import requests
            resp = requests.get(
                f"http://{self.ip}/", timeout=3
            )
            return resp.status_code == 200
        except Exception:
            return False

    def close(self):
        if self._mqtt_client:
            try:
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None

    @staticmethod
    def _fallback_status() -> Dict:
        return {
            'connected': False,
            'charging': False,
            'power_w': 0,
            'energy_kwh': 0,
            'current_a': 0,
            'max_current_a': 32,
            'phases': 3,
            'timestamp': datetime.now(),
        }
