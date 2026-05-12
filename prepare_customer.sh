#!/bin/bash
# =========================================================
# Vista-Energy — Kundenversion vorbereiten
# =========================================================
# Entfernt alle privaten Daten und bereitet den Mini-PC
# fuer einen neuen Kunden vor. Nach dem Start wird der
# Setup-Wizard im Browser angezeigt.
#
# ACHTUNG: Dieses Script loescht deine persoenlichen Daten!
#          Nur auf Kunden-Geraeten ausfuehren!
#
# Verwendung:
#   sudo bash prepare_customer.sh
# =========================================================

set -e

APP_DIR="/home/werner/energy-optimizer"
NEXUS_DIR="/home/werner/.nexus"

echo "========================================="
echo "  Vista-Energy — Kundenversion"
echo "========================================="
echo ""
echo "WARNUNG: Alle persoenlichen Daten werden geloescht!"
echo "         Nur auf NEUEN Kunden-Geraeten ausfuehren!"
echo ""
read -p "Fortfahren? (ja/nein): " CONFIRM
if [ "$CONFIRM" != "ja" ]; then
    echo "Abgebrochen."
    exit 1
fi

echo ""
echo "[1/6] Service stoppen..."
systemctl stop energy-dashboard 2>/dev/null || true

echo "[2/6] Private Daten entfernen..."
# .env auf Template zuruecksetzen
cp "$APP_DIR/.env.template" "$APP_DIR/.env"
# config.yaml auf Template zuruecksetzen
cp "$APP_DIR/config.yaml.template" "$APP_DIR/config.yaml"
# Lizenzdaten loeschen
rm -f "$APP_DIR/.license.json"
# Datenbank loeschen (wird beim Start neu erstellt)
rm -f "$APP_DIR/energy_optimizer.db"
# Lernprofil loeschen (wird neu aufgebaut)
rm -f "$APP_DIR/learned_profile.db"
# Update-Backups loeschen
rm -rf "$APP_DIR/backups/"

echo "[3/6] Nexus-Daten zuruecksetzen..."
# Setup-Status loeschen (Wizard startet)
rm -f "$NEXUS_DIR/setup_complete"
# Flask-Secret erneuern
rm -f "$NEXUS_DIR/flask_secret.key"
# Session-Daten loeschen
rm -f "$NEXUS_DIR/controller_state.json"
rm -f "$NEXUS_DIR/program_state.json"
rm -f "$NEXUS_DIR/battery_plan.json"
rm -f "$NEXUS_DIR/update_state.json"
rm -f "$NEXUS_DIR/ev_vehicles.json"

echo "[4/6] Cache aufraeumen..."
find "$APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
rm -f "$APP_DIR/services/__pycache__/"*.pyc

echo "[5/6] Log-Dateien bereinigen..."
rm -f "$APP_DIR/"*.log
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true

echo "[6/6] Service fuer Autostart vorbereiten..."
systemctl enable energy-dashboard 2>/dev/null || true

echo ""
echo "========================================="
echo "  FERTIG! Mini-PC ist bereit fuer Kunden."
echo "========================================="
echo ""
echo "  Naechste Schritte:"
echo "  1. Mini-PC zum Kunden bringen"
echo "  2. Per LAN-Kabel anschliessen"
echo "  3. Mini-PC einschalten"
echo "  4. Im Browser: http://<IP>:8080"
echo "     → Setup-Wizard startet automatisch"
echo ""
echo "  IP-Adresse finden: Router-Interface pruefen"
echo "  oder: hostname -I"
echo ""
