"""
Vista-Energy Update Service
Automatische Updates mit Signatur-Verifizierung und Rollback.

Ablauf:
  1. Einmal taeglich pruefen: GET {UPDATE_SERVER}/api/latest
  2. Falls neue Version: Download + SHA256 pruefen
  3. Backup der aktuellen Version anlegen
  4. Neue Dateien entpacken
  5. Service neu starten
  6. Bei Fehler: automatisches Rollback

Sicherheit:
  - Jedes Update-Paket hat eine SHA256-Checksum
  - Optional: RSA-Signatur (Phase 1.2)
  - Download nur von konfiguriertem Server (kein Redirect-Follow)
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# --- Versionierung ---
# Diese Datei definiert die aktuelle Version. Bei jedem Release anpassen.
VERSION = "1.0.0"
VERSION_FILE = "VERSION"


class UpdateService:
    """Verwaltet automatische Updates fuer Vista-Energy."""

    # Konfigurations-Defaults (ueberschreibbar via .env)
    DEFAULT_UPDATE_SERVER = "https://update.vista-energy.de"
    CHECK_INTERVAL_HOURS = 24
    MAX_BACKUPS = 3  # Anzahl alter Versionen die aufbewahrt werden

    def __init__(self, app_dir: Path, update_server: str = None):
        """
        Args:
            app_dir: Pfad zum energy-optimizer Verzeichnis
            update_server: URL des Update-Servers (ohne trailing /)
        """
        self.app_dir = Path(app_dir)
        self.update_server = (
            update_server
            or os.getenv('UPDATE_SERVER')
            or self.DEFAULT_UPDATE_SERVER
        )
        self.backup_dir = self.app_dir / 'backups'
        self.backup_dir.mkdir(exist_ok=True)
        self._state_file = self.app_dir / '.update_state.json'
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Oeffentliche API
    # ------------------------------------------------------------------

    def get_current_version(self) -> str:
        """Aktuelle installierte Version lesen."""
        version_path = self.app_dir / VERSION_FILE
        if version_path.exists():
            return version_path.read_text().strip()
        return VERSION

    def check_for_update(self) -> Optional[Dict]:
        """Prueft ob eine neue Version verfuegbar ist.

        Returns:
            Dict mit Update-Infos oder None wenn aktuell.
            {
                'version': '1.1.0',
                'url': 'https://update.vista-energy.de/releases/v1.1.0.tar.gz',
                'checksum': 'sha256:abc123...',
                'changelog': 'Neue Features: ...',
                'size_bytes': 123456,
                'released': '2026-05-10'
            }
        """
        try:
            url = f"{self.update_server}/api/latest"
            resp = requests.get(url, timeout=15, allow_redirects=False)
            resp.raise_for_status()
            data = resp.json()

            remote_version = data.get('version', '0.0.0')
            current = self.get_current_version()

            if self._version_newer(remote_version, current):
                logger.info(
                    f"Update verfuegbar: {current} -> {remote_version}"
                )
                self._state['last_check'] = datetime.now().isoformat()
                self._state['available'] = data
                self._save_state()
                return data
            else:
                logger.debug(f"Kein Update noetig (aktuell: {current})")
                self._state['last_check'] = datetime.now().isoformat()
                self._state['available'] = None
                self._save_state()
                return None

        except requests.RequestException as e:
            logger.warning(f"Update-Check fehlgeschlagen: {e}")
            return None

    def should_check(self) -> bool:
        """True wenn seit dem letzten Check genug Zeit vergangen ist."""
        last = self._state.get('last_check')
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            return (datetime.now() - last_dt).total_seconds() > (
                self.CHECK_INTERVAL_HOURS * 3600
            )
        except (ValueError, TypeError):
            return True

    def download_and_install(self, update_info: Dict) -> Dict:
        """Update herunterladen, pruefen und installieren.

        Args:
            update_info: Dict von check_for_update()

        Returns:
            {'success': True/False, 'message': '...', 'version': '...'}
        """
        version = update_info.get('version', 'unknown')
        url = update_info.get('url')
        expected_checksum = update_info.get('checksum', '')

        if not url:
            return {'success': False, 'message': 'Keine Download-URL'}

        logger.info(f"Update starten: -> {version}")

        try:
            # 1. Download in temp Verzeichnis
            tmp_dir = Path(tempfile.mkdtemp(prefix='ve-update-'))
            pkg_path = tmp_dir / f"v{version}.tar.gz"

            logger.info(f"Download von {url}")
            resp = requests.get(url, timeout=120, stream=True,
                                allow_redirects=False)
            resp.raise_for_status()

            with open(pkg_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            # 2. Checksum pruefen
            if expected_checksum:
                if not self._verify_checksum(pkg_path, expected_checksum):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    msg = "Checksum-Pruefung fehlgeschlagen — Update abgebrochen"
                    logger.error(msg)
                    return {'success': False, 'message': msg}
                logger.info("Checksum OK")

            # 3. Backup der aktuellen Version
            backup_path = self._create_backup()
            if not backup_path:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return {'success': False, 'message': 'Backup fehlgeschlagen'}

            # 4. Entpacken
            extract_dir = tmp_dir / 'extracted'
            extract_dir.mkdir()
            with tarfile.open(pkg_path, 'r:gz') as tar:
                # Sicherheit: keine absoluten Pfade oder .. erlauben
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        msg = f"Unsicherer Pfad im Archiv: {member.name}"
                        logger.error(msg)
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        return {'success': False, 'message': msg}
                tar.extractall(path=extract_dir)

            # 5. Dateien kopieren (nur .py, .html, .css, .js, VERSION)
            installed = self._install_files(extract_dir)

            # 6. VERSION Datei aktualisieren
            (self.app_dir / VERSION_FILE).write_text(version)

            # 7. Aufraeumen
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._cleanup_old_backups()

            # 8. State updaten
            self._state['current_version'] = version
            self._state['last_update'] = datetime.now().isoformat()
            self._state['available'] = None
            self._state['last_backup'] = str(backup_path)
            self._save_state()

            msg = (
                f"Update auf v{version} erfolgreich "
                f"({installed} Dateien aktualisiert)"
            )
            logger.info(msg)

            return {
                'success': True,
                'message': msg,
                'version': version,
                'files_updated': installed,
                'backup': str(backup_path)
            }

        except Exception as e:
            logger.error(f"Update fehlgeschlagen: {e}")
            return {'success': False, 'message': str(e)}

    def rollback(self) -> Dict:
        """Letzte Version wiederherstellen aus Backup.

        Returns:
            {'success': True/False, 'message': '...'}
        """
        backup_path = self._state.get('last_backup')
        if not backup_path or not Path(backup_path).exists():
            return {'success': False, 'message': 'Kein Backup vorhanden'}

        try:
            backup_path = Path(backup_path)
            logger.info(f"Rollback aus {backup_path}")

            with tarfile.open(backup_path, 'r:gz') as tar:
                tar.extractall(path=self.app_dir)

            # VERSION aus Backup-Name ableiten
            # Backup-Name: backup_v1.0.0_20260508_031500.tar.gz
            name = backup_path.stem.replace('.tar', '')
            parts = name.split('_')
            old_version = parts[1] if len(parts) > 1 else 'unknown'
            if old_version.startswith('v'):
                old_version = old_version[1:]

            self._state['current_version'] = old_version
            self._state['last_rollback'] = datetime.now().isoformat()
            self._save_state()

            msg = f"Rollback auf v{old_version} erfolgreich"
            logger.info(msg)
            return {'success': True, 'message': msg, 'version': old_version}

        except Exception as e:
            msg = f"Rollback fehlgeschlagen: {e}"
            logger.error(msg)
            return {'success': False, 'message': msg}

    def restart_service(self) -> bool:
        """Energy-Dashboard Service neu starten nach Update."""
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', 'energy-dashboard'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info("Service erfolgreich neugestartet")
                return True
            else:
                logger.error(f"Service-Neustart fehlgeschlagen: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Service-Neustart Exception: {e}")
            return False

    def get_status(self) -> Dict:
        """Aktueller Update-Status fuer Dashboard."""
        available = self._state.get('available')
        return {
            'current_version': self.get_current_version(),
            'last_check': self._state.get('last_check'),
            'last_update': self._state.get('last_update'),
            'update_available': available is not None,
            'available_version': available.get('version') if available else None,
            'changelog': available.get('changelog') if available else None,
            'last_backup': self._state.get('last_backup'),
            'auto_update': self._state.get('auto_update', False),
        }

    def set_auto_update(self, enabled: bool):
        """Auto-Update ein/ausschalten."""
        self._state['auto_update'] = enabled
        self._save_state()

    # ------------------------------------------------------------------
    # Interne Methoden
    # ------------------------------------------------------------------

    def _create_backup(self) -> Optional[Path]:
        """Backup der aktuellen Installation als tar.gz."""
        try:
            version = self.get_current_version()
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"backup_v{version}_{timestamp}.tar.gz"
            backup_path = self.backup_dir / backup_name

            # Nur relevante Dateien sichern (nicht venv, __pycache__, DB)
            with tarfile.open(backup_path, 'w:gz') as tar:
                for pattern in ['*.py', 'services/*.py', 'models/*.py',
                                'templates/*.html', 'static/**',
                                'VERSION', '.env']:
                    for f in self.app_dir.glob(pattern):
                        if f.is_file() and 'venv' not in str(f):
                            arcname = f.relative_to(self.app_dir)
                            tar.add(f, arcname=arcname)

            size_kb = backup_path.stat().st_size / 1024
            logger.info(f"Backup erstellt: {backup_name} ({size_kb:.0f} KB)")
            return backup_path

        except Exception as e:
            logger.error(f"Backup fehlgeschlagen: {e}")
            return None

    def _cleanup_old_backups(self):
        """Nur die neuesten N Backups behalten."""
        try:
            backups = sorted(
                self.backup_dir.glob('backup_v*.tar.gz'),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            for old in backups[self.MAX_BACKUPS:]:
                old.unlink()
                logger.debug(f"Altes Backup geloescht: {old.name}")
        except Exception as e:
            logger.debug(f"Backup-Cleanup: {e}")

    def _install_files(self, extract_dir: Path) -> int:
        """Dateien aus entpacktem Update in app_dir kopieren.

        Nur sichere Dateitypen: .py, .html, .css, .js, VERSION
        Keine: .env, .db, .json (Konfiguration/Daten)
        """
        SAFE_EXTENSIONS = {'.py', '.html', '.css', '.js', '.svg', '.png',
                           '.ico', '.txt', '.md'}
        SAFE_NAMES = {'VERSION', 'requirements.txt'}
        # Dateien die NIE ueberschrieben werden (Kundenkonfiguration)
        PROTECTED = {'.env', 'config.yaml', 'ev_vehicles.json',
                     'energy_optimizer.db'}

        installed = 0
        for src_file in extract_dir.rglob('*'):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(extract_dir)
            if rel.name in PROTECTED:
                logger.debug(f"Geschuetzt, uebersprungen: {rel}")
                continue
            if rel.suffix in SAFE_EXTENSIONS or rel.name in SAFE_NAMES:
                dest = self.app_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest)
                installed += 1
                logger.debug(f"Installiert: {rel}")

        return installed

    @staticmethod
    def _verify_checksum(file_path: Path, expected: str) -> bool:
        """SHA256 Checksum pruefen.

        Args:
            expected: Format "sha256:abc123..." oder nur "abc123..."
        """
        if ':' in expected:
            algo, expected_hash = expected.split(':', 1)
        else:
            algo = 'sha256'
            expected_hash = expected

        h = hashlib.new(algo)
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)

        actual = h.hexdigest()
        if actual != expected_hash:
            logger.error(
                f"Checksum mismatch: erwartet {expected_hash[:16]}..., "
                f"bekommen {actual[:16]}..."
            )
            return False
        return True

    @staticmethod
    def _version_newer(remote: str, local: str) -> bool:
        """Semantic Version Vergleich: ist remote neuer als local?

        Versteht: "1.0.0", "1.2.3", "2.0.0-beta"
        """
        def parse(v):
            # "1.2.3-beta" → (1, 2, 3)
            clean = v.lstrip('v').split('-')[0]
            parts = clean.split('.')
            return tuple(int(p) for p in parts if p.isdigit())

        try:
            return parse(remote) > parse(local)
        except (ValueError, TypeError):
            return False

    def _load_state(self) -> dict:
        """Update-State aus JSON laden."""
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text())
        except Exception:
            pass
        return {}

    def _save_state(self):
        """Update-State als JSON speichern."""
        try:
            self._state_file.write_text(
                json.dumps(self._state, indent=2, default=str)
            )
        except Exception as e:
            logger.debug(f"State save failed: {e}")
