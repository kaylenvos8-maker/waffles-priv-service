import os
import sys
import json
import secrets
import string
import hashlib
import subprocess
import re
from pathlib import Path
from datetime import datetime, timedelta

try:
    from flask import Flask, request, jsonify, render_template_string, redirect, url_for
except ImportError:
    print("Missing flask. Run: pip install flask")
    sys.exit(1)

BASE_DIR = Path(__file__).parent
LICENSES_FILE = BASE_DIR / 'licenses.json'
ADMIN_SECRET_FILE = BASE_DIR / '.admin_secret'
LICENSE_SERVER_PORT = 5001

PLANS = {
    'basic': {
        'name': 'Basic',
        'max_checks': 500,
        'max_checks_daily': True,
        'allow_signup': False,
        'max_workers': 5,
        'allow_proxy_rotation': False,
        'allow_custom_names': True,
    },
    'pro': {
        'name': 'Pro',
        'max_checks': 5000,
        'max_checks_daily': True,
        'allow_signup': True,
        'max_workers': 10,
        'allow_proxy_rotation': False,
        'allow_custom_names': True,
    },
    'premium': {
        'name': 'Premium',
        'max_checks': 999999999,
        'max_checks_daily': False,
        'allow_signup': True,
        'max_workers': 20,
        'allow_proxy_rotation': True,
        'allow_custom_names': True,
    }
}

DURATIONS = {
    '1d': {'label': '1 Day', 'days': 1},
    '7d': {'label': '7 Days', 'days': 7},
    '30d': {'label': '30 Days', 'days': 30},
    '90d': {'label': '90 Days', 'days': 90},
    '365d': {'label': '1 Year', 'days': 365},
    'lifetime': {'label': 'Lifetime', 'days': None},
}

app = Flask(__name__)


def load_licenses():
    if LICENSES_FILE.exists():
        try:
            return json.loads(LICENSES_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'keys': {}, 'admin_key': None}


def save_licenses(data):
    LICENSES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')


def generate_admin_key():
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(24))


def get_admin_key():
    data = load_licenses()
    if not data.get('admin_key'):
        data['admin_key'] = generate_admin_key()
        save_licenses(data)
    return data['admin_key']


def generate_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = ['WAFFLE']
    for _ in range(4):
        parts.append(''.join(secrets.choice(alphabet) for _ in range(6)))
    return '-'.join(parts)


def get_hwid():
    try:
        result = subprocess.run(['wmic', 'csproduct', 'get', 'uuid'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and line != 'UUID' and not line.startswith('wmic'):
                h = hashlib.sha256(line.encode()).hexdigest()
                return h
    except Exception:
        pass
    try:
        result = subprocess.run(['cmd', '/c', 'vol', 'C:'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if 'Volume Serial Number' in line:
                serial = line.split()[-1]
                return hashlib.sha256(serial.encode()).hexdigest()
    except Exception:
        pass
    return hashlib.sha256(b'unknown').hexdigest()


def check_rate_limit(license_key):
    data = load_licenses()
    key_data = data['keys'].get(license_key)
    if not key_data:
        return True
    plan = PLANS.get(key_data['plan'])
    if not plan or not plan.get('max_checks_daily'):
        return True
    today = datetime.now().strftime('%Y-%m-%d')
    daily = key_data.get('daily_usage', {})
    count = daily.get(today, 0)
    if count >= plan['max_checks']:
        return False
    return True


def record_check(license_key):
    data = load_licenses()
    key_data = data['keys'].get(license_key)
    if not key_data:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    if 'daily_usage' not in key_data:
        key_data['daily_usage'] = {}
    key_data['daily_usage'][today] = key_data['daily_usage'].get(today, 0) + 1
    key_data['total_checks'] = key_data.get('total_checks', 0) + 1
    save_licenses(data)


def require_admin(f):
    def wrapper(*args, **kwargs):
        admin_key = get_admin_key()
        auth = request.headers.get('Authorization', '')
        if auth == f'Bearer {admin_key}':
            return f(*args, **kwargs)
        body_key = request.json.get('admin_key') if request.is_json else None
        if body_key == admin_key:
            return f(*args, **kwargs)
        query_key = request.args.get('admin_key')
        if query_key == admin_key:
            return f(*args, **kwargs)
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    wrapper.__name__ = f.__name__
    return wrapper


@app.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'server': 'waffles-license', 'time': datetime.now().isoformat()})


@app.route('/api/validate', methods=['POST'])
def api_validate():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    if not key:
        return jsonify({'ok': False, 'error': 'No key provided'})

    licenses = load_licenses()
    key_data = licenses['keys'].get(key)
    if not key_data:
        return jsonify({'ok': False, 'error': 'Invalid key'})

    if key_data.get('revoked'):
        return jsonify({'ok': False, 'error': 'Key revoked'})

    if key_data.get('expires'):
        exp = datetime.fromisoformat(key_data['expires'])
        if exp < datetime.now():
            return jsonify({'ok': False, 'error': 'Key expired'})

    client_hwid = data.get('hwid', '')

    if key_data.get('hwid'):
        if key_data['hwid'] != client_hwid:
            return jsonify({'ok': False, 'error': 'Key already in use on another machine'})
    else:
        key_data['hwid'] = client_hwid
        key_data['activated'] = True
        key_data['activated_at'] = datetime.now().isoformat()

    key_data['last_seen'] = datetime.now().isoformat()
    key_data['last_ip'] = request.remote_addr
    save_licenses(licenses)

    plan = key_data['plan']
    plan_info = PLANS[plan]
    remaining = None
    if plan_info.get('max_checks_daily'):
        today = datetime.now().strftime('%Y-%m-%d')
        used = key_data.get('daily_usage', {}).get(today, 0)
        remaining = max(0, plan_info['max_checks'] - used)

    expires = key_data.get('expires')
    if expires:
        expires_dt = datetime.fromisoformat(expires)
        expires_str = expires_dt.strftime('%Y-%m-%d')
        days_left = (expires_dt - datetime.now()).days
    else:
        expires_str = 'Never'
        days_left = 9999

    return jsonify({
        'ok': True,
        'plan': plan,
        'plan_name': plan_info['name'],
        'expires': expires_str,
        'days_left': max(0, days_left),
        'max_checks': plan_info['max_checks'],
        'max_checks_daily': plan_info['max_checks_daily'],
        'remaining_checks': remaining,
        'allow_signup': plan_info['allow_signup'],
        'max_workers': plan_info['max_workers'],
        'allow_proxy_rotation': plan_info['allow_proxy_rotation'],
        'allow_custom_names': plan_info['allow_custom_names'],
        'total_checks': key_data.get('total_checks', 0),
        'activated': True,
    })


@app.route('/api/record_check', methods=['POST'])
def api_record_check():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    count = data.get('count', 1)
    for _ in range(count):
        record_check(key)
    return jsonify({'ok': True})


@app.route('/api/check_limits', methods=['POST'])
def api_check_limits():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    if not key:
        return jsonify({'ok': False, 'error': 'No key', 'blocked': True})
    licenses = load_licenses()
    key_data = licenses['keys'].get(key)
    ok = check_rate_limit(key)
    remaining = None
    max_checks = None
    if key_data:
        plan_info = PLANS.get(key_data['plan'], {})
        max_checks = plan_info.get('max_checks')
        if plan_info.get('max_checks_daily'):
            today = datetime.now().strftime('%Y-%m-%d')
            used = key_data.get('daily_usage', {}).get(today, 0)
            remaining = max(0, max_checks - used)
    if not ok:
        return jsonify({'ok': False, 'error': 'Daily check limit reached', 'blocked': True, 'remaining_checks': remaining, 'max_checks': max_checks})
    return jsonify({'ok': True, 'blocked': False, 'remaining_checks': remaining, 'max_checks': max_checks})


@app.route('/api/generate', methods=['POST'])
@require_admin
def api_generate():
    data = request.get_json()
    plan = data.get('plan', 'basic')
    duration = data.get('duration', '30d')
    notes = data.get('notes', '')

    if plan not in PLANS:
        return jsonify({'ok': False, 'error': f'Invalid plan. Choose: {", ".join(PLANS.keys())}'})
    if duration not in DURATIONS:
        return jsonify({'ok': False, 'error': f'Invalid duration. Choose: {", ".join(DURATIONS.keys())}'})

    key = data.get('key') or generate_key()
    dur_info = DURATIONS[duration]

    key_data = {
        'plan': plan,
        'created': datetime.now().isoformat(),
        'created_by': request.remote_addr,
        'notes': notes,
        'activated': False,
        'revoked': False,
        'total_checks': 0,
        'daily_usage': {},
    }

    if dur_info['days'] is not None:
        key_data['expires'] = (datetime.now() + timedelta(days=dur_info['days'])).isoformat()
    else:
        key_data['expires'] = None

    licenses = load_licenses()
    if key in licenses['keys']:
        return jsonify({'ok': False, 'error': 'Key already exists'})
    licenses['keys'][key] = key_data
    save_licenses(licenses)

    return jsonify({
        'ok': True,
        'key': key,
        'plan': plan,
        'duration': duration,
        'expires': key_data.get('expires', 'lifetime'),
        'notes': notes,
    })


@app.route('/api/keys', methods=['GET'])
@require_admin
def api_list_keys():
    licenses = load_licenses()
    keys_list = []
    for key, kdata in licenses['keys'].items():
        plan_info = PLANS.get(kdata['plan'], {})
        today = datetime.now().strftime('%Y-%m-%d')
        daily_used = kdata.get('daily_usage', {}).get(today, 0)

        expires = kdata.get('expires')
        if expires:
            expired = datetime.fromisoformat(expires) < datetime.now()
        else:
            expired = False

        keys_list.append({
            'key': key,
            'plan': kdata['plan'],
            'plan_name': plan_info.get('name', ''),
            'created': kdata.get('created', ''),
            'expires': expires,
            'expired': expired,
            'activated': kdata.get('activated', False),
            'revoked': kdata.get('revoked', False),
            'hwid': (kdata.get('hwid', '')[:16] + '...') if kdata.get('hwid') else None,
            'notes': kdata.get('notes', ''),
            'total_checks': kdata.get('total_checks', 0),
            'daily_used': daily_used,
            'last_seen': kdata.get('last_seen', ''),
        })
    return jsonify({'ok': True, 'keys': keys_list, 'admin_key': get_admin_key()})


@app.route('/api/revoke', methods=['POST'])
@require_admin
def api_revoke():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    licenses = load_licenses()
    if key not in licenses['keys']:
        return jsonify({'ok': False, 'error': 'Key not found'})
    licenses['keys'][key]['revoked'] = True
    licenses['keys'][key]['revoked_at'] = datetime.now().isoformat()
    save_licenses(licenses)
    return jsonify({'ok': True, 'message': f'Key {key} revoked'})


@app.route('/api/unrevoke', methods=['POST'])
@require_admin
def api_unrevoke():
    data = request.get_json()
    key = data.get('key', '').strip().upper()
    licenses = load_licenses()
    if key not in licenses['keys']:
        return jsonify({'ok': False, 'error': 'Key not found'})
    licenses['keys'][key]['revoked'] = False
    if 'revoked_at' in licenses['keys'][key]:
        del licenses['keys'][key]['revoked_at']
    save_licenses(licenses)
    return jsonify({'ok': True, 'message': f'Key {key} un-revoked'})


@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    admin_key = get_admin_key()
    message = None

    if request.method == 'POST':
        submitted_key = request.form.get('admin_key', '')
        if submitted_key != admin_key:
            return redirect(url_for('admin_panel', error='unauthorized'))
        plan = request.form.get('plan', 'basic')
        duration = request.form.get('duration', '30d')
        notes = request.form.get('notes', '')

        if plan not in PLANS:
            return redirect(url_for('admin_panel', error='invalid_plan'))
        if duration not in DURATIONS:
            return redirect(url_for('admin_panel', error='invalid_duration'))

        key = generate_key()
        dur_info = DURATIONS[duration]
        key_data = {
            'plan': plan,
            'created': datetime.now().isoformat(),
            'created_by': request.remote_addr,
            'notes': notes,
            'activated': False,
            'revoked': False,
            'total_checks': 0,
            'daily_usage': {},
        }
        if dur_info['days'] is not None:
            key_data['expires'] = (datetime.now() + timedelta(days=dur_info['days'])).isoformat()
        else:
            key_data['expires'] = None

        licenses = load_licenses()
        licenses['keys'][key] = key_data
        save_licenses(licenses)
        return redirect(url_for('admin_panel', ok='1', key=key, plan=plan, dur=dur_info['label']))

    msg_key = request.args.get('key')
    if msg_key:
        msg_plan = request.args.get('plan', '')
        msg_dur = request.args.get('dur', '')
        message = ('ok', msg_key, msg_plan, msg_dur)
    elif request.args.get('error') == 'unauthorized':
        message = ('error', 'Unauthorized')
    elif request.args.get('error') == 'invalid_plan':
        message = ('error', 'Invalid plan')
    elif request.args.get('error') == 'invalid_duration':
        message = ('error', 'Invalid duration')

    licenses = load_licenses()
    keys_list = sorted(licenses['keys'].items(), key=lambda x: x[1].get('created', ''), reverse=True)
    today = datetime.now().strftime('%Y-%m-%d')

    total_keys = len(keys_list)
    active_keys = sum(1 for _, v in keys_list if v.get('activated') and not v.get('revoked') and (not v.get('expires') or datetime.fromisoformat(v['expires']) >= datetime.now()))
    revoked_keys = sum(1 for _, v in keys_list if v.get('revoked'))
    total_checks_all = sum(v.get('total_checks', 0) for _, v in keys_list)

    return render_template_string(ADMIN_HTML,
        admin_key=admin_key, message=message, keys=keys_list,
        PLANS=PLANS, today=today, datetime=datetime,
        total_keys=total_keys, active_keys=active_keys,
        revoked_keys=revoked_keys, total_checks_all=total_checks_all)


@app.route('/api/plans')
def api_plans():
    return jsonify({
        'ok': True,
        'plans': {k: {'name': v['name'], 'key': k} for k, v in PLANS.items()},
        'durations': {k: v['label'] for k, v in DURATIONS.items()}
    })


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
.c.c2::before{background:linear-gradient(90deg,#30d158,#24a844)}
.c.c3::before{background:linear-gradient(90deg,#ff453a,#cc3a30)}
.c.c4::before{background:linear-gradient(90deg,#ff9f0a,#e88a00)}
.c .c-n{font-size:28px;font-weight:700;letter-spacing:-.5px;margin-bottom:2px}
.c .c-l{font-size:10px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.gc{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:16px;padding:24px 28px;margin-bottom:24px;transition:border-color .3s}
.gc:hover{border-color:rgba(255,255,255,.07)}
.gc h2{font-size:10px;color:#5a5a6a;text-transform:uppercase;letter-spacing:.7px;margin-bottom:18px;font-weight:600}
.gc h2 span{color:#3a3a4a;font-weight:400}
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
.msg-ok{background:rgba(48,209,88,.05);border:1px solid rgba(48,209,88,.1);color:#30d158}
.msg-err{background:rgba(255,69,58,.05);border:1px solid rgba(255,69,58,.1);color:#ff453a}
.msg .kv{font-family:'SF Mono','SF Pro',monospace;font-size:12px;color:#5ac8fa;user-select:all;word-break:break-all;background:rgba(0,0,0,.2);padding:4px 12px;border-radius:8px;letter-spacing:.3px}
.msg .mb{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);color:#5a5a6a;padding:5px 14px;border-radius:8px;cursor:pointer;font-size:10px;margin-left:auto;white-space:nowrap;transition:all .2s;font-family:inherit}
.msg .mb:hover{background:rgba(255,255,255,.08);color:#eeeef5;border-color:rgba(255,255,255,.1)}
.kc{color:#5a5a6a;font-size:11px;font-family:'SF Mono','SF Pro',monospace;letter-spacing:.2px}
.em{background:rgba(255,255,255,.015);border-radius:10px;padding:6px 12px;font-size:10px;color:#3a3a4a}
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
    <div class="c c4"><div class="c-n" style="color:#f59e0b">{{ total_checks_all }}</div><div class="c-l">Total Checks</div></div>
  </div>

  {% if message %}
  <div class="msg msg-{{ message[0] }}">
    {% if message[0] == 'ok' %}
      <span style="font-size:16px;flex-shrink:0;width:22px;height:22px;display:flex;align-items:center;justify-content:center;background:rgba(48,209,88,.1);border-radius:50%">&#10003;</span>
      <span><strong style="text-transform:capitalize;color:#eeeef5">{{ message[2] }}</strong> <span style="color:#5a5a6a">/</span> {{ message[3] }}</span>
      <span class="kv" id="nk" style="color:#fbbf24">{{ message[1] }}</span>
      <button class="mb" onclick="navigator.clipboard.writeText(document.getElementById('nk').textContent).then(()=>{this.textContent='Copied';setTimeout(()=>this.textContent='Copy',1800)})">Copy</button>
    {% else %}
      <span style="font-size:16px;flex-shrink:0;width:22px;height:22px;display:flex;align-items:center;justify-content:center;background:rgba(255,69,58,.1);border-radius:50%">&#10007;</span>
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
          {% set dused = v.daily_usage.get(today, 0) %}
          {% set plan_info = PLANS.get(v.plan, {}) %}
          {% set daily_limit = plan_info.get('max_checks', 0) if plan_info.get('max_checks_daily') else 0 %}
          <tr class="{{ 'st' if v.revoked or expired }}">
            <td class="kc">{{ k }}</td>
            <td><span class="bdg bdg-{{ v.plan }}">{{ PLANS[v.plan]['name'] }}</span></td>
            <td style="color:#5a5a6a">{% if v.expires %}{{ datetime.fromisoformat(v.expires).strftime('%b %d, %Y') }}{% else %}<span style="color:#30d158">&#8734; Lifetime</span>{% endif %}</td>
            <td style="color:#5a5a6a">{{ v.total_checks }}</td>
            <td>{% if daily_limit > 0 %}<div style="display:flex;align-items:center;gap:6px"><div style="flex:1;background:rgba(255,255,255,.04);border-radius:4px;height:4px;overflow:hidden;min-width:50px"><div style="height:100%;width:{{ (dused / daily_limit * 100)|round }}%;background:{% if dused >= daily_limit %}#ff453a{% elif dused > daily_limit * 0.8 %}#ff9f0a{% else %}#30d158{% endif %};border-radius:4px;transition:width .3s"></div></div><span style="font-size:10px;color:var(--text3)">{{ dused }}/{{ daily_limit }}</span></div>{% else %}<span style="color:#3a3a4a;font-size:10px">--</span>{% endif %}</td>
            <td>{% if v.revoked %}<span style="color:#ff453a;font-weight:600">Revoked</span>{% elif expired %}<span style="color:#3a3a4a">Expired</span>{% elif v.activated %}<span style="color:#30d158;font-weight:600">Active</span>{% else %}<span style="color:#ff9f0a">Unused</span>{% endif %}</td>
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


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Waffles License Server')
    parser.add_argument('--gen-key', choices=list(PLANS.keys()), help='Generate a key from CLI')
    parser.add_argument('--duration', choices=list(DURATIONS.keys()), default='30d', help='Key duration (default: 30d)')
    parser.add_argument('--notes', default='', help='Notes for the key')
    parser.add_argument('--port', type=int, default=LICENSE_SERVER_PORT, help=f'Port (default: {LICENSE_SERVER_PORT})')
    args = parser.parse_args()

    if args.gen_key:
        plan = args.gen_key
        key = generate_key()
        dur_info = DURATIONS[args.duration]
        key_data = {
            'plan': plan,
            'created': datetime.now().isoformat(),
            'notes': args.notes,
            'activated': False,
            'revoked': False,
            'total_checks': 0,
            'daily_usage': {},
        }
        if dur_info['days'] is not None:
            key_data['expires'] = (datetime.now() + timedelta(days=dur_info['days'])).isoformat()
        else:
            key_data['expires'] = None

        licenses = load_licenses()
        licenses['keys'][key] = key_data
        save_licenses(licenses)

        expires_str = dur_info['label']
        print(f"\n  Generated {plan.upper()} key ({expires_str}):")
        print(f"  {'='*45}")
        print(f"  {key}")
        print(f"  {'='*45}\n")
        return

    admin_key = get_admin_key()
    port = args.port

    key_count = len(load_licenses().get('keys', {}))

    print(f"\n  {'='*50}")
    print(f"   Waffles License Server")
    print(f"  {'='*50}")
    print(f"  Admin panel:  http://localhost:{port}/admin")
    print(f"  Admin key:    {admin_key}")
    print(f"  Keys stored:  {key_count}")
    print(f"  Storage:      {LICENSES_FILE}")
    print(f"  {'='*50}")
    print(f"  Quick gen:    python license_server.py --gen-key premium --duration lifetime")
    print(f"  {'='*50}\n")

    app.run(host='localhost', port=port, debug=False)


if __name__ == '__main__':
    main()
