import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import config
import queries
from fishbowl_client import FishbowlClient, FishbowlError
from fedex_client import FedexClient, FedexError


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
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="Logout", command=self._logout).pack(side="right", padx=(0, 8))

        cols = ("ship_num", "city", "state", "zip", "pallets", "total_weight")
        headers = ("Ship #", "City", "State", "Zip", "Pallets", "Total Weight (lb)")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=130, anchor="w")
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
                    ),
                )
            self.app.set_status(f"Loaded {len(grouped)} shipments.")

        self.app.run_async(do, ok)

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
        icols = ("item", "qty", "uom", "po")
        iheaders = ("Item #", "Qty", "UOM", "PO")
        self.item_tree = ttk.Treeview(item_frame, columns=icols, show="headings", height=10)
        for c, h in zip(icols, iheaders):
            self.item_tree.heading(c, text=h)
            self.item_tree.column(c, width=100, anchor="w")
        self.item_tree.pack(fill="both", expand=True)

        actions = ttk.Frame(self, padding=10)
        actions.pack(fill="x")
        self.quote_btn = ttk.Button(actions, text="Get FedEx Rates", command=self._get_rates)
        self.quote_btn.pack(side="left")
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

        self.app.set_status(f"Loading items for {num}...")

        def do():
            return self.app.fb.data_query(queries.items_sql_for(num))

        def ok(rows):
            self.app.current_items = rows
            for r in rows:
                self.item_tree.insert(
                    "", "end",
                    values=(r.get("item_num", ""), r.get("qty", ""), r.get("uom", ""), r.get("po_number", "")),
                )
            self.app.set_status(f"Shipment {num}: {len(pallets)} pallets, {len(rows)} items.")

        self.app.run_async(do, ok)

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
        if self._last_response is None:
            return
        self.rates_tree.pack_forget()
        self.raw_text.pack(fill="both", expand=True)
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", json.dumps(self._last_response, indent=2))
        self.toggle_raw_btn.config(text="Show rate table")
        self._raw_visible = True

    def _hide_raw(self):
        self.raw_text.pack_forget()
        self.rates_tree.pack(fill="both", expand=True)
        self.toggle_raw_btn.config(text="Show raw response")
        self._raw_visible = False


if __name__ == "__main__":
    app = App()
    app.mainloop()
