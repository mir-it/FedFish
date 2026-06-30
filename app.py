import json
import queue
import re
import smtplib
import threading
import time
import tkinter as tk
from email.message import EmailMessage
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import config
from carrier_resolvers import (
    CarrierPreferenceStore,
    DELIVERY_SERVICE_NONE_LABEL,
    SHIPMENT_TERMS_OPTIONS,
    SHIP_TYPE_OPTIONS,
    VALID_SHIP_TYPES,
    delivery_service_code_from_label,
    delivery_service_from_fishbowl_carrier,
    delivery_service_label_from_code,
    delivery_service_options_for_carrier,
    shipment_terms_code_from_label,
    shipment_terms_from_carrier_name,
    shipment_terms_label_from_code,
    ship_type_code_from_label,
    ship_type_label_from_code,
)
from sales_order import (
    SalesOrderSession,
    SalesOrderSummary,
    SynapseDetailLine,
    SynapseHeaderOverrides,
    SynapseLastSendStore,
    load_order_summaries,
)
from fishbowl_client import FishbowlAuthError, FishbowlClient
from synapse_client import SynapseClient, SynapseConfig, SynapseCreateOrderError
from uom_conversion import load_coverage_map_from_csv, normalize_uom, suggest_each_qty


def _safe_logout(client: "FishbowlClient") -> None:
    try:
        client.logout()
    except Exception:
        pass


def _send_pdf_email(to_addr: str, subject: str, body: str, attachment_paths, cc_addrs=None):
    """Send one or more PDF attachments over SMTP (Gmail-compatible, STARTTLS).

    ``attachment_paths`` may be a single path or a list of paths.
    ``cc_addrs`` may be a list of CC recipients.
    Raises on failure so the caller can surface the error to the user.
    """
    if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
        raise ValueError("SMTP_USERNAME / SMTP_PASSWORD are not set in your .env.")
    if not to_addr:
        raise ValueError("No recipient email address.")

    if isinstance(attachment_paths, (str, Path)):
        attachment_paths = [attachment_paths]
    attachment_paths = [p for p in (attachment_paths or []) if str(p).strip()]
    if not attachment_paths:
        raise ValueError("No attachments selected.")

    cc_addrs = [a.strip() for a in (cc_addrs or []) if a and a.strip()]

    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USERNAME
    msg["To"] = to_addr
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg.set_content(body or "")

    for ap in attachment_paths:
        path = Path(ap)
        if not path.is_file():
            raise ValueError(f"Attachment not found: {ap}")
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=path.name,
        )

    recipients = [to_addr] + cc_addrs
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg, to_addrs=recipients)


_LOT_WITH_QTY_UOM_DATE_PREFIX_RE = re.compile(
    r"^(?:\d+\s*[a-zA-Z]+\s+)?\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\s*/\s*(.+)$",
    re.IGNORECASE,
)
_LOT_DATE_PREFIX_RE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\s*/\s*")


def _normalize_lot_number(raw_lot) -> str:
    lot = str(raw_lot or "").strip()
    if not lot:
        return ""
    lot = " ".join(lot.split())
    prefixed = _LOT_WITH_QTY_UOM_DATE_PREFIX_RE.match(lot)
    if prefixed:
        return prefixed.group(1).strip()
    return _LOT_DATE_PREFIX_RE.sub("", lot, count=1).strip()


_NOTED_QTY_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:pieces|piece|pcs|pc|each|ea|sheets|sheet)\b",
    re.IGNORECASE,
)


def _parse_noted_qty(text: str):
    """Pull a quantity like '100 pieces', '100pcs', '100 each', '100 ea', '100 sheets',
    or '4,245 pieces' (commas as thousands separators) from a note.

    Returns an int (or float when fractional) when found, otherwise None.
    """
    m = _NOTED_QTY_RE.search(str(text or ""))
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    return int(val) if val.is_integer() else val


SENT_SHIPMENTS_FILE = Path(__file__).with_name("synapse_sent_shipments.txt")

INACTIVITY_SECONDS = 15 * 60
IDLE_CHECK_MS = 60_000


class LoginFrame(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent)
        self.app = app

        outer = ttk.Frame(self, padding=40)
        outer.place(relx=0.5, rely=0.4, anchor="center")

        ttk.Label(outer, text="Fishbowl Login", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 20)
        )

        self.host_var = tk.StringVar(value=config.FB_HOST)
        self.port_var = tk.StringVar(value=config.FB_PORT)
        self.username_var = tk.StringVar(value=config.FB_USERNAME)
        self.password_var = tk.StringVar(value=config.FB_PASSWORD)

        fields = [
            ("Host:", self.host_var),
            ("Port:", self.port_var),
            ("Username:", self.username_var),
            ("Password:", self.password_var),
        ]
        for i, (label, var) in enumerate(fields, start=1):
            ttk.Label(outer, text=label).grid(row=i, column=0, sticky="e", padx=(0, 10), pady=6)
            show = "*" if label == "Password:" else None
            entry = ttk.Entry(outer, textvariable=var, width=32, show=show)
            entry.grid(row=i, column=1, sticky="w", pady=6)
            if i == 1:
                self._first_entry = entry

        self.login_btn = ttk.Button(outer, text="Login", command=self._do_login)
        self.login_btn.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(20, 0))

        self.bind("<Return>", lambda e: self._do_login())

    def on_show(self):
        self.login_btn.config(state="normal")
        self._first_entry.focus_set()

    def _do_login(self):
        host = self.host_var.get().strip()
        port = self.port_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not host or not port or not username or not password:
            messagebox.showwarning("Missing fields", "Enter host, port, username, and password.")
            return
        self.login_btn.config(state="disabled")
        self.app.connect_fishbowl(
            host,
            port,
            username,
            password,
            on_success=lambda: self.app.show("shipments"),
            on_failure=lambda: self.login_btn.config(state="normal"),
        )


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fishbowl Shipping")
        self.geometry("1000x650")
        self.minsize(900, 600)

        self.fb: FishbowlClient | None = None

        self.order_summaries: dict[str, SalesOrderSummary] = {}
        self.cached_so_rows: dict[str, dict] = {}
        self.current_so_num: str | None = None
        self.current_order: SalesOrderSession | None = None
        self.hdr_instructions_by_so: dict[str, str] = {}
        self.synapse_sent_shipments: set[str] = self._load_sent_shipments()
        self.synapse_failed_shipments: dict[str, str] = {}
        self.carrier_prefs = CarrierPreferenceStore()
        self.synapse_last_send = SynapseLastSendStore()
        self.synapse_last_send.on_save_error = self.set_status

        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken")
        status.pack(side="bottom", fill="x")
        self.carrier_prefs.on_save_error = self.set_status

        self.frames: dict[str, ttk.Frame] = {}
        for name, cls in [
            ("login", LoginFrame),
            ("shipments", ShipmentsFrame),
            ("detail", PalletDetailFrame),
        ]:
            frame = cls(self.container, self)
            self.frames[name] = frame
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._last_activity = time.monotonic()
        self._idle_timer_id: str | None = None
        self._bind_activity_tracking()

        self.show("login")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def show(self, name: str):
        frame = self.frames[name]
        frame.tkraise()
        if hasattr(frame, "on_show"):
            frame.on_show()

    def set_status(self, text: str):
        self.status_var.set(text)

    def connect_fishbowl(self, host, port, username, password, on_success=None, on_failure=None):
        self.set_status("Connecting to Fishbowl...")
        client = FishbowlClient(host, port)

        def do():
            client.login(username, password)
            return client

        def ok(connected_client):
            self.fb = connected_client
            self._last_activity = time.monotonic()
            self._start_idle_monitor()
            self.set_status("Connected.")
            shipments = self.frames.get("shipments")
            if isinstance(shipments, ShipmentsFrame) and not shipments.tree.get_children():
                shipments.refresh()
            if on_success:
                on_success()

        def err(exc):
            self.set_status(f"Login failed: {exc}")
            messagebox.showerror("Fishbowl login failed", str(exc))
            if on_failure:
                on_failure()

        self.run_async(do, ok, err)

    def logout(self, show_message: str | None = None):
        self._stop_idle_monitor()
        client = self.fb
        self.fb = None
        self.order_summaries.clear()
        self.cached_so_rows.clear()
        self.current_so_num = None
        self.current_order = None
        shipments = self.frames.get("shipments")
        if isinstance(shipments, ShipmentsFrame):
            shipments.clear_view()
        self.set_status("Logged out.")
        self.show("login")
        if show_message:
            messagebox.showinfo("Session ended", show_message)
        if client and client.token:
            threading.Thread(target=_safe_logout, args=(client,), daemon=True).start()

    def _handle_auth_error(self, exc):
        self.set_status("Session expired. Please log in again.")
        self.logout()

    def _bind_activity_tracking(self):
        def record(_event=None):
            if self.fb:
                self._last_activity = time.monotonic()

        for seq in ("<Button-1>", "<Button-2>", "<Button-3>", "<Key>"):
            self.bind_all(seq, record, add="+")

    def _start_idle_monitor(self):
        self._stop_idle_monitor()
        self._schedule_idle_check()

    def _stop_idle_monitor(self):
        if self._idle_timer_id is not None:
            self.after_cancel(self._idle_timer_id)
            self._idle_timer_id = None

    def _schedule_idle_check(self):
        self._idle_timer_id = self.after(IDLE_CHECK_MS, self._check_idle)

    def _check_idle(self):
        self._idle_timer_id = None
        if not self.fb:
            return
        if time.monotonic() - self._last_activity >= INACTIVITY_SECONDS:
            self.logout()
            return
        self._schedule_idle_check()

    def run_async(self, fn, on_success, on_error=None):
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                q.put(("ok", fn()))
            except Exception as e:
                q.put(("err", e))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            if kind == "ok":
                on_success(payload)
            else:
                if isinstance(payload, FishbowlAuthError):
                    self._handle_auth_error(payload)
                    return
                if on_error:
                    on_error(payload)
                else:
                    messagebox.showerror("Error", str(payload))
                    self.set_status(f"Error: {payload}")

        self.after(100, poll)

    def _on_close(self):
        self._stop_idle_monitor()
        if self.fb and self.fb.token:
            try:
                self.fb.logout()
            except Exception:
                pass
        self.destroy()

    def _load_sent_shipments(self) -> set[str]:
        if not SENT_SHIPMENTS_FILE.exists():
            return set()
        try:
            lines = SENT_SHIPMENTS_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return set()
        return {line.strip() for line in lines if line.strip()}

    def save_sent_shipments(self):
        payload = "\n".join(sorted(self.synapse_sent_shipments))
        if payload:
            payload += "\n"
        try:
            SENT_SHIPMENTS_FILE.write_text(payload, encoding="utf-8")
        except Exception as e:
            self.set_status(f"Warning: failed saving sent shipment memory ({e}).")


class ShipmentsFrame(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self._all_shipment_rows: list[SalesOrderSummary] = []

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Sales Order Shipments", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Label(top, text="Search SO #:").pack(side="left", padx=(16, 6))
        self.ship_search_var = tk.StringVar(value="")
        self.ship_search_entry = ttk.Entry(top, textvariable=self.ship_search_var, width=18)
        self.ship_search_entry.pack(side="left")
        self.ship_search_var.trace_add("write", lambda *_: self._render_shipments())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="Logout", command=self._logout).pack(side="right", padx=(0, 8))

        cols = ("so_num", "customer", "city", "state", "zip", "carrier", "synapse_status")
        headers = ("SO #", "Customer", "City", "State", "Zip", "Carrier", "Synapse")
        self._status_col_idx = len(cols) - 1
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=130, anchor="w")
        self.tree.column("customer", width=200, anchor="w")
        self.tree.column("synapse_status", width=90, anchor="center")
        self.tree.tag_configure("synapse_sent", background="#dff0d8")
        self.tree.tag_configure("synapse_failed", background="#f8d7da")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Double-1>", lambda e: self._view_selected())
        self.tree.bind("<Button-3>", self._on_shipments_tree_right_click)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="View Pallets →", command=self._view_selected).pack(side="right")

    def on_show(self):
        if self.app.fb and not self.tree.get_children():
            self.refresh()

    def clear_view(self):
        self._all_shipment_rows = []
        self.ship_search_var.set("")
        for row in self.tree.get_children():
            self.tree.delete(row)

    def _logout(self):
        self.app.logout()

    def _on_shipments_tree_right_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        values = self.tree.item(row_id, "values") or ()
        ship_text = str(values[0] if values else row_id).strip()
        if not ship_text:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda t=ship_text: self._copy_ship_num_text(t))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_ship_num_text(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.app.set_status(f"Copied SO #: {text}")

    def refresh(self):
        if not self.app.fb:
            self.app.set_status("Not connected to Fishbowl.")
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.app.set_status("Loading shipments...")

        def do():
            return load_order_summaries(self.app.fb)

        def ok(result):
            if self.app.fb is None:
                return
            summaries, cached_so_rows = result
            self.app.order_summaries = {s.so_num: s for s in summaries if s.so_num}
            self.app.cached_so_rows = cached_so_rows
            self._all_shipment_rows = summaries
            shown = self._render_shipments()
            self.app.set_status(f"Loaded {len(summaries)} sales orders (showing {shown}).")

        self.app.run_async(do, ok)

    def _status_text_and_tags_for_so(self, so_num: str) -> tuple[str, tuple[str, ...]]:
        if so_num in self.app.synapse_sent_shipments:
            return "SENT", ("synapse_sent",)
        if so_num in self.app.synapse_failed_shipments:
            return "FAILED", ("synapse_failed",)
        return "", ()

    def _render_shipments(self) -> int:
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

        search = str(self.ship_search_var.get() or "").strip().lower()
        shown = 0
        for summary in self._all_shipment_rows:
            so_num = summary.so_num
            if search and search not in so_num.lower():
                continue
            status_text, tags = self._status_text_and_tags_for_so(so_num)
            self.tree.insert(
                "",
                "end",
                iid=so_num,
                values=(
                    so_num,
                    summary.customer_name,
                    summary.city,
                    summary.state,
                    summary.zip_code,
                    summary.carrier_name,
                    status_text,
                ),
                tags=tags,
            )
            shown += 1
        return shown

    def mark_synapse_sent(self, ship_num: str):
        self.app.synapse_sent_shipments.add(ship_num)
        self.app.synapse_failed_shipments.pop(ship_num, None)
        self.app.save_sent_shipments()
        if not ship_num or not self.tree.exists(ship_num):
            return
        current_values = list(self.tree.item(ship_num, "values") or ())
        if len(current_values) <= self._status_col_idx:
            current_values += [""] * ((self._status_col_idx + 1) - len(current_values))
        current_values[self._status_col_idx] = "SENT"
        self.tree.item(ship_num, values=tuple(current_values), tags=("synapse_sent",))

    def mark_synapse_failed(self, ship_num: str, reason: str):
        self.app.synapse_failed_shipments[ship_num] = reason
        self.app.synapse_sent_shipments.discard(ship_num)
        self.app.save_sent_shipments()
        if not ship_num or not self.tree.exists(ship_num):
            return
        current_values = list(self.tree.item(ship_num, "values") or ())
        if len(current_values) <= self._status_col_idx:
            current_values += [""] * ((self._status_col_idx + 1) - len(current_values))
        current_values[self._status_col_idx] = "FAILED"
        self.tree.item(ship_num, values=tuple(current_values), tags=("synapse_failed",))

    def _view_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select shipment", "Pick a shipment row first.")
            return
        self.app.current_so_num = sel[0]
        self.app.show("detail")


class PalletDetailFrame(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self._items_load_seq = 0
        self._review_rows: list[dict] = []
        self._delivery_service_options: tuple[tuple[str, str], ...] = ()

        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        self.title_var = tk.StringVar()
        ttk.Label(header, textvariable=self.title_var, font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(header, text="← Back", command=lambda: self.app.show("shipments")).pack(side="right")

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)

        order_frame = ttk.LabelFrame(mid, text="Order", padding=5)
        order_frame.pack(fill="x", pady=(0, 8))

        scac_row = ttk.Frame(order_frame)
        scac_row.pack(fill="x")
        ttk.Label(scac_row, text="SCAC:").pack(side="left")
        self.scac_var = tk.StringVar(value="")
        ttk.Entry(scac_row, textvariable=self.scac_var, width=16).pack(side="left", padx=(6, 0))
        ttk.Label(scac_row, text="Carrier:").pack(side="left", padx=(18, 6))
        self.carrier_name_var = tk.StringVar(value="")
        ttk.Label(scac_row, textvariable=self.carrier_name_var, width=24).pack(side="left")
        ttk.Label(scac_row, text="Ship Type:").pack(side="left", padx=(18, 6))
        self.ship_type_var = tk.StringVar(value=ship_type_label_from_code(config.SYNAPSE_SHIP_TYPE))
        ttk.Combobox(
            scac_row,
            textvariable=self.ship_type_var,
            width=24,
            values=tuple(f"{code} - {desc}" for code, desc in SHIP_TYPE_OPTIONS),
            state="readonly",
        ).pack(side="left")
        ttk.Label(scac_row, text="Shipment Terms:").pack(side="left", padx=(18, 6))
        self.shipment_terms_var = tk.StringVar(
            value=shipment_terms_label_from_code("3RD")
        )
        ttk.Combobox(
            scac_row,
            textvariable=self.shipment_terms_var,
            width=28,
            values=tuple(f"{code} - {desc}" for code, desc in SHIPMENT_TERMS_OPTIONS),
            state="readonly",
        ).pack(side="left")

        delivery_row = ttk.Frame(order_frame)
        delivery_row.pack(fill="x", pady=(8, 0))
        ttk.Label(delivery_row, text="Delivery Service:").pack(side="left")
        self.delivery_service_var = tk.StringVar(value=DELIVERY_SERVICE_NONE_LABEL)
        self.delivery_service_combo = ttk.Combobox(
            delivery_row,
            textvariable=self.delivery_service_var,
            width=40,
            values=(DELIVERY_SERVICE_NONE_LABEL,),
            state="disabled",
        )
        self.delivery_service_combo.pack(side="left", padx=(6, 0))
        self.delivery_service_hint = ttk.Label(
            delivery_row,
            text="(FedEx / UPS only)",
            foreground="#888888",
        )
        self.delivery_service_hint.pack(side="left", padx=(8, 0))

        item_frame = ttk.LabelFrame(mid, text="Items", padding=5)
        item_frame.pack(fill="both", expand=True)
        icols = ("item", "coverage", "noted_qty", "send_qty", "lot", "note")
        iheaders = ("Item", "SF per EA", "Send Qty", "Fallback Qty", "Lot #", "Note")
        self.item_tree = ttk.Treeview(item_frame, columns=icols, show="headings", height=12)
        for c, h in zip(icols, iheaders):
            self.item_tree.heading(c, text=h)
            self.item_tree.column(c, width=100, anchor="w")
        self.item_tree.column("item", width=180, anchor="w")
        self.item_tree.column("noted_qty", width=90, anchor="w")
        self.item_tree.column("send_qty", width=100, anchor="w")
        self.item_tree.column("note", width=170, anchor="w")
        self.item_tree.pack(fill="both", expand=True)
        self.item_tree.bind("<Double-1>", self._on_review_item_double_click)

        actions = ttk.Frame(self, padding=10)
        actions.pack(fill="x")
        self.synapse_btn = ttk.Button(actions, text="Create Synapse Order", command=self._create_synapse_order)
        self.synapse_btn.pack(side="left")
        self.email_bol_btn = ttk.Button(
            actions,
            text="Email PDF to NJ",
            command=self._email_bol_to_warehouse,
        )
        self.email_bol_btn.pack(side="left", padx=(8, 0))
        self.toggle_raw_btn = ttk.Button(
            actions,
            text="Show payload",
            command=self._toggle_raw,
            state="disabled",
        )
        self.toggle_raw_btn.pack(side="left", padx=8)

        instruct_frame = ttk.LabelFrame(self, text="Order Instructions (hdrinstruct.instructions)", padding=5)
        instruct_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.instructions_var = tk.StringVar(value="")
        self.instructions_entry = ttk.Entry(instruct_frame, textvariable=self.instructions_var)
        self.instructions_entry.pack(side="left", fill="x", expand=True)
        self.instructions_count_var = tk.StringVar(value="0/255")
        ttk.Label(instruct_frame, textvariable=self.instructions_count_var).pack(side="right", padx=(8, 0))
        self._instructions_trace_suspended = False
        self.instructions_var.trace_add("write", self._on_instructions_change)

        self.raw_frame = ttk.LabelFrame(self, text="Synapse Payload/Response", padding=5)
        self.raw_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.raw_text = tk.Text(self.raw_frame, height=10, wrap="none")
        self.raw_text.pack(fill="both", expand=True)
        self._raw_visible = False

    def on_show(self):
        num = self.app.current_so_num
        if not num:
            return
        if not self.app.fb:
            self.app.set_status("Not connected to Fishbowl.")
            return
        so_row = self.app.cached_so_rows.get(num)
        if not so_row:
            messagebox.showwarning(
                "Order not loaded",
                "Cached sales-order data is missing. Go back and click Refresh on the shipments list.",
            )
            self.app.show("shipments")
            return
        summary = self.app.order_summaries.get(num)
        location = summary.location_title if summary else ""
        self.title_var.set(f"SO #{num}  →  {location}" if location else f"SO #{num}")
        self._set_instructions_text(self.app.hdr_instructions_by_so.get(num, ""))

        self.item_tree.delete(*self.item_tree.get_children())
        self._review_rows = []
        self.carrier_name_var.set("")
        self._hide_raw()
        self._update_raw_btn_state()
        self.app.current_order = None
        load_seq = self._items_load_seq + 1
        self._items_load_seq = load_seq

        self.app.set_status(f"Loading sales order {num}...")

        def do():
            return load_seq, num, SalesOrderSession.load(
                self.app.fb,
                num,
                so_row=so_row,
                last_send_store=self.app.synapse_last_send,
            )

        def ok(payload):
            load_seq, so_num, session = payload
            if load_seq != self._items_load_seq or so_num != self.app.current_so_num:
                return
            self.app.current_order = session
            self._set_instructions_text(
                session.order.resolved_instructions(self.app.hdr_instructions_by_so.get(so_num, ""))
            )
            self._setup_review_ui(session)
            self._update_raw_btn_state()
            self.app.set_status(f"SO {so_num}: {len(session.order.lines)} items.")

        self.app.run_async(do, ok)

    def _create_synapse_order(self):
        if not config.SYNAPSE_USERNAME or not config.SYNAPSE_PASSWORD:
            messagebox.showerror(
                "Synapse not configured",
                "Set SYNAPSE_USERNAME and SYNAPSE_PASSWORD in your .env (and restart the app).",
            )
            return

        so_num = str(self.app.current_so_num or "").strip()
        session = self.app.current_order
        if not so_num:
            messagebox.showwarning("No sales order", "No sales order is selected.")
            return
        if not session:
            messagebox.showwarning("No items", "Sales order details are not loaded yet.")
            return

        header_fields = self._read_order_header_fields()
        if header_fields is None:
            return
        carrier_scac, ship_type, shipment_terms, delivery_service = header_fields
        if ship_type not in VALID_SHIP_TYPES:
            ship_type = config.SYNAPSE_SHIP_TYPE
        reviewed = self._collect_reviewed_lines_for_send()
        if reviewed is None:
            return

        try:
            order_data = session.order.build_synapse_payload(
                header=SynapseHeaderOverrides(
                    carrier_scac=carrier_scac,
                    ship_type=ship_type,
                    shipment_terms=shipment_terms,
                    delivery_service=delivery_service,
                ),
                details=[
                    SynapseDetailLine(
                        item=r["item"],
                        send_uom=r["send_uom"],
                        send_qty=r["send_qty"],
                        lot_number=r.get("lot_number", ""),
                        dtl_pass_thru_num_10=r.get("dtl_pass_thru_num_10"),
                    )
                    for r in reviewed
                ],
                instructions=str(self.instructions_var.get() or "").strip(),
                normalize_lot=_normalize_lot_number,
            )
        except ValueError as e:
            messagebox.showerror("Cannot create order", str(e))
            return

        carrier_name = session.order.carrier_name
        self.synapse_btn.config(state="disabled")
        self.app.set_status("Creating Synapse order...")

        def do():
            client = SynapseClient(
                SynapseConfig(
                    base_url=config.SYNAPSE_BASE_URL,
                    username=config.SYNAPSE_USERNAME,
                    password=config.SYNAPSE_PASSWORD,
                )
            )
            client.login()
            response = client.create_order(order_data)
            return {
                "response": response,
                "sent_payload": client.last_payload_sent or order_data,
            }

        def ok(result):
            self.synapse_btn.config(state="normal")
            sent_payload = result.get("sent_payload")
            response = result.get("response")
            session.record_synapse_send(
                sent_payload,
                response,
                store=self.app.synapse_last_send,
            )
            self.app.carrier_prefs.remember_scac(carrier_name, carrier_scac)
            self.app.carrier_prefs.remember_ship_type(carrier_name, ship_type)
            if self.app.current_so_num:
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_sent(self.app.current_so_num)
            self._update_raw_btn_state()
            if self._raw_visible:
                self._refresh_raw_text()
            self.app.set_status("Synapse order created.")
            messagebox.showinfo("Synapse", "Order created successfully in Synapse.")

        def err(e):
            self.synapse_btn.config(state="normal")
            response = {"error": str(e)}
            sent_payload = order_data
            if isinstance(e, SynapseCreateOrderError) and e.sent_payload is not None:
                sent_payload = e.sent_payload
            session.record_synapse_send(
                sent_payload,
                response,
                store=self.app.synapse_last_send,
            )
            self._update_raw_btn_state()
            if self._raw_visible:
                self._refresh_raw_text()
            if self.app.current_so_num:
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_failed(self.app.current_so_num, str(e))
            messagebox.showerror("Synapse error", str(e))
            self.app.set_status("Synapse order creation failed.")

        self.app.run_async(do, ok, err)

    def _set_instructions_text(self, value: str):
        self._instructions_trace_suspended = True
        try:
            capped = str(value or "")[:255]
            self.instructions_var.set(capped)
            self.instructions_count_var.set(f"{len(capped)}/255")
        finally:
            self._instructions_trace_suspended = False

    def _on_instructions_change(self, *_):
        if self._instructions_trace_suspended:
            return
        current = str(self.instructions_var.get() or "")
        if len(current) > 255:
            current = current[:255]
            self._set_instructions_text(current)
        else:
            self.instructions_count_var.set(f"{len(current)}/255")

        so_num = self.app.current_so_num
        if not so_num:
            return
        if current.strip():
            self.app.hdr_instructions_by_so[so_num] = current
        else:
            self.app.hdr_instructions_by_so.pop(so_num, None)
        if self._raw_visible:
            self._refresh_raw_text()

    def _should_exclude_item(self, item_num: str) -> bool:
        s = (item_num or "").strip().lower()
        if not s:
            return True
        keywords = [k.strip().lower() for k in (config.EXCLUDE_ITEM_KEYWORDS or "").split(",") if k.strip()]
        return any(k in s for k in keywords)

    def _setup_review_ui(self, session: SalesOrderSession):
        order = session.order
        carrier_name = order.carrier_name
        self.carrier_name_var.set(carrier_name)
        self.scac_var.set(self.app.carrier_prefs.resolve_scac(carrier_name))

        ship_type = self.app.carrier_prefs.resolve_ship_type(carrier_name)
        if ship_type not in VALID_SHIP_TYPES:
            ship_type = config.SYNAPSE_SHIP_TYPE
        self.ship_type_var.set(ship_type_label_from_code(ship_type))

        shipment_terms = shipment_terms_from_carrier_name(carrier_name) or "3RD"
        self.shipment_terms_var.set(shipment_terms_label_from_code(shipment_terms))

        self._delivery_service_options = delivery_service_options_for_carrier(carrier_name)
        delivery_enabled = bool(self._delivery_service_options)
        delivery_values = (
            DELIVERY_SERVICE_NONE_LABEL,
            *(f"{code} - {desc}" for code, desc in self._delivery_service_options),
        )
        self.delivery_service_combo.config(
            values=delivery_values,
            state="readonly" if delivery_enabled else "disabled",
        )
        initial_delivery_service = delivery_service_from_fishbowl_carrier(
            carrier_name, order.carrier_service_name
        )
        self.delivery_service_var.set(
            delivery_service_label_from_code(initial_delivery_service, self._delivery_service_options)
            if delivery_enabled
            else DELIVERY_SERVICE_NONE_LABEL
        )
        self.delivery_service_hint.pack_forget()
        if not delivery_enabled:
            self.delivery_service_hint.pack(side="left", padx=(8, 0))

        review_lines = order.aggregated_review_lines(
            should_exclude_item=self._should_exclude_item,
            normalize_lot=_normalize_lot_number,
            normalize_uom=normalize_uom,
        )
        self._review_rows = self._build_synapse_review_rows(review_lines)
        self._populate_review_item_tree()

    def _build_synapse_review_rows(self, lines: list[dict]) -> list[dict]:
        coverage_map = {}
        if config.PRODUCT_COVERAGE_CSV:
            try:
                coverage_map = load_coverage_map_from_csv(config.PRODUCT_COVERAGE_CSV)
            except Exception as e:
                messagebox.showwarning(
                    "Coverage file not loaded",
                    f"Could not load PRODUCT_COVERAGE_CSV.\n\n{e}\n\nYou'll need to override quantities manually.",
                )

        rows: list[dict] = []
        for l in lines:
            item = l["item"]
            item_key = item.upper()
            fb_uom = normalize_uom(l.get("fb_uom", ""))
            fb_qty = float(l.get("fb_qty") or 0)
            cov = coverage_map.get(item_key).coverage_sf_per_ea if item_key in coverage_map else None

            send_uom = "EA"
            send_qty = None
            note = ""

            if fb_uom == "SF" and cov:
                send_qty, fractional = suggest_each_qty(fb_qty, cov)
                if fractional:
                    note = "SF→EA rounded up"
                else:
                    note = "SF→EA converted"
            elif fb_uom == "SF" and not cov:
                note = "No coverage found"
            elif fb_uom in {"EA", "PCS"}:
                send_uom = "EA"
                send_qty = int(fb_qty) if float(fb_qty).is_integer() else None
                if send_qty is None:
                    note = "Needs integer"
                else:
                    note = f"{fb_uom} used as-is (no conversion)"
            elif fb_uom == "BOX":
                send_uom = "BOX"
                send_qty = int(fb_qty) if float(fb_qty).is_integer() else None
                if send_qty is None:
                    note = "Needs integer"
                else:
                    note = "BOX used as-is (no conversion)"
            else:
                note = "Review"

            rows.append(
                {
                    "item": item,
                    "fb_uom": fb_uom,
                    "fb_qty": fb_qty,
                    "coverage_sf_per_ea": cov,
                    "send_uom": send_uom,
                    "send_qty": send_qty,
                    "lot_number": _normalize_lot_number(l.get("tracking", "")),
                    "dtl_pass_thru_num_10": l.get("dtl_pass_thru_num_10"),
                    "note": note,
                    "noted_qty": _parse_noted_qty(l.get("soitem_note", "")),
                }
            )

        return rows

    def _review_tree_column_name(self, col_id: str) -> str:
        if not col_id or col_id == "#0":
            return ""
        idx = int(col_id[1:]) - 1
        cols = self.item_tree["columns"]
        if 0 <= idx < len(cols):
            return str(cols[idx])
        return ""

    def _populate_review_item_tree(self):
        self.item_tree.delete(*self.item_tree.get_children())
        for i, r in enumerate(self._review_rows):
            cov = r.get("coverage_sf_per_ea")
            noted_qty = r.get("noted_qty")
            self.item_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    r["item"],
                    "" if cov is None else f"{cov:.3f}".rstrip("0").rstrip("."),
                    "" if noted_qty is None else str(noted_qty),
                    "" if r.get("send_qty") is None else str(r["send_qty"]),
                    r.get("lot_number", ""),
                    r.get("note", ""),
                ),
            )

    def _refresh_review_row(self, idx: int):
        r = self._review_rows[idx]
        cov = r.get("coverage_sf_per_ea")
        noted_qty = r.get("noted_qty")
        self.item_tree.item(
            str(idx),
            values=(
                r["item"],
                "" if cov is None else f"{cov:.3f}".rstrip("0").rstrip("."),
                "" if noted_qty is None else str(noted_qty),
                "" if r.get("send_qty") is None else str(r["send_qty"]),
                r.get("lot_number", ""),
                r.get("note", ""),
            ),
        )

    def _on_review_item_double_click(self, event):
        row_id = self.item_tree.identify_row(event.y)
        col = self.item_tree.identify_column(event.x)
        if not row_id:
            return
        idx = int(row_id)
        col_name = self._review_tree_column_name(col)
        if col_name == "noted_qty":
            current = self._review_rows[idx].get("noted_qty")
            win = tk.Toplevel(self)
            win.title("Edit Send Qty")
            win.transient(self.winfo_toplevel())
            win.grab_set()
            ttk.Label(
                win,
                text=f"{self._review_rows[idx]['item']} send qty (integer, blank to use Fallback Qty):",
            ).pack(padx=10, pady=(10, 4))
            var = tk.StringVar(value="" if current is None else str(current))
            ent = ttk.Entry(win, textvariable=var, width=20)
            ent.pack(padx=10, pady=(0, 10))
            ent.focus_set()

            def save_noted():
                s = var.get().strip()
                if not s:
                    self._review_rows[idx]["noted_qty"] = None
                    self._refresh_review_row(idx)
                    win.destroy()
                    return
                try:
                    v = int(s)
                    if v <= 0:
                        raise ValueError()
                except Exception:
                    messagebox.showerror(
                        "Invalid qty",
                        "Send Qty must be a positive integer (or blank to use Fallback Qty).",
                        parent=win,
                    )
                    return
                self._review_rows[idx]["noted_qty"] = v
                self._refresh_review_row(idx)
                win.destroy()

            ttk.Button(win, text="Save", command=save_noted).pack(padx=10, pady=(0, 10))
            win.bind("<Return>", lambda e: save_noted())
            return

        if col_name == "send_qty":
            current = self._review_rows[idx].get("send_qty")
            win = tk.Toplevel(self)
            win.title("Edit Fallback Qty")
            win.transient(self.winfo_toplevel())
            win.grab_set()
            ttk.Label(win, text=f"{self._review_rows[idx]['item']} fallback qty (integer):").pack(
                padx=10, pady=(10, 4)
            )
            var = tk.StringVar(value="" if current is None else str(current))
            ent = ttk.Entry(win, textvariable=var, width=20)
            ent.pack(padx=10, pady=(0, 10))
            ent.focus_set()

            def save():
                s = var.get().strip()
                try:
                    v = int(s)
                    if v <= 0:
                        raise ValueError()
                except Exception:
                    messagebox.showerror("Invalid qty", "Fallback Qty must be a positive integer.", parent=win)
                    return
                self._review_rows[idx]["send_qty"] = v
                self._refresh_review_row(idx)
                win.destroy()

            ttk.Button(win, text="Save", command=save).pack(padx=10, pady=(0, 10))
            win.bind("<Return>", lambda e: save())
            return

        if col_name == "lot":
            current_lot = self._review_rows[idx].get("lot_number", "")
            win = tk.Toplevel(self)
            win.title("Edit Lot Number")
            win.transient(self.winfo_toplevel())
            win.grab_set()
            ttk.Label(win, text=f"{self._review_rows[idx]['item']} lot number:").pack(padx=10, pady=(10, 4))
            var = tk.StringVar(value=str(current_lot or ""))
            ent = ttk.Entry(win, textvariable=var, width=30)
            ent.pack(padx=10, pady=(0, 10))
            ent.focus_set()

            def save_lot():
                self._review_rows[idx]["lot_number"] = var.get().strip()
                self._refresh_review_row(idx)
                win.destroy()

            ttk.Button(win, text="Save", command=save_lot).pack(padx=10, pady=(0, 10))
            win.bind("<Return>", lambda e: save_lot())

    def _email_bol_to_warehouse(self):
        to_addr = (config.NJ_WAREHOUSE_EMAIL or "").strip()
        if not to_addr:
            messagebox.showerror(
                "Warehouse email not set",
                "NJ_WAREHOUSE_EMAIL is not set in your .env. Add it and restart the app.",
            )
            return
        if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
            messagebox.showerror(
                "Email not configured",
                "Set SMTP_USERNAME and SMTP_PASSWORD in your .env (and restart the app).",
            )
            return

        pdf_paths = filedialog.askopenfilenames(
            title="Select PDF(s) to email",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not pdf_paths:
            return
        pdf_paths = list(pdf_paths)

        order = self.app.current_order.order if self.app.current_order else None
        so_num = str(self.app.current_so_num or "").strip()

        subject_default = f"NJ - New Order # {so_num}".strip()
        carrier_name = order.carrier_name if order else ""
        items_text = "\n".join(order.email_item_lines()) if order else "*items*"

        body_default = (
            "Hello,\n\n"
            f"Please find the new order attached. Please pack well and ship via {carrier_name}\n\n"
            f"{items_text}\n"
            f"\nThank You,\nBest Regards"
        )
        cc_default = (config.NJ_WAREHOUSE_CC or "").strip()
        self._open_bol_email_dialog(to_addr, subject_default, body_default, pdf_paths, cc_default)

    def _open_bol_email_dialog(self, to_addr: str, subject_default: str, body_default: str, pdf_paths, cc_default: str = ""):
        if isinstance(pdf_paths, (str, Path)):
            pdf_paths = [pdf_paths]
        pdf_paths = list(pdf_paths or [])

        win = tk.Toplevel(self)
        win.title("Email PDF to NJ")
        win.transient(self.winfo_toplevel())
        win.grab_set()

        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="To:").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(frm, text=to_addr).grid(row=0, column=1, sticky="w", pady=(0, 4))

        ttk.Label(frm, text="Cc:").grid(row=1, column=0, sticky="w", pady=(0, 4))
        cc_var = tk.StringVar(value=cc_default)
        cc_ent = ttk.Entry(frm, textvariable=cc_var, width=60)
        cc_ent.grid(row=1, column=1, sticky="we", pady=(0, 4))

        ttk.Label(frm, text="Subject:").grid(row=2, column=0, sticky="w", pady=(0, 4))
        subject_var = tk.StringVar(value=subject_default)
        subject_ent = ttk.Entry(frm, textvariable=subject_var, width=60)
        subject_ent.grid(row=2, column=1, sticky="we", pady=(0, 4))

        ttk.Label(frm, text="Attachments:").grid(row=3, column=0, sticky="nw", pady=(0, 4))
        attach_text = "\n".join(Path(p).name for p in pdf_paths) or "(none)"
        ttk.Label(frm, text=attach_text, justify="left").grid(row=3, column=1, sticky="w", pady=(0, 4))

        ttk.Label(frm, text="Message:").grid(row=4, column=0, sticky="nw", pady=(0, 4))
        body_text = tk.Text(frm, width=60, height=10, wrap="word")
        body_text.insert("1.0", body_default)
        body_text.grid(row=4, column=1, sticky="we", pady=(0, 4))

        frm.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))

        def do_send():
            subject = subject_var.get().strip()
            body = body_text.get("1.0", "end").strip()
            cc_addrs = [a for a in re.split(r"[,;]", cc_var.get()) if a.strip()]
            send_btn.config(state="disabled")
            cancel_btn.config(state="disabled")
            self.app.set_status("Sending email...")

            def work():
                _send_pdf_email(to_addr, subject, body, pdf_paths, cc_addrs)
                return True

            def ok(_):
                self.app.set_status(f"Email sent to {to_addr}.")
                win.destroy()
                messagebox.showinfo("Email sent", f"Email sent to {to_addr}.")

            def err(e):
                self.app.set_status("Failed to send email.")
                send_btn.config(state="normal")
                cancel_btn.config(state="normal")
                messagebox.showerror(
                    "Email failed",
                    f"Could not send the email.\n\n{e}",
                    parent=win,
                )

            self.app.run_async(work, ok, err)

        cancel_btn = ttk.Button(btn_row, text="Cancel", command=win.destroy)
        cancel_btn.pack(side="right", padx=(8, 0))
        send_btn = ttk.Button(btn_row, text="Send", command=do_send)
        send_btn.pack(side="right")
        subject_ent.focus_set()

    def _peek_order_header_fields(self) -> tuple[str, str, str, str] | None:
        scac = (self.scac_var.get() or "").strip()
        if not scac:
            return None
        ship_type = ship_type_code_from_label(self.ship_type_var.get() or "")
        if ship_type not in VALID_SHIP_TYPES:
            return None
        shipment_terms = shipment_terms_code_from_label(self.shipment_terms_var.get() or "")
        if not shipment_terms:
            return None
        delivery_service = (
            delivery_service_code_from_label(self.delivery_service_var.get() or "", self._delivery_service_options)
            if self._delivery_service_options
            else ""
        )
        return scac, ship_type, shipment_terms, delivery_service

    def _peek_reviewed_lines(self) -> list[dict] | None:
        if not self._review_rows:
            return None
        out: list[dict] = []
        for r in self._review_rows:
            noted_qty = r.get("noted_qty")
            qty = noted_qty if noted_qty is not None else r.get("send_qty")
            if qty is None:
                return None
            normalized_lot = _normalize_lot_number(r.get("lot_number") or "")
            if config.SYNAPSE_REQUIRE_LOT and not normalized_lot:
                return None
            out.append(
                {
                    "item": r["item"],
                    "send_uom": r["send_uom"],
                    "send_qty": qty,
                    "lot_number": normalized_lot,
                    "dtl_pass_thru_num_10": r.get("dtl_pass_thru_num_10"),
                }
            )
        return out

    def _try_build_draft_synapse_payload(self) -> dict | None:
        session = self.app.current_order
        if not session:
            return None
        header_fields = self._peek_order_header_fields()
        reviewed = self._peek_reviewed_lines()
        if not header_fields or not reviewed:
            return None
        carrier_scac, ship_type, shipment_terms, delivery_service = header_fields
        if ship_type not in VALID_SHIP_TYPES:
            ship_type = config.SYNAPSE_SHIP_TYPE
        try:
            return session.order.build_synapse_payload(
                header=SynapseHeaderOverrides(
                    carrier_scac=carrier_scac,
                    ship_type=ship_type,
                    shipment_terms=shipment_terms,
                    delivery_service=delivery_service,
                ),
                details=[
                    SynapseDetailLine(
                        item=r["item"],
                        send_uom=r["send_uom"],
                        send_qty=r["send_qty"],
                        lot_number=r.get("lot_number", ""),
                        dtl_pass_thru_num_10=r.get("dtl_pass_thru_num_10"),
                    )
                    for r in reviewed
                ],
                instructions=str(self.instructions_var.get() or "").strip(),
                normalize_lot=_normalize_lot_number,
            )
        except ValueError:
            return None

    def _read_order_header_fields(self) -> tuple[str, str, str, str] | None:
        scac = (self.scac_var.get() or "").strip()
        if not scac:
            messagebox.showerror("Missing SCAC", "Enter a SCAC value.")
            return None
        ship_type = ship_type_code_from_label(self.ship_type_var.get() or "")
        if ship_type not in VALID_SHIP_TYPES:
            messagebox.showerror("Invalid ship type", "Ship Type must be one of A, C, L, P, R, S, T.")
            return None
        shipment_terms = shipment_terms_code_from_label(self.shipment_terms_var.get() or "")
        if not shipment_terms:
            messagebox.showerror("Missing shipment terms", "Select shipment terms.")
            return None
        delivery_service = (
            delivery_service_code_from_label(self.delivery_service_var.get() or "", self._delivery_service_options)
            if self._delivery_service_options
            else ""
        )
        return scac, ship_type, shipment_terms, delivery_service

    def _collect_reviewed_lines_for_send(self) -> list[dict] | None:
        out: list[dict] = []
        for r in self._review_rows:
            noted_qty = r.get("noted_qty")
            qty = noted_qty if noted_qty is not None else r.get("send_qty")
            if qty is None:
                messagebox.showerror(
                    "Missing qty",
                    f"Missing qty for item {r['item']}. Set Send Qty or Fallback Qty.",
                )
                return None
            normalized_lot = _normalize_lot_number(r.get("lot_number") or "")
            if config.SYNAPSE_REQUIRE_LOT and not normalized_lot:
                messagebox.showerror(
                    "Missing lot number",
                    f"Lot number is required for item {r['item']}. Double-click the Lot # cell to enter it.",
                )
                return None
            out.append(
                {
                    "item": r["item"],
                    "send_uom": r["send_uom"],
                    "send_qty": qty,
                    "lot_number": normalized_lot,
                    "dtl_pass_thru_num_10": r.get("dtl_pass_thru_num_10"),
                }
            )
        return out

    def _update_raw_btn_state(self):
        session = self.app.current_order
        draft_payload = self._try_build_draft_synapse_payload()
        has_data = bool(session and session.has_raw_display_data(draft_payload))
        self.toggle_raw_btn.config(state="normal" if has_data else "disabled")

    def _toggle_raw(self):
        if self._raw_visible:
            self._hide_raw()
        else:
            self._show_raw()

    def _raw_bundle(self) -> dict:
        session = self.app.current_order
        if not session:
            return {}
        return session.debug_bundle(draft_payload=self._try_build_draft_synapse_payload())

    def _refresh_raw_text(self):
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", json.dumps(self._raw_bundle(), indent=2, default=str))

    def _show_raw(self):
        session = self.app.current_order
        if not session:
            return
        draft_payload = self._try_build_draft_synapse_payload()
        if not session.has_raw_display_data(draft_payload):
            return
        self.raw_text.pack(fill="both", expand=True)
        self._refresh_raw_text()
        self.toggle_raw_btn.config(text="Hide payload")
        self._raw_visible = True

    def _hide_raw(self):
        self.raw_text.pack_forget()
        self.toggle_raw_btn.config(text="Show payload")
        self._raw_visible = False


if __name__ == "__main__":
    app = App()
    app.mainloop()
