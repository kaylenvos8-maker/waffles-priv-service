import sys
import threading
import json
import time as time_module
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from pantoo_checker import (
    scan_usernames, build_charset, check_roblox, load_profiles, save_profiles,
    load_pending_signups, save_pending_signups, batch_signup_all, signup_roblox,
    add_pending_signup, ROBLOX_HITS_FILE, CHECKED_FILE, CONFIG_FILE, BASE_DIR, PENDING_SIGNUPS_FILE
)
from license_client import (
    validate_key, get_license, check_feature, check_limits,
    record_check, clear_cached_license, start_background_validation,
    check_server_alive
)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'waffles-secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

scan_thread = None
stop_event = threading.Event()
scan_lock = threading.Lock()

PROXIES_FILE = BASE_DIR / 'proxies.txt'
SCAN_HISTORY_FILE = BASE_DIR / 'scan_history.json'


def load_scan_history():
    if SCAN_HISTORY_FILE.exists():
        try:
            return json.loads(SCAN_HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return []

def save_scan_history(history):
    SCAN_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding='utf-8')

def add_scan_session(data):
    history = load_scan_history()
    session = {
        'timestamp': datetime.now().isoformat(),
        'duration': data.get('duration', 0),
        'checked': data.get('checked', 0),
        'hits': data.get('hits', 0),
        'errors': data.get('errors', 0),
        'mode': data.get('mode', ''),
        'lengths': data.get('lengths', []),
        'workers': data.get('workers', 0),
    }
    history.insert(0, session)
    if len(history) > 50:
        history = history[:50]
    save_scan_history(history)

def load_settings():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {
        'sound': False,
        'scroll': True,
        'save': True,
        'desktopNotify': True,
        'timeout': 10,
        'retries': 1,
        'accent': '#007aff'
    }


def save_settings(data):
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')


def load_proxies():
    if PROXIES_FILE.exists():
        return [l.strip() for l in PROXIES_FILE.read_text(encoding='utf-8').splitlines() if l.strip()]
    return []


def save_proxies(proxies):
    PROXIES_FILE.write_text('\n'.join(proxies), encoding='utf-8')


def send_log(message, type='info'):
    socketio.emit('log', {'message': message, 'type': type, 'time': time_module.time()})


def send_stats(checked, hits, errors=0):
    socketio.emit('stats', {'checked': checked, 'hits': hits, 'errors': errors})


def background_scan(data):
    global scan_thread
    try:
        lic = get_license()
        if not lic:
            send_log("No valid license. Scan blocked.", 'error')
            return

        lengths = data.get('lengths', [5])
        charset = build_charset(data.get('charset', 'a'))
        mode = data.get('mode', 'u')
        max_checks = data.get('maxChecks', 0)
        delay = data.get('delay', 0.3)
        workers = min(data.get('workers', 8), lic.get('max_workers', 5))
        proxy_raw = data.get('proxy', '').strip()
        use_checked = data.get('saveChecked', True)
        custom_names = data.get('customNames')
        use_proxy_rotation = data.get('proxyRotation', False) and lic.get('allow_proxy_rotation', False)
        timeout = data.get('timeout', 10)
        auto_signup = data.get('autoSignup', False) and lic.get('allow_signup', False)
        signup_pw = data.get('signupPassword', '')

        if not lic.get('allow_signup') and auto_signup:
            send_log("Signup not allowed on your plan. Upgrade to Pro or Premium.", 'warn')
            auto_signup = False
            data['autoSignup'] = False

        if lic.get('max_checks_daily') and lic.get('remaining_checks') is not None:
            if lic['remaining_checks'] <= 0:
                send_log("Daily check limit reached. Upgrade your plan or wait.", 'error')
                return
            send_log(f"Daily checks remaining: {lic['remaining_checks']} / {lic['max_checks']}", 'info')

        proxies = None
        if use_proxy_rotation:
            all_proxies = load_proxies()
            if all_proxies:
                proxies = all_proxies
                send_log(f"Proxy rotation enabled ({len(all_proxies)} proxies)", 'info')
        elif proxy_raw:
            proxies = [{'http': proxy_raw, 'https': proxy_raw}]

        socketio.emit('scan_start', {'lengths': lengths, 'mode': mode})

        if custom_names:
            names_list = [n.strip() for n in custom_names.split('\n') if n.strip()]
            send_log(f"Custom list: {len(names_list)} names", 'info')
        else:
            names_list = None
            send_log(f"Starting scan: {'+'.join(str(l) for l in lengths)}L, mode={mode}, workers={workers}, delay={delay}s", 'info')

        if proxies:
            proxy_count = len(proxies) if isinstance(proxies, list) else 1
            send_log(f"Using {proxy_count} proxy/proxies", 'info')

        _scan_start = [time_module.time()]
        _scan_check_count = [0]
        _scan_hit_count = [0]
        _scan_error_count = [0]
        _last_limit_check = [0]

        def on_check_impl(c, h, e):
            send_stats(c, h, e)
            record_check()
            _scan_check_count[0] = c
            _scan_hit_count[0] = h
            _scan_error_count[0] = e
            if _scan_check_count[0] - _last_limit_check[0] >= 50:
                _last_limit_check[0] = _scan_check_count[0]
                ok, msg = check_limits()
                if not ok:
                    send_log(f"Limit reached: {msg}. Stopping scan.", 'error')
                    stop_event.set()
                    socketio.emit('license_limit_reached', {'msg': msg})

        scan_usernames(
            lengths, charset, mode, max_checks, delay, workers, proxies,
            on_hit=lambda l, n: (
                send_log(f"[HIT] {n} ({l}L)", 'hit'),
                socketio.emit('new_hit', {'length': l, 'name': n, 'time': time_module.time()}),
                load_and_emit_hits()
            ),
            on_check=on_check_impl,
            on_error=lambda e: send_log(f"Error/rate-limit count: {e}", 'warn'),
            on_signup=lambda n, ok, msg: send_log(f"[SIGNUP] {n} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error'),
            stop_event=stop_event,
            custom_names=names_list,
            use_save_checked=use_checked,
            signup_password=signup_pw if auto_signup else None
        )

        if not stop_event.is_set():
            send_log("Scan complete.", 'success')
        add_scan_session({
            'duration': round(time_module.time() - _scan_start[0], 1) if _scan_start else 0,
            'checked': _scan_check_count[0],
            'hits': _scan_hit_count[0],
            'errors': _scan_error_count[0],
            'mode': mode,
            'lengths': lengths,
            'workers': workers,
        })
    except Exception as e:
        send_log(f"Error: {str(e)}", 'error')
    finally:
        stop_event.clear()
        with scan_lock:
            scan_thread = None
        socketio.emit('scan_done')
        send_log("Ready.", 'info')


def load_and_emit_hits():
    if ROBLOX_HITS_FILE.exists():
        content = ROBLOX_HITS_FILE.read_text(encoding='utf-8')
        lines = []
        for line in content.strip().splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                lines.append({'length': parts[0], 'name': parts[1]})
        socketio.emit('hits', {'hits': lines})
    else:
        socketio.emit('hits', {'hits': []})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        data = request.get_json()
        if data:
            save_settings(data)
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': 'no data'}), 400
    return jsonify(load_settings())


@app.route('/api/proxies', methods=['GET', 'POST', 'DELETE'])
def api_proxies():
    if request.method == 'GET':
        return jsonify(load_proxies())
    elif request.method == 'POST':
        data = request.get_json()
        if data and 'proxy' in data:
            proxies = load_proxies()
            if data['proxy'] not in proxies:
                proxies.append(data['proxy'])
                save_proxies(proxies)
            return jsonify({'ok': True, 'proxies': proxies})
        return jsonify({'ok': False, 'error': 'no proxy'}), 400
    elif request.method == 'DELETE':
        data = request.get_json()
        if data and 'proxy' in data:
            proxies = load_proxies()
            if data['proxy'] in proxies:
                proxies.remove(data['proxy'])
                save_proxies(proxies)
            return jsonify({'ok': True, 'proxies': proxies})
        return jsonify({'ok': False, 'error': 'no proxy'}), 400


@app.route('/api/proxy_test', methods=['POST'])
def api_proxy_test():
    data = request.get_json()
    proxy = data.get('proxy', '')
    if not proxy:
        return jsonify({'ok': False, 'error': 'No proxy'})
    p = {'http': proxy, 'https': proxy}
    t0 = time_module.time()
    result = check_roblox('zzzxxyy12345', proxies=p)
    elapsed = round(time_module.time() - t0, 2)
    return jsonify({'ok': result is not False, 'time': elapsed, 'status': 'valid' if result is not False else 'invalid'})


@app.route('/api/profiles', methods=['GET', 'POST', 'DELETE'])
def api_profiles():
    if request.method == 'GET':
        return jsonify(load_profiles())
    elif request.method == 'POST':
        data = request.get_json()
        if data and 'name' in data and 'config' in data:
            profiles = load_profiles()
            profiles[data['name']] = data['config']
            save_profiles(profiles)
            return jsonify({'ok': True, 'profiles': profiles})
        return jsonify({'ok': False, 'error': 'need name and config'}), 400
    elif request.method == 'DELETE':
        data = request.get_json()
        if data and 'name' in data:
            profiles = load_profiles()
            profiles.pop(data['name'], None)
            save_profiles(profiles)
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
            parts = line.split('\t', 1)
            if len(parts) == 2:
                names.append(parts[1])
        return '\n'.join(names) + '\n', 200, {'Content-Type': 'text/plain', 'Content-Disposition': 'attachment; filename=hits.txt'}
    return '', 200, {'Content-Type': 'text/plain'}


@socketio.on('connect')
def handle_connect(auth=None):
    settings = load_settings()
    emit('settings_loaded', settings)
    emit('log', {'message': 'Connected to server.', 'type': 'success', 'time': time_module.time()})
    load_and_emit_hits()
    history = load_scan_history()
    emit('scan_history', {'history': history})
    signups = load_pending_signups()
    emit('pending_signups', {'signups': signups})
    total = len(signups)
    success = sum(1 for s in signups if s['status'] == 'signed')
    emit('signup_stats', {'total': total, 'success': success, 'failed': total - success})
    server_alive = check_server_alive()
    lic = get_license()
    if lic:
        emit('license_status', {'ok': True, 'plan': lic['plan'], 'plan_name': lic['plan_name'], 'expires': lic.get('expires', 'Never'), 'days_left': lic.get('days_left', 0), 'max_checks': lic.get('max_checks', 0), 'remaining_checks': lic.get('remaining_checks'), 'allow_signup': lic.get('allow_signup', False), 'max_workers': lic.get('max_workers', 5), 'allow_proxy_rotation': lic.get('allow_proxy_rotation', False)})
    else:
        emit('license_status', {'ok': False, 'server_alive': server_alive})


@socketio.on('validate_license')
def handle_validate_license(data):
    key = data.get('key', '').strip().upper()
    if not key:
        emit('license_result', {'ok': False, 'error': 'No key provided'})
        return
    info, err = validate_key(key)
    if info:
        emit('license_result', {'ok': True, 'plan': info['plan'], 'plan_name': info['plan_name'], 'expires': info.get('expires', 'Never'), 'days_left': info.get('days_left', 0), 'max_checks': info.get('max_checks', 0), 'remaining_checks': info.get('remaining_checks'), 'allow_signup': info.get('allow_signup', False), 'max_workers': info.get('max_workers', 5), 'allow_proxy_rotation': info.get('allow_proxy_rotation', False)})
        emit('log', {'message': f"License validated: {info['plan_name']} plan", 'type': 'success', 'time': time_module.time()})
    else:
        emit('license_result', {'ok': False, 'error': err})


@socketio.on('get_license')
def handle_get_license():
    lic = get_license()
    if lic:
        emit('license_status', {'ok': True, 'plan': lic['plan'], 'plan_name': lic['plan_name'], 'expires': lic.get('expires', 'Never'), 'days_left': lic.get('days_left', 0), 'max_checks': lic.get('max_checks', 0), 'remaining_checks': lic.get('remaining_checks'), 'allow_signup': lic.get('allow_signup', False), 'max_workers': lic.get('max_workers', 5), 'allow_proxy_rotation': lic.get('allow_proxy_rotation', False)})
    else:
        emit('license_status', {'ok': False})


@socketio.on('get_hits')
def handle_get_hits():
    load_and_emit_hits()


@socketio.on('get_log')
def handle_get_log():
    pass


@socketio.on('start_scan')
def handle_start_scan(data):
    global scan_thread
    with scan_lock:
        if scan_thread and scan_thread.is_alive():
            emit('log', {'message': 'Still stopping previous scan, please wait...', 'type': 'warn', 'time': time_module.time()})
            return
        stop_event.clear()
        scan_thread = threading.Thread(target=background_scan, args=(data,), daemon=True)
        scan_thread.start()
    emit('log', {'message': 'Scan started.', 'type': 'info', 'time': time_module.time()})


@socketio.on('stop_scan')
def handle_stop_scan():
    stop_event.set()
    emit('log', {'message': 'Stopping scan...', 'type': 'warn', 'time': time_module.time()})


@socketio.on('run_diag')
def handle_diag():
    def run():
        send_log("Running diagnostics...", 'info')
        test_names = ['zzzxxyy12345', 'abcdef_noop', 'test____123', 'xwyzabc']
        for name in test_names:
            status = check_roblox(name)
            label = 'AVAILABLE' if status else 'TAKEN/ERROR'
            send_log(f"  {name} -> {label}", 'hit' if status else 'error')
        send_log("Diagnostics complete.", 'success')
    threading.Thread(target=run, daemon=True).start()


@socketio.on('clear_hits')
def handle_clear_hits():
    if ROBLOX_HITS_FILE.exists():
        ROBLOX_HITS_FILE.write_text('')
        emit('log', {'message': 'Hits file cleared.', 'type': 'warn', 'time': time_module.time()})
        load_and_emit_hits()


@socketio.on('save_settings')
def handle_save_settings(data):
    save_settings(data)
    emit('log', {'message': 'Settings saved.', 'type': 'success', 'time': time_module.time()})


@socketio.on('clear_cache')
def handle_clear_cache():
    if CHECKED_FILE.exists():
        CHECKED_FILE.write_text('')
        emit('log', {'message': 'Checked names cache cleared.', 'type': 'warn', 'time': time_module.time()})


@socketio.on('get_pending_signups')
def handle_get_pending_signups():
    signups = load_pending_signups()
    emit('pending_signups', {'signups': signups})
    total = len(signups)
    success = sum(1 for s in signups if s['status'] == 'signed')
    emit('signup_stats', {'total': total, 'success': success, 'failed': total - success})


@socketio.on('batch_signup')
def handle_batch_signup(data):
    password = data.get('password', '')
    if not password:
        emit('log', {'message': 'No signup password set in Settings', 'type': 'error', 'time': time_module.time()})
        return
    emit('log', {'message': 'Starting batch signup...', 'type': 'info', 'time': time_module.time()})
    stop_sig = threading.Event()
    def run():
        results = batch_signup_all(
            password,
            on_each=lambda n, ok, msg: (
                emit('signup_result', {'name': n, 'ok': ok, 'msg': msg}),
                send_log(f"[SIGNUP] {n} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error')
            ),
            stop_event=stop_sig
        )
        signups = load_pending_signups()
        emit('pending_signups', {'signups': signups})
        total = len(results)
        success = sum(1 for _, ok, _ in results if ok)
        emit('signup_stats', {'total': total, 'success': success, 'failed': total - success})
        send_log(f"Batch signup complete. {success}/{total} succeeded ({total > 0 and round(success/total*100, 1) or 0}%)", 'success')
    threading.Thread(target=run, daemon=True).start()


@socketio.on('signup_single')
def handle_signup_single(data):
    name = data.get('name', '')
    password = data.get('password', '')
    if not name or not password:
        emit('log', {'message': 'Missing name or password', 'type': 'error', 'time': time_module.time()})
        return
    emit('log', {'message': f"Signing up {name}...", 'type': 'info', 'time': time_module.time()})
    def run():
        ok, msg = signup_roblox(name, password)
        signups = load_pending_signups()
        for s in signups:
            if s['name'] == name:
                s['status'] = 'signed' if ok else 'failed'
                s['signed_at'] = time_module.time()
                s['message'] = msg
                break
        save_pending_signups(signups)
        emit('signup_result', {'name': name, 'ok': ok, 'msg': msg})
        emit('pending_signups', {'signups': signups})
        send_log(f"[SIGNUP] {name} -> {'OK' if ok else 'FAIL'}: {msg}", 'hit' if ok else 'error')
    threading.Thread(target=run, daemon=True).start()


@socketio.on('get_dashboard')
def handle_get_dashboard():
    hit_count = 0
    if ROBLOX_HITS_FILE.exists():
        hit_count = len([l for l in ROBLOX_HITS_FILE.read_text(encoding='utf-8').splitlines() if l.strip()])
    cache_count = 0
    if CHECKED_FILE.exists():
        cache_count = len([l for l in CHECKED_FILE.read_text(encoding='utf-8').splitlines() if l.strip()])
    signups = load_pending_signups()
    pending_count = sum(1 for s in signups if s['status'] == 'pending')
    signed_count = sum(1 for s in signups if s['status'] == 'signed')
    history = load_scan_history()
    emit('dashboard', {
        'total_checked': cache_count,
        'total_hits': hit_count,
        'pending_signups': pending_count,
        'signed_signups': signed_count,
        'scan_sessions': len(history),
    })

@socketio.on('get_scan_history')
def handle_get_scan_history():
    history = load_scan_history()
    emit('scan_history', {'history': history})

if __name__ == '__main__':
    start_background_validation()
    print("Waffles Web App starting on http://localhost:5000")
    socketio.run(app, host='localhost', port=5000, debug=False, allow_unsafe_werkzeug=True)
