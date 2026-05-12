#!/usr/bin/env python3
"""
Initialize the Energy Optimizer database.
Creates all tables and sets up initial configuration.
"""

import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from models.database import db

load_dotenv()

def init_database():
    """Initialize SQLite database with all tables."""
    db_path = Path(__file__).parent.parent / 'energy_optimizer.db'
    
    print(f"Initializing database at: {db_path}")
    
    # Configure SQLAlchemy
    from flask import Flask
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    
    with app.app_context():
        # Create all tables
        db.create_all()
        print("✅ Database tables created successfully!")
        
        # Verify tables
        from models.database import EnergyReading, PriceData, ChargeLog, Config, ConsumptionProfile
        
        tables = [EnergyReading, PriceData, ChargeLog, Config, ConsumptionProfile]
        for table in tables:
            table_name = table.__tablename__
            count = table.query.count()
            print(f"  - {table_name}: {count} records")
        
        print("\n🎉 Energy Optimizer database initialized!")
        print("\nNext steps:")
        print("  1. Copy .env.example to .env and fill in your tokens")
        print("  2. Run: python scripts/create_service.py")
        print("  3. Start: systemctl --user start energy-optimizer")


if __name__ == '__main__':
    init_database()