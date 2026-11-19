# gui.py - UI of the desktop client (tkinter = part of Python, no dependencies)
# -------------------------------------------------------------------------------------------
#   Login form    server, user, password, directory (SSO), 2FA code, device name
#   Main window   open dashboard, relay drive, remote shell, remote screen (RDP/VNC),
#                 autostart, sign out
# Network runs in background threads; results come back via root.after.

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__, autostart, browser, features, updater
from .api import Api, ApiError, default_device_name
from .store import Store, default_data_dir

ACCENT = "#2dd4bf"
BG = "#0f172a"
PANEL = "#1e293b"
FG = "#e2e8f0"
SUB = "#94a3b8"


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (base / name, base / "resources" / name, Path(__file__).resolve().parent / "resources" / name):
        if p.exists():
            return p
    return base / name


class ClientApp:
    def __init__(self, minimized: bool = False):
        self.store = Store(default_data_dir())
        # fully automatic setup/removal of RMM WireGuard tunnels (vpn.py)
        from . import vpn as _vpn
        self.vpn = _vpn.VpnSync(self.store, default_data_dir(), lambda: self.api,
                                on_status=lambda text: self.root.after(0, self._vpn_status, text),
                                refresh=self._refresh_session)
        self.api: Api | None = None
        self.root = tk.Tk()
        self.root.title("BYTERA RMM")
        self.root.geometry("440x740")
        self.root.minsize(400, 560)
        self.root.configure(bg=BG)
        self._icon()
        self._style()
        # Ctrl+A = select all (Tk default on Linux would be "start of line")
        for cls in ("TEntry", "Entry"):
            self.root.bind_class(cls, "<Control-a>", self._select_all)
            self.root.bind_class(cls, "<Control-A>", self._select_all)
        self.frame = None
        # loading bar at the bottom: "… [Stop]" during longer actions
        self.busy_bar = tk.Frame(self.root, bg=PANEL)
        # pack the button FIRST so it never slides out of the window with long text
        self.busy_btn = ttk.Button(self.busy_bar, text="Stop", style="Danger.TButton")
        self.busy_btn.pack(side="right", padx=8, pady=4)
        self.busy_label = tk.Label(self.busy_bar, text="", bg=PANEL, fg=FG, anchor="w", font=("Segoe UI", 9),
                                   width=1, wraplength=300, justify="left")
        self.busy_label.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=6)
        self._busy_token = None
        if minimized:
            self.root.iconify()
        self.show_busy("Connecting …")
        self.root.after(50, self.auto_login)

    # ------------------------------------------------------------- Appearance
    def _icon(self):
        try:
            if os.name == "nt" and resource("rmmclient.ico").exists():
                self.root.iconbitmap(default=str(resource("rmmclient.ico")))
            elif resource("rmmclient-64.png").exists():
                self._img = tk.PhotoImage(file=str(resource("rmmclient-64.png")))
                self.root.iconphoto(True, self._img)
        except Exception:
            pass

    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Sub.TLabel", foreground=SUB, font=("Segoe UI", 9))
        st.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, bordercolor="#334155", padding=6)
        st.configure("TCombobox", fieldbackground=PANEL, foreground=FG, background=PANEL, padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TRadiobutton", background=[("active", BG)], foreground=[("disabled", "#475569")])
        st.configure("TButton", background=PANEL, foreground=FG, bordercolor="#334155", padding=(12, 9), relief="flat")
        st.map("TButton", background=[("active", "#334155"), ("disabled", BG)], foreground=[("disabled", "#475569")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0c1420", font=("Segoe UI", 10, "bold"))
        st.map("Accent.TButton", background=[("active", "#5eead4"), ("disabled", "#334155")])
        st.configure("Danger.TButton", foreground="#f87171")

    @staticmethod
    def _select_all(event):
        event.widget.select_range(0, "end")
        event.widget.icursor("end")
        return "break"

    def _new_frame(self):
        # Scrollable content: if the content does not fit the window, a scrollbar
        # appears on the right (previously everything was cut off at the bottom). self.frame
        # stays the INNER content frame, all callers work unchanged.
        if getattr(self, "_scroll_host", None) is not None:
            try: self._scroll_host.destroy()
            except Exception: pass
        host = ttk.Frame(self.root)
        host.pack(fill="both", expand=True)
        self._scroll_host = host
        canvas = tk.Canvas(host, highlightthickness=0, bd=0, bg=BG)
        vbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=22)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())   # content to window width
            # only show the scrollbar when needed.
            need = inner.winfo_reqheight() > canvas.winfo_height()
            if need and not vbar.winfo_ismapped(): vbar.pack(side="right", fill="y")
            elif not need and vbar.winfo_ismapped(): vbar.pack_forget()
        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        self.root.after_idle(_resize)      # re-evaluate reliably after the first layout

        # mouse wheel / touchpad (Windows/macOS: <MouseWheel>, Linux: Button-4/5)
        def _wheel(e):
            if inner.winfo_reqheight() <= canvas.winfo_height():
                return
            delta = -1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1
            canvas.yview_scroll(delta, "units")
        def _bind_wheel(_e=None):
            canvas.bind_all("<MouseWheel>", _wheel)
            canvas.bind_all("<Button-4>", _wheel)
            canvas.bind_all("<Button-5>", _wheel)
        def _unbind_wheel(_e=None):
            canvas.unbind_all("<MouseWheel>"); canvas.unbind_all("<Button-4>"); canvas.unbind_all("<Button-5>")
        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        host.bind("<Destroy>", lambda e: _unbind_wheel() if e.widget is host else None)

        self.frame = inner
        return self.frame

    # ------------------------------------------------------------- Loading bar + stop
    def bar_show(self, text: str, on_stop, button: str = "Stop"):
        self.busy_label.config(text=text)
        self.busy_btn.config(text=button, command=on_stop)
        if not self.busy_bar.winfo_ismapped():
            self.busy_bar.pack(side="bottom", fill="x", before=self.frame) if self.frame else self.busy_bar.pack(side="bottom", fill="x")

    def bar_hide(self):
        self._busy_token = None
        if self.busy_bar.winfo_ismapped():
            self.busy_bar.pack_forget()

    def bar_text(self, text: str):
        if self.busy_bar.winfo_ismapped():
            self.busy_label.config(text=text)

    def bg(self, fn, done, *args, busy: str | None = None):
        """fn(*args) in the background; done(result, error) afterwards in the UI thread.
        busy="text" shows the loading bar with a stop button: stop cancels running commands/downloads,
        the result is then discarded."""
        token = {"cancelled": False}
        if busy:
            def stop():
                token["cancelled"] = True
                features.cancel_current()
                updater.cancel()
                self.bar_hide()
                self.info("Cancelled.")
                if getattr(self, "btn_login", None) is not None:
                    try:
                        self.btn_login.state(["!disabled"])
                        self.lbl_status.config(text="Cancelled.")
                    except Exception:
                        pass
            self._busy_token = token
            self.bar_show(f"{busy}", stop)

        def run():
            try:
                res, err = fn(*args), None
            except Exception as e:      # noqa: BLE001 - message goes to the user
                res, err = None, e

            def finish():
                if token["cancelled"]:
                    return
                if busy and self._busy_token is token:
                    self.bar_hide()
                done(res, err)
            self.root.after(0, finish)
        threading.Thread(target=run, daemon=True).start()

    def show_busy(self, text):
        f = self._new_frame()
        ttk.Label(f, text="BYTERA RMM", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text=text, style="Sub.TLabel").pack(anchor="w", pady=(10, 0))

    # ------------------------------------------------------------- Start
    def auto_login(self):
        server, token = self.store.get("server"), self.store.load_token()
        if not (server and token):
            return self.show_login()
        api = Api(server, verify_tls=not self.store.get("insecure", False), store=self.store)

        def done(res, err):
            if err is None:
                self.api = api
                self.show_main()
                if self.store.get("open_on_start", True):
                    self.open_dashboard()
                self.root.after(3000, lambda: self.check_update(auto=True))
            elif isinstance(err, ApiError) and err.status == 401:
                self.store.clear_token()
                self.show_login(f"Please sign in again: {err}")
            else:
                self.show_offline(str(err))
        self.bg(api.session, done, token)

    def show_offline(self, msg):
        f = self._new_frame()
        ttk.Label(f, text="No connection", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text=msg, style="Sub.TLabel", wraplength=380).pack(anchor="w", pady=(8, 16))
        ttk.Button(f, text="Try again", style="Accent.TButton", command=self.auto_login).pack(fill="x")
        ttk.Button(f, text="Sign in differently", command=self.show_login).pack(fill="x", pady=(8, 0))

    # ------------------------------------------------------------- Sign-in
    def show_login(self, message: str = ""):
        f = self._new_frame()
        ttk.Label(f, text="BYTERA RMM", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"Client {__version__} - sign in once, the sign-in is then kept.",
                  style="Sub.TLabel", wraplength=390).pack(anchor="w", pady=(2, 12))
        self.v = {k: tk.StringVar(value=val) for k, val in {
            "server": self.store.get("server", ""), "user": self.store.get("username", ""), "pass": "",
            "code": "", "name": self.store.get("device_name") or default_device_name()}.items()}
        self.v_insecure = tk.BooleanVar(value=bool(self.store.get("insecure", False)))

        def field(label, key, show=None):
            ttk.Label(f, text=label, style="Sub.TLabel").pack(anchor="w", pady=(6, 1))
            e = ttk.Entry(f, textvariable=self.v[key], show=show)
            e.pack(fill="x")
            return e

        e_server = field("Server address", "server")
        e_server.bind("<FocusOut>", lambda e: self.load_realms())
        field("Username", "user")
        e_pass = field("Password", "pass", show="•")
        ttk.Label(f, text="Sign in via", style="Sub.TLabel").pack(anchor="w", pady=(6, 1))
        self.realm_ids, self.cb_realm = ["local"], ttk.Combobox(f, values=["Local account"], state="readonly")
        self.cb_realm.current(0)
        self.cb_realm.pack(fill="x")
        field("One-time code (2FA) - only if enabled", "code")
        field("Name of this device", "name")
        ttk.Checkbutton(f, text="Do not verify certificate (self-signed)", variable=self.v_insecure).pack(anchor="w", pady=(8, 0))
        self.btn_login = ttk.Button(f, text="Sign in", style="Accent.TButton", command=self.do_login)
        self.btn_login.pack(fill="x", pady=(14, 0))
        self.lbl_status = ttk.Label(f, text=message, style="Sub.TLabel", wraplength=390)
        self.lbl_status.pack(anchor="w", pady=(10, 0))
        e_pass.bind("<Return>", lambda e: self.do_login())
        (e_pass if self.v["server"].get() and self.v["user"].get() else e_server).focus_set()
        if self.v["server"].get():
            self.load_realms()

    def load_realms(self):
        try:
            api = Api(self.v["server"].get(), verify_tls=not self.v_insecure.get(), store=self.store)
        except ApiError:
            return

        def done(realms, err):
            if err or not realms or not self.cb_realm.winfo_exists():
                return
            self.realm_ids = ["local"] + [r["id"] for r in realms]
            self.cb_realm["values"] = ["Local account"] + [r["name"] for r in realms]
        self.bg(api.realms, done)

    def do_login(self):
        try:
            api = Api(self.v["server"].get(), verify_tls=not self.v_insecure.get(), store=self.store)
        except ApiError as e:
            self.lbl_status.config(text=f"Error: {e}")
            return
        idx = self.cb_realm.current() if self.cb_realm.current() >= 0 else 0
        realm = self.realm_ids[idx] if idx < len(self.realm_ids) else "local"
        user, pw, code, name = self.v["user"].get().strip(), self.v["pass"].get(), self.v["code"].get().strip(), self.v["name"].get().strip()
        self.btn_login.state(["disabled"])
        self.lbl_status.config(text="Signing in …")

        def work():
            api.login(user, pw, realm, code)
            return api.register_device(name)

        def done(res, err):
            self.btn_login.state(["!disabled"])
            if err:
                msg = str(err)
                self.lbl_status.config(text="Please enter the one-time code from the authenticator app." if msg == "2fa_required" else f"Error: {msg}")
                return
            self.store.set(server=api.server, username=user, insecure=bool(self.v_insecure.get()),
                           device_name=name, device_id=res["device"]["id"])
            self.store.save_token(res["device_token"])
            self.api = api
            self.show_main()
            self.open_dashboard()
            self.root.after(3000, lambda: self.check_update(auto=True))
        self.bg(work, done, busy="Signing in …")

    # ------------------------------------------------------------- Main window
    def show_main(self):
        f = self._new_frame()
        ttk.Label(f, text="BYTERA RMM", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"Signed in as {self.store.get('username')}\n{self.api.server}",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 16))

        def btn(text, cmd, style="TButton"):
            b = ttk.Button(f, text=text, command=cmd, style=style)
            b.pack(fill="x", pady=3)
            return b

        btn("Open dashboard", self.open_dashboard, "Accent.TButton")
        mounted = bool(self.store.get("drive_letter") or self.store.get("linux_mount"))
        self.btn_mount = btn("Disconnect relay drive" if mounted else "Connect relay as a drive", self.toggle_mount)
        btn("Remote shell …", lambda: self.choose_client("Remote shell", self.open_shell))
        btn("RMM console", self.open_console)
        # Remote screen: also offer offline agents - a direct connection (local IP/VPN) needs no agent
        btn("Remote screen (Remote Desktop) …", lambda: self.choose_client("Remote screen via Remote Desktop", lambda c: self.screen(c, "rdp"), online_only=False))
        btn("Remote screen (VNC) …", lambda: self.choose_client("Remote screen via VNC", lambda c: self.screen(c, "vnc"), online_only=False))
        ttk.Separator(f).pack(fill="x", pady=12)
        # VPN: automatically set up, connect and (on expiry) delete tunnels I am a participant of
        self.v_vpn = tk.BooleanVar(value=bool(self.store.get("vpn_auto", True)))

        def _vpn_toggle():
            self.store.set(vpn_auto=bool(self.v_vpn.get()))
            if self.v_vpn.get():
                self.vpn.now()
            else:
                self.bg(self.vpn.remove_all, lambda *_: self._vpn_status("VPN automation off - tunnels removed"))
        ttk.Checkbutton(f, text="Manage VPN tunnels automatically", variable=self.v_vpn, command=_vpn_toggle).pack(anchor="w")
        self.lbl_vpn = ttk.Label(f, text="VPN: checking …", style="Sub.TLabel")
        self.lbl_vpn.pack(anchor="w", pady=(2, 4))
        # VPN area directly in the main window (real-time state, connect/disconnect)
        from .vpnpanel import VpnPanel
        if getattr(self, "vpnpanel", None):
            self.vpnpanel.stop()
        self.vpnpanel = VpnPanel(self, f, default_data_dir())
        self.vpn.start()
        ttk.Separator(f).pack(fill="x", pady=8)
        self.v_auto = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(f, text="Start when signing in to the computer", variable=self.v_auto,
                        command=self.toggle_autostart).pack(anchor="w")
        self.v_open = tk.BooleanVar(value=bool(self.store.get("open_on_start", True)))
        ttk.Checkbutton(f, text="Open dashboard at startup", variable=self.v_open,
                        command=lambda: self.store.set(open_on_start=bool(self.v_open.get()))).pack(anchor="w", pady=(4, 0))
        self.v_close = tk.BooleanVar(value=bool(self.store.get("close_after_dashboard", False)))
        ttk.Checkbutton(f, text="Close window when the dashboard opens", variable=self.v_close,
                        command=lambda: self.store.set(close_after_dashboard=bool(self.v_close.get()))).pack(anchor="w", pady=(4, 0))
        self.v_auto_upd = tk.BooleanVar(value=bool(self.store.get("auto_update", True)))
        ttk.Checkbutton(f, text="Update automatically", variable=self.v_auto_upd,
                        command=lambda: self.store.set(auto_update=bool(self.v_auto_upd.get()))).pack(anchor="w", pady=(4, 0))
        btn(f"Check for updates  (version {__version__})", lambda: self.check_update(auto=False)).pack_configure(pady=(10, 0))
        btn("Sign out this device", self.logout, "Danger.TButton").pack_configure(pady=(14, 0))
        self.lbl_info = ttk.Label(f, text="", style="Sub.TLabel", wraplength=390)
        self.lbl_info.pack(anchor="w", pady=(10, 0))

    def info(self, text):
        if getattr(self, "lbl_info", None) and self.lbl_info.winfo_exists():
            self.lbl_info.config(text=text)

    # ------------------------------------------------------------- Actions
    def _fresh(self, then, busy: str = "Connecting …"):
        """Fetch a fresh session before each open (so the token never expires)."""
        def done(res, err):
            if err:
                if isinstance(err, ApiError) and err.status == 401:
                    self.store.clear_token()
                    return self.show_login(f"Signed out: {err}")
                return messagebox.showerror("BYTERA RMM", str(err))
            then()
        self.bg(self.api.session, done, self.store.load_token(), busy=busy)

    def open_dashboard(self):
        def opened():
            self.info("Dashboard opened in " + browser.open_app_window(self.api.dashboard_url(), default_data_dir() / "browser"))
            if self.store.get("close_after_dashboard", False):
                self._auto_close(4)
        self._fresh(opened, busy="Opening dashboard …")

    def open_console(self):
        """RMM console (module "console"): devices, live metrics, terminal, commands - with tab completion."""
        self._fresh(lambda: browser.open_app_window(self.api.dashboard_url("console"), default_data_dir() / "browser",
                                                    size=(1100, 680)), busy="Opening RMM console …")

    def _auto_close(self, seconds: int):
        """Close the control window after the dashboard opens - with "Keep open"."""
        token = {"keep": False}
        self._busy_token = token

        def keep():
            token["keep"] = True
            self.bar_hide()
            self.info("Control window stays open.")

        def tick(n):
            if token["keep"] or self._busy_token is not token:
                return
            if n <= 0:
                self.root.destroy()
                return
            self.bar_show(f"Dashboard open - window closes in {n} s", keep, button="Keep open")
            self.root.after(1000, lambda: tick(n - 1))
        tick(seconds)

    def open_shell(self, client):
        self._fresh(lambda: browser.open_app_window(self.api.dashboard_url("terminal", client["id"]),
                                                    default_data_dir() / "browser", size=(1000, 640)), busy="Opening remote shell …")

    def screen(self, client, protocol):
        """Remote screen: first choose target/port (gateway, local IP, custom, last), then connect."""
        def got(info, err):
            if err:
                return messagebox.showerror("Remote screen", str(err))
            self.connect_dialog(client, protocol, info)
        self.bg(self.api.target_info, got, client["id"], protocol, busy="Loading connection options …")

    def connect_dialog(self, client, protocol, info):
        name = "Remote Desktop" if protocol == "rdp" else "VNC"
        std = info["standard_port"]
        d = info.get("default") or {}
        last = info.get("last")
        win = tk.Toplevel(self.root)
        win.title(f"Remote screen via {name} - {client.get('hostname')}")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"{name}: {client.get('hostname')}", style="Title.TLabel").pack(anchor="w", pady=(0, 8))

        host_mode = tk.StringVar(value=d.get("host_mode") or "local")
        port_mode = tk.StringVar(value=d.get("port_mode") or "standard")
        v_host = tk.StringVar(value=d.get("host") or "")
        v_port = tk.StringVar(value=str(d.get("port") or ""))
        v_remember = tk.BooleanVar(value=False)

        ttk.Label(f, text="Target", style="Sub.TLabel").pack(anchor="w")
        rb = {}
        rb["gateway"] = ttk.Radiobutton(f, text="RMM gateway (server forwards, reachable only from this device)",
                                        value="gateway", variable=host_mode)
        rb["local"] = ttk.Radiobutton(f, text=f"Local IP of the device: {info.get('local_ip') or 'unknown'}",
                                      value="local", variable=host_mode)
        if last:
            rb["last"] = ttk.Radiobutton(f, text=f"Last connection: {last.get('display', '')}", value="last",
                                         variable=host_mode)
        for k in ("gateway", "local", "last"):
            if k in rb:
                rb[k].pack(anchor="w", pady=1)
        if not info.get("gateway_available"):
            rb["gateway"].state(["disabled"])
            if host_mode.get() == "gateway":
                host_mode.set("local")
        if not info.get("local_ip"):
            rb["local"].state(["disabled"])
            if host_mode.get() == "local":
                host_mode.set("gateway" if info.get("gateway_available") else "custom")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(1, 8))
        ttk.Radiobutton(row, text="Custom address:", value="custom", variable=host_mode).pack(side="left")
        e_host = ttk.Entry(row, textvariable=v_host, width=28)
        e_host.pack(side="left", padx=(6, 0), fill="x", expand=True)
        e_host.bind("<FocusIn>", lambda e: host_mode.set("custom"))

        ttk.Label(f, text="Port", style="Sub.TLabel").pack(anchor="w")
        prow = ttk.Frame(f)
        prow.pack(fill="x", pady=(1, 8))
        rp_std = ttk.Radiobutton(prow, text=f"Standard ({std})", value="standard", variable=port_mode)
        rp_gw = ttk.Radiobutton(prow, text="RMM-Gateway", value="gateway", variable=port_mode)
        rp_cu = ttk.Radiobutton(prow, text="Custom:", value="custom", variable=port_mode)
        e_port = ttk.Entry(prow, textvariable=v_port, width=7)
        for w in (rp_std, rp_gw, rp_cu):
            w.pack(side="left", padx=(0, 8))
        e_port.pack(side="left")
        e_port.bind("<FocusIn>", lambda e: port_mode.set("custom"))
        ttk.Checkbutton(f, text="Remember as default for this device", variable=v_remember).pack(anchor="w", pady=(2, 10))
        status = ttk.Label(f, text="", style="Sub.TLabel", wraplength=420)

        def sync(*_):
            hm = host_mode.get()
            fixed = hm in ("gateway", "last")
            for w in (rp_std, rp_gw, rp_cu, e_port):
                w.state(["disabled"] if fixed else ["!disabled"])
            rp_gw.state(["disabled"])                      # gateway port only together with the gateway target
            if hm == "gateway":
                port_mode.set("gateway")
            elif port_mode.get() == "gateway":
                port_mode.set("standard")
            e_host.state(["!disabled"])
        host_mode.trace_add("write", sync)
        sync()

        def go():
            hm, pm = host_mode.get(), port_mode.get()
            port = None
            if hm not in ("gateway", "last") and pm == "custom":
                try:
                    port = int(v_port.get())
                    assert 1 <= port <= 65535
                except (ValueError, AssertionError):
                    status.config(text="Please enter a port between 1 and 65535.")
                    return
            if hm == "custom" and not v_host.get().strip():
                status.config(text="Please enter an address.")
                return
            status.config(text="Connecting …")

            def done(res, err):
                if err:
                    status.config(text=f"Error: {err}")
                    return
                try:
                    msg = features.launch_screen(protocol, res["host"], int(res["port"]))
                    hint = " (reachable only from this device)" if res.get("host_mode") == "gateway" else ""
                    self.info(f"{msg}  {res['host']}:{res['port']}{hint}")
                    win.destroy()
                except features.FeatureError as e:
                    status.config(text=str(e))
            self.bg(self.api.connect, done, client["id"], protocol, hm, v_host.get().strip(), pm, port,
                    bool(v_remember.get()), busy="Setting up remote screen connection …")

        ttk.Button(f, text="Connect", style="Accent.TButton", command=go).pack(fill="x")
        status.pack(anchor="w", pady=(8, 0))
        win.bind("<Return>", lambda e: go())

    def toggle_mount(self):
        mounted = bool(self.store.get("drive_letter") or self.store.get("linux_mount"))
        if mounted:
            fn, args = features.unmount_relay, (self.store,)
        else:
            fn, args = features.mount_relay, (self.api.server, self.store.get("username", ""), self.store.load_token(), self.store)

        def done(msg, err):
            if err:
                return messagebox.showerror("Relay drive", str(err))
            self.info(msg)
            now = bool(self.store.get("drive_letter") or self.store.get("linux_mount"))
            self.btn_mount.config(text="Disconnect relay drive" if now else "Connect relay as a drive")
        self.bg(fn, done, *args, busy="Disconnecting drive …" if mounted else "Connecting relay drive …")

    def toggle_autostart(self):
        try:
            autostart.set_enabled(bool(self.v_auto.get()))
        except Exception as e:      # noqa: BLE001
            messagebox.showerror("Autostart", str(e))
            self.v_auto.set(autostart.is_enabled())

    def choose_client(self, title, on_choose, online_only: bool = True):
        def done(clients, err):
            if err:
                return messagebox.showerror(title, str(err))
            clients = sorted([c for c in clients if c.get("online") or not online_only],
                             key=lambda c: (not c.get("online"), (c.get("hostname") or "").lower()))
            if not clients:
                return messagebox.showinfo(title, "No device online." if online_only else "No devices visible.")
            win = tk.Toplevel(self.root)
            win.title(f"{title} - choose a device")
            win.configure(bg=BG)
            win.geometry("380x420")
            win.transient(self.root)
            q = tk.StringVar()
            ttk.Entry(win, textvariable=q).pack(fill="x", padx=12, pady=(12, 6))
            lb = tk.Listbox(win, bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="#0c1420",
                            highlightthickness=0, borderwidth=0, font=("Segoe UI", 10))
            lb.pack(fill="both", expand=True, padx=12)
            shown: list[dict] = []

            def refill(*_):
                lb.delete(0, "end")
                shown.clear()
                for c in clients:
                    label = (f"{'●' if c.get('online') else '○'} {c.get('hostname') or c['id']}   ({c.get('platform') or '?'})"
                             + ("" if c.get("online") else "  – offline"))
                    if q.get().lower() in label.lower():
                        shown.append(c)
                        lb.insert("end", label)
                if shown:
                    lb.selection_set(0)
            q.trace_add("write", refill)
            refill()

            def ok(*_):
                sel = lb.curselection()
                if sel:
                    c = shown[sel[0]]
                    win.destroy()
                    on_choose(c)
            lb.bind("<Double-Button-1>", ok)
            win.bind("<Return>", ok)
            ttk.Button(win, text="Open", style="Accent.TButton", command=ok).pack(fill="x", padx=12, pady=12)
        self.bg(self.api.clients, done, busy="Loading device list …")

    def logout(self):
        if not messagebox.askyesno("Sign out", "Sign out this device? The stored sign-in will be deleted."):
            return
        dev = self.store.get("device_id")

        def work():
            try:
                features.unmount_relay(self.store)
            except Exception:
                pass
            try:                                   # remove own VPN tunnels on sign-out
                if getattr(self, "vpnpanel", None):
                    self.vpnpanel.stop()
                self.vpn.stop()
                self.vpn.remove_all()
            except Exception:
                pass
            if dev:
                try:
                    self.api.remove_device(dev)
                except ApiError:
                    pass

        def done(_, __):
            self.store.clear_token()
            self.api = None
            self.show_login("Signed out.")
        self.bg(work, done)

    # ------------------------------------------------------------- VPN
    def _vpn_status(self, text: str):
        try:
            if getattr(self, "lbl_vpn", None) is not None and self.lbl_vpn.winfo_exists():
                self.lbl_vpn.config(text=text)
        except Exception:
            pass

    def _refresh_session(self):
        """Renew the session with the stored device token (for background queries)."""
        token = self.store.load_token()
        if self.api is not None and token:
            self.api.session(token)

    # ------------------------------------------------------------- Updates
    def check_update(self, auto: bool):
        if not self.api:
            return
        if auto and not self.store.get("auto_update", True):
            return

        def got(res, err):
            if err:
                if not auto:
                    messagebox.showerror("Update", f"Search failed: {err}")
                return
            if not res["newer"]:
                if not auto:
                    messagebox.showinfo("Update", res["reason"] or f"The app is up to date (version {res['current']}).")
                return
            if auto or messagebox.askyesno("Update", f"Version {res['latest']} is available (installed: {res['current']}).\n"
                                                     "Install now? The app restarts afterwards."):
                self.install_update(res)
        self.bg(updater.check, got, self.api, busy=None if auto else "Searching for updates …")

    def install_update(self, res):
        def progress(done, total):
            self.root.after(0, lambda: self.bar_text(f"Downloading update {res['latest']} … {done * 100 // total} %"))

        def work():
            pkg = updater.download(self.api, res["file"], progress)
            return updater.apply(res["kind"], pkg)

        def done(how, err):
            if err:
                if not isinstance(err, updater.Cancelled):
                    messagebox.showerror("Update", str(err))
                return
            self.info(f"Installing update {res['latest']} - the app restarts …")
            if how == "exit":
                self.root.after(500, self.root.destroy)
            else:
                self.root.after(500, updater.restart)
        self.bg(work, done, busy=f"Downloading update {res['latest']} …")

    def run(self):
        self.root.mainloop()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    link = next((a for a in argv if a.lower().startswith("bytera-rmm:")), None)
    if link:                                    # from the dashboard: open RDP/VNC/SSH/terminal externally
        from .launcher import handle
        return handle(link)
    deeplink = next((a for a in argv if a.lower().startswith("bytera://")), None)
    if deeplink:                                # internal address scheme bytera://<kind>/... (like steam://)
        from .launcher import handle_deeplink
        return handle_deeplink(deeplink)
    try:
        from . import legacy
        if legacy.migrate():                    # rename: the app moves to the new location and restarts there
            return
    except Exception:
        pass
    try:
        from . import urlscheme
        urlscheme.register()                    # bytera-rmm:// for this user (path can change)
    except Exception:
        pass
    try:
        from . import autostart
        autostart.migrate_legacy()              # migrate the app autostart from the old name (RAPALLE.net)
    except Exception:
        pass
    ClientApp(minimized="--minimized" in argv).run()
