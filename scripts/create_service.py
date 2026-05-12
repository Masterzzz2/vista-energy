#!/usr/bin/env python3
"""
Create systemd service for Energy Optimizer.
Run this script once to install the service.
"""

import os
import sys
from pathlib import Path

def create_service():
    service_content = """[Unit]
Description=Energy Optimizer - Smart Energy Management
After=network.target

[Service]
Type=simple
User=werner
Group=werner
WorkingDirectory=/home/werner/energy-optimizer
ExecStart=/home/werner/energy-optimizer/venv/bin/python app.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/werner/energy-optimizer/.env

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""

    service_path = Path.home() / '.config/systemd/user/energy-optimizer.service'
    service_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write service file
    with open(service_path, 'w') as f:
        f.write(service_content)
    
    print(f"✅ Service file created at: {service_path}")
    print("\nTo enable and start the service:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable energy-optimizer")
    print("  systemctl --user start energy-optimizer")
    print("\nTo check status:")
    print("  systemctl --user status energy-optimizer")
    print("\nTo view logs:")
    print("  journalctl --user -u energy-optimizer -f")

if __name__ == '__main__':
    create_service()