"""
Waffles Priv Service - Combined server for deployment (Render, Railway, etc.)
Serves both the web app (port 5000) and license server (port 5001) in one process.
"""
import sys, os, threading, json, time, hashlib, subprocess, re, secrets, string, logging
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).parent

# ── License server imports ──────────────────────────────────────────
from flask import Flask, render_template, render_template_string, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit

# ── Pantoo checker imports ──────────────────────────────────────────
sys.path.insert(0, str(BASE_DIR))
from pantoo_checker import (
    scan_usernames, build_charset, check_roblox, load_profiles, save_profiles,
    load_pending_signups, save_pending_signups, batch_signup_all, signup_roblox,
    add_pending_signup, ROBLOX_HITS_FILE, CHECKED_FILE, CONFIG_FILE, PENDING_SIGNUPS_FILE
)

# ── License server logic (inlined to avoid separate port) ───────────
LICENSES_FILE = BASE_DIR / 'licenses.json'

PLANS = {
    'basic':    {'name': 'Basic',    'max_checks': 500,       'max_checks_daily': True,  'allow_signup': False, 'max_workers': 5,  'allow_proxy_rotation': False, 'allow_custom_names': True},
    'pro':      {'name': 'Pro',      'max_checks': 5000,      'max_checks_daily': True,  'allow_signup': True,  'max_workers': 10, 'allow_proxy_rotation': False, 'allow_custom_names': True},
    'premium':  {'name': 'Premium',  'max_checks': 999999999, 'max_checks_daily': False, 'allow_signup': True,  'max_workers': 20, 'allow_proxy_rotation': True,  'allow_custom_names': True},
}

DURATIONS = {
    '1d': {'label': '1 Day', 'days': 1}, '7d': {'label': '7 Days', 'days': 7},
    '30d': {'label': '30 Days', 'days': 30}, '90d': {'label': '90 Days', 'days': 90},
    '365d': {'label': '1 Year', 'days': 365}, 'lifetime': {'label': 'Lifetime', 'days': None},
}

def lic_load():
    if LICENSES_FILE.exists():
        try: return json.loads(LICENSES_FILE.read_text(encoding='utf-8'))
        except Exception: pass
    return {'keys': {}, 'admin_key': None}

def lic_save(data):
    LICENSES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')

def gen_admin_key():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(24))

def get_admin_key():
    data = lic_load()
    if not data.get('admin_key'):
        data['admin_key'] = gen_admin_key()
        lic_save(data)
    return data['admin_key']

def gen_license_key():
    alpha = string.ascii_uppercase + string.digits
    return 'WAFFLE-' + '-'.join(''.join(secrets.choice(alpha) for _ in range(6)) for _ in range(4))

def get_hwid():
    try:
        r = subprocess.run(['wmic', 'csproduct', 'get', 'uuid'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and line != 'UUID' and not line.startswith('wmic'):
                return hashlib.sha256(line.encode()).hexdigest()
    except Exception: pass
    try:
        r = subprocess.run(['cmd', '/c', 'vol', 'C:'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if 'Volume Serial Number' in line:
                return hashlib.sha256(line.split()[-1].encode()).hexdigest()
    except Exception: pass
    return hashlib.sha256(b'unknown').hexdigest()

# ── Create unified Flask app ────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'waffles-deploy-secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── Web app state ───────────────────────────────────────────────────
scan_thread = None
stop_event = threading.Event()
scan_lock = threading.Lock()
PROXIES_FILE = BASE_DIR / 'proxies.txt'
SCAN_HISTORY_FILE = BASE_DIR / 'scan_history.json'

# ── Web app helpers ─────────────────────────────────────────────────
def load_settings():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except Exception: pass
    return {'sound': False, 'scroll': True, 'save': True, 'desktopNotify': True, 'timeout': 10, 'retries': 1, 'accent': '#f59e0b'}

def save_settings(data):
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')

def load_proxies():
    if PROXIES_FILE.exists():
        return [l.strip() for l in PROXIES_FILE.read_text(encoding='utf-8').splitlines() if l.strip()]
    return []

def save_proxies(plist):
    PROXIES_FILE.write_text('\n'.join(plist), encoding='utf-8')

def load_scan_history():
    if SCAN_HISTORY_FILE.exists():
        try: return json.loads(SCAN_HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception: pass
    return []

def save_scan_history(h):
    SCAN_HISTORY_FILE.write_text(json.dumps(h, indent=2), encoding='utf-8')

def add_scan_session(d):
    h = load_scan_history()
    h.insert(0, {'timestamp': datetime.now().isoformat(), 'duration': d.get('duration',0), 'checked': d.get('checked',0), 'hits': d.get('hits',0), 'errors': d.get('errors',0), 'mode': d.get('mode',''), 'lengths': d.get('lengths',[]), 'workers': d.get('workers',0)})
    if len(h) > 50: h = h[:50]
    save_scan_history(h)

def send_log(msg, type='info'):
    socketio.emit('log', {'message': msg, 'type': type, 'time': time.time()})

def send_stats(chk, hits, err=0):
    socketio.emit('stats', {'checked': chk, 'hits': hits, 'errors': err})

def load_and_emit_hits():
    if ROBLOX_HITS_FILE.exists():
        lines = []
        for line in ROBLOX_HITS_FILE.read_text(encoding='utf-8').splitlines():
            p = line.split('\t', 1)
            if len(p) == 2: lines.append({'length': p[0], 'name': p[1]})
        socketio.emit('hits', {'hits': lines})
    else:
        socketio.emit('hits', {'hits': []})

# ── Background scan ─────────────────────────────────────────────────
def background_scan(data):
    global scan_thread
    try:
        lic_info = get_cached_license()
        if not lic_info:
            send_log("No valid license. Scan blocked.", 'error')
            return
        lengths = data.get('lengths', [5])
        charset = build_charset(data.get('charset', 'a'))
        mode = data.get('mode', 'u')
        max_checks = data.get('maxChecks', 0)
        delay = data.get('delay', 0.3)
        workers = min(data.get('workers', 8), lic_info.get('max_workers', 5))
        proxy_raw = data.get('proxy', '').strip()
        use_checked = data.get('saveChecked', True)
        custom_names = data.get('customNames')
        use_proxy_rotation = data.get('proxyRotation', False) and lic_info.get('allow_proxy_rotation', False)
        timeout = data.get('timeout', 10)
        auto_signup = data.get('autoSignup', False) and lic_info.get('allow_signup', False)
        signup_pw = data.get('signupPassword', '')
        if not lic_info.get('allow_signup') and auto_signup:
            send_log("Signup not allowed on your plan.", 'warn')
            auto_signup = False
        proxies = None
        if use_proxy_rotation:
            all_proxies = load_proxies()
            if all_proxies: proxies = all_proxies
        elif proxy_raw:
            proxies = [{'http': proxy_raw, 'https': proxy_raw}]
        socketio.emit('scan_start', {'lengths': lengths, 'mode': mode})
        names_list = None
        if custom_names:
            names_list = [n.strip() for n in custom_names.split('\n') if n.strip()]
        _scan_start = [time.time()]
        _scan_check_count = [0]
        _scan_hit_count = [0]
        _scan_error_count = [0]
        _last_limit_check = [0]
        def on_check_impl(c, h, e):
            send_stats(c, h, e)
            _scan_check_count[0] = c
            _scan_hit_count[0] = h
            _scan_error_count[0] = e
        scan_usernames(
            lengths, charset, mode, max_checks, delay, workers, proxies,
            on_hit=lambda l, n: (send_log(f"[HIT] {n} ({l}L)", 'hit'), socketio.emit('new_hit', {'length': l, 'name': n, 'time': time.time()}), load_and_emit_hits()),
            on_check=on_check_impl,
            on_error=lambda e: send_log(f"Error/rate-limit count: {e}", 'warn'),
            on_signup=lambda n, ok, msg: send_log(f"[SIGNUP] {n} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error'),
            stop_event=stop_event, custom_names=names_list, use_save_checked=use_checked,
            signup_password=signup_pw if auto_signup else None
        )
        add_scan_session({'duration': round(time.time() - _scan_start[0], 1), 'checked': _scan_check_count[0], 'hits': _scan_hit_count[0], 'errors': _scan_error_count[0], 'mode': mode, 'lengths': lengths, 'workers': workers})
    except Exception as e:
        send_log(f"Error: {str(e)}", 'error')
    finally:
        stop_event.clear()
        with scan_lock: scan_thread = None
        socketio.emit('scan_done')
        send_log("Ready.", 'info')

# ── License client cache (inlined) ──────────────────────────────────
LICENSE_CACHE_FILE = BASE_DIR / '.license_cache'
_license_info = None
_license_lock = threading.Lock()

def load_cached_license():
    if LICENSE_CACHE_FILE.exists():
        try: return json.loads(LICENSE_CACHE_FILE.read_text(encoding='utf-8'))
        except: pass
    return None

def save_cached_license(info):
    LICENSE_CACHE_FILE.write_text(json.dumps(info), encoding='utf-8')

def get_cached_license():
    global _license_info
    with _license_lock:
        if _license_info: return _license_info
    cached = load_cached_license()
    if cached:
        with _license_lock: _license_info = cached
        return cached
    return None

# ── License API routes ──────────────────────────────────────────────
@app.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'server': 'waffles-license', 'time': datetime.now().isoformat()})

@app.route('/api/plans')
def api_plans():
    return jsonify({'ok': True, 'plans': {k: {'name': v['name'], 'key': k} for k, v in PLANS.items()}, 'durations': {k: v['label'] for k, v in DURATIONS.items()}})

def require_admin_kw():
    admin_key = get_admin_key()
    auth = request.headers.get('Authorization', '')
    if auth == f'Bearer {admin_key}': return True
    if request.is_json and request.json.get('admin_key') == admin_key: return True
    if request.args.get('admin_key') == admin_key: return True
    return False

@app.route('/api/validate', methods=['POST'])
def api_validate():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    if not key: return jsonify({'ok': False, 'error': 'No key provided'})
    licenses = lic_load()
    kd = licenses['keys'].get(key)
    if not kd: return jsonify({'ok': False, 'error': 'Invalid key'})
    if kd.get('revoked'): return jsonify({'ok': False, 'error': 'Key revoked'})
    if kd.get('expires') and datetime.fromisoformat(kd['expires']) < datetime.now():
        return jsonify({'ok': False, 'error': 'Key expired'})
    client_hwid = data.get('hwid', '')
    if kd.get('hwid') and kd['hwid'] != client_hwid:
        return jsonify({'ok': False, 'error': 'Key already in use on another machine'})
    if not kd.get('hwid'):
        kd['hwid'] = client_hwid
        kd['activated'] = True
        kd['activated_at'] = datetime.now().isoformat()
    kd['last_seen'] = datetime.now().isoformat()
    kd['last_ip'] = request.remote_addr
    lic_save(licenses)
    plan_info = PLANS[kd['plan']]
    remaining = None
    if plan_info.get('max_checks_daily'):
        used = kd.get('daily_usage', {}).get(datetime.now().strftime('%Y-%m-%d'), 0)
        remaining = max(0, plan_info['max_checks'] - used)
    expires = kd.get('expires')
    if expires:
        ed = datetime.fromisoformat(expires)
        expires_str, days_left = ed.strftime('%Y-%m-%d'), (ed - datetime.now()).days
    else:
        expires_str, days_left = 'Never', 9999
    info = {
        'key': key, 'plan': kd['plan'], 'plan_name': plan_info['name'],
        'expires': expires_str, 'days_left': max(0, days_left),
        'max_checks': plan_info['max_checks'], 'max_checks_daily': plan_info['max_checks_daily'],
        'remaining_checks': remaining, 'allow_signup': plan_info['allow_signup'],
        'max_workers': plan_info['max_workers'], 'allow_proxy_rotation': plan_info['allow_proxy_rotation'],
        'allow_custom_names': plan_info['allow_custom_names'], 'total_checks': kd.get('total_checks', 0), 'activated': True,
    }
    with _license_lock:
        global _license_info
        _license_info = info
    save_cached_license(info)
    return jsonify({'ok': True, **info})

@app.route('/api/generate', methods=['POST'])
def api_generate():
    if not require_admin_kw(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    data = request.get_json()
    plan = data.get('plan', 'basic')
    duration = data.get('duration', '30d')
    notes = data.get('notes', '')
    if plan not in PLANS: return jsonify({'ok': False, 'error': 'Invalid plan'})
    if duration not in DURATIONS: return jsonify({'ok': False, 'error': 'Invalid duration'})
    key = data.get('key') or gen_license_key()
    kd = {'plan': plan, 'created': datetime.now().isoformat(), 'notes': notes, 'activated': False, 'revoked': False, 'total_checks': 0, 'daily_usage': {}}
    if DURATIONS[duration]['days'] is not None:
        kd['expires'] = (datetime.now() + timedelta(days=DURATIONS[duration]['days'])).isoformat()
    else:
        kd['expires'] = None
    licenses = lic_load()
    if key in licenses['keys']: return jsonify({'ok': False, 'error': 'Key already exists'})
    licenses['keys'][key] = kd
    lic_save(licenses)
    return jsonify({'ok': True, 'key': key, 'plan': plan, 'duration': duration, 'expires': kd.get('expires', 'lifetime'), 'notes': notes})

@app.route('/api/keys', methods=['GET'])
def api_list_keys():
    if not require_admin_kw(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    licenses = lic_load()
    today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({'ok': True, 'keys': [{'key': k, 'plan': kd['plan'], 'plan_name': PLANS.get(kd['plan'], {}).get('name',''), 'created': kd.get('created',''), 'expires': kd.get('expires'), 'expired': bool(kd.get('expires') and datetime.fromisoformat(kd['expires']) < datetime.now()), 'activated': kd.get('activated',False), 'revoked': kd.get('revoked',False), 'hwid': (kd.get('hwid','')[:16]+'...') if kd.get('hwid') else None, 'notes': kd.get('notes',''), 'total_checks': kd.get('total_checks',0), 'daily_used': kd.get('daily_usage',{}).get(today,0), 'last_seen': kd.get('last_seen','')} for k, kd in licenses['keys'].items()], 'admin_key': get_admin_key()})

@app.route('/api/record_check', methods=['POST'])
def api_record_check():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    count = data.get('count', 1)
    licenses = lic_load()
    kd = licenses['keys'].get(key)
    if kd:
        today = datetime.now().strftime('%Y-%m-%d')
        kd.setdefault('daily_usage', {})
        kd['daily_usage'][today] = kd['daily_usage'].get(today, 0) + count
        kd['total_checks'] = kd.get('total_checks', 0) + count
        lic_save(licenses)
    return jsonify({'ok': True})

@app.route('/api/check_limits', methods=['POST'])
def api_check_limits():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    if not key: return jsonify({'ok': False, 'error': 'No key', 'blocked': True})
    licenses = lic_load()
    kd = licenses['keys'].get(key)
    remaining, max_chk = None, None
    if kd:
        pi = PLANS.get(kd['plan'], {})
        max_chk = pi.get('max_checks')
        if pi.get('max_checks_daily'):
            used = kd.get('daily_usage', {}).get(datetime.now().strftime('%Y-%m-%d'), 0)
            remaining = max(0, max_chk - used)
            if remaining <= 0:
                return jsonify({'ok': False, 'error': 'Daily check limit reached', 'blocked': True, 'remaining_checks': remaining, 'max_checks': max_chk})
    return jsonify({'ok': True, 'blocked': False, 'remaining_checks': remaining, 'max_checks': max_chk})

@app.route('/api/revoke', methods=['POST'])
def api_revoke():
    if not require_admin_kw(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    licenses = lic_load()
    if key not in licenses['keys']: return jsonify({'ok': False, 'error': 'Key not found'})
    licenses['keys'][key]['revoked'] = True
    licenses['keys'][key]['revoked_at'] = datetime.now().isoformat()
    lic_save(licenses)
    return jsonify({'ok': True, 'message': f'Key {key} revoked'})

@app.route('/api/unrevoke', methods=['POST'])
def api_unrevoke():
    if not require_admin_kw(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    licenses = lic_load()
    if key not in licenses['keys']: return jsonify({'ok': False, 'error': 'Key not found'})
    licenses['keys'][key]['revoked'] = False
    licenses['keys'][key].pop('revoked_at', None)
    lic_save(licenses)
    return jsonify({'ok': True, 'message': f'Key {key} un-revoked'})

# ── Admin panel ─────────────────────────────────────────────────────
@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    admin_key = get_admin_key()
    message = None
    if request.method == 'POST':
        sk = request.form.get('admin_key', '')
        if sk != admin_key: return redirect(url_for('admin_panel', error='unauthorized'))
        plan = request.form.get('plan', 'basic')
        duration = request.form.get('duration', '30d')
        notes = request.form.get('notes', '')
        if plan not in PLANS: return redirect(url_for('admin_panel', error='invalid_plan'))
        if duration not in DURATIONS: return redirect(url_for('admin_panel', error='invalid_duration'))
        key = gen_license_key()
        kd = {'plan': plan, 'created': datetime.now().isoformat(), 'notes': notes, 'activated': False, 'revoked': False, 'total_checks': 0, 'daily_usage': {}}
        if DURATIONS[duration]['days'] is not None:
            kd['expires'] = (datetime.now() + timedelta(days=DURATIONS[duration]['days'])).isoformat()
        licenses = lic_load()
        licenses['keys'][key] = kd
        lic_save(licenses)
        return redirect(url_for('admin_panel', ok='1', key=key, plan=plan, dur=DURATIONS[duration]['label']))
    msg_key = request.args.get('key')
    if msg_key:
        message = ('ok', msg_key, request.args.get('plan',''), request.args.get('dur',''))
    elif request.args.get('error') == 'unauthorized': message = ('error', 'Unauthorized')
    elif request.args.get('error') == 'invalid_plan': message = ('error', 'Invalid plan')
    elif request.args.get('error') == 'invalid_duration': message = ('error', 'Invalid duration')
    licenses = lic_load()
    keys_list = sorted(licenses['keys'].items(), key=lambda x: x[1].get('created',''), reverse=True)
    today = datetime.now().strftime('%Y-%m-%d')
    total_keys = len(keys_list)
    active_keys = sum(1 for _,v in keys_list if v.get('activated') and not v.get('revoked') and (not v.get('expires') or datetime.fromisoformat(v['expires']) >= datetime.now()))
    revoked_keys = sum(1 for _,v in keys_list if v.get('revoked'))
    total_checks_all = sum(v.get('total_checks',0) for _,v in keys_list)
    return render_template_string(ADMIN_HTML, admin_key=admin_key, message=message, keys=keys_list, PLANS=PLANS, today=today, datetime=datetime, total_keys=total_keys, active_keys=active_keys, revoked_keys=revoked_keys, total_checks_all=total_checks_all)

# ── Web app routes ──────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        data = request.get_json()
        if data: save_settings(data); return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': 'no data'}), 400
    return jsonify(load_settings())

@app.route('/api/proxies', methods=['GET', 'POST', 'DELETE'])
def api_proxies():
    if request.method == 'GET': return jsonify(load_proxies())
    elif request.method == 'POST':
        data = request.get_json()
        if data and 'proxy' in data:
            proxies = load_proxies()
            if data['proxy'] not in proxies: proxies.append(data['proxy']); save_proxies(proxies)
            return jsonify({'ok': True, 'proxies': proxies})
        return jsonify({'ok': False, 'error': 'no proxy'}), 400
    elif request.method == 'DELETE':
        data = request.get_json()
        if data and 'proxy' in data:
            proxies = load_proxies()
            if data['proxy'] in proxies: proxies.remove(data['proxy']); save_proxies(proxies)
            return jsonify({'ok': True, 'proxies': proxies})
        return jsonify({'ok': False, 'error': 'no proxy'}), 400

@app.route('/api/proxy_test', methods=['POST'])
def api_proxy_test():
    data = request.get_json()
    proxy = data.get('proxy', '')
    if not proxy: return jsonify({'ok': False, 'error': 'No proxy'})
    t0 = time.time()
    result = check_roblox('zzzxxyy12345', proxies={'http': proxy, 'https': proxy})
    return jsonify({'ok': result is not False, 'time': round(time.time() - t0, 2), 'status': 'valid' if result is not False else 'invalid'})

@app.route('/api/profiles', methods=['GET', 'POST', 'DELETE'])
def api_profiles():
    if request.method == 'GET': return jsonify(load_profiles())
    elif request.method == 'POST':
        data = request.get_json()
        if data and 'name' in data and 'config' in data:
            profiles = load_profiles(); profiles[data['name']] = data['config']; save_profiles(profiles)
            return jsonify({'ok': True, 'profiles': profiles})
        return jsonify({'ok': False, 'error': 'need name and config'}), 400
    elif request.method == 'DELETE':
        data = request.get_json()
        if data and 'name' in data:
            profiles = load_profiles(); profiles.pop(data['name'], None); save_profiles(profiles)
            return jsonify({'ok': True, 'profiles': profiles})
        return jsonify({'ok': False, 'error': 'need name'}), 400

@app.route('/api/cache_count')
def api_cache_count():
    count = 0
    if CHECKED_FILE.exists():
        count = len([l for l in CHECKED_FILE.read_text(encoding='utf-8').splitlines() if l.strip()])
    return jsonify({'count': count})

@app.route('/api/export_hits')
def api_export_hits():
    if ROBLOX_HITS_FILE.exists():
        names = []
        for line in ROBLOX_HITS_FILE.read_text(encoding='utf-8').splitlines():
            p = line.split('\t', 1)
            if len(p) == 2: names.append(p[1])
        return '\n'.join(names) + '\n', 200, {'Content-Type': 'text/plain', 'Content-Disposition': 'attachment; filename=hits.txt'}
    return '', 200, {'Content-Type': 'text/plain'}

# ── SocketIO events ─────────────────────────────────────────────────
@socketio.on('connect')
def handle_connect(auth=None):
    settings = load_settings()
    emit('settings_loaded', settings)
    emit('log', {'message': 'Connected to server.', 'type': 'success', 'time': time.time()})
    load_and_emit_hits()
    history = load_scan_history()
    emit('scan_history', {'history': history})
    signups = load_pending_signups()
    emit('pending_signups', {'signups': signups})
    success = sum(1 for s in signups if s['status'] == 'signed')
    emit('signup_stats', {'total': len(signups), 'success': success, 'failed': len(signups) - success})
    server_alive = True
    lic = get_cached_license()
    if lic:
        emit('license_status', {'ok': True, 'plan': lic['plan'], 'plan_name': lic['plan_name'], 'expires': lic.get('expires', 'Never'), 'days_left': lic.get('days_left', 0), 'max_checks': lic.get('max_checks', 0), 'remaining_checks': lic.get('remaining_checks'), 'allow_signup': lic.get('allow_signup', False), 'max_workers': lic.get('max_workers', 5), 'allow_proxy_rotation': lic.get('allow_proxy_rotation', False)})
    else:
        emit('license_status', {'ok': False, 'server_alive': server_alive})

@socketio.on('validate_license')
def handle_validate_license(data):
    key = data.get('key', '').strip().upper()
    if not key: emit('license_result', {'ok': False, 'error': 'No key provided'}); return
    try:
        r = __import__('requests').post(f'http://localhost:{os.environ.get("PORT", 5000)}/api/validate', json={'key': key, 'hwid': get_hwid()}, timeout=5)
        if r.status_code == 200:
            d = r.json()
            if d.get('ok'):
                emit('license_result', {'ok': True, 'plan': d['plan'], 'plan_name': d['plan_name'], 'expires': d.get('expires','Never'), 'days_left': d.get('days_left',0), 'max_checks': d.get('max_checks',0), 'remaining_checks': d.get('remaining_checks'), 'allow_signup': d.get('allow_signup',False), 'max_workers': d.get('max_workers',5), 'allow_proxy_rotation': d.get('allow_proxy_rotation',False)})
                emit('log', {'message': f"License validated: {d['plan_name']}", 'type': 'success', 'time': time.time()})
                return
            emit('license_result', {'ok': False, 'error': d.get('error','Validation failed')})
        else:
            emit('license_result', {'ok': False, 'error': f'Server error: HTTP {r.status_code}'})
    except Exception as e:
        emit('license_result', {'ok': False, 'error': str(e)})

@socketio.on('get_license')
def handle_get_license():
    lic = get_cached_license()
    if lic:
        emit('license_status', {'ok': True, 'plan': lic['plan'], 'plan_name': lic['plan_name'], 'expires': lic.get('expires','Never'), 'days_left': lic.get('days_left',0), 'max_checks': lic.get('max_checks',0), 'remaining_checks': lic.get('remaining_checks'), 'allow_signup': lic.get('allow_signup',False), 'max_workers': lic.get('max_workers',5), 'allow_proxy_rotation': lic.get('allow_proxy_rotation',False)})
    else:
        emit('license_status', {'ok': False})

@socketio.on('get_hits')
def handle_get_hits(): load_and_emit_hits()

@socketio.on('get_log')
def handle_get_log(): pass

@socketio.on('start_scan')
def handle_start_scan(data):
    global scan_thread
    with scan_lock:
        if scan_thread and scan_thread.is_alive():
            emit('log', {'message': 'Still stopping previous scan...', 'type': 'warn', 'time': time.time()}); return
        stop_event.clear()
        scan_thread = threading.Thread(target=background_scan, args=(data,), daemon=True)
        scan_thread.start()
    emit('log', {'message': 'Scan started.', 'type': 'info', 'time': time.time()})

@socketio.on('stop_scan')
def handle_stop_scan():
    stop_event.set()
    emit('log', {'message': 'Stopping scan...', 'type': 'warn', 'time': time.time()})

@socketio.on('run_diag')
def handle_diag():
    def run():
        send_log("Running diagnostics...", 'info')
        for name in ['zzzxxyy12345', 'abcdef_noop', 'test____123', 'xwyzabc']:
            status = check_roblox(name)
            send_log(f"  {name} -> {'AVAILABLE' if status else 'TAKEN/ERROR'}", 'hit' if status else 'error')
        send_log("Diagnostics complete.", 'success')
    threading.Thread(target=run, daemon=True).start()

@socketio.on('clear_hits')
def handle_clear_hits():
    if ROBLOX_HITS_FILE.exists(): ROBLOX_HITS_FILE.write_text('')
    emit('log', {'message': 'Hits cleared.', 'type': 'warn', 'time': time.time()})
    load_and_emit_hits()

@socketio.on('save_settings')
def handle_save_settings(data): save_settings(data); emit('log', {'message': 'Settings saved.', 'type': 'success', 'time': time.time()})

@socketio.on('clear_cache')
def handle_clear_cache():
    if CHECKED_FILE.exists(): CHECKED_FILE.write_text('')
    emit('log', {'message': 'Cache cleared.', 'type': 'warn', 'time': time.time()})

@socketio.on('get_pending_signups')
def handle_get_pending_signups():
    signups = load_pending_signups()
    emit('pending_signups', {'signups': signups})
    success = sum(1 for s in signups if s['status'] == 'signed')
    emit('signup_stats', {'total': len(signups), 'success': success, 'failed': len(signups) - success})

@socketio.on('batch_signup')
def handle_batch_signup(data):
    pw = data.get('password', '')
    if not pw: emit('log', {'message': 'No signup password set', 'type': 'error', 'time': time.time()}); return
    emit('log', {'message': 'Starting batch signup...', 'type': 'info', 'time': time.time()})
    def run():
        results = batch_signup_all(pw, on_each=lambda n, ok, msg: (emit('signup_result', {'name': n, 'ok': ok, 'msg': msg}), send_log(f"[SIGNUP] {n} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error')), stop_event=threading.Event())
        signups = load_pending_signups()
        emit('pending_signups', {'signups': signups})
        success = sum(1 for _, ok, _ in results if ok)
        emit('signup_stats', {'total': len(results), 'success': success, 'failed': len(results) - success})
        send_log(f"Batch signup complete. {success}/{len(results)} succeeded", 'success')
    threading.Thread(target=run, daemon=True).start()

@socketio.on('signup_single')
def handle_signup_single(data):
    name = data.get('name', '')
    pw = data.get('password', '')
    if not name or not pw: emit('log', {'message': 'Missing name or password', 'type': 'error', 'time': time.time()}); return
    def run():
        ok, msg = signup_roblox(name, pw)
        signups = load_pending_signups()
        for s in signups:
            if s['name'] == name: s['status'] = 'signed' if ok else 'failed'; s['signed_at'] = time.time(); s['message'] = msg; break
        save_pending_signups(signups)
        emit('signup_result', {'name': name, 'ok': ok, 'msg': msg})
        emit('pending_signups', {'signups': signups})
        send_log(f"[SIGNUP] {name} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error')
    threading.Thread(target=run, daemon=True).start()

@socketio.on('get_dashboard')
def handle_get_dashboard():
    hit_count = 0
    if ROBLOX_HITS_FILE.exists(): hit_count = len([l for l in ROBLOX_HITS_FILE.read_text(encoding='utf-8').splitlines() if l.strip()])
    cache_count = 0
    if CHECKED_FILE.exists(): cache_count = len([l for l in CHECKED_FILE.read_text(encoding='utf-8').splitlines() if l.strip()])
    signups = load_pending_signups()
    emit('dashboard', {'total_checked': cache_count, 'total_hits': hit_count, 'pending_signups': sum(1 for s in signups if s['status'] == 'pending'), 'signed_signups': sum(1 for s in signups if s['status'] == 'signed'), 'scan_sessions': len(load_scan_history())})

@socketio.on('get_scan_history')
def handle_get_scan_history():
    emit('scan_history', {'history': load_scan_history()})

# ── Admin panel HTML (same as before with Waffles branding) ────────
ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Waffles · Admin</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'SF Pro Display','SF Pro Text','Helvetica Neue',sans-serif;background:#07070e;color:#eeeef5;min-height:100vh;display:flex;-webkit-font-smoothing:antialiased}
.sb{width:236px;background:linear-gradient(180deg,#0c0c16 0%,#090910 100%);border-right:1px solid rgba(255,255,255,.04);padding:32px 22px;display:flex;flex-direction:column;gap:28px;flex-shrink:0;position:relative;overflow:hidden}
.sb::before{content:'';position:absolute;top:-120px;left:-60px;width:300px;height:300px;background:radial-gradient(circle,rgba(245,158,11,.04) 0%,transparent 70%);pointer-events:none}
.sb .lg{font-size:20px;font-weight:700;letter-spacing:-.5px;background:linear-gradient(135deg,#f0f0f5 0%,#f59e0b 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;position:relative}
.sb .lg small{font-size:9px;display:block;color:#4a4a5a;margin-top:2px;font-weight:400;-webkit-text-fill-color:#4a4a5a;letter-spacing:.3px}
.sb .ak-c{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:14px;padding:18px;position:relative}
.sb .ak-c::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent)}
.sb .ak-c .ak-l{font-size:8px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px;font-weight:600}
.sb .ak-c code{font-family:'SF Mono','SF Pro',monospace;font-size:10px;color:#f59e0b;word-break:break-all;display:block;margin-bottom:10px;line-height:1.5;user-select:all;opacity:.9}
.sb .ak-c .cpy{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.05);color:#5a5a6a;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:9px;transition:all .2s;width:100%;font-weight:500;letter-spacing:.3px;font-family:inherit}
.sb .ak-c .cpy:hover{background:rgba(255,255,255,.07);color:#eeeef5;border-color:rgba(255,255,255,.1)}
.sb .nv{display:flex;flex-direction:column;gap:3px;position:relative}
.sb .nv a{color:#4a4a5a;text-decoration:none;font-size:12px;padding:9px 14px;border-radius:10px;transition:all .2s;font-weight:500;display:flex;align-items:center;gap:10px}
.sb .nv a:hover{background:rgba(255,255,255,.03);color:#c0c0d0}
.sb .nv a.act{background:rgba(245,158,11,.07);color:#f59e0b;font-weight:600}
.sb .nv a.act::before{content:'';width:3px;height:16px;background:#f59e0b;border-radius:2px;margin-left:-14px;box-shadow:0 0 10px rgba(245,158,11,.3)}
.sb .ft{font-size:9px;color:#2a2a3a;margin-top:auto;text-align:center;position:relative}
.mn{flex:1;padding:36px 44px;overflow-y:auto;max-height:100vh;animation:fIn .5s ease}
.mn h1{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-bottom:3px}
.mn .sub{font-size:13px;color:#5a5a6a;margin-bottom:30px;font-weight:400}
.cds{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:32px}
.c{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:16px;padding:20px 22px;transition:all .3s;position:relative;overflow:hidden}
.c:hover{background:rgba(255,255,255,.03);border-color:rgba(255,255,255,.07);transform:translateY(-2px)}
.c::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:16px 16px 0 0;opacity:0;transition:opacity .3s}
.c:hover::before{opacity:1}
.c.c1::before{background:linear-gradient(90deg,#fbbf24,#f59e0b)}
.c.c2::before{background:linear-gradient(90deg,#34d399,#10b981)}
.c.c3::before{background:linear-gradient(90deg,#f87171,#dc2626)}
.c.c4::before{background:linear-gradient(90deg,#fb923c,#ea580c)}
.c .c-n{font-size:28px;font-weight:700;letter-spacing:-.5px;margin-bottom:2px}
.c .c-l{font-size:10px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.gc{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:16px;padding:24px 28px;margin-bottom:24px;transition:border-color .3s}
.gc:hover{border-color:rgba(255,255,255,.07)}
.gc h2{font-size:10px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.7px;margin-bottom:18px;font-weight:600}
.gf{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
.gf .fd{min-width:150px;flex:1}
.gf label{font-size:9px;color:#5a5a6a;display:block;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.gf select,.gf input{padding:10px 16px;background:#0c0c14;border:1px solid rgba(255,255,255,.05);border-radius:10px;color:#eeeef5;font-size:13px;outline:none;width:100%;transition:all .25s;font-family:inherit}
.gf select:focus,.gf input:focus{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.1);background:#0e0e18}
.gf select option{background:#0c0c14;color:#eeeef5}
.btn{position:relative;padding:10px 26px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;background:linear-gradient(135deg,#fbbf24,#f59e0b);color:#0a0a12;transition:all .25s;white-space:nowrap;font-family:inherit;overflow:hidden}
.btn::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(180deg,rgba(255,255,255,.2) 0%,transparent 50%);border-radius:10px;pointer-events:none}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(245,158,11,.35)}
.btn:active{transform:translateY(0);box-shadow:0 2px 10px rgba(245,158,11,.2)}
.tw{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:16px;overflow:hidden}
.tw-h{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid rgba(255,255,255,.04)}
.tw-h h2{font-size:10px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.tw-h span{font-size:11px;color:#3a3a4a;font-weight:500;background:rgba(255,255,255,.03);padding:2px 10px;border-radius:20px}
.t{width:100%;border-collapse:collapse;font-size:12px}
.t th{padding:12px 22px;text-align:left;color:#5a5a6a;font-size:9px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;border-bottom:1px solid rgba(255,255,255,.03)}
.t td{padding:12px 22px;border-bottom:1px solid rgba(255,255,255,.02);font-size:12px;transition:background .2s}
.t tr:last-child td{border-bottom:none}
.t tr:hover td{background:rgba(255,255,255,.015)}
.bdg{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.bdg-basic{background:rgba(251,191,36,.08);color:#fbbf24;border:1px solid rgba(251,191,36,.12)}
.bdg-pro{background:rgba(245,158,11,.08);color:#f59e0b;border:1px solid rgba(245,158,11,.12)}
.bdg-premium{background:rgba(217,119,6,.08);color:#d97706;border:1px solid rgba(217,119,6,.12)}
.st{opacity:.3;transition:opacity .3s}
.msg{display:flex;align-items:center;gap:14px;padding:14px 20px;margin-bottom:22px;border-radius:12px;font-size:13px;animation:sl .4s ease;position:relative}
.msg-ok{background:rgba(52,211,153,.05);border:1px solid rgba(52,211,153,.1);color:#34d399}
.msg-err{background:rgba(248,113,113,.05);border:1px solid rgba(248,113,113,.1);color:#f87171}
.msg .kv{font-family:'SF Mono','SF Pro',monospace;font-size:12px;color:#fbbf24;user-select:all;word-break:break-all;background:rgba(0,0,0,.2);padding:4px 12px;border-radius:8px;letter-spacing:.3px}
.msg .mb{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);color:#5a5a6a;padding:5px 14px;border-radius:8px;cursor:pointer;font-size:10px;margin-left:auto;white-space:nowrap;transition:all .2s;font-family:inherit}
.msg .mb:hover{background:rgba(255,255,255,.08);color:#eeeef5;border-color:rgba(255,255,255,.1)}
.kc{color:#5a5a6a;font-size:11px;font-family:'SF Mono','SF Pro',monospace;letter-spacing:.2px}
.em2{background:rgba(255,255,255,.015);border-radius:10px;padding:6px 12px;font-size:10px;color:#3a3a4a}
@keyframes sl{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
@keyframes fIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:900px){.sb{width:200px;padding:24px 16px}.mn{padding:28px 24px}.gf .fd{min-width:120px}}
@media(max-width:700px){.sb{display:none}.mn{padding:20px 16px}}
</style>
</head>
<body>
<div class="sb">
  <div class="lg">Waffles<small>License Server</small></div>
  <div class="ak-c">
    <div class="ak-l">Admin Key</div>
    <code>{{ admin_key }}</code>
    <button class="cpy" onclick="navigator.clipboard.writeText('{{ admin_key }}').then(()=>{this.textContent='Copied';setTimeout(()=>this.textContent='Copy',1800)})">Copy</button>
  </div>
  <div class="nv">
    <a href="#" class="act">Dashboard</a>
  </div>
  <div class="ft">Waffles License Server v2.0</div>
</div>
<div class="mn">
  <h1>Dashboard</h1>
  <div class="sub">License key management</div>
  <div class="cds">
    <div class="c c1"><div class="c-n" style="color:#fbbf24">{{ total_keys }}</div><div class="c-l">Total Keys</div></div>
    <div class="c c2"><div class="c-n" style="color:#34d399">{{ active_keys }}</div><div class="c-l">Activated</div></div>
    <div class="c c3"><div class="c-n" style="color:#f87171">{{ revoked_keys }}</div><div class="c-l">Revoked</div></div>
    <div class="c c4"><div class="c-n" style="color:#fb923c">{{ total_checks_all }}</div><div class="c-l">Total Checks</div></div>
  </div>
  {% if message %}
  <div class="msg msg-{{ message[0] }}">
    {% if message[0] == 'ok' %}
      <span style="font-size:16px;flex-shrink:0;width:22px;height:22px;display:flex;align-items:center;justify-content:center;background:rgba(52,211,153,.1);border-radius:50%">&#10003;</span>
      <span><strong style="text-transform:capitalize;color:#eeeef5">{{ message[2] }}</strong> <span style="color:#5a5a6a">/</span> {{ message[3] }}</span>
      <span class="kv" id="nk">{{ message[1] }}</span>
      <button class="mb" onclick="navigator.clipboard.writeText(document.getElementById('nk').textContent).then(()=>{this.textContent='Copied';setTimeout(()=>this.textContent='Copy',1800)})">Copy</button>
    {% else %}
      <span style="font-size:16px;flex-shrink:0;width:22px;height:22px;display:flex;align-items:center;justify-content:center;background:rgba(248,113,113,.1);border-radius:50%">&#10007;</span>
      <span>{{ message[1] }}</span>
    {% endif %}
  </div>
  {% endif %}
  <div class="gc">
    <h2>Generate New Key</h2>
    <form method="POST" action="/admin">
      <input type="hidden" name="admin_key" value="{{ admin_key }}">
      <div class="gf">
        <div class="fd"><label>Plan</label><select name="plan"><option value="basic">Basic</option><option value="pro">Pro</option><option value="premium">Premium</option></select></div>
        <div class="fd"><label>Duration</label><select name="duration"><option value="1d">1 Day</option><option value="7d">7 Days</option><option value="30d" selected>30 Days</option><option value="90d">90 Days</option><option value="365d">1 Year</option><option value="lifetime">Lifetime</option></select></div>
        <div class="fd" style="min-width:180px"><label>Notes</label><input type="text" name="notes" placeholder="Buyer name or email"></div>
        <button class="btn" type="submit">Generate</button>
      </div>
    </form>
  </div>
  <div class="tw">
    <div class="tw-h"><h2>Keys</h2><span>{{ total_keys }} total</span></div>
    {% if keys %}
    <table class="t">
      <thead><tr><th>Key</th><th>Plan</th><th>Expires</th><th>Checks</th><th>Today</th><th>Status</th></tr></thead>
      <tbody>
      {% for k, v in keys %}
        {% set expired = v.expires and datetime.fromisoformat(v.expires) < datetime.now() %}
        {% set dused = v.daily_usage.get(today, 0) if v.daily_usage else 0 %}
        {% set plan_info = PLANS.get(v.plan, {}) %}
        {% set daily_limit = plan_info.get('max_checks', 0) if plan_info.get('max_checks_daily') else 0 %}
        <tr class="{{ 'st' if v.revoked or expired }}">
          <td class="kc">{{ k }}</td>
          <td><span class="bdg bdg-{{ v.plan }}">{{ PLANS[v.plan]['name'] }}</span></td>
          <td style="color:#5a5a6a">{% if v.expires %}{{ datetime.fromisoformat(v.expires).strftime('%b %d, %Y') }}{% else %}<span style="color:#34d399">&#8734; Lifetime</span>{% endif %}</td>
          <td style="color:#5a5a6a">{{ v.total_checks }}</td>
          <td>{% if daily_limit > 0 %}<div style="display:flex;align-items:center;gap:6px"><div style="flex:1;background:rgba(255,255,255,.04);border-radius:4px;height:4px;overflow:hidden;min-width:50px"><div style="height:100%;width:{{ (dused / daily_limit * 100)|round }}%;background:{% if dused >= daily_limit %}#f87171{% elif dused > daily_limit * 0.8 %}#fb923c{% else %}#34d399{% endif %};border-radius:4px;transition:width .3s"></div></div><span style="font-size:10px;color:var(--text3)">{{ dused }}/{{ daily_limit }}</span></div>{% else %}<span style="color:#3a3a4a;font-size:10px">--</span>{% endif %}</td>
          <td>{% if v.revoked %}<span style="color:#f87171;font-weight:600">Revoked</span>{% elif expired %}<span style="color:#3a3a4a">Expired</span>{% elif v.activated %}<span style="color:#34d399;font-weight:600">Active</span>{% else %}<span style="color:#fbbf24">Unused</span>{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:48px 20px;text-align:center;color:#3a3a4a;font-size:14px;font-weight:400">No keys yet. Generate one above.</div>
    {% endif %}
  </div>
</div>
</body>
</html>"""

# ── Entry point ─────────────────────────────────────────────────────
if __name__ == '__main__':
    admin_key = get_admin_key()
    key_count = len(lic_load().get('keys', {}))
    port = int(os.environ.get('PORT', 5000))
    print(f"\n  {'='*50}")
    print(f"   Waffles Priv Service")
    print(f"  {'='*50}")
    print(f"   Web App + License API on port {port}")
    print(f"   Admin panel:  http://localhost:{port}/admin")
    print(f"   Admin key:    {admin_key}")
    print(f"   Keys stored:  {key_count}")
    print(f"  {'='*50}\n")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
