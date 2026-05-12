"""
Web UI für Energy Optimizer
Flask + HTMX für dynamische Updates
"""

import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps

def create_app(energy_app):
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.secret_key = "energy-optimizer-secret-key-2024"
    
    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if request.path.startswith('/api/') and not session.get('authenticated'):
                return jsonify({"error": "Unauthorized"}), 401
            if request.path == '/' and not session.get('authenticated'):
                return jsonify({"error": "Unauthorized"}), 401
            return f(*args, **kwargs)
        return decorated
    
    @app.route('/')
    @require_auth
    def index():
        return render_template('index.html', mode=energy_app.mode)
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        session['authenticated'] = True
        return redirect(url_for('index'))
    
    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))
    
    @app.route('/api/status')
    @require_auth
    def api_status():
        return jsonify(energy_app.get_status())
    
    @app.route('/api/mode', methods=['POST'])
    @require_auth
    def api_mode():
        mode = request.json.get('mode', 'automatic')
        if mode in ['manual', 'automatic', 'ai']:
            energy_app.set_mode(mode)
            return jsonify({"success": True, "mode": mode})
        return jsonify({"success": False, "error": "Invalid mode"})
    
    @app.route('/api/manual', methods=['POST'])
    @require_auth
    def api_manual():
        settings = {
            'battery_soc_target': request.json.get('battery_soc_target', 100),
            'charge_power_w': request.json.get('charge_power_w', 0),
            'discharge_lock': request.json.get('discharge_lock', 0),
            'enabled': 1
        }
        energy_app.db.save_manual_settings(settings)
        return jsonify({"success": True})
    
    @app.route('/api/optimize-now', methods=['POST'])
    @require_auth
    def api_optimize_now():
        return jsonify(energy_app.run_optimization())
    
    @app.route('/api/prices')
    @require_auth
    def api_prices():
        return jsonify({"prices": energy_app.tibber.get_current_prices()})
    
    @app.route('/api/pv-forecast')
    @require_auth
    def api_pv_forecast():
        return jsonify({"forecast": energy_app.forecast.get_forecast()})
    
    @app.route('/api/consumption-profile')
    @require_auth
    def api_consumption_profile():
        return jsonify(energy_app.db.get_consumption_profile())
    
    @app.route('/api/history')
    @require_auth
    def api_history():
        hours = request.args.get('hours', 24, type=int)
        return jsonify({"history": energy_app.db.get_history(hours=hours)})
    
    @app.route('/api/battery')
    @require_auth
    def api_battery():
        return jsonify(energy_app.fronius.get_battery_status())
    
    @app.route('/api/wattpilot')
    @require_auth
    def api_wattpilot():
        return jsonify(energy_app.wattpilot.get_status())
    
    @app.route('/api/weather')
    @require_auth
    def api_weather():
        return jsonify(energy_app.weather.get_current_weather())
    
    @app.route('/static/<path:filename>')
    def static_files(filename):
        return app.send_static_file(filename)
    
    return app
