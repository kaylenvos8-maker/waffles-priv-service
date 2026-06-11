import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import queue
import time
from datetime import datetime

from pantoo_checker import (
    scan_usernames, build_charset, run_diagnostics,
    ROBLOX_HITS_FILE, CHECKED_FILE, LOG_FILE, BASE_DIR
)


class PantooGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Pantoo Roblox Username Checker v2")
        self.root.geometry("800x650")
        self.root.minsize(700, 550)

        self.stop_event = threading.Event()
        self.scan_thread = None
        self.msg_queue = queue.Queue()

        self.setup_styles()
        self.create_widgets()
        self.process_queue()

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
        style.configure('Header.TLabel', font=('Segoe UI', 16, 'bold'))
        style.configure('Stats.TLabel', font=('Segoe UI', 11))
        style.configure('Hit.TLabel', foreground='green', font=('Segoe UI', 10, 'bold'))

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(main_frame, text="PANTOO Roblox Username Checker", style='Header.TLabel')
        header.pack(pady=(0, 10))

        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        # --- Scan Tab ---
        scan_tab = ttk.Frame(notebook, padding="10")
        notebook.add(scan_tab, text="Scanner")

        # Config frame
        cfg_frame = ttk.LabelFrame(scan_tab, text="Scan Configuration", padding="10")
        cfg_frame.pack(fill=tk.X, pady=(0, 10))

        row1 = ttk.Frame(cfg_frame)
        row1.pack(fill=tk.X, pady=3)
        ttk.Label(row1, text="Length:", width=12).pack(side=tk.LEFT)
        self.length_var = tk.StringVar(value="5")
        ttk.Combobox(row1, textvariable=self.length_var, values=["4", "5", "4+5"], width=10, state="readonly").pack(side=tk.LEFT, padx=5)
        ttk.Label(row1, text="Charset:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.charset_var = tk.StringVar(value="a - letters only")
        ttk.Combobox(row1, textvariable=self.charset_var, values=["a - letters only", "d - digits only", "ad - alphanumeric"], width=20, state="readonly").pack(side=tk.LEFT, padx=5)

        row2 = ttk.Frame(cfg_frame)
        row2.pack(fill=tk.X, pady=3)
        ttk.Label(row2, text="Mode:", width=12).pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="u - unique (pronounceable)")
        ttk.Combobox(row2, textvariable=self.mode_var, values=[
            "u - unique (pronounceable)", "g - aggressive mix",
            "s - sequential", "r - purely random"
        ], width=30, state="readonly").pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="Workers:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.workers_var = tk.StringVar(value="8")
        ttk.Spinbox(row2, from_=1, to=30, textvariable=self.workers_var, width=5).pack(side=tk.LEFT, padx=5)

        row3 = ttk.Frame(cfg_frame)
        row3.pack(fill=tk.X, pady=3)
        ttk.Label(row3, text="Delay (s):", width=12).pack(side=tk.LEFT)
        self.delay_var = tk.StringVar(value="0.3")
        ttk.Spinbox(row3, from_=0.05, to=5.0, increment=0.05, textvariable=self.delay_var, width=7).pack(side=tk.LEFT, padx=5)
        ttk.Label(row3, text="Max checks:", width=10).pack(side=tk.LEFT, padx=(20, 0))
        self.max_var = tk.StringVar(value="0")
        ttk.Spinbox(row3, from_=0, to=1000000, textvariable=self.max_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(row3, text="(0 = unlimited)", style='Stats.TLabel').pack(side=tk.LEFT, padx=3)

        row4 = ttk.Frame(cfg_frame)
        row4.pack(fill=tk.X, pady=3)
        ttk.Label(row4, text="Proxy:", width=12).pack(side=tk.LEFT)
        self.proxy_var = tk.StringVar(value="")
        proxy_entry = ttk.Entry(row4, textvariable=self.proxy_var, width=50)
        proxy_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Label(row4, text="(blank = none)", style='Stats.TLabel').pack(side=tk.LEFT)

        # Control buttons
        btn_frame = ttk.Frame(scan_tab)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = ttk.Button(btn_frame, text="Start Scan", command=self.start_scan, width=15)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_scan, width=10, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.diag_btn = ttk.Button(btn_frame, text="Run Diagnostics", command=self.run_diag, width=15)
        self.diag_btn.pack(side=tk.LEFT)

        # Stats
        stats_frame = ttk.LabelFrame(scan_tab, text="Statistics", padding="10")
        stats_frame.pack(fill=tk.X, pady=(0, 10))

        stats_row = ttk.Frame(stats_frame)
        stats_row.pack(fill=tk.X)
        ttk.Label(stats_row, text="Checked:", style='Stats.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        self.checked_label = ttk.Label(stats_row, text="0", style='Stats.TLabel')
        self.checked_label.pack(side=tk.LEFT, padx=(0, 30))

        ttk.Label(stats_row, text="Hits:", style='Stats.TLabel', foreground='green').pack(side=tk.LEFT, padx=(0, 5))
        self.hits_label = ttk.Label(stats_row, text="0", style='Stats.TLabel', foreground='green')
        self.hits_label.pack(side=tk.LEFT, padx=(0, 30))

        ttk.Label(stats_row, text="Status:", style='Stats.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        self.status_label = ttk.Label(stats_row, text="Idle", style='Stats.TLabel')
        self.status_label.pack(side=tk.LEFT)

        # Log
        log_frame = ttk.LabelFrame(scan_tab, text="Output Log", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, font=('Consolas', 9),
                                                    bg='#1e1e1e', fg='#d4d4d4', insertbackground='white')
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # --- Hits Tab ---
        hits_tab = ttk.Frame(notebook, padding="10")
        notebook.add(hits_tab, text="Hits")

        hits_top = ttk.Frame(hits_tab)
        hits_top.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(hits_top, text="Refresh Hits", command=self.refresh_hits).pack(side=tk.LEFT)
        ttk.Button(hits_top, text="Clear Hits File", command=self.clear_hits).pack(side=tk.LEFT, padx=(10, 0))

        self.hits_text = scrolledtext.ScrolledText(hits_tab, height=20, font=('Consolas', 10))
        self.hits_text.pack(fill=tk.BOTH, expand=True)
        self.refresh_hits()

    def log(self, message, color=None):
        self.msg_queue.put(('log', message, color))

    def update_stats(self, checked, hits):
        self.msg_queue.put(('stats', checked, hits))

    def set_status(self, status):
        self.msg_queue.put(('status', status))

    def hit_notification(self, length, name):
        self.msg_queue.put(('hit', length, name))

    def process_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if msg[0] == 'log':
                    _, text, color = msg
                    tag = None
                    if color == 'green':
                        tag = 'hit'
                    elif color == 'red':
                        tag = 'error'
                    elif color == 'yellow':
                        tag = 'warn'
                    elif color == 'cyan':
                        tag = 'info'

                    if not self.log_text.tag_names():
                        self.log_text.tag_configure('hit', foreground='#4ec94e')
                        self.log_text.tag_configure('error', foreground='#e06060')
                        self.log_text.tag_configure('warn', foreground='#e0c060')
                        self.log_text.tag_configure('info', foreground='#60c0e0')

                    timestamp = datetime.now().strftime('%H:%M:%S')
                    self.log_text.insert(tk.END, f"[{timestamp}] {text}\n", tag if tag else ())
                    self.log_text.see(tk.END)

                elif msg[0] == 'stats':
                    _, checked, hits = msg
                    self.checked_label.config(text=str(checked))
                    self.hits_label.config(text=str(hits))

                elif msg[0] == 'status':
                    _, status = msg
                    self.status_label.config(text=status)

                elif msg[0] == 'hit':
                    _, length, name = msg
                    self.log(f"[HIT] {name} ({length}L)", 'green')
                    self.refresh_hits()

        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def parse_config(self):
        length_raw = self.length_var.get()
        if length_raw == "4+5":
            lengths = [4, 5]
        else:
            lengths = [int(length_raw)]

        charset_raw = self.charset_var.get()[0]
        if charset_raw == 'a':
            charset = 'a'
        elif charset_raw == 'd':
            charset = 'd'
        else:
            charset = 'ad'
        charset = build_charset(charset)

        mode_raw = self.mode_var.get()[0]
        mode = mode_raw.lower()

        workers = int(self.workers_var.get())
        delay = float(self.delay_var.get())
        max_checks = int(self.max_var.get())

        proxy = self.proxy_var.get().strip()
        proxies = {'http': proxy, 'https': proxy} if proxy else None

        return lengths, charset, mode, max_checks, delay, workers, proxies

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return

        self.stop_event.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.diag_btn.config(state=tk.DISABLED)
        self.set_status("Scanning...")

        lengths, charset, mode, max_checks, delay, workers, proxies = self.parse_config()

        lengths_str = '+'.join(str(l) for l in lengths)
        self.log(f"Starting scan: {lengths_str}L, mode={mode}, workers={workers}, delay={delay}s", 'info')
        if proxies:
            self.log(f"Using proxy: {proxies['http'][:30]}...", 'info')
        self.log(f"Hits file: {ROBLOX_HITS_FILE}", 'info')
        self.log(f"Checked file: {CHECKED_FILE}", 'info')

        def scan_thread():
            try:
                scan_usernames(
                    lengths, charset, mode, max_checks, delay, workers, proxies,
                    on_hit=lambda l, n: self.hit_notification(l, n),
                    on_check=lambda c, h: self.update_stats(c, h),
                    stop_event=self.stop_event
                )
            except Exception as e:
                self.log(f"Error: {e}", 'red')
            finally:
                self.msg_queue.put(('scan_done',))

        self.scan_thread = threading.Thread(target=scan_thread, daemon=True)
        self.scan_thread.start()

        self.wait_for_scan()

    def wait_for_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self.root.after(200, self.wait_for_scan)
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.diag_btn.config(state=tk.NORMAL)
            self.set_status("Done")
            self.log("Scan finished.", 'info')

    def stop_scan(self):
        self.stop_event.set()
        self.set_status("Stopping...")
        self.log("Stopping scan...", 'warn')

    def run_diag(self):
        def diag_thread():
            self.log("Running diagnostics...", 'info')
            try:
                from pantoo_checker import check_roblox
                test_names = ['zzzxxyy12345', 'abcdef_noop', 'test____123']
                for name in test_names:
                    status = check_roblox(name)
                    label = 'AVAILABLE' if status else 'TAKEN/ERROR'
                    self.log(f"  {name} -> {label}", 'green' if status else 'red')
            except Exception as e:
                self.log(f"Diagnostics error: {e}", 'red')
            self.log("Diagnostics complete.", 'info')

        threading.Thread(target=diag_thread, daemon=True).start()

    def refresh_hits(self):
        self.hits_text.delete(1.0, tk.END)
        if ROBLOX_HITS_FILE.exists():
            with open(ROBLOX_HITS_FILE, 'r') as f:
                content = f.read()
                if content.strip():
                    self.hits_text.insert(tk.END, content)
                else:
                    self.hits_text.insert(tk.END, "(no hits yet)\n")
        else:
            self.hits_text.insert(tk.END, "(no hits file)\n")

    def clear_hits(self):
        if ROBLOX_HITS_FILE.exists():
            ROBLOX_HITS_FILE.write_text('')
            self.refresh_hits()
            self.log("Hits file cleared.", 'warn')


if __name__ == '__main__':
    root = tk.Tk()
    app = PantooGUI(root)
    root.mainloop()
