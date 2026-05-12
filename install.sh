#!/bin/bash
# Vista-Energy — One-Click Installer
# Usage: curl -sSL https://install.vista-energy.de | bash
set -e

echo "========================================"
echo "  Vista-Energy Installer v1.0"
echo "========================================"
echo ""

# Farben
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

INSTALL_DIR="/home/$USER/energy-optimizer"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="energy-dashboard"

# System pruefen
echo -e "${YELLOW}[1/6] System pruefen...${NC}"
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}Python3 nicht gefunden. Installiere...${NC}"
    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Python $PYTHON_VERSION gefunden"

# Verzeichnis erstellen
echo -e "${YELLOW}[2/6] Vista-Energy herunterladen...${NC}"
if [ -d "$INSTALL_DIR" ]; then
    echo "  Vorhandene Installation gefunden — Update-Modus"
    cd "$INSTALL_DIR"
    # Backup vor Update
    BACKUP_NAME="backup-$(date +%Y%m%d_%H%M%S)"
    mkdir -p backups
    cp app.py "backups/app.py.$BACKUP_NAME" 2>/dev/null || true
    cp .env "backups/.env.$BACKUP_NAME" 2>/dev/null || true
    cp config.yaml "backups/config.yaml.$BACKUP_NAME" 2>/dev/null || true
else
    echo "  Neuinstallation"
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Hier wuerde normalerweise der Download von GitHub/Update-Server stehen
# git clone / wget / curl ...

# Virtual Environment
echo -e "${YELLOW}[3/6] Python-Umgebung einrichten...${NC}"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Systemd Service
echo -e "${YELLOW}[4/6] Systemd-Service einrichten...${NC}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ ! -f "$SERVICE_FILE" ]; then
    sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=Vista-Energy Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python3 app.py
Restart=always
RestartSec=5
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
SVCEOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
fi

# Service starten
echo -e "${YELLOW}[5/6] Service starten...${NC}"
sudo systemctl restart "$SERVICE_NAME"
sleep 3

# Status pruefen
echo -e "${YELLOW}[6/6] Installation pruefen...${NC}"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    # IP-Adresse ermitteln
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "${GREEN}========================================"
    echo "  Vista-Energy erfolgreich installiert!"
    echo "========================================"
    echo ""
    echo "  Dashboard: http://${IP}:8080"
    echo "  Setup:     http://${IP}:8080/setup"
    echo ""
    echo "  Service:   sudo systemctl status $SERVICE_NAME"
    echo "  Logs:      sudo journalctl -u $SERVICE_NAME -f"
    echo -e "========================================${NC}"
else
    echo -e "${RED}Service konnte nicht gestartet werden!${NC}"
    echo "Fehler-Details:"
    sudo journalctl -u "$SERVICE_NAME" -n 20 --no-pager
    exit 1
fi
