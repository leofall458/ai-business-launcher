"""Launch Bridge LLC - local admin desktop app.

Run with: python launch_bridge_admin.py

On startup this sets up everything the rest of the codebase's browser
automation (app/scc_llc_filer.py, app/ein_filer.py, app/check_scc_status.py)
already assumes is in place - a local Chrome reachable over CDP at
CDP_URL, with an SCC session saved to disk and kept alive - then shows a
dashboard for approving/filing orders pulled straight from Firestore.

Deliberately reuses the existing pipeline functions (run_scc_filing,
run_ein_filing, check_once) from app/main.py and app/check_scc_status.py
instead of re-implementing any filing/scraping logic here - this is a
GUI front end for the same automation the web admin dashboard already
triggers, not a separate code path.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    import pystray
except ImportError:
    pystray = None
    print("⚠️ pystray not installed - the system tray icon will be skipped (pip install pystray).")

from playwright.sync_api import sync_playwright

try:
    from app.main import ORDERS, run_scc_filing, run_ein_filing, EIN_ELIGIBLE_STATES, SCC_FILED_STATES
    from app.check_scc_status import check_once as check_scc_status_once
    from app.utils.irs_hours import is_irs_open, next_irs_open, format_eta
except Exception as e:
    print(f"❌ Could not import the Launch Bridge app package: {e}")
    print("   Run this from the repo root, with the same venv used by the rest of the project")
    print("   (gcloud application-default credentials are also required - see app/secrets.py).")
    sys.exit(1)

CDP_URL = "http://172.27.176.1:9222"
DEBUG_PORT = 9222
SCC_LOGIN_URL = "https://cis.scc.virginia.gov/Account/Login"
SCC_DASHBOARD_URL = "https://cis.scc.virginia.gov/OnlineDashboard/Index"
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scc_session.json")
APP_BASE_URL = "https://app.launchbridge.ai"
KEEP_ALIVE_INTERVAL_SECONDS = 180
STATUS_POLL_INTERVAL_SECONDS = 5
ORDERS_POLL_INTERVAL_SECONDS = 30
FIREWALL_RULE_NAME = "LaunchBridge Chrome Debug Port"

CHROME_CANDIDATES = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

# Dark theme to match the rest of Launch Bridge's UI (status.html/admin.html).
COLORS = {
    "bg": "#0a0e17",
    "panel": "#111827",
    "panel_border": "#1f2937",
    "text": "#e5e7eb",
    "text_dim": "#9ca3af",
    "blue": "#2563eb",
    "blue_hover": "#1d4ed8",
    "green": "#16a34a",
    "green_dim": "#14532d",
    "red": "#dc2626",
    "yellow": "#ca8a04",
    "indigo": "#4f46e5",
}


def log_line(text: str):
    print(f"[{time.strftime('%H:%M:%S')}] {text}")


# ============================================================
# Shared state (written by background threads, read by the Tk UI loop)
# ============================================================

class AppState:
    def __init__(self):
        self.chrome_ok = False
        self.scc_session_ok = None  # None = not checked yet
        self.pending_orders = []
        self.last_orders_error = None


state = AppState()
shutdown_event = threading.Event()
ui_queue = queue.Queue()  # (callable, args) pairs to run on the Tk main thread


def post_to_ui(fn, *args):
    ui_queue.put((fn, args))


# ============================================================
# Chrome debug port proxy + launch (Windows side, via WSL interop)
# ============================================================

def run_powershell(script: str, elevated: bool, timeout: int = 30) -> tuple[bool, str]:
    """Runs a PowerShell script via WSL's Windows interop. Elevated runs
    request UAC consent via Start-Process -Verb RunAs - the script itself
    is passed as a base64-encoded -EncodedCommand to avoid quoting hell
    across the WSL -> powershell.exe -> elevated-powershell.exe hops."""
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    if elevated:
        outer = (
            f"Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait "
            f"-ArgumentList '-NoProfile -EncodedCommand {encoded}'"
        )
        cmd = ["powershell.exe", "-NoProfile", "-Command", outer]
    else:
        cmd = ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


def proxy_already_configured() -> bool:
    ok, output = run_powershell("netsh interface portproxy show v4tov4", elevated=False, timeout=15)
    return ok and str(DEBUG_PORT) in output


def setup_chrome_debug_proxy() -> str:
    """Forwards Windows 0.0.0.0:9222 -> 127.0.0.1:9222 (where Chrome's
    --remote-debugging-port actually listens) and opens the firewall for
    it, so WSL-side Playwright can reach Chrome at CDP_URL. Requires admin
    - triggers a UAC prompt the first time; skipped on later launches once
    netsh already shows the entry."""
    if proxy_already_configured():
        return "✅ Chrome debug port proxy already configured."

    script = f"""
$port = {DEBUG_PORT}
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$port | Out-Null
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$port connectaddress=127.0.0.1 connectport=$port
if (-not (Get-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -DisplayName "{FIREWALL_RULE_NAME}" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}}
"""
    ok, output = run_powershell(script, elevated=True, timeout=60)
    if ok:
        return "✅ Chrome debug port proxy configured (admin approval may have been required)."
    return f"⚠️ Could not set up the Chrome debug port proxy - you may need to do this manually. ({output[:200]})"


def launch_chrome() -> str:
    chrome_exe = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not chrome_exe:
        return "⚠️ Could not find chrome.exe in Program Files - launch Chrome manually with --remote-debugging-port=9222."

    profile_dir = r"C:\LaunchBridgeChromeDebugProfile"
    try:
        subprocess.Popen([
            chrome_exe,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile_dir}",
            SCC_LOGIN_URL,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "✅ Chrome launched with remote debugging enabled."
    except Exception as e:
        return f"⚠️ Could not launch Chrome: {e}"


def is_chrome_debug_reachable() -> bool:
    try:
        resp = requests.get(f"{CDP_URL}/json/version", timeout=2)
        return resp.ok
    except Exception:
        return False


def chrome_status_poller():
    while not shutdown_event.is_set():
        state.chrome_ok = is_chrome_debug_reachable()
        shutdown_event.wait(STATUS_POLL_INTERVAL_SECONDS)


# ============================================================
# SCC session: save + keep-alive (same logic as save_scc_session.py /
# keep_scc_alive.py, minus the interactive input() prompts - by the time
# these run, the user has already clicked Continue in the GUI)
# ============================================================

def save_scc_session() -> tuple[bool, str]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            cookies = context.cookies()
            with open(SESSION_FILE, "w") as f:
                json.dump(cookies, f)
        if not cookies:
            return False, "⚠️ Connected to Chrome but found no cookies - make sure you're logged into SCC."
        return True, f"✅ Saved {len(cookies)} SCC session cookie(s)."
    except Exception as e:
        return False, f"⚠️ Could not save SCC session: {e}"


def ping_scc_session() -> bool:
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        with open(SESSION_FILE, "r") as f:
            cookies = json.load(f)
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
        response = session.get(SCC_DASHBOARD_URL, timeout=10)
        return "Hi," in response.text
    except Exception:
        return False


def keep_scc_alive_loop():
    while not shutdown_event.is_set():
        alive = ping_scc_session()
        state.scc_session_ok = alive
        if not alive:
            log_line("❌ SCC session expired or not yet saved.")
        shutdown_event.wait(KEEP_ALIVE_INTERVAL_SECONDS)


# ============================================================
# Firestore: pending orders
# ============================================================

def fmt_date(ts) -> str:
    if not ts:
        return "-"
    try:
        return ts.strftime("%b %-d, %Y %I:%M %p").replace(" 0", " ")
    except Exception:
        return str(ts)


def fetch_pending_orders() -> list:
    """Every order not yet fully complete - fetch-then-filter in Python,
    same convention used throughout app/main.py, since order volume here
    is small enough that a full scan is simpler than a composite index."""
    orders = []
    for doc in ORDERS.stream():
        order = doc.to_dict() or {}
        if order.get("state") == "complete":
            continue
        order["id"] = doc.id
        orders.append(order)
    orders.sort(key=lambda o: o.get("created_at") or 0, reverse=True)
    return orders


def orders_poller():
    while not shutdown_event.is_set():
        try:
            orders = fetch_pending_orders()
            state.pending_orders = orders
            state.last_orders_error = None
        except Exception as e:
            state.last_orders_error = str(e)
        # No need to signal the UI - DashboardApp._redraw_orders_tick polls
        # state.pending_orders on its own timer, on the main thread.
        shutdown_event.wait(ORDERS_POLL_INTERVAL_SECONDS)


# ============================================================
# System tray
# ============================================================

def build_tray_image(pending_count: int) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=14, fill=(37, 99, 235, 255))
    try:
        main_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        badge_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
    except Exception:
        main_font = ImageFont.load_default()
        badge_font = main_font
    draw.text((size / 2, size / 2), "LB", fill="white", anchor="mm", font=main_font)
    if pending_count > 0:
        badge_text = str(pending_count) if pending_count < 100 else "99+"
        draw.ellipse([size - 28, -2, size + 2, 28], fill=(220, 38, 38, 255))
        draw.text((size - 13, 13), badge_text, fill="white", anchor="mm", font=badge_font)
    return img


class TrayApp:
    def __init__(self, on_open, on_check_orders, on_quit):
        self.icon = None
        self._last_count = -1
        if pystray is None:
            return
        self.icon = pystray.Icon(
            "launch_bridge_admin",
            build_tray_image(0),
            "Launch Bridge Admin",
            menu=pystray.Menu(
                pystray.MenuItem("Open Dashboard", lambda: on_open()),
                pystray.MenuItem("Check Orders", lambda: on_check_orders()),
                pystray.MenuItem("Quit", lambda: on_quit()),
            ),
        )

    def start(self):
        if self.icon:
            threading.Thread(target=self.icon.run, daemon=True).start()

    def update_count(self, count: int):
        if self.icon and count != self._last_count:
            self._last_count = count
            self.icon.icon = build_tray_image(count)
            self.icon.title = f"Launch Bridge Admin - {count} pending order{'s' if count != 1 else ''}"

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


# ============================================================
# Startup / splash window
# ============================================================

class SplashWindow(tk.Toplevel):
    def __init__(self, root, on_done):
        super().__init__(root)
        self.on_done = on_done
        self.title("Launch Bridge Admin - Starting Up")
        self.geometry("560x380")
        self.configure(bg=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # block closing mid-startup

        tk.Label(self, text="🚀 Launch Bridge Admin", font=("Segoe UI", 16, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(pady=(20, 4))
        tk.Label(self, text="Setting up Chrome + SCC session...", font=("Segoe UI", 10),
                 bg=COLORS["bg"], fg=COLORS["text_dim"]).pack(pady=(0, 12))

        self.log_box = tk.Text(self, height=14, bg=COLORS["panel"], fg=COLORS["text"],
                                insertbackground=COLORS["text"], relief="flat", padx=10, pady=10)
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")

        self.login_frame = tk.Frame(self, bg=COLORS["bg"])
        self.continue_btn = None

    def log(self, text: str):
        """Safe to call from any thread - startup runs on a background
        thread (so the splash window stays responsive while we shell out
        to PowerShell/Chrome), but Tk/Tcl may only be touched from the
        main thread, and that includes scheduling calls like .after()
        itself, not just widget mutation - calling .after() cross-thread
        is what was segfaulting/hanging this app under X11. post_to_ui
        only ever touches a plain thread-safe queue.Queue; the actual Tk
        call happens later, on the main thread, via pump_ui_queue."""
        log_line(text)
        post_to_ui(self._log_on_main_thread, text)

    def _log_on_main_thread(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def show_login_prompt(self, on_continue):
        self.log("👉 Please log into SCC in the Chrome window that just opened, then click Continue.")
        self.login_frame.pack(pady=(0, 16))
        self.continue_btn = tk.Button(
            self.login_frame, text="Continue →", font=("Segoe UI", 11, "bold"),
            bg=COLORS["green"], fg="white", activebackground=COLORS["green_dim"],
            relief="flat", padx=24, pady=8, command=on_continue,
        )
        self.continue_btn.pack()

    def hide_login_prompt(self):
        self.login_frame.pack_forget()


# ============================================================
# Main dashboard
# ============================================================

class DashboardApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.tray = None
        self.order_rows_frame = None
        self.checking_scc = False
        self._build_ui()
        self._redraw_orders_tick()
        self._refresh_status_indicators()

    # ---- layout ----

    def _build_ui(self):
        self.root.title("Launch Bridge Admin")
        self.root.geometry("980x720")
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=16, pady=(16, 8))
        tk.Label(header, text="Launch Bridge Admin", font=("Segoe UI", 18, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(side="left")

        btns = tk.Frame(header, bg=COLORS["bg"])
        btns.pack(side="right")
        self._make_button(btns, "Refresh Orders", COLORS["blue"], self.on_refresh_orders).pack(side="left", padx=4)
        self.check_scc_btn = self._make_button(btns, "Check SCC Status", COLORS["indigo"], self.on_check_scc_status)
        self.check_scc_btn.pack(side="left", padx=4)
        self._make_button(btns, "Open Admin Dashboard", COLORS["green"], self.on_open_admin).pack(side="left", padx=4)

        # ---- status panel ----
        status_panel = tk.Frame(self.root, bg=COLORS["panel"], highlightbackground=COLORS["panel_border"],
                                 highlightthickness=1)
        status_panel.pack(fill="x", padx=16, pady=8)
        self.chrome_status_label = self._status_indicator(status_panel, "Chrome debug port")
        self.scc_status_label = self._status_indicator(status_panel, "SCC session")
        self.irs_status_label = self._status_indicator(status_panel, "IRS hours")

        # ---- orders panel ----
        orders_header = tk.Frame(self.root, bg=COLORS["bg"])
        orders_header.pack(fill="x", padx=16, pady=(12, 4))
        self.orders_title_label = tk.Label(orders_header, text="Pending Orders", font=("Segoe UI", 13, "bold"),
                                            bg=COLORS["bg"], fg=COLORS["text"])
        self.orders_title_label.pack(side="left")

        canvas_frame = tk.Frame(self.root, bg=COLORS["bg"])
        canvas_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.canvas = tk.Canvas(canvas_frame, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.order_rows_frame = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.order_rows_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.order_rows_frame, anchor="nw", width=930)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._render_empty_orders("Loading orders...")

    def _make_button(self, parent, text, color, command):
        return tk.Button(parent, text=text, font=("Segoe UI", 9, "bold"), bg=color, fg="white",
                          activebackground=color, relief="flat", padx=10, pady=6, command=command, cursor="hand2")

    def _status_indicator(self, parent, name) -> tk.Label:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(side="left", padx=20, pady=14)
        tk.Label(row, text=name, font=("Segoe UI", 9), bg=COLORS["panel"], fg=COLORS["text_dim"]).pack(anchor="w")
        value_label = tk.Label(row, text="checking...", font=("Segoe UI", 11, "bold"),
                                bg=COLORS["panel"], fg=COLORS["text"])
        value_label.pack(anchor="w")
        return value_label

    def _render_empty_orders(self, message):
        for child in self.order_rows_frame.winfo_children():
            child.destroy()
        tk.Label(self.order_rows_frame, text=message, font=("Segoe UI", 10), bg=COLORS["bg"],
                 fg=COLORS["text_dim"]).pack(pady=30)

    # ---- order rows ----

    def render_orders(self, orders: list):
        for child in self.order_rows_frame.winfo_children():
            child.destroy()

        self.orders_title_label.configure(text=f"Pending Orders ({len(orders)})")
        if self.tray:
            self.tray.update_count(len(orders))

        if not orders:
            self._render_empty_orders("No pending orders.")
            return

        irs_open = is_irs_open()
        for order in orders:
            self._render_order_row(order, irs_open)

    def _render_order_row(self, order: dict, irs_open: bool):
        order_id = order.get("id")
        card = tk.Frame(self.order_rows_frame, bg=COLORS["panel"], highlightbackground=COLORS["panel_border"],
                         highlightthickness=1)
        card.pack(fill="x", pady=5, padx=2)

        top = tk.Frame(card, bg=COLORS["panel"])
        top.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(top, text=order.get("business_name") or "(no business name)", font=("Segoe UI", 11, "bold"),
                 bg=COLORS["panel"], fg=COLORS["text"]).pack(side="left")
        tk.Label(top, text=order.get("state", "draft").replace("_", " ").title(), font=("Segoe UI", 9, "bold"),
                 bg=COLORS["blue"], fg="white", padx=8, pady=2).pack(side="right")

        details = tk.Frame(card, bg=COLORS["panel"])
        details.pack(fill="x", padx=14, pady=(0, 8))
        detail_text = f"{order.get('full_name', '-')} · {fmt_date(order.get('created_at'))}"
        tk.Label(details, text=detail_text, font=("Segoe UI", 9), bg=COLORS["panel"],
                 fg=COLORS["text_dim"]).pack(side="left")

        actions = tk.Frame(card, bg=COLORS["panel"])
        actions.pack(fill="x", padx=14, pady=(0, 10))

        state_val = order.get("state", "draft")
        show_approve_scc = (not order.get("skip_llc_formation")) and state_val not in SCC_FILED_STATES
        show_file_ein = (not order.get("skip_ein")) and state_val in EIN_ELIGIBLE_STATES and irs_open

        if show_approve_scc:
            self._make_button(actions, "Approve + File SCC", COLORS["blue"],
                               lambda oid=order_id, name=order.get("business_name", ""): self.on_approve_scc(oid, name)
                               ).pack(side="left", padx=(0, 6))
        if show_file_ein:
            self._make_button(actions, "File EIN", COLORS["indigo"],
                               lambda oid=order_id, name=order.get("business_name", ""): self.on_file_ein(oid, name)
                               ).pack(side="left", padx=(0, 6))
        self._make_button(actions, "View Status Page", COLORS["panel_border"],
                           lambda oid=order_id: webbrowser.open(f"{APP_BASE_URL}/status/{oid}")
                           ).pack(side="left", padx=(0, 6))
        self._make_button(actions, "Open Admin", COLORS["panel_border"], self.on_open_admin).pack(side="left")

    # ---- status indicator refresh (Tk thread, cheap/non-blocking only) ----

    def _refresh_status_indicators(self):
        self._set_indicator(self.chrome_status_label, state.chrome_ok, "Connected", "Not reachable")

        if state.scc_session_ok is None:
            self.scc_status_label.configure(text="⏳ Checking...", fg=COLORS["text_dim"])
        else:
            self._set_indicator(self.scc_status_label, state.scc_session_ok, "Active", "Expired")

        if is_irs_open():
            self.irs_status_label.configure(text="✅ Open", fg=COLORS["green"])
        else:
            eta = format_eta(next_irs_open())
            self.irs_status_label.configure(text=f"❌ Closed · opens {eta}", fg=COLORS["red"])

        if not self.root.winfo_exists():
            return
        self.root.after(2000, self._refresh_status_indicators)

    def _set_indicator(self, label, ok, ok_text, bad_text):
        if ok:
            label.configure(text=f"✅ {ok_text}", fg=COLORS["green"])
        else:
            label.configure(text=f"❌ {bad_text}", fg=COLORS["red"])

    # ---- periodic redraw (self-rescheduling from the main thread only -
    # safe, since it just reads plain Python attributes that worker
    # threads write to; it never has another thread call into Tk) ----

    def _redraw_orders_tick(self):
        self.render_orders(state.pending_orders)
        if self.root.winfo_exists():
            self.root.after(1000, self._redraw_orders_tick)

    # ---- button handlers ----

    def on_refresh_orders(self):
        threading.Thread(target=self._refresh_orders_bg, daemon=True).start()

    def _refresh_orders_bg(self):
        try:
            state.pending_orders = fetch_pending_orders()
            state.last_orders_error = None
        except Exception as e:
            state.last_orders_error = str(e)
            post_to_ui(lambda: messagebox.showerror("Refresh failed", f"Could not load orders: {e}"))

    def on_open_admin(self):
        webbrowser.open(f"{APP_BASE_URL}/admin")

    def on_check_scc_status(self):
        if self.checking_scc:
            return
        self.checking_scc = True
        self.check_scc_btn.configure(text="Checking...", state="disabled")
        threading.Thread(target=self._check_scc_status_bg, daemon=True).start()

    def _check_scc_status_bg(self):
        try:
            check_scc_status_once()
        except Exception as e:
            post_to_ui(lambda: messagebox.showerror("SCC check failed", str(e)))
        finally:
            self.checking_scc = False
            post_to_ui(lambda: self.check_scc_btn.configure(text="Check SCC Status", state="normal"))
            self.on_refresh_orders()

    def on_approve_scc(self, order_id: str, business_name: str):
        if not messagebox.askyesno(
            "Confirm SCC Filing",
            f"This will file the Articles of Organization for '{business_name}' with the Virginia SCC "
            f"and charge $100 to the on-file card.\n\nContinue?",
        ):
            return
        threading.Thread(target=self._run_filing, args=(run_scc_filing, order_id, "SCC filing"), daemon=True).start()

    def on_file_ein(self, order_id: str, business_name: str):
        if not messagebox.askyesno(
            "Confirm EIN Filing",
            f"This will automatically submit the EIN application for '{business_name}' to the IRS.\n\n"
            f"This issues a real, permanent EIN immediately and cannot be undone.\n\nContinue?",
        ):
            return
        threading.Thread(target=self._run_filing, args=(run_ein_filing, order_id, "EIN filing"), daemon=True).start()

    def _run_filing(self, fn, order_id, label):
        try:
            fn(order_id)
        except Exception as e:
            post_to_ui(lambda: messagebox.showerror(f"{label} failed", str(e)))
        self.on_refresh_orders()

    # ---- tray integration ----

    def attach_tray(self, tray: TrayApp):
        self.tray = tray

    def _hide_to_tray(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()


# ============================================================
# Startup orchestration
# ============================================================

def run_startup_sequence(root: tk.Tk, splash: SplashWindow, on_complete):
    def step1_3():
        splash.log("🔧 Setting up Chrome debug port proxy...")
        splash.log(setup_chrome_debug_proxy())
        splash.log("🌐 Launching Chrome...")
        splash.log(launch_chrome())
        time.sleep(2)
        post_to_ui(splash.show_login_prompt, on_continue)

    def on_continue():
        splash.hide_login_prompt()
        splash.log("⏳ Saving SCC session from Chrome...")
        threading.Thread(target=step5_7, daemon=True).start()

    def step5_7():
        ok, msg = save_scc_session()
        splash.log(msg)
        state.scc_session_ok = ok if ok else None
        splash.log("🔄 Starting SCC keep-alive...")
        threading.Thread(target=keep_scc_alive_loop, daemon=True).start()
        splash.log("✅ Startup complete - opening dashboard.")
        time.sleep(1)
        post_to_ui(on_complete)

    threading.Thread(target=step1_3, daemon=True).start()


def pump_ui_queue(root: tk.Tk):
    """The only thing allowed to touch Tk from outside the main thread is
    this function's own scheduling of itself, which always happens from
    the main thread (either here, the first time, or via the .after()
    call below, which then runs on the main thread too). Every other
    piece of code - startup, the keep-alive loop, order/status pollers,
    the tray menu - hands work to the main thread exclusively through
    post_to_ui()/ui_queue, never by calling a Tk method directly."""
    try:
        while True:
            fn, args = ui_queue.get_nowait()
            fn(*args)
    except queue.Empty:
        pass
    if root.winfo_exists():
        root.after(100, pump_ui_queue, root)


def main():
    root = tk.Tk()
    root.withdraw()  # hidden until startup finishes
    root.after(100, pump_ui_queue, root)

    splash = SplashWindow(root, on_done=None)

    def on_startup_complete():
        splash.destroy()
        dashboard = DashboardApp(root)
        root.deiconify()

        threading.Thread(target=chrome_status_poller, daemon=True).start()
        threading.Thread(target=orders_poller, daemon=True).start()

        def on_quit():
            shutdown_event.set()
            if dashboard.tray:
                dashboard.tray.stop()
            post_to_ui(root.destroy)

        tray = TrayApp(on_open=dashboard.show, on_check_orders=dashboard.on_refresh_orders, on_quit=on_quit)
        dashboard.attach_tray(tray)
        tray.start()
        root.protocol("WM_DELETE_WINDOW", dashboard._hide_to_tray)

    run_startup_sequence(root, splash, on_startup_complete)
    root.mainloop()


if __name__ == "__main__":
    main()
