"""
Vista-Energy — Plugin Registry

Zentrale Verwaltung aller Hardware-Plugins.
Laedt basierend auf config.yaml die richtigen Implementierungen.

Beispiel config.yaml:
  inverter:
    type: fronius_gen24
    ip: 192.168.1.80
  wallbox:
    type: fronius_wattpilot
    ip: 192.168.1.81
  tariff:
    type: tibber
    api_key: xxxxx

Verwendung:
    registry = PluginRegistry(config_path)
    inverter = registry.get_inverter()       # → FroniusGen24Inverter
    tariff = registry.get_tariff()           # → TibberTariff
    wallbox = registry.get_wallbox()         # → WallboxBase oder None
"""

import logging
import os
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)


# ==================================================================
# Plugin-Katalog: Alle bekannten Plugins
# ==================================================================

INVERTER_PLUGINS = {
    'fronius_gen24': {
        'module': 'services.inverters.fronius_gen24',
        'class': 'FroniusGen24Inverter',
        'name': 'Fronius Symo GEN24',
    },
    # Zukuenftige Plugins:
    # 'sma_tripower': {
    #     'module': 'services.inverters.sma_tripower',
    #     'class': 'SMATriPowerInverter',
    #     'name': 'SMA Sunny Tripower',
    # },
    # 'huawei_sun2000': {
    #     'module': 'services.inverters.huawei_sun2000',
    #     'class': 'HuaweiSun2000Inverter',
    #     'name': 'Huawei SUN2000',
    # },
}

WALLBOX_PLUGINS = {
    'fronius_wattpilot': {
        'module': 'services.wallboxes.fronius_wattpilot',
        'class': 'FroniusWattPilotWallbox',
        'name': 'Fronius WattPilot (OCPP)',
    },
    # 'goe_charger': {
    #     'module': 'services.wallboxes.goe_charger',
    #     'class': 'GoEChargerWallbox',
    #     'name': 'go-e Charger',
    # },
}

BATTERY_PLUGINS = {
    # Meist ueber Wechselrichter gesteuert, daher selten direkt noetig
}

TARIFF_PLUGINS = {
    'tibber': {
        'module': 'services.tariffs.tibber',
        'class': 'TibberTariff',
        'name': 'Tibber (dynamisch)',
    },
    'fixed_price': {
        'module': 'services.tariffs.fixed_price',
        'class': 'FixedPriceTariff',
        'name': 'Festpreis (Grundversorger)',
    },
    'dual_rate': {
        'module': 'services.tariffs.dual_rate',
        'class': 'DualRateTariff',
        'name': 'HT/NT Doppeltarif (Tag/Nacht)',
    },
    # 'awattar': {
    #     'module': 'services.tariffs.awattar',
    #     'class': 'AWattarTariff',
    #     'name': 'aWATTar HOURLY',
    # },
}


class PluginRegistry:
    """Zentrale Plugin-Verwaltung.

    Laedt config.yaml und instanziiert die passenden Plugins.
    Faellt auf .env-Werte zurueck wenn keine config.yaml existiert
    (Rueckwaertskompatibilitaet mit bestehenden Installationen).
    """

    DEFAULT_CONFIG = 'config.yaml'

    def __init__(self, app_dir: str = None):
        self.app_dir = Path(app_dir) if app_dir else Path('.')
        self.config_path = self.app_dir / self.DEFAULT_CONFIG
        self.config = self._load_config()

        # Plugin-Instanzen (lazy loading)
        self._inverter = None
        self._wallbox = None
        self._battery = None
        self._tariff = None

    # ------------------------------------------------------------------
    # Oeffentliche API
    # ------------------------------------------------------------------

    def get_inverter(self):
        """Wechselrichter-Plugin holen oder erstellen.

        Returns:
            InverterBase-Instanz oder None
        """
        if self._inverter is None:
            self._inverter = self._load_plugin(
                'inverter', INVERTER_PLUGINS
            )
        return self._inverter

    def get_wallbox(self):
        """Wallbox-Plugin holen (optional, kann None sein).

        Returns:
            WallboxBase-Instanz oder None
        """
        if self._wallbox is None:
            self._wallbox = self._load_plugin(
                'wallbox', WALLBOX_PLUGINS
            )
        return self._wallbox

    def get_battery(self):
        """Batterie-Plugin holen (optional).

        Returns:
            BatteryBase-Instanz oder None
        """
        if self._battery is None:
            self._battery = self._load_plugin(
                'battery', BATTERY_PLUGINS
            )
        return self._battery

    def get_tariff(self):
        """Tarif-Plugin holen.

        Returns:
            TariffBase-Instanz oder None
        """
        if self._tariff is None:
            self._tariff = self._load_plugin(
                'tariff', TARIFF_PLUGINS
            )
        return self._tariff

    def get_system_info(self) -> Dict:
        """Uebersicht aller geladenen Plugins.

        Returns:
            {
                'inverter': {'type': 'fronius_gen24', 'name': ..., 'connected': ...},
                'wallbox':  {...} oder None,
                'battery':  {...} oder None,
                'tariff':   {'type': 'tibber', ...},
            }
        """
        info = {}

        inv = self.get_inverter()
        if inv:
            inv_info = inv.get_info()
            info['inverter'] = {
                'type': inv.plugin_id(),
                'name': inv.plugin_name(),
                'connected': inv_info.get('connected', False),
                'manufacturer': inv_info.get('manufacturer', ''),
                'model': inv_info.get('model', ''),
            }
        else:
            info['inverter'] = None

        wb = self.get_wallbox()
        if wb:
            wb_info = wb.get_info()
            info['wallbox'] = {
                'type': wb.plugin_id(),
                'name': wb.plugin_name(),
                'connected': wb_info.get('connected', False),
            }
        else:
            info['wallbox'] = None

        info['battery'] = None  # Meist ueber Inverter

        tar = self.get_tariff()
        if tar:
            tar_info = tar.get_info()
            info['tariff'] = {
                'type': tar.plugin_id(),
                'name': tar.plugin_name(),
                'connected': tar_info.get('connected', False),
                'provider': tar_info.get('provider', ''),
            }
        else:
            info['tariff'] = None

        return info

    def list_available_plugins(self) -> Dict:
        """Alle verfuegbaren (installierten) Plugins auflisten.

        Fuer Setup-Wizard und Dashboard.
        """
        return {
            'inverters': [
                {'id': k, 'name': v['name']}
                for k, v in INVERTER_PLUGINS.items()
            ],
            'wallboxes': [
                {'id': k, 'name': v['name']}
                for k, v in WALLBOX_PLUGINS.items()
            ],
            'batteries': [
                {'id': k, 'name': v['name']}
                for k, v in BATTERY_PLUGINS.items()
            ],
            'tariffs': [
                {'id': k, 'name': v['name']}
                for k, v in TARIFF_PLUGINS.items()
            ],
        }

    def close_all(self):
        """Alle Plugin-Verbindungen schliessen."""
        for plugin in [self._inverter, self._wallbox, self._battery, self._tariff]:
            if plugin and hasattr(plugin, 'close'):
                try:
                    plugin.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Interne Methoden
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        """config.yaml laden oder aus .env-Werten generieren."""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = yaml.safe_load(f) or {}
                logger.info(f"Konfiguration geladen: {self.config_path}")
                return config
            except Exception as e:
                logger.error(f"config.yaml Fehler: {e}")

        # Fallback: .env-Werte (Rueckwaertskompatibel)
        logger.info("Keine config.yaml gefunden, verwende .env-Fallback")
        return self._config_from_env()

    def _config_from_env(self) -> dict:
        """Konfiguration aus .env-Variablen ableiten (Legacy-Support)."""
        config = {}

        # Fronius Inverter (IP aus WATTPLOT_IP oder Standard)
        fronius_ip = os.getenv('WATTPLOT_IP', '192.168.1.80')
        if fronius_ip:
            config['inverter'] = {
                'type': 'fronius_gen24',
                'ip': fronius_ip,
            }

        # Tibber (API-Key aus TIBBER_API_TOKEN)
        tibber_key = os.getenv('TIBBER_API_TOKEN')
        if tibber_key:
            config['tariff'] = {
                'type': 'tibber',
                'api_key': tibber_key,
            }

        return config

    def _load_plugin(self, category: str, catalog: dict):
        """Plugin dynamisch laden basierend auf Konfiguration.

        Args:
            category: 'inverter', 'wallbox', 'battery', 'tariff'
            catalog: Zugehoeriger Plugin-Katalog

        Returns:
            Plugin-Instanz oder None
        """
        cfg = self.config.get(category)
        if not cfg:
            logger.debug(f"Kein {category} konfiguriert")
            return None

        plugin_type = cfg.get('type')
        if not plugin_type:
            logger.warning(f"{category}: kein 'type' angegeben")
            return None

        if plugin_type not in catalog:
            logger.error(
                f"Unbekanntes {category}-Plugin: '{plugin_type}'. "
                f"Verfuegbar: {list(catalog.keys())}"
            )
            return None

        plugin_info = catalog[plugin_type]

        try:
            # Dynamischer Import
            import importlib
            module = importlib.import_module(plugin_info['module'])
            cls = getattr(module, plugin_info['class'])

            # Konfiguration ohne 'type' als kwargs uebergeben
            kwargs = {k: v for k, v in cfg.items() if k != 'type'}
            instance = cls(**kwargs)

            logger.info(
                f"{category.capitalize()} Plugin geladen: "
                f"{plugin_info['name']} ({plugin_type})"
            )
            return instance

        except ImportError as e:
            logger.error(
                f"{category} Plugin '{plugin_type}' konnte nicht "
                f"importiert werden: {e}"
            )
            return None
        except Exception as e:
            logger.error(
                f"{category} Plugin '{plugin_type}' Initialisierung "
                f"fehlgeschlagen: {e}"
            )
            return None
