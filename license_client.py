import json
import time
import hashlib
import subprocess
import threading
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent
LICENSE_CACHE_FILE = BASE_DIR / '.license_cache'
LICENSE_SERVER_URL = 'http://localhost:5001'

log = logging.getLogger(__name__)

_license_info = None
_license_lock = threading.Lock()
_validate_interval = 300
_last_validate = 0
_record_counter = 0
_record_batch_size = 10
_record_lock = threading.Lock()


def get_hwid():
    try:
        result = subprocess.run(['wmic', 'csproduct', 'get', 'uuid'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and line != 'UUID' and not line.startswith('wmic'):
                return hashlib.sha256(line.encode()).hexdigest()
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


def load_cached_license():
    if LICENSE_CACHE_FILE.exists():
        try:
            return json.loads(LICENSE_CACHE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return None


def save_cached_license(info):
    LICENSE_CACHE_FILE.write_text(json.dumps(info), encoding='utf-8')


def clear_cached_license():
    if LICENSE_CACHE_FILE.exists():
        LICENSE_CACHE_FILE.unlink()


def check_server_alive():
    import requests
    try:
        r = requests.get(f'{LICENSE_SERVER_URL}/api/health', timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def validate_key(key):
    import requests
    try:
        r = requests.post(f'{LICENSE_SERVER_URL}/api/validate', json={
            'key': key,
            'hwid': get_hwid(),
        }, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get('ok'):
                info = {
                    'key': key,
                    'plan': data['plan'],
                    'plan_name': data['plan_name'],
                    'expires': data['expires'],
                    'days_left': data['days_left'],
                    'max_checks': data['max_checks'],
                    'max_checks_daily': data['max_checks_daily'],
                    'remaining_checks': data['remaining_checks'],
                    'allow_signup': data['allow_signup'],
                    'max_workers': data['max_workers'],
                    'allow_proxy_rotation': data['allow_proxy_rotation'],
                    'allow_custom_names': data['allow_custom_names'],
                    'total_checks': data['total_checks'],
                    'validated_at': time.time(),
                }
                global _license_info
                with _license_lock:
                    _license_info = info
                save_cached_license(info)
                return info, None
            return None, data.get('error', 'Validation failed')
        return None, f'Server error: HTTP {r.status_code}'
    except requests.exceptions.ConnectionError:
        cached = load_cached_license()
        if cached:
            with _license_lock:
                _license_info = cached
            return cached, None
        return None, 'Cannot reach license server'
    except requests.exceptions.Timeout:
        return None, 'License server timed out'
    except Exception as e:
        return None, str(e)


def get_license():
    global _license_info
    with _license_lock:
        if _license_info:
            return _license_info
    cached = load_cached_license()
    if cached:
        with _license_lock:
            _license_info = cached
        return cached
    return None


def record_check():
    global _record_counter
    info = get_license()
    if not info:
        return
    with _record_lock:
        _record_counter += 1
        if _record_counter < _record_batch_size:
            return
        _record_counter = 0
    import requests
    try:
        r = requests.post(f'{LICENSE_SERVER_URL}/api/record_check', json={
            'key': info['key'],
            'count': _record_batch_size,
        }, timeout=5)
        if r.status_code == 200:
            if info.get('remaining_checks') is not None:
                with _license_lock:
                    info['remaining_checks'] = max(0, info['remaining_checks'] - _record_batch_size)
                save_cached_license(info)
    except Exception:
        pass


def check_limits():
    info = get_license()
    if not info:
        return False, 'No license'
    if info.get('max_checks_daily'):
        import requests
        try:
            r = requests.post(f'{LICENSE_SERVER_URL}/api/check_limits', json={
                'key': info['key'],
            }, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get('blocked'):
                    return False, 'Daily check limit reached'
                if 'remaining_checks' in data and data['remaining_checks'] is not None:
                    with _license_lock:
                        info['remaining_checks'] = data['remaining_checks']
                        info['max_checks'] = data.get('max_checks', info.get('max_checks'))
                    save_cached_license(info)
        except Exception:
            pass
    return True, None


def check_feature(feature):
    info = get_license()
    if not info:
        return False
    return info.get(feature, False)


def background_validate(stop_event=None):
    global _last_validate
    while True:
        if stop_event and stop_event.is_set():
            break
        time.sleep(_validate_interval)
        info = get_license()
        if info:
            _, err = validate_key(info['key'])
            if err:
                log.warning(f"License re-validation failed: {err}")


def start_background_validation(stop_event=None):
    t = threading.Thread(target=background_validate, args=(stop_event,), daemon=True)
    t.start()
    return t
