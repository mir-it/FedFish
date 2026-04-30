import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date, timedelta, datetime

import config
import queries
from fishbowl_client import FishbowlClient, FishbowlError
from fedex_client import FedexClient, FedexError
from synapse_client import SynapseClient, SynapseConfig
from uom_conversion import load_coverage_map_from_csv, normalize_uom, suggest_each_qty


def _row_get_any(row: dict | None, *keys: str):
    if not row:
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        k = key.lower()
        if k in lowered and lowered[k] not in (None, ""):
            return lowered[k]
    return None


def _to_boolish(value) -> bool:
    s = str(value or "").strip().upper()
    return s in {"Y", "YES", "TRUE", "1", "T"}


def _parse_fb_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_terms(raw: str) -> str:
    s = (raw or "").strip()
    u = s.upper().replace("-", "").replace(" ", "")
    if u in {"3RD", "3RDPARTY", "THIRDPARTY", "THIRDPARTYBILL"}:
        return "3RD"
    return s.upper() if s else ""


def _normalize_country(raw) -> str:
    c = str(raw or "").strip().upper()
    if not c:
        return ""
    return "USA" if c in {"US", "USA"} else c


def _scac_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    scac_map = {
        "will advise": "9999",
        "freight": "FXSW",
        "daylight": "DYLT",
        "estes express lines": "EXLA",
        "customer's own carrier": "CSPU",
        "fedex": "FEDM",
        "ups": "UPSM",
        "usps": "UPSN",
    }
    return scac_map.get(name, "")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fishbowl → FedEx Freight")
        self.geometry("1000x650")
        self.minsize(900, 600)

        self.fb: FishbowlClient | None = None
        self.fedex = FedexClient(
            client_id=config.FEDEX_CLIENT_ID,
            client_secret=config.FEDEX_CLIENT_SECRET,
            account_number=config.FEDEX_ACCOUNT_NUMBER,
            base_url=config.FEDEX_BASE_URL,
        )

        self.shipments_by_num: dict[str, list[dict]] = {}
        self.current_ship_num: str | None = None
        self.current_items: list[dict] = []
        self.current_order_context: dict = {}
        self.synapse_sent_shipments: set[str] = set()
        self.synapse_failed_shipments: dict[str, str] = {}

        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken")
        status.pack(side="bottom", fill="x")

        self.frames: dict[str, ttk.Frame] = {}
        for name, cls in [
            ("login", LoginFrame),
            ("shipments", ShipmentsFrame),
            ("detail", PalletDetailFrame),
        ]:
            frame = cls(self.container, self)
            self.frames[name] = frame
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.show("login")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def show(self, name: str):
        frame = self.frames[name]
        frame.tkraise()
        if hasattr(frame, "on_show"):
            frame.on_show()

    def set_status(self, text: str):
        self.status_var.set(text)

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
                if on_error:
                    on_error(payload)
                else:
                    messagebox.showerror("Error", str(payload))
                    self.set_status(f"Error: {payload}")

        self.after(100, poll)

    def _on_close(self):
        if self.fb and self.fb.token:
            try:
                self.fb.logout()
            except Exception:
                pass
        self.destroy()


class LoginFrame(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        wrapper = ttk.Frame(self, padding=40)
        wrapper.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(wrapper, text="Fishbowl Server Login", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 20)
        )

        self.host = tk.StringVar(value=config.FB_HOST)
        self.port = tk.StringVar(value=config.FB_PORT)
        self.user = tk.StringVar(value=config.FB_USERNAME)
        self.pw = tk.StringVar(value=config.FB_PASSWORD)

        rows = [
            ("Host", self.host, False),
            ("Port", self.port, False),
            ("Username", self.user, False),
            ("Password", self.pw, True),
        ]
        for i, (label, var, secret) in enumerate(rows, start=1):
            ttk.Label(wrapper, text=label).grid(row=i, column=0, sticky="e", padx=(0, 10), pady=4)
            entry = ttk.Entry(wrapper, textvariable=var, width=35, show="*" if secret else "")
            entry.grid(row=i, column=1, pady=4)

        self.connect_btn = ttk.Button(wrapper, text="Connect", command=self._connect)
        self.connect_btn.grid(row=len(rows) + 1, column=0, columnspan=2, pady=(20, 0))

        self.msg = ttk.Label(wrapper, text="", foreground="red")
        self.msg.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(10, 0))

    def _connect(self):
        host = self.host.get().strip()
        port = self.port.get().strip()
        user = self.user.get().strip()
        pw = self.pw.get()
        if not all([host, port, user, pw]):
            self.msg.config(text="All fields required.")
            return
        self.msg.config(text="")
        self.connect_btn.config(state="disabled")
        self.app.set_status("Connecting to Fishbowl...")

        client = FishbowlClient(host, port)

        def do_login():
            client.login(user, pw)
            return client

        def ok(c):
            self.app.fb = c
            self.connect_btn.config(state="normal")
            self.app.set_status("Connected.")
            self.app.show("shipments")

        def err(e):
            self.connect_btn.config(state="normal")
            self.msg.config(text=str(e))
            self.app.set_status("Login failed.")

        self.app.run_async(do_login, ok, err)


class ShipmentsFrame(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Packed Shipments", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Send All", command=self._send_all_synapse).pack(side="right")
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="Logout", command=self._logout).pack(side="right", padx=(0, 8))

        cols = ("ship_num", "city", "state", "zip", "pallets", "total_weight", "synapse_status")
        headers = ("Ship #", "City", "State", "Zip", "Pallets", "Total Weight (lb)", "Synapse")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=130, anchor="w")
        self.tree.column("synapse_status", width=90, anchor="center")
        self.tree.tag_configure("synapse_sent", background="#dff0d8")
        self.tree.tag_configure("synapse_failed", background="#f8d7da")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Double-1>", lambda e: self._view_selected())

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="View Pallets →", command=self._view_selected).pack(side="right")

    def on_show(self):
        if not self.tree.get_children():
            self.refresh()

    def refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.app.set_status("Loading shipments...")

        def do():
            return self.app.fb.data_query(queries.SHIPMENTS_SQL)

        def ok(rows):
            grouped: dict[str, list[dict]] = {}
            for r in rows:
                grouped.setdefault(r["ship_num"], []).append(r)
            self.app.shipments_by_num = grouped
            for num, pallets in grouped.items():
                first = pallets[0]
                total_w = sum(float(p.get("weight") or 0) for p in pallets)
                self.tree.insert(
                    "",
                    "end",
                    iid=num,
                    values=(
                        num,
                        first.get("city", ""),
                        first.get("state", ""),
                        first.get("zip", ""),
                        len(pallets),
                        f"{total_w:.1f}",
                        "SENT" if num in self.app.synapse_sent_shipments else ("FAILED" if num in self.app.synapse_failed_shipments else ""),
                    ),
                    tags=(
                        ("synapse_sent",)
                        if num in self.app.synapse_sent_shipments
                        else (("synapse_failed",) if num in self.app.synapse_failed_shipments else ())
                    ),
                )
            self.app.set_status(f"Loaded {len(grouped)} shipments.")

        self.app.run_async(do, ok)

    def mark_synapse_sent(self, ship_num: str):
        self.app.synapse_sent_shipments.add(ship_num)
        self.app.synapse_failed_shipments.pop(ship_num, None)
        if not ship_num or not self.tree.exists(ship_num):
            return
        current_values = list(self.tree.item(ship_num, "values") or ())
        if len(current_values) < 7:
            current_values += [""] * (7 - len(current_values))
        current_values[6] = "SENT"
        self.tree.item(ship_num, values=tuple(current_values), tags=("synapse_sent",))

    def mark_synapse_failed(self, ship_num: str, reason: str):
        self.app.synapse_failed_shipments[ship_num] = reason
        self.app.synapse_sent_shipments.discard(ship_num)
        if not ship_num or not self.tree.exists(ship_num):
            return
        current_values = list(self.tree.item(ship_num, "values") or ())
        if len(current_values) < 7:
            current_values += [""] * (7 - len(current_values))
        current_values[6] = "FAILED"
        self.tree.item(ship_num, values=tuple(current_values), tags=("synapse_failed",))

    def _send_all_synapse(self):
        if not config.SYNAPSE_USERNAME or not config.SYNAPSE_PASSWORD:
            messagebox.showerror(
                "Synapse not configured",
                "Set SYNAPSE_USERNAME and SYNAPSE_PASSWORD in your .env (and restart the app).",
            )
            return
        ship_nums = list(self.app.shipments_by_num.keys())
        if not ship_nums:
            messagebox.showinfo("No shipments", "No packed shipments available to send.")
            return

        detail_frame = self.app.frames.get("detail")
        if not isinstance(detail_frame, PalletDetailFrame):
            messagebox.showerror("Internal error", "Detail frame unavailable.")
            return

        self.app.set_status(f"Sending {len(ship_nums)} shipments to Synapse...")

        def do():
            client = SynapseClient(
                SynapseConfig(
                    base_url=config.SYNAPSE_BASE_URL,
                    username=config.SYNAPSE_USERNAME,
                    password=config.SYNAPSE_PASSWORD,
                )
            )
            client.login()
            results: list[dict] = []
            for ship_num in ship_nums:
                try:
                    rows = self.app.fb.data_query(queries.items_sql_for(ship_num))
                    ctx = detail_frame._load_order_context(ship_num)
                    review_lines = detail_frame._build_review_lines(rows)
                    reviewed = detail_frame._auto_review_synapse_lines(review_lines)
                    carrier_name = next((str(r.get("carrier_name") or "").strip() for r in rows if str(r.get("carrier_name") or "").strip()), "")
                    scac = _scac_from_carrier_name(carrier_name) or config.SYNAPSE_CARRIER
                    order_data = detail_frame._build_order_data(rows, ctx, reviewed, scac)
                    client.create_order(order_data)
                    results.append({"ship_num": ship_num, "ok": True})
                except Exception as e:
                    results.append({"ship_num": ship_num, "ok": False, "error": str(e)})
            return results

        def ok(results):
            sent = 0
            failed = 0
            for r in results:
                num = str(r.get("ship_num") or "")
                if r.get("ok"):
                    sent += 1
                    self.mark_synapse_sent(num)
                else:
                    failed += 1
                    self.mark_synapse_failed(num, str(r.get("error") or "Unknown error"))
            self.app.set_status(f"Send all complete: {sent} sent, {failed} failed.")
            messagebox.showinfo("Send All complete", f"Sent: {sent}\nFailed: {failed}")

        def err(e):
            messagebox.showerror("Send All error", str(e))
            self.app.set_status("Send all failed.")

        self.app.run_async(do, ok, err)

    def _view_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select shipment", "Pick a shipment row first.")
            return
        self.app.current_ship_num = sel[0]
        self.app.show("detail")

    def _logout(self):
        if self.app.fb:
            try:
                self.app.fb.logout()
            except Exception:
                pass
            self.app.fb = None
        self.tree.delete(*self.tree.get_children())
        self.app.shipments_by_num = {}
        self.app.synapse_sent_shipments = set()
        self.app.synapse_failed_shipments = {}
        self.app.show("login")


class PalletDetailFrame(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        self.title_var = tk.StringVar()
        ttk.Label(header, textvariable=self.title_var, font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(header, text="← Back", command=lambda: self.app.show("shipments")).pack(side="right")

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)

        pallet_frame = ttk.LabelFrame(mid, text="Pallets", padding=5)
        pallet_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        pcols = ("n", "weight", "len", "width", "height")
        pheaders = ("#", "Weight", "Len", "Width", "Height")
        self.pallet_tree = ttk.Treeview(pallet_frame, columns=pcols, show="headings", height=10)
        for c, h in zip(pcols, pheaders):
            self.pallet_tree.heading(c, text=h)
            self.pallet_tree.column(c, width=80, anchor="w")
        self.pallet_tree.pack(fill="both", expand=True)

        item_frame = ttk.LabelFrame(mid, text="Items", padding=5)
        item_frame.pack(side="left", fill="both", expand=True, padx=(5, 0))
        icols = ("item", "qty", "uom", "tracking", "carrier", "po")
        iheaders = ("Item #", "Qty", "UOM", "Tracking", "Carrier", "PO")
        self.item_tree = ttk.Treeview(item_frame, columns=icols, show="headings", height=10)
        for c, h in zip(icols, iheaders):
            self.item_tree.heading(c, text=h)
            self.item_tree.column(c, width=100, anchor="w")
        self.item_tree.column("tracking", width=160, anchor="w")
        self.item_tree.column("carrier", width=130, anchor="w")
        self.item_tree.pack(fill="both", expand=True)

        actions = ttk.Frame(self, padding=10)
        actions.pack(fill="x")
        self.quote_btn = ttk.Button(actions, text="Get FedEx Rates", command=self._get_rates)
        self.quote_btn.pack(side="left")
        self.synapse_btn = ttk.Button(actions, text="Create Synapse Order", command=self._create_synapse_order)
        self.synapse_btn.pack(side="left", padx=8)
        self.toggle_raw_btn = ttk.Button(actions, text="Show raw response", command=self._toggle_raw, state="disabled")
        self.toggle_raw_btn.pack(side="left", padx=8)

        self.rates_frame = ttk.LabelFrame(self, text="FedEx Rates", padding=5)
        self.rates_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        rcols = ("service", "transit", "charge", "currency")
        rheaders = ("Service", "Transit", "Net Charge", "Currency")
        self.rates_tree = ttk.Treeview(self.rates_frame, columns=rcols, show="headings", height=6)
        for c, h in zip(rcols, rheaders):
            self.rates_tree.heading(c, text=h)
            self.rates_tree.column(c, width=150, anchor="w")
        self.rates_tree.pack(fill="both", expand=True)

        self.raw_text = tk.Text(self.rates_frame, height=10, wrap="none")
        self._raw_visible = False
        self._last_response: dict | None = None
        self._last_synapse_response: dict | None = None
        self._last_synapse_payload: dict | None = None

    def on_show(self):
        num = self.app.current_ship_num
        if not num:
            return
        pallets = self.app.shipments_by_num.get(num, [])
        first = pallets[0] if pallets else {}
        self.title_var.set(
            f"Ship #{num}  →  {first.get('city', '')}, {first.get('state', '')}  {first.get('zip', '')}"
        )

        self.pallet_tree.delete(*self.pallet_tree.get_children())
        for i, p in enumerate(pallets, start=1):
            self.pallet_tree.insert(
                "", "end",
                values=(
                    i,
                    p.get("weight", ""),
                    p.get("length", ""),
                    p.get("width", ""),
                    p.get("height", ""),
                ),
            )

        self.item_tree.delete(*self.item_tree.get_children())
        self.rates_tree.delete(*self.rates_tree.get_children())
        self.toggle_raw_btn.config(state="disabled")
        self._hide_raw()
        self._last_response = None
        self._last_synapse_response = None
        self._last_synapse_payload = None
        self.app.current_order_context = {}

        self.app.set_status(f"Loading items for {num}...")

        def do():
            rows = self.app.fb.data_query(queries.items_sql_for(num))
            ctx = self._load_order_context(num)
            return rows, ctx

        def ok(payload):
            rows, ctx = payload
            self.app.current_items = rows
            self.app.current_order_context = ctx
            for r in rows:
                item_num = str(r.get("item_num", "") or "")
                if self._should_exclude_item(item_num):
                    continue
                self.item_tree.insert(
                    "", "end",
                    values=(
                        r.get("item_num", ""),
                        r.get("qty", ""),
                        r.get("uom", ""),
                        r.get("tracking", ""),
                        r.get("carrier_name", ""),
                        r.get("po_number", ""),
                    ),
                )
            self.app.set_status(f"Shipment {num}: {len(pallets)} pallets, {len(rows)} items.")

        self.app.run_async(do, ok)

    def _query_first(self, sql: str) -> dict:
        try:
            rows = self.app.fb.data_query(sql)
        except Exception:
            return {}
        return rows[0] if rows else {}

    def _state_code_from_id(self, state_id) -> str:
        try:
            state_int = int(str(state_id).strip())
        except (TypeError, ValueError):
            return ""
        row = self._query_first(queries.state_code_sql_for(state_int))
        return str(_row_get_any(row, "code") or "").strip()

    def _load_order_context(self, ship_num: str) -> dict:
        ship_row = self._query_first(queries.ship_sql_for(ship_num))
        so_row = self._query_first(queries.so_sql_for(ship_num))
        customer_row = {}
        customer_id = _row_get_any(so_row, "customerId", "customerid", "customer")
        if customer_id not in (None, ""):
            try:
                customer_row = self._query_first(queries.customer_sql_for(int(customer_id)))
            except Exception:
                customer_row = {}
        return {
            "ship": ship_row,
            "so": so_row,
            "customer": customer_row,
        }

    def _build_review_lines(self, rows: list[dict]) -> list[dict]:
        aggregated: dict[tuple[str, str, str], float] = {}
        for r in rows:
            item_num = str(r.get("item_num") or "").strip()
            if self._should_exclude_item(item_num):
                continue
            tracking = str(r.get("tracking") or "").strip()
            uom = normalize_uom(str(r.get("uom") or ""))
            qty = r.get("qty") or 0
            if not item_num or not uom:
                continue
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            aggregated[(item_num, uom, tracking)] = aggregated.get((item_num, uom, tracking), 0.0) + qty_f
        review_lines: list[dict] = []
        for (item_num, uom, tracking), qty in aggregated.items():
            if qty:
                review_lines.append(
                    {"item": item_num, "tracking": tracking, "fb_uom": uom, "fb_qty": qty}
                )
        return review_lines

    def _auto_review_synapse_lines(self, lines: list[dict]) -> list[dict]:
        coverage_map = {}
        if config.PRODUCT_COVERAGE_CSV:
            try:
                coverage_map = load_coverage_map_from_csv(config.PRODUCT_COVERAGE_CSV)
            except Exception:
                coverage_map = {}

        out: list[dict] = []
        for l in lines:
            item = l["item"]
            item_key = item.upper()
            fb_uom = normalize_uom(l.get("fb_uom", ""))
            fb_qty = float(l.get("fb_qty") or 0)
            cov = coverage_map.get(item_key).coverage_sf_per_ea if item_key in coverage_map else None

            send_uom = "EA"
            send_qty = None
            if fb_uom == "SF" and cov:
                send_qty, _ = suggest_each_qty(fb_qty, cov)
            elif fb_uom in {"EA", "PCS"}:
                send_uom = "EA"
                send_qty = int(fb_qty) if float(fb_qty).is_integer() else None
            elif fb_uom == "BOX":
                send_uom = "BOX"
                send_qty = int(fb_qty) if float(fb_qty).is_integer() else None

            if send_qty is None:
                raise ValueError(f"Missing Send Qty for item {item}.")
            lot_number = str(l.get("tracking") or "").strip()
            if config.SYNAPSE_REQUIRE_LOT and not lot_number:
                raise ValueError(f"Lot number is required for item {item}.")
            out.append(
                {
                    "item": item,
                    "send_uom": send_uom,
                    "send_qty": int(send_qty),
                    "lot_number": lot_number,
                }
            )
        return out

    def _build_order_data(self, rows: list[dict], ctx: dict, reviewed: list[dict], carrier: str) -> dict:
        if not rows:
            raise ValueError("No items are loaded for this shipment yet.")
        first = rows[0]
        ship_row = ctx.get("ship", {}) or {}
        so_row = ctx.get("so", {}) or {}
        customer_row = ctx.get("customer", {}) or {}
        po_number = str(first.get("po_number") or "").strip()
        if not po_number:
            raise ValueError("This shipment has no PO number in Fishbowl.")

        ship_to_name = str(_row_get_any(ship_row, "shipToName") or first.get("ship_to_name") or "").strip() or "SHIP TO"
        ship_to_name = ship_to_name[:40]
        ship_to_address_1 = str(_row_get_any(ship_row, "shipToAddress", "shipToAddress1") or first.get("address_1") or "").strip()
        ship_to_city = str(_row_get_any(ship_row, "shipToCity") or first.get("city") or "").strip()
        ship_to_state = str(_row_get_any(ship_row, "shipToState", "shipToStateId") or first.get("state") or "").strip()
        ship_to_postal_code = str(_row_get_any(ship_row, "shipToZip", "shipToPostalCode") or first.get("zip") or "").strip()
        if not all([ship_to_address_1, ship_to_city, ship_to_state, ship_to_postal_code]):
            raise ValueError("Fishbowl is missing one or more ship-to fields (address/city/state/zip) for this shipment.")

        details = [
            {
                "item": r["item"],
                "uom_entered": r["send_uom"],
                "qty_entered": r["send_qty"],
                "inventory_status": config.SYNAPSE_INVENTORY_STATUS,
                "lot_number": r.get("lot_number", ""),
            }
            for r in reviewed
        ]
        if not details:
            raise ValueError("Could not build any detail lines from Fishbowl items.")

        ship_date = (
            _parse_fb_date(_row_get_any(ship_row, "dateCreated", "dateLastModified"))
            or _parse_fb_date(_row_get_any(so_row, "dateCreated", "dateIssued", "dateCompleted"))
            or date.today()
        )
        requested_ship = _parse_fb_date(_row_get_any(so_row, "dateScheduledFulfillment", "dateFirstShip", "dateNeeded")) or ship_date
        ship_no_later = _parse_fb_date(_row_get_any(so_row, "dateLastFulfillment", "dateDue", "dateExpiration")) or requested_ship
        cancel_after = _parse_fb_date(_row_get_any(so_row, "dateExpiration", "dateExpires", "dateCompleted")) or (
            ship_date + timedelta(days=max(config.SYNAPSE_CANCEL_AFTER_DAYS, 0))
        )

        shipment_terms = _normalize_terms(
            str(_row_get_any(so_row, "shipmentTerms", "shipTerms", "freightTerms", "termCode") or "")
        )
        if not shipment_terms:
            third_party_flag = _to_boolish(_row_get_any(so_row, "isThirdParty", "thirdPartyBilling", "thirdParty"))
            shipment_terms = "3RD" if third_party_flag else (config.SYNAPSE_SHIPMENT_TERMS or "").strip().upper()

        bill_to_name = str(_row_get_any(so_row, "billToName") or _row_get_any(customer_row, "name") or config.SYNAPSE_BILLTO_NAME or "").strip()[:40]
        bill_to_address_1 = str(_row_get_any(so_row, "billToAddress", "billToAddress1") or config.SYNAPSE_BILLTO_ADDRESS_1 or "").strip()
        bill_to_city = str(_row_get_any(so_row, "billToCity") or config.SYNAPSE_BILLTO_CITY or "").strip()
        bill_to_state = str(_row_get_any(so_row, "billToState") or "").strip()
        if not bill_to_state:
            bill_to_state_id = _row_get_any(so_row, "billToStateId")
            if bill_to_state_id not in (None, ""):
                bill_to_state = self._state_code_from_id(bill_to_state_id)
        if not bill_to_state:
            bill_to_state = str(_row_get_any(customer_row, "state") or "").strip()
        if not bill_to_state:
            bill_to_state = str(config.SYNAPSE_BILLTO_STATE or "").strip()
        bill_to_postal_code = str(_row_get_any(so_row, "billToZip", "billToPostalCode") or config.SYNAPSE_BILLTO_POSTAL_CODE or "").strip()
        bill_to_country_code = _normalize_country(
            _row_get_any(so_row, "billToCountry", "billToCountryCode") or config.SYNAPSE_BILLTO_COUNTRY_CODE or "USA"
        )

        order_data = {
            "header": {
                "func": "A",
                "custid": config.SYNAPSE_CUSTID,
                "po_number": po_number,
                "order_type": "O",
                "reference": po_number,
                "from_facility": config.SYNAPSE_FROM_FACILITY,
                "to_facility": config.SYNAPSE_TO_FACILITY,
                "carrier": carrier,
                "ship_type": config.SYNAPSE_SHIP_TYPE,
                "shipment_terms": shipment_terms or config.SYNAPSE_SHIPMENT_TERMS,
                "ship_date": ship_date,
                "appointment_date": requested_ship,
                "requested_ship": requested_ship,
                "ship_not_before": requested_ship,
                "ship_no_later": ship_no_later,
                "cancel_after": cancel_after,
                "shipper_name": config.SHIPPER_NAME,
                "shipper_address_1": config.SHIPPER_ADDRESS_1,
                "shipper_city": config.SHIPPER_CITY,
                "shipper_state": config.SHIPPER_STATE,
                "shipper_postal_code": config.SHIPPER_ZIP,
                "shipper_country_code": _normalize_country(config.SHIPPER_COUNTRY),
                "ship_to_name": ship_to_name,
                "ship_to_address_1": ship_to_address_1,
                "ship_to_city": ship_to_city,
                "ship_to_state": ship_to_state,
                "ship_to_postal_code": ship_to_postal_code,
                "ship_to_country_code": "USA",
            },
            "details": details,
        }
        if (shipment_terms or config.SYNAPSE_SHIPMENT_TERMS or "").strip().upper() == "3RD":
            order_data["header"].update(
                {
                    "bill_to_name": bill_to_name,
                    "bill_to_address_1": bill_to_address_1,
                    "bill_to_city": bill_to_city,
                    "bill_to_state": bill_to_state,
                    "bill_to_postal_code": bill_to_postal_code,
                    "bill_to_country_code": bill_to_country_code,
                }
            )
        return order_data

    def _create_synapse_order(self):
        if not config.SYNAPSE_USERNAME or not config.SYNAPSE_PASSWORD:
            messagebox.showerror(
                "Synapse not configured",
                "Set SYNAPSE_USERNAME and SYNAPSE_PASSWORD in your .env (and restart the app).",
            )
            return

        if not self.app.current_items:
            messagebox.showwarning("No items", "No items are loaded for this shipment yet.")
            return

        first = self.app.current_items[0]
        ship_num = self.app.current_ship_num
        ctx = self.app.current_order_context or {}
        if ship_num and not ctx:
            ctx = self._load_order_context(ship_num)
            self.app.current_order_context = ctx
        ship_row = ctx.get("ship", {}) or {}
        so_row = ctx.get("so", {}) or {}
        customer_row = ctx.get("customer", {}) or {}
        po_number = str(first.get("po_number") or "").strip()
        if not po_number:
            messagebox.showerror("Missing PO", "This shipment has no PO number in Fishbowl.")
            return

        ship_to_name = str(
            _row_get_any(ship_row, "shipToName") or first.get("ship_to_name") or ""
        ).strip() or "SHIP TO"
        ship_to_name = ship_to_name[:40]
        ship_to_address_1 = str(
            _row_get_any(ship_row, "shipToAddress", "shipToAddress1") or first.get("address_1") or ""
        ).strip()
        ship_to_city = str(_row_get_any(ship_row, "shipToCity") or first.get("city") or "").strip()
        ship_to_state = str(_row_get_any(ship_row, "shipToState", "shipToStateId") or first.get("state") or "").strip()
        ship_to_postal_code = str(
            _row_get_any(ship_row, "shipToZip", "shipToPostalCode") or first.get("zip") or ""
        ).strip()
        if not all([ship_to_address_1, ship_to_city, ship_to_state, ship_to_postal_code]):
            messagebox.showerror(
                "Missing Ship-To",
                "Fishbowl is missing one or more ship-to fields (address/city/state/zip) for this shipment.",
            )
            return

        # Aggregate duplicate item lines (same item + uom).
        aggregated: dict[tuple[str, str, str], float] = {}
        for r in self.app.current_items:
            item_num = str(r.get("item_num") or "").strip()
            if self._should_exclude_item(item_num):
                continue
            tracking = str(r.get("tracking") or "").strip()
            uom = normalize_uom(str(r.get("uom") or ""))
            qty = r.get("qty") or 0
            if not item_num or not uom:
                continue
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            aggregated[(item_num, uom, tracking)] = aggregated.get((item_num, uom, tracking), 0.0) + qty_f

        review_lines: list[dict] = []
        for (item_num, uom, tracking), qty in aggregated.items():
            if qty:
                review_lines.append(
                    {
                        "item": item_num,
                        "tracking": tracking,
                        "fb_uom": uom,
                        "fb_qty": qty,
                    }
                )

        reviewed_payload = self._review_synapse_lines(review_lines)
        if reviewed_payload is None:
            self.app.set_status("Synapse order cancelled.")
            return
        reviewed, carrier = reviewed_payload

        details = [
            {
                "item": r["item"],
                "uom_entered": r["send_uom"],
                "qty_entered": r["send_qty"],
                "inventory_status": config.SYNAPSE_INVENTORY_STATUS,
                "lot_number": r.get("lot_number", ""),
            }
            for r in reviewed
        ]
        if not details:
            messagebox.showerror("No order lines", "Could not build any detail lines from Fishbowl items.")
            return

        # Date windows used by many warehouses for allocation.
        ship_date = (
            _parse_fb_date(_row_get_any(ship_row, "dateCreated", "dateLastModified"))
            or _parse_fb_date(_row_get_any(so_row, "dateCreated", "dateIssued", "dateCompleted"))
            or date.today()
        )
        requested_ship = _parse_fb_date(
            _row_get_any(so_row, "dateScheduledFulfillment", "dateFirstShip", "dateNeeded")
        ) or ship_date
        ship_no_later = _parse_fb_date(
            _row_get_any(so_row, "dateLastFulfillment", "dateDue", "dateExpiration")
        ) or requested_ship
        cancel_after = _parse_fb_date(
            _row_get_any(so_row, "dateExpiration", "dateExpires", "dateCompleted")
        ) or (ship_date + timedelta(days=max(config.SYNAPSE_CANCEL_AFTER_DAYS, 0)))

        shipment_terms = _normalize_terms(
            str(
                _row_get_any(
                    so_row,
                    "shipmentTerms",
                    "shipTerms",
                    "freightTerms",
                    "termCode",
                )
                or ""
            )
        )
        if not shipment_terms:
            third_party_flag = _to_boolish(
                _row_get_any(so_row, "isThirdParty", "thirdPartyBilling", "thirdParty")
            )
            shipment_terms = "3RD" if third_party_flag else (config.SYNAPSE_SHIPMENT_TERMS or "").strip().upper()
        bill_to_name = str(
            _row_get_any(so_row, "billToName")
            or _row_get_any(customer_row, "name")
            or config.SYNAPSE_BILLTO_NAME
            or ""
        ).strip()[:40]
        bill_to_address_1 = str(
            _row_get_any(so_row, "billToAddress", "billToAddress1")
            or config.SYNAPSE_BILLTO_ADDRESS_1
            or ""
        ).strip()
        bill_to_city = str(_row_get_any(so_row, "billToCity") or config.SYNAPSE_BILLTO_CITY or "").strip()
        bill_to_state = str(_row_get_any(so_row, "billToState") or "").strip()
        if not bill_to_state:
            bill_to_state_id = _row_get_any(so_row, "billToStateId")
            if bill_to_state_id not in (None, ""):
                bill_to_state = self._state_code_from_id(bill_to_state_id)
        if not bill_to_state:
            bill_to_state = str(_row_get_any(customer_row, "state") or "").strip()
        if not bill_to_state:
            bill_to_state = str(config.SYNAPSE_BILLTO_STATE or "").strip()
        bill_to_postal_code = str(
            _row_get_any(so_row, "billToZip", "billToPostalCode") or config.SYNAPSE_BILLTO_POSTAL_CODE or ""
        ).strip()
        bill_to_country_code = _normalize_country(
            _row_get_any(so_row, "billToCountry", "billToCountryCode")
            or config.SYNAPSE_BILLTO_COUNTRY_CODE
            or "USA"
        )

        order_data = {
            "header": {
                "func": "A",
                "custid": config.SYNAPSE_CUSTID,
                "po_number": po_number,
                "order_type": "O",
                "reference": po_number,
                "from_facility": config.SYNAPSE_FROM_FACILITY,
                "to_facility": config.SYNAPSE_TO_FACILITY,
                "carrier": carrier,
                "ship_type": config.SYNAPSE_SHIP_TYPE,
                "shipment_terms": shipment_terms or config.SYNAPSE_SHIPMENT_TERMS,
                "ship_date": ship_date,
                "appointment_date": requested_ship,
                "requested_ship": requested_ship,
                "ship_not_before": requested_ship,
                "ship_no_later": ship_no_later,
                "cancel_after": cancel_after,
                "shipper_name": config.SHIPPER_NAME,
                "shipper_address_1": config.SHIPPER_ADDRESS_1,
                "shipper_city": config.SHIPPER_CITY,
                "shipper_state": config.SHIPPER_STATE,
                "shipper_postal_code": config.SHIPPER_ZIP,
                "shipper_country_code": _normalize_country(config.SHIPPER_COUNTRY),
                "ship_to_name": ship_to_name,
                "ship_to_address_1": ship_to_address_1,
                "ship_to_city": ship_to_city,
                "ship_to_state": ship_to_state,
                "ship_to_postal_code": ship_to_postal_code,
                "ship_to_country_code": "USA",
            },
            "details": details,
        }

        # If shipment terms are 3rd party, attach bill-to info (account + address).
        if (shipment_terms or config.SYNAPSE_SHIPMENT_TERMS or "").strip().upper() == "3RD":
            order_data["header"].update(
                {
                    "bill_to_name": bill_to_name,
                    "bill_to_address_1": bill_to_address_1,
                    "bill_to_city": bill_to_city,
                    "bill_to_state": bill_to_state,
                    "bill_to_postal_code": bill_to_postal_code,
                    "bill_to_country_code": bill_to_country_code,
                }
            )

        self._last_synapse_payload = order_data
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
            return client.create_order(order_data)

        def ok(resp):
            self.synapse_btn.config(state="normal")
            self._last_synapse_response = resp
            self.toggle_raw_btn.config(state="normal")
            if self.app.current_ship_num:
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_sent(self.app.current_ship_num)
            self.app.set_status("Synapse order created.")
            messagebox.showinfo("Synapse", "Order created successfully in Synapse.")

        def err(e):
            self.synapse_btn.config(state="normal")
            self._last_synapse_response = {"error": str(e)}
            self.toggle_raw_btn.config(state="normal")
            if self.app.current_ship_num:
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_failed(self.app.current_ship_num, str(e))
            messagebox.showerror("Synapse error", str(e))
            self.app.set_status("Synapse order creation failed.")

        self.app.run_async(do, ok, err)

    def _should_exclude_item(self, item_num: str) -> bool:
        s = (item_num or "").strip().lower()
        if not s:
            return True
        keywords = [k.strip().lower() for k in (config.EXCLUDE_ITEM_KEYWORDS or "").split(",") if k.strip()]
        return any(k in s for k in keywords)

    def _review_synapse_lines(self, lines: list[dict]) -> tuple[list[dict], str] | None:
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
            elif fb_uom == "BOX":
                send_uom = "BOX"
                send_qty = int(fb_qty) if float(fb_qty).is_integer() else None
                if send_qty is None:
                    note = "Needs integer"
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
                    "lot_number": l.get("tracking", ""),
                    "note": note,
                }
            )

        carrier_name = str((self.app.current_items[0].get("carrier_name") if self.app.current_items else "") or "").strip()
        initial_scac = _scac_from_carrier_name(carrier_name)
        if not initial_scac:
            initial_scac = config.SYNAPSE_CARRIER
        dlg = SynapseLinesDialog(self, rows, initial_scac=initial_scac)
        self.wait_window(dlg)
        if dlg.result is None:
            return None
        return dlg.result, dlg.scac

    def _get_rates(self):
        num = self.app.current_ship_num
        pallets = self.app.shipments_by_num.get(num, [])
        if not pallets:
            messagebox.showwarning("No pallets", "This shipment has no pallets.")
            return
        first = pallets[0]

        shipper = {
            "city": config.SHIPPER_CITY,
            "stateOrProvinceCode": config.SHIPPER_STATE,
            "postalCode": config.SHIPPER_ZIP,
            "countryCode": config.SHIPPER_COUNTRY,
        }
        recipient = {
            "city": (first.get("city") or "").upper(),
            "stateOrProvinceCode": first.get("state", ""),
            "postalCode": str(first.get("zip", "")),
            "countryCode": "US",
        }

        payload = self.app.fedex.build_payload(shipper, recipient, pallets)

        self.quote_btn.config(state="disabled")
        self.app.set_status("Requesting FedEx rates...")
        self.rates_tree.delete(*self.rates_tree.get_children())

        def do():
            return self.app.fedex.rate_quote(payload)

        def ok(resp):
            self.quote_btn.config(state="normal")
            self._last_response = resp
            self.toggle_raw_btn.config(state="normal")
            rates = FedexClient.extract_rates(resp)
            if not rates:
                self.app.set_status("FedEx returned no rate options. Check raw response.")
            else:
                for r in rates:
                    self.rates_tree.insert(
                        "", "end",
                        values=(r["service"], r["transit"], r["net_charge"], r["currency"]),
                    )
                self.app.set_status(f"Received {len(rates)} rate options.")

        def err(e):
            self.quote_btn.config(state="normal")
            if isinstance(e, FedexError):
                self._last_response = {"error": str(e)}
                self.toggle_raw_btn.config(state="normal")
            messagebox.showerror("FedEx error", str(e))
            self.app.set_status("FedEx request failed.")

        self.app.run_async(do, ok, err)

    def _toggle_raw(self):
        if self._raw_visible:
            self._hide_raw()
        else:
            self._show_raw()

    def _show_raw(self):
        if self._last_response is None and self._last_synapse_response is None:
            return
        self.rates_tree.pack_forget()
        self.raw_text.pack(fill="both", expand=True)
        self.raw_text.delete("1.0", "end")
        if self._last_response is not None:
            # FedEx mode
            self.raw_text.insert("1.0", json.dumps(self._last_response, indent=2, default=str))
        else:
            # Synapse mode: show both request + response
            bundle = {
                "synapse_payload": self._last_synapse_payload,
                "synapse_response": self._last_synapse_response,
            }
            self.raw_text.insert("1.0", json.dumps(bundle, indent=2, default=str))
        self.toggle_raw_btn.config(text="Show rate table")
        self._raw_visible = True

    def _hide_raw(self):
        self.raw_text.pack_forget()
        self.rates_tree.pack(fill="both", expand=True)
        self.toggle_raw_btn.config(text="Show raw response")
        self._raw_visible = False


class SynapseLinesDialog(tk.Toplevel):
    def __init__(self, parent: ttk.Frame, rows: list[dict], initial_scac: str = ""):
        super().__init__(parent)
        self.title("Review Synapse Order Lines")
        self.geometry("900x420")
        self.resizable(True, True)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self.result: list[dict] | None = None
        self.scac: str = ""
        self._rows = rows

        ttk.Label(
            self,
            text="Review and adjust the quantities that will be sent to Synapse.\n"
            "Double-click Send Qty to edit. Qty must be an integer.",
            justify="left",
        ).pack(fill="x", padx=10, pady=(10, 6))
        scac_row = ttk.Frame(self)
        scac_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(scac_row, text="SCAC:").pack(side="left")
        self.scac_var = tk.StringVar(value=(initial_scac or "").strip())
        ttk.Entry(scac_row, textvariable=self.scac_var, width=16).pack(side="left", padx=(6, 0))

        cols = ("item", "fb_qty", "fb_uom", "coverage", "send_qty", "send_uom", "lot", "note")
        headers = ("Item", "FB Qty", "FB UOM", "SF per EA", "Send Qty", "Send UOM", "Lot #", "Note")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=120, anchor="w")
        self.tree.column("item", width=160)
        self.tree.column("note", width=170)
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for i, r in enumerate(self._rows):
            cov = r.get("coverage_sf_per_ea")
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    r["item"],
                    f"{r['fb_qty']:.3f}".rstrip("0").rstrip("."),
                    r["fb_uom"],
                    "" if cov is None else f"{cov:.3f}".rstrip("0").rstrip("."),
                    "" if r.get("send_qty") is None else str(r["send_qty"]),
                    r["send_uom"],
                    r.get("lot_number", ""),
                    r.get("note", ""),
                ),
            )

        self.tree.bind("<Double-1>", self._on_double_click)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Send to Synapse", command=self._ok).pack(side="right", padx=(0, 8))

    def _on_double_click(self, event):
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id:
            return
        idx = int(row_id)
        # #5 = send_qty, #7 = lot
        if col == "#5":
            current = self._rows[idx].get("send_qty")

            win = tk.Toplevel(self)
            win.title("Edit Send Qty")
            win.transient(self)
            win.grab_set()
            ttk.Label(win, text=f"{self._rows[idx]['item']} send qty (integer):").pack(padx=10, pady=(10, 4))
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
                    messagebox.showerror("Invalid qty", "Send Qty must be a positive integer.", parent=win)
                    return
                self._rows[idx]["send_qty"] = v
                self._refresh_row(idx)
                win.destroy()

            ttk.Button(win, text="Save", command=save).pack(padx=10, pady=(0, 10))
            win.bind("<Return>", lambda e: save())
            return

        if col == "#7":
            current_lot = self._rows[idx].get("lot_number", "")
            win = tk.Toplevel(self)
            win.title("Edit Lot Number")
            win.transient(self)
            win.grab_set()
            ttk.Label(win, text=f"{self._rows[idx]['item']} lot number:").pack(padx=10, pady=(10, 4))
            var = tk.StringVar(value=str(current_lot or ""))
            ent = ttk.Entry(win, textvariable=var, width=30)
            ent.pack(padx=10, pady=(0, 10))
            ent.focus_set()

            def save_lot():
                self._rows[idx]["lot_number"] = var.get().strip()
                self._refresh_row(idx)
                win.destroy()

            ttk.Button(win, text="Save", command=save_lot).pack(padx=10, pady=(0, 10))
            win.bind("<Return>", lambda e: save_lot())

    def _refresh_row(self, idx: int):
        r = self._rows[idx]
        cov = r.get("coverage_sf_per_ea")
        self.tree.item(
            str(idx),
            values=(
                r["item"],
                f"{r['fb_qty']:.3f}".rstrip("0").rstrip("."),
                r["fb_uom"],
                "" if cov is None else f"{cov:.3f}".rstrip("0").rstrip("."),
                "" if r.get("send_qty") is None else str(r["send_qty"]),
                r["send_uom"],
                r.get("lot_number", ""),
                r.get("note", ""),
            ),
        )

    def _ok(self):
        scac = (self.scac_var.get() or "").strip()
        if not scac:
            messagebox.showerror("Missing SCAC", "Enter a SCAC value.", parent=self)
            return
        out: list[dict] = []
        for r in self._rows:
            qty = r.get("send_qty")
            if qty is None:
                messagebox.showerror(
                    "Missing qty",
                    f"Missing Send Qty for item {r['item']}. Double-click the Send Qty cell to enter it.",
                    parent=self,
                )
                return
            if config.SYNAPSE_REQUIRE_LOT and not str(r.get("lot_number") or "").strip():
                messagebox.showerror(
                    "Missing lot number",
                    f"Lot number is required for item {r['item']}. Double-click the Lot # cell to enter it.",
                    parent=self,
                )
                return
            out.append(
                {
                    "item": r["item"],
                    "send_uom": r["send_uom"],
                    "send_qty": qty,
                    "lot_number": str(r.get("lot_number") or "").strip(),
                }
            )
        self.scac = scac
        self.result = out
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
