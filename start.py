"""
Waffles - Start both License Server + Web App
"""
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

BASE = Path(__file__).parent

print(f"\n  {'='*50}")
print(f"   Waffles Priv Service - Starting servers")
print(f"  {'='*50}")
print(f"   License Server  -> http://localhost:5001/admin")
print(f"   Web App         -> http://localhost:5000")
print(f"  {'='*50}\n")

license_proc = subprocess.Popen(
    [sys.executable, '-B', str(BASE / 'license_server.py')],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)

time.sleep(2)

web_proc = subprocess.Popen(
    [sys.executable, '-B', str(BASE / 'web_app.py')],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)

time.sleep(2)

webbrowser.open('http://localhost:5000')

try:
    license_proc.wait()
except KeyboardInterrupt:
    print("\nShutting down...")
    web_proc.terminate()
    license_proc.terminate()
