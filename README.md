# PANTOO Roblox Username Scanner v2

Scans for available 4/5-letter Roblox usernames. Comes with both CLI and GUI.

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

## GUI Mode (recommended)

```bash
python pantoo_gui.py
```
Or double-click `run_gui.bat`.

## CLI Mode

```bash
python pantoo_checker.py
```
Or double-click `run_cli.bat`.

## Features

- GUI with real-time stats and live log output
- 4 scan modes: sequential, pronounceable unique, aggressive mix, purely random
- Letters, digits, or alphanumeric charsets
- Multi-threaded scanning
- Proxy support (HTTP/HTTPS)
- Smart rate limiting
- Resumable (skips already-checked names)
- Diagnostics mode to test API connectivity
- Hit tracking with dedicated hits viewer
