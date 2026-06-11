import itertools
import random
import time
import os
import sys
import json
import logging
import re
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path
from collections import deque

try:
    import requests
except ImportError:
    print("Missing 'requests' library. Run: pip install requests")
    sys.exit(1)

try:
    from colorama import init as colorama_init, Fore, Style
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    class DummyColor:
        RESET = ''
        def __getattr__(self, name):
            return ''
    Fore = Style = DummyColor()
    def colorama_init(*args, **kwargs):
        return

ASCII = r"""
__        __  _____  ______ _ _      _   _  ___  
\ \      / / |  ___||  ___/| | |    | \ | || _ \ 
 \ \ /\ / /  | |_   | |_  | | |    |  \| || |/ /
  \ V  V /   |  _|  |  _| | | |    | |\  ||  <  
   \_/\_/    |_|    |_|   |_|_|____|_| \_||_|\_\ 
                       |_____|                    
                 WAFFLES PRIV SERVICE v2
"""

BASE_DIR = Path(__file__).parent
ROBLOX_HITS_FILE = BASE_DIR / 'roblox_hits.txt'
CHECKED_FILE = BASE_DIR / '.checked.txt'
CONFIG_FILE = BASE_DIR / 'config.json'
PROFILES_FILE = BASE_DIR / 'profiles.json'
PENDING_SIGNUPS_FILE = BASE_DIR / 'pending_signups.json'
LOG_FILE = BASE_DIR / 'waffles.log'
ROBLOX_VALIDATE = 'https://auth.roblox.com/v1/usernames/validate'

PROFANITY = {'shit', 'fuck', 'bitch', 'cunt', 'ass', 'dick', 'piss', 'damn', 'cock', 'porn', 'sex', 'slut', 'whore', 'bastard', 'fag'}
RARER = 'zxqjw'
COMMON = 'etaoinshrdlcumwfgypbvk'

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
]

ACCEPT_LANGS = ['en-US,en;q=0.9', 'en-US,en;q=0.8', 'en-GB,en;q=0.9', 'en;q=0.8', 'en-US,en;q=0.7,fr;q=0.3']

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler(sys.stderr)]
)
log = logging.getLogger(__name__)

_write_lock = Lock()


def init_colors():
    if COLORAMA_AVAILABLE:
        colorama_init(autoreset=True)


def print_line(text='', color='', bold=False, end='\n'):
    prefix = ''
    suffix = ''
    if color:
        prefix += color
    if bold:
        prefix += Style.BRIGHT
    if prefix:
        suffix = Style.RESET_ALL
    sys.stdout.write(prefix + text + suffix + end)
    sys.stdout.flush()


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    init_colors()
    clear_screen()
    print_line(ASCII, Fore.CYAN, bold=True)
    print_line('      Improved Roblox username checker with GUI support', Fore.YELLOW)
    print_line()


def prompt_choice(prompt, options, default=None):
    options_text = '/'.join(options)
    default_str = f" (default {default})" if default is not None else ""
    while True:
        choice = input(f"{prompt} [{options_text}]{default_str}: ").strip().lower()
        if not choice and default is not None:
            return default
        if choice in options:
            return choice
        print_line('Invalid choice. Try again.', Fore.RED)


def prompt_text(prompt, default=None):
    default_str = f" (default {default})" if default is not None else ""
    answer = input(f"{prompt}{default_str}: ").strip()
    return answer or default


def build_charset(choice):
    if choice == 'd':
        return '0123456789'
    if choice == 'ad':
        return 'abcdefghijklmnopqrstuvwxyz0123456789'
    return 'abcdefghijklmnopqrstuvwxyz'


def load_checked():
    if not CHECKED_FILE.exists():
        return set()
    with open(CHECKED_FILE, 'r', encoding='utf-8') as f:
        return {line.strip() for line in f if line.strip()}


def save_checked(key):
    with _write_lock:
        with open(CHECKED_FILE, 'a', encoding='utf-8') as f:
            f.write(key + '\n')


def save_hit(length, name):
    entry = f"{length}\t{name}"
    with _write_lock:
        with open(ROBLOX_HITS_FILE, 'a', encoding='utf-8') as f:
            f.write(entry + '\n')
    print_line(f"[HIT] {name} (ROBLOX, {length}L)", Fore.GREEN, bold=True)


def load_pending_signups():
    if PENDING_SIGNUPS_FILE.exists():
        try:
            return json.loads(PENDING_SIGNUPS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return []


def save_pending_signups(signups):
    PENDING_SIGNUPS_FILE.write_text(json.dumps(signups, indent=2), encoding='utf-8')


def add_pending_signup(length, name):
    signups = load_pending_signups()
    if not any(s['name'] == name for s in signups):
        signups.append({'name': name, 'length': length, 'found': time.time(), 'status': 'pending'})
        save_pending_signups(signups)
    return signups


_rate_adjust_lock = Lock()
_consecutive_429 = 0
_base_delay = 0.3


def random_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Content-Type': 'application/json',
        'Origin': 'https://www.roblox.com',
        'Referer': 'https://www.roblox.com/',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': random.choice(ACCEPT_LANGS),
        'Accept-Encoding': 'gzip, deflate, br',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
        'Connection': 'keep-alive',
    }


def check_roblox(username, timeout=10, proxies=None):
    global _consecutive_429, _base_delay
    session = requests.Session()
    headers = random_headers()
    payload = {
        'username': username,
        'birthday': '1990-01-01T00:00:00.000Z',
        'context': 'Signup'
    }
    try:
        r = session.post(ROBLOX_VALIDATE, json=payload, headers=headers, timeout=timeout, proxies=proxies)
    except requests.exceptions.Timeout:
        return None
    except Exception as e:
        log.debug(f"Request failed for {username}: {e}")
        return False

    if r.status_code == 403:
        token = r.headers.get('x-csrf-token') or r.headers.get('X-CSRF-Token')
        if token:
            headers['X-CSRF-Token'] = token
            try:
                r = session.post(ROBLOX_VALIDATE, json=payload, headers=headers, timeout=timeout, proxies=proxies)
            except requests.exceptions.Timeout:
                return None
            except Exception as e:
                log.debug(f"Retry failed for {username}: {e}")
                return False

    if r.status_code != 200:
        if r.status_code == 429:
            with _rate_adjust_lock:
                _consecutive_429 += 1
                backoff = min(_consecutive_429 * 0.5, 10)
                _base_delay = min(_base_delay * 1.3, 5.0)
            if backoff > 0:
                time.sleep(backoff)
            return None
        return False
    with _rate_adjust_lock:
        if _consecutive_429 > 0:
            _consecutive_429 = max(0, _consecutive_429 - 1)
        _base_delay = max(0.3, _base_delay * 0.95)
    try:
        data = r.json()
    except Exception:
        return False
    return isinstance(data, dict) and data.get('code') == 0


class RostileSolver:
    def __init__(self, session, challenge_id):
        self.challenge_id = challenge_id
        self.session = session
        self.screen_width = 1920
        self.screen_height = 1080

    def _bezier_curve(self, p0, p1, p2, p3, t):
        return (1 - t)**3 * p0 + 3*(1 - t)**2 * t * p1 + 3*(1 - t) * t**2 * p2 + t**3 * p3

    def _generate_mouse_movements(self, duration):
        patterns = []
        # bezier
        def bezier():
            m, sx, sy = [], 1920, 1080
            cx1, cy1 = random.randint(0,sx), random.randint(0,sy)
            cx2, cy2 = random.randint(0,sx), random.randint(0,sy)
            ex, ey = random.randint(0,sx), random.randint(0,sy)
            for s in range(int(duration*100)+1):
                t = s/(duration*100)
                m.append({'x': self._bezier_curve(0,cx1,cx2,ex,t), 'y': self._bezier_curve(0,cy1,cy2,ey,t), 'timestamp': s*(duration*1000/100)})
            return m
        patterns.append(bezier)
        # jittered
        def jittered():
            m, sx, sy = [], 1920, 1080
            ex, ey = random.randint(0,sx), random.randint(0,sy)
            for s in range(int(duration*100)+1):
                t = s/(duration*100)
                m.append({'x': ex*t+random.uniform(-5,5), 'y': ey*t+random.uniform(-5,5), 'timestamp': s*(duration*1000/100)})
            return m
        patterns.append(jittered)
        # sine
        def sine():
            m, sx, sy = [], 1920, 1080
            ex, ey = random.randint(0,sx), random.randint(0,sy)
            amp, freq = random.uniform(20,50), random.uniform(0.05,0.15)
            for s in range(int(duration*100)+1):
                t = s/(duration*100)
                m.append({'x': ex*t, 'y': ey*t+amp*math.sin(freq*s), 'timestamp': s*(duration*1000/100)})
            return m
        patterns.append(sine)
        return random.choice(patterns)()

    def solve(self):
        sol = {
            'challengeId': self.challenge_id,
            'solution': {
                'buttonClicked': True,
                'click': {'x': 950.0, 'y': 530.0, 'timestamp': random.uniform(5000,15000), 'duration': random.uniform(25,50)},
                'completionTime': random.uniform(2000,3000),
                'mouseMovements': self._generate_mouse_movements(random.uniform(1.0,3.0)),
                'screenSize': {'width': 1920, 'height': 1080},
                'buttonLocation': {'x': 960.0, 'y': 540.0, 'width': 360.0, 'height': 48.0},
                'windowSize': {'width': 1920, 'height': 1080},
                'isMobile': False
            }
        }
        r = self.session.post('https://apis.roblox.com/rostile/v1/verify', json=sol)
        data = r.json()
        token = data.get('redemptionToken')
        if token:
            self.session.post('https://apis.roblox.com/challenge/v1/continue', json={
                'challengeId': self.challenge_id,
                'challengeType': 'rostile',
                'challengeMetadata': f'{{"redemptionToken":"{token}"}}'
            })
            return True
        return False


def signup_roblox(username, password, proxies=None):
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/json',
        'Origin': 'https://www.roblox.com',
        'Referer': 'https://www.roblox.com/'
    }
    try:
        r = session.post('https://auth.roblox.com/v2/signup', headers=headers, timeout=15, proxies=proxies)
        token = r.headers.get('x-csrf-token')
        if not token:
            return False, 'no CSRF token'
        headers['x-csrf-token'] = token
        year = random.randint(1980, 2005)
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthday = f"{year:04d}-{month:02d}-{day:02d}T00:00:00.000Z"
        payload = {
            'username': username,
            'password': password,
            'birthday': birthday,
            'gender': random.choice([1, 2]),
            'isTosAgreed': True,
            'email': ''
        }
        r = session.post('https://auth.roblox.com/v2/signup', json=payload, headers=headers, timeout=15, proxies=proxies)
        if r.status_code == 200:
            return True, 'signed up'
        # Check for captcha challenge
        challenge_id = r.headers.get('x-challenge-id') or r.headers.get('x-roblox-challenge-id')
        if r.status_code == 403 and challenge_id:
            solver = RostileSolver(session, challenge_id)
            if solver.solve():
                r = session.post('https://auth.roblox.com/v2/signup', json=payload, headers=headers, timeout=15, proxies=proxies)
                if r.status_code == 200:
                    return True, 'signed up after captcha'
                data = r.json()
                msg = data.get('errors', [{}])[0].get('message', '') or r.text[:150]
                return False, f"captcha retry HTTP {r.status_code}: {msg}"
            return False, 'captcha solve failed'
        data = r.json()
        msg = data.get('errors', [{}])[0].get('message', '') or r.text[:150]
        return False, f"HTTP {r.status_code}: {msg}"
    except Exception as e:
        return False, str(e)


def batch_signup_all(password, on_each=None, stop_event=None):
    signups = load_pending_signups()
    pending = [s for s in signups if s['status'] == 'pending']
    results = []
    for s in pending:
        if stop_event and stop_event.is_set():
            break
        ok, msg = signup_roblox(s['name'], password)
        s['status'] = 'signed' if ok else 'failed'
        s['signed_at'] = time.time()
        s['message'] = msg
        results.append((s['name'], ok, msg))
        save_pending_signups(signups)
        if on_each:
            on_each(s['name'], ok, msg)
    return results


def rotate_proxy(proxies_list):
    if not proxies_list:
        return None
    idx = random.randint(0, len(proxies_list) - 1)
    raw = proxies_list[idx]
    return {'http': raw, 'https': raw}


class RateLimiter:
    def __init__(self, calls_per_sec=3):
        self.calls_per_sec = calls_per_sec
        self.window = deque()
        self.lock = Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            while self.window and self.window[0] < now - 1:
                self.window.popleft()
            target = self.calls_per_sec
            if len(self.window) >= target:
                sleep_time = self.window[0] + 1 - now
                if sleep_time > 0:
                    jitter = random.uniform(-sleep_time * 0.3, sleep_time * 0.3)
                    time.sleep(max(0, sleep_time + jitter))
            self.window.append(time.time())

    def set_rate(self, calls_per_sec):
        with self.lock:
            self.calls_per_sec = max(0.5, calls_per_sec)


def generate_sequential(length, charset):
    for combo in itertools.product(charset, repeat=length):
        yield ''.join(combo)


def generate_random(length, charset, max_count=None):
    rng = random.Random()
    seen = set()
    total_possible = len(charset) ** length
    count = 0
    stall_limit = min(100000, total_possible * 10)
    stalled = 0
    while True:
        if max_count and count >= max_count:
            break
        name = ''.join(rng.choice(charset) for _ in range(length))
        if name not in seen:
            seen.add(name)
            yield name
            count += 1
            stalled = 0
        else:
            stalled += 1
            if stalled > stall_limit:
                break


def contains_profanity(name):
    lower = name.lower()
    return any(word in lower for word in PROFANITY)


def generate_unique(length, charset, max_count=10000):
    vowels = 'aeiou'
    consonants = ''.join(ch for ch in charset if ch not in vowels and ch.isalpha())
    rng = random.Random()
    count = 0
    while count < max_count:
        parts = []
        for _ in range(length):
            r = rng.random()
            if r < 0.2:
                parts.append(rng.choice(RARER if RARER else charset))
            elif r < 0.5:
                parts.append(rng.choice(vowels))
            elif r < 0.8:
                parts.append(rng.choice(consonants or charset))
            else:
                parts.append(rng.choice('0123456789'))
        name = ''.join(parts)
        if not contains_profanity(name):
            yield name
            count += 1


def generate_aggressive(length, charset, max_count=10000):
    rng = random.Random()
    letters = ''.join(ch for ch in charset if ch.isalpha()) or charset
    digits = '0123456789'
    patterns = ['dL', 'Ld', 'LdL', 'LL', 'Ldd', 'dLL']
    count = 0
    while count < max_count // 2:
        pattern = rng.choice(patterns)
        parts = []
        for token in pattern:
            parts.append(rng.choice(digits) if token == 'd' else rng.choice(letters))
        name = ''.join(parts)[:length]
        if len(name) < length:
            name += rng.choice(letters + digits) * (length - len(name))
            name = name[:length]
        if not contains_profanity(name):
            yield name
            count += 1
    yield from generate_unique(length, charset, max_count - count)


def run_diagnostics():
    print_line('\nRunning diagnostics...', Fore.YELLOW, bold=True)
    test_names = ['zzzxxyy12345', 'abcdef_noop', 'test____123']
    for name in test_names:
        status = check_roblox(name)
        label = 'AVAILABLE' if status else 'TAKEN/ERROR'
        print_line(f"  {name} -> {label}", Fore.CYAN)
    print_line('Diagnostics complete.', Fore.YELLOW)


def get_generator(mode, length, charset):
    if mode == 's':
        return generate_sequential(length, charset)
    if mode == 'u':
        return generate_unique(length, charset)
    if mode == 'g':
        return generate_aggressive(length, charset)
    return generate_random(length, charset)


def scan_usernames(lengths, charset, mode, max_checks, delay, workers, proxies=None, on_hit=None, on_check=None, on_error=None, on_signup=None, stop_event=None, custom_names=None, use_save_checked=True, signup_password=None):
    checked_set = load_checked() if use_save_checked else set()
    total_checked = 0
    total_hits = 0
    total_errors = 0
    stop_after = max_checks if max_checks > 0 else float('inf')
    rate_limiter = RateLimiter(calls_per_sec=1.0 / max(delay, 0.05))
    cancelled = False
    proxy_list = proxies if isinstance(proxies, list) else ([proxies] if proxies else [])

    def candidate_key(length, name):
        return f"{length}:{name}"

    def check_candidate(name):
        if stop_event and stop_event.is_set():
            return name, False, False
        rate_limiter.wait()
        p = rotate_proxy(proxy_list) if len(proxy_list) > 1 else (proxy_list[0] if proxy_list else None)
        result = check_roblox(name, proxies=p)
        if result is None:
            return name, False, True
        return name, result, False

    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    try:
        if custom_names:
            for name in custom_names:
                if stop_event and stop_event.is_set():
                    cancelled = True
                    break
                key = candidate_key(0, name)
                if key in checked_set:
                    continue
                future = executor.submit(check_candidate, name)
                pending[future] = (0, name)

                done = {f for f in pending if f.done()}
                for f in done:
                    len_checked, orig_name = pending.pop(f)
                    _, found, is_err = f.result()
                    ck = candidate_key(len_checked, orig_name)
                    checked_set.add(ck)
                    save_checked(ck)
                    total_checked += 1
                    if is_err:
                        total_errors += 1
                        if on_error:
                            on_error(total_errors)
                    elif found:
                        total_hits += 1
                        save_hit(len_checked, orig_name)
                        if on_hit:
                            on_hit(len_checked, orig_name)
                        if signup_password:
                            add_pending_signup(len_checked, orig_name)
                            if on_signup:
                                on_signup(orig_name, True, 'pending')
                    if on_check:
                        on_check(total_checked, total_hits, total_errors)
        else:
            for length in lengths:
                print_line(f"\nScanning {length}-letter names on Roblox...", Fore.MAGENTA, bold=True)
                log.info(f"Starting scan for {length}L names, mode={mode}, charset={charset}")
                generator = get_generator(mode, length, charset)
                for name in generator:
                    if stop_event and stop_event.is_set():
                        cancelled = True
                        break
                    if total_checked >= stop_after:
                        break
                    key = candidate_key(length, name)
                    if key in checked_set:
                        continue
                    future = executor.submit(check_candidate, name)
                    pending[future] = (length, name)

                    done = {f for f in pending if f.done()}
                    for f in done:
                        len_checked, orig_name = pending.pop(f)
                        _, found, is_err = f.result()
                        ck = candidate_key(len_checked, orig_name)
                        checked_set.add(ck)
                        if use_save_checked: save_checked(ck)
                        total_checked += 1
                        if is_err:
                            total_errors += 1
                            if on_error:
                                on_error(total_errors)
                        elif found:
                            total_hits += 1
                            save_hit(len_checked, orig_name)
                            if on_hit:
                                on_hit(len_checked, orig_name)
                            if signup_password:
                                add_pending_signup(len_checked, orig_name)
                                if on_signup:
                                    on_signup(orig_name, True, 'pending')
                        if on_check:
                            on_check(total_checked, total_hits, total_errors)
                if cancelled or total_checked >= stop_after:
                    break

        if not cancelled:
            for f in as_completed(pending):
                len_checked, orig_name = pending.pop(f)
                _, found, is_err = f.result()
                ck = candidate_key(len_checked, orig_name)
                checked_set.add(ck)
                if use_save_checked: save_checked(ck)
                total_checked += 1
                if is_err:
                    total_errors += 1
                    if on_error:
                        on_error(total_errors)
                elif found:
                    total_hits += 1
                    save_hit(len_checked, orig_name)
                    if on_hit:
                        on_hit(len_checked, orig_name)
                    if signup_password:
                        add_pending_signup(len_checked, orig_name)
                        if on_signup:
                            on_signup(orig_name, True, 'pending')
                if on_check:
                    on_check(total_checked, total_hits, total_errors)

    except KeyboardInterrupt:
        cancelled = True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if cancelled:
            print_line(f"\nStopped by user. Checked {total_checked}, found {total_hits} hits.", Fore.RED)
            log.info(f"Interrupted. Checked {total_checked}, hits {total_hits}")
        else:
            print_line(f"\nFinished. Checked {total_checked} names, found {total_hits} hits.", Fore.GREEN, bold=True)
            print_line(f"Hits saved to {ROBLOX_HITS_FILE}", Fore.CYAN)
            log.info(f"Scan complete. Checked {total_checked}, hits {total_hits}")

    return total_checked, total_hits


def load_profiles():
    if PROFILES_FILE.exists():
        try:
            return json.loads(PROFILES_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_profiles(profiles):
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2), encoding='utf-8')


def cli_main():
    print_header()
    choice = prompt_choice('Choose scan length', ['4', '5', 'diag'], default='5')
    if choice == 'diag':
        run_diagnostics()
        return
    lengths = [int(choice)]
    charset_choice = prompt_choice('Charset', ['a', 'ad', 'd'], default='a')
    charset = build_charset(charset_choice)
    mode = prompt_choice('Mode', ['s', 'u', 'g', 'r'], default='u')
    max_checks = int(prompt_text('Max checks (0 for unlimited)', '0'))
    delay = float(prompt_text('Delay between requests in seconds', '0.3'))
    workers = int(prompt_text('Concurrent workers (1-20)', '8'))
    proxy = prompt_text('Proxy (http://user:pass@ip:port, or blank for none)', '')
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    scan_usernames(lengths, charset, mode, max_checks, delay, workers, proxies)


if __name__ == '__main__':
    cli_main()
