import json
import queue
import re
import smtplib
import threading
import tkinter as tk
from email.message import EmailMessage
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from datetime import date, datetime

import config
import queries
from fishbowl_client import FishbowlClient, FishbowlError
from synapse_client import SynapseClient, SynapseConfig, SynapseCreateOrderError
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
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M:%S",
    ):
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


def _synapse_reference_from_ship_num(raw_ship_num) -> str:
    ship_num = str(raw_ship_num or "").strip()
    if not ship_num:
        return ""
    if ship_num[:1].upper() == "S":
        return ship_num[1:].strip()
    return ship_num


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


def _scac_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    scac_map = {
        "will advise": "9999",
        "freight": "FXSW",
        "daylight": "DYLT",
        "estes express lines": "EXLA",
        "customer's own carrier": "CSPU",
        "ups": "UPSM",
        "usps": "UPSN",
        'fedex': "FEDM",
        'aaa cooper': "AACT"
    }
    return scac_map.get(name, "")


def _normalize_carrier_name(raw_name: str) -> str:
    return " ".join(str(raw_name or "").strip().lower().split())


# Carrier-name keywords that should force shipment terms to 3RD (third party collect).
SHIPMENT_TERMS_3RD_CARRIER_KEYWORDS: tuple[str, ...] = ("fedex", "ups")


def _shipment_terms_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ""
    if any(k in name for k in SHIPMENT_TERMS_3RD_CARRIER_KEYWORDS):
        return "3RD"
    return ""


def _is_ups_carrier(raw_name: str) -> bool:
    # Word-boundary match so "ups" matches UPS / "UPS Ground" but not "USPS".
    return bool(re.search(r"\bups\b", str(raw_name or "").strip().lower()))


SHIP_TYPE_BY_CARRIER_NAME_KEYWORDS: dict[str, tuple[str, ...]] = {
    # Edit this mapping as needed to add more carrier-name patterns.
    "S": ("ups ground",),
    "A": ("air",),
    "L": ("dhe", "ch robinson"),
    "P": ("will call", "pick up", "customer's own carrier"),
    "C": ("full container", "ocean"),
}
VALID_SHIP_TYPES: set[str] = {"A", "C", "L", "P", "R", "S", "T"}


def _ship_type_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ""
    for ship_type, keywords in SHIP_TYPE_BY_CARRIER_NAME_KEYWORDS.items():
        if any(k in name for k in keywords):
            return ship_type
    return ""


SHIP_TYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("A", "Air"),
    ("C", "Sea"),
    ("L", "LTL"),
    ("P", "Customer p/u"),
    ("R", "Rail"),
    ("S", "Small Package"),
    ("T", "Truckload"),
)


def _ship_type_label_from_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    for option_code, desc in SHIP_TYPE_OPTIONS:
        if normalized == option_code:
            return f"{option_code} - {desc}"
    default_code, default_desc = SHIP_TYPE_OPTIONS[0]
    return f"{default_code} - {default_desc}"


def _ship_type_code_from_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    code = text.split(" - ", 1)[0].strip().upper()
    for option_code, _ in SHIP_TYPE_OPTIONS:
        if code == option_code:
            return option_code
    return ""


SHIPMENT_TERMS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("3RD", "Third party collect"),
    ("COL", "COLLECT"),
    ("PCK", "CONSIGNEE P/U"),
    ("PPD", "Prepaid"),
)


def _shipment_terms_label_from_code(code: str) -> str:
    normalized = _normalize_terms(code)
    for option_code, desc in SHIPMENT_TERMS_OPTIONS:
        if normalized == option_code:
            return f"{option_code} - {desc}"
    default_code, default_desc = SHIPMENT_TERMS_OPTIONS[0]
    return f"{default_code} - {default_desc}"


def _shipment_terms_code_from_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    code = text.split(" - ", 1)[0].strip().upper()
    for option_code, _ in SHIPMENT_TERMS_OPTIONS:
        if code == option_code:
            return option_code
    return ""


# Carrier-specific delivery services -> header "delivery_service" code.
# Only offered when the carrier matches one of these mappings.
FEDEX_DELIVERY_SERVICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "FedEx Priority Overnight"),
    ("2", "FedEx Standard Overnight"),
    ("3", "FedEx 2Day"),
    ("4", "FedEx 1Day Freight"),
    ("5", "FedEx 2Day Freight"),
    ("6", "FedEx First Overnight"),
    ("7", "FedEx Express Saver - 3 day serv"),
    ("8", "FedEx 3Day Freight"),
    ("10", "FedEx 2Day AM"),
    ("11", "FedEx First Overnight"),
    ("90", "FedEx Ground House - Residential"),
    ("92", "FedEx Ground Service"),
    ("INT1", "FedEx International PRIORITY"),
    ("INT2", "FedEx International ECONOMY"),
    ("INTR", "FedEx International GROUND"),
)
UPS_DELIVERY_SERVICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("2AIR", "2nd Day Air AM"),
    ("2DAY", "2nd Day Air"),
    ("3DAY", "3 Day Select"),
    ("EXPR", "UPS Express - 10AM"),
    ("GRND", "Ground"),
    ("IEXP", "Worldwide Express"),
    ("ISTD", "Standard"),
    ("ISTN", "Worldwide Standard"),
    ("ISVR", "Worldwide Saver (Express)"),
    ("IXPD", "Worldwide Expedited"),
    ("IXPL", "Worldwide Express Plus"),
    ("NDAM", "Next Day Air Early AM"),
    ("NDAS", "Next Day Air Saver"),
    ("NDAY", "Next Day Air"),
)
DELIVERY_SERVICE_NONE_LABEL = "(none)"

# Fishbowl carrierservice.name -> Synapse delivery_service code.
FISHBOWL_FEDEX_SERVICE_TO_DELIVERY_CODE: dict[str, str] = {
    "2 day": "3",
    "2 day a.m.": "10",
    "2nd day air": "3",
    "2nd day air am": "10",
    "3 day select": "7",
    "europe first international priority": "INT1",
    "express saver": "7",
    "fedex 2day": "3",
    "fedex express saver": "7",
    "fedex ground": "92",
    "fedex home delivery": "90",
    "fedex standard overnight": "2",
    "first overnight": "6",
    "ground": "92",
    "home delivery": "90",
    "international economy": "INT2",
    "international first": "INT1",
    "international priority": "INT1",
    "next day air": "1",
    "next day air early am": "6",
    "next day air saver": "2",
    "priority overnight": "1",
    "smartpost": "92",
    "standard overnight": "2",
}
FISHBOWL_UPS_SERVICE_TO_DELIVERY_CODE: dict[str, str] = {
    "2nd day air": "2DAY",
    "2nd day air a.m.": "2AIR",
    "3 day select": "3DAY",
    "ground": "GRND",
    "mail innovations (domestic)": "GRND",
    "next day air": "NDAY",
    "next day air early a.m.": "NDAM",
    "next day air saver": "NDAS",
    "standard": "ISTD",
    "surepost": "GRND",
    "surepost lightweight": "GRND",
    "worldwide expedited": "IXPD",
    "worldwide express": "IEXP",
    "worldwide express plus": "IXPL",
    "worldwide saver": "ISVR",
}


def _is_fedex_carrier(raw_name: str) -> bool:
    return "fedex" in str(raw_name or "").strip().lower()


def _consignee_for_carrier(raw_name: str, customer_name: str = "") -> str:
    customer = str(customer_name or "").strip()
    if _is_ups_carrier(raw_name):
        if customer == "Ecom HD":
            return "HDMIR"
        return "MIRUPS"
    if _is_fedex_carrier(raw_name):
        if customer == "Ecom LS":
            return "LOWMIR"
        return "MIRFEX"
    return ""


def _fishbowl_carrier_service_name(ctx: dict) -> str:
    ship_row = (ctx or {}).get("ship", {}) or {}
    return str(_row_get_any(ship_row, "carrier_service_name", "carrierServiceName") or "").strip()


def _delivery_service_from_fishbowl_carrier(carrier_name: str, service_name: str = "") -> str:
    key = " ".join(str(service_name or "").strip().lower().rstrip("?").split())
    if not key:
        return ""
    if _is_fedex_carrier(carrier_name):
        return FISHBOWL_FEDEX_SERVICE_TO_DELIVERY_CODE.get(key, "")
    if _is_ups_carrier(carrier_name):
        return FISHBOWL_UPS_SERVICE_TO_DELIVERY_CODE.get(key, "")
    return ""


def _delivery_service_options_for_carrier(raw_name: str) -> tuple[tuple[str, str], ...]:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ()
    if "fedex" in name:
        return FEDEX_DELIVERY_SERVICE_OPTIONS
    if _is_ups_carrier(raw_name):
        return UPS_DELIVERY_SERVICE_OPTIONS
    return ()


def _delivery_service_label_from_code(code: str, options: tuple[tuple[str, str], ...]) -> str:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return DELIVERY_SERVICE_NONE_LABEL
    for option_code, desc in options:
        if normalized == option_code.upper():
            return f"{option_code} - {desc}"
    return DELIVERY_SERVICE_NONE_LABEL


def _delivery_service_code_from_label(label: str, options: tuple[tuple[str, str], ...]) -> str:
    text = str(label or "").strip()
    if not text or text == DELIVERY_SERVICE_NONE_LABEL:
        return ""
    code = text.split(" - ", 1)[0].strip()
    for option_code, _ in options:
        if code.upper() == option_code.upper():
            return option_code
    return ""


HEADER_FIELD_LIMITS: dict[str, int] = {
    "func": 1,
    "custid": 10,
    "po_number": 20,
    "order_type": 1,
    "reference": 20,
    "from_facility": 3,
    "carrier": 10,
    "ship_type": 1,
    "shipment_terms": 3,
    "ship_to_name": 40,
    "ship_to_address_1": 40,
    "ship_to_address_2": 40,
    "ship_to_city": 30,
    "ship_to_state": 2,
    "ship_to_postal_code": 5,
    "ship_to_country_code": 3,
    "bill_to_name": 40,
    "bill_to_address_1": 40,
    "bill_to_city": 30,
    "bill_to_state": 2,
    "bill_to_postal_code": 5,
    "bill_to_country_code": 3,
}

HEADER_UPPERCASE_FIELDS: set[str] = {
    "func",
    "custid",
    "order_type",
    "from_facility",
    "carrier",
    "ship_type",
    "shipment_terms",
    "ship_to_state",
    "ship_to_country_code",
    "bill_to_state",
    "bill_to_country_code",
}

HEADER_DATE_FIELDS: set[str] = set()


def _split_ship_to_address(raw: str, customer_name: str = "") -> tuple[str, str]:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    # Only the "Ecom HD" customer splits a multi-line address into address_1 / address_2.
    # For everyone else, keep the whole address on address_1 regardless of line count.
    if str(customer_name or "").strip() != "Ecom HD":
        return " ".join(line.strip() for line in text.split("\n") if line.strip()), ""
    if "\n" not in text:
        return text, ""
    first_line, rest = text.split("\n", 1)
    other_lines = [part.strip() for part in rest.split("\n") if part.strip()]
    return " ".join(other_lines), first_line.strip()


def _format_header_date_yyyymmdd(value) -> str:
    parsed = _parse_fb_date(value)
    if parsed:
        return parsed.strftime("%Y%m%d")
    digits = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return str(value or "").strip()[:8]


def _apply_header_field_limits(header: dict) -> dict:
    normalized: dict[str, str] = {}
    for key, value in header.items():
        if key in HEADER_DATE_FIELDS:
            normalized[key] = _format_header_date_yyyymmdd(value)
            continue
        text = str(value or "").strip()
        if key in HEADER_UPPERCASE_FIELDS:
            text = text.upper()
        max_len = HEADER_FIELD_LIMITS.get(key)
        if max_len is not None:
            text = text[:max_len]
        normalized[key] = text
    return normalized


SENT_SHIPMENTS_FILE = Path(__file__).with_name("synapse_sent_shipments.txt")
SYNAPSE_ORDER_INFO_FILE = Path(__file__).with_name("synapse_order_info_map.txt")
SYNAPSE_LAST_SEND_FILE = Path(__file__).with_name("synapse_last_send_map.txt")
CARRIER_SCAC_MAP_FILE = Path(__file__).with_name("carrier_scac_map.txt")
CARRIER_SHIP_TYPE_MAP_FILE = Path(__file__).with_name("carrier_ship_type_map.txt")


def _extract_synapse_order_info_fields(response: dict | None) -> dict | None:
    if not isinstance(response, dict) or response.get("error"):
        return None
    orderid = response.get("orderid")
    shipid = response.get("shipid")
    if orderid is None or shipid is None:
        return None
    return {
        "orderid": int(orderid),
        "shipid": int(shipid),
        "reference": str(response.get("reference") or "").strip(),
        "po": str(response.get("po") or "").strip(),
    }


def _order_info_payload_from_fields(fields: dict) -> dict:
    return {
        "orderid": fields["orderid"],
        "shipid": fields["shipid"],
        "custid": "MIRMOS",
        "po": fields.get("po") or "",
        "reference": fields.get("reference") or "",
    }


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fishbowl Shipping")
        self.geometry("1000x650")
        self.minsize(900, 600)

        self.fb: FishbowlClient | None = None

        self.shipments_by_num: dict[str, list[dict]] = {}
        self.current_ship_num: str | None = None
        self.current_items: list[dict] = []
        self.current_order_context: dict = {}
        self.hdr_instructions_by_ship: dict[str, str] = {}
        self.synapse_sent_shipments: set[str] = self._load_sent_shipments()
        self.synapse_failed_shipments: dict[str, str] = {}
        self.carrier_scac_map: dict[str, str] = self._load_carrier_scac_map()
        self.carrier_ship_type_map: dict[str, str] = self._load_carrier_ship_type_map()
        self.synapse_order_info_by_ship: dict[str, dict] = self._load_synapse_order_info_map()
        self.synapse_last_send_by_ship: dict[str, dict] = self._load_synapse_last_send_map()

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

    def _load_carrier_scac_map(self) -> dict[str, str]:
        if not CARRIER_SCAC_MAP_FILE.exists():
            return {}
        try:
            lines = CARRIER_SCAC_MAP_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        out: dict[str, str] = {}
        for line in lines:
            text = line.strip()
            if not text or "\t" not in text:
                continue
            carrier_key, scac = text.split("\t", 1)
            carrier_norm = _normalize_carrier_name(carrier_key)
            scac_norm = str(scac or "").strip().upper()
            if carrier_norm and scac_norm:
                out[carrier_norm] = scac_norm
        return out

    def save_carrier_scac_map(self):
        lines = [f"{carrier}\t{scac}" for carrier, scac in sorted(self.carrier_scac_map.items()) if carrier and scac]
        payload = "\n".join(lines)
        if payload:
            payload += "\n"
        try:
            CARRIER_SCAC_MAP_FILE.write_text(payload, encoding="utf-8")
        except Exception as e:
            self.set_status(f"Warning: failed saving carrier SCAC map ({e}).")

    def resolve_scac_for_carrier(self, carrier_name: str) -> str:
        carrier_norm = _normalize_carrier_name(carrier_name)
        if carrier_norm and carrier_norm in self.carrier_scac_map:
            return self.carrier_scac_map[carrier_norm]
        return _scac_from_carrier_name(carrier_name) or config.SYNAPSE_CARRIER

    def remember_scac_for_carrier(self, carrier_name: str, scac: str):
        carrier_norm = _normalize_carrier_name(carrier_name)
        scac_norm = str(scac or "").strip().upper()
        if not carrier_norm or not scac_norm:
            return
        if self.carrier_scac_map.get(carrier_norm) == scac_norm:
            return
        self.carrier_scac_map[carrier_norm] = scac_norm
        self.save_carrier_scac_map()

    def _load_carrier_ship_type_map(self) -> dict[str, str]:
        if not CARRIER_SHIP_TYPE_MAP_FILE.exists():
            return {}
        try:
            lines = CARRIER_SHIP_TYPE_MAP_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        out: dict[str, str] = {}
        for line in lines:
            text = line.strip()
            if not text or "\t" not in text:
                continue
            carrier_key, ship_type = text.split("\t", 1)
            carrier_norm = _normalize_carrier_name(carrier_key)
            ship_type_norm = str(ship_type or "").strip().upper()
            if carrier_norm and ship_type_norm in VALID_SHIP_TYPES:
                out[carrier_norm] = ship_type_norm
        return out

    def save_carrier_ship_type_map(self):
        lines = [
            f"{carrier}\t{ship_type}"
            for carrier, ship_type in sorted(self.carrier_ship_type_map.items())
            if carrier and ship_type in VALID_SHIP_TYPES
        ]
        payload = "\n".join(lines)
        if payload:
            payload += "\n"
        try:
            CARRIER_SHIP_TYPE_MAP_FILE.write_text(payload, encoding="utf-8")
        except Exception as e:
            self.set_status(f"Warning: failed saving carrier ship type map ({e}).")

    def resolve_ship_type_for_carrier(self, carrier_name: str) -> str:
        carrier_norm = _normalize_carrier_name(carrier_name)
        if carrier_norm and carrier_norm in self.carrier_ship_type_map:
            return self.carrier_ship_type_map[carrier_norm]
        resolved = str(_ship_type_from_carrier_name(carrier_name) or config.SYNAPSE_SHIP_TYPE).strip().upper()
        return resolved if resolved in VALID_SHIP_TYPES else config.SYNAPSE_SHIP_TYPE

    def remember_ship_type_for_carrier(self, carrier_name: str, ship_type: str):
        carrier_norm = _normalize_carrier_name(carrier_name)
        ship_type_norm = str(ship_type or "").strip().upper()
        if not carrier_norm or ship_type_norm not in VALID_SHIP_TYPES:
            return
        if self.carrier_ship_type_map.get(carrier_norm) == ship_type_norm:
            return
        self.carrier_ship_type_map[carrier_norm] = ship_type_norm
        self.save_carrier_ship_type_map()

    def _load_synapse_order_info_map(self) -> dict[str, dict]:
        if not SYNAPSE_ORDER_INFO_FILE.exists():
            return {}
        try:
            lines = SYNAPSE_ORDER_INFO_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for line in lines:
            text = line.strip()
            if not text or "\t" not in text:
                continue
            ship_num, raw_json = text.split("\t", 1)
            ship_key = ship_num.strip()
            if not ship_key:
                continue
            try:
                fields = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(fields, dict) and fields.get("orderid") is not None and fields.get("shipid") is not None:
                out[ship_key] = fields
        return out

    def save_synapse_order_info_map(self):
        lines = []
        for ship_num in sorted(self.synapse_order_info_by_ship):
            fields = self.synapse_order_info_by_ship.get(ship_num) or {}
            if fields.get("orderid") is None or fields.get("shipid") is None:
                continue
            lines.append(f"{ship_num}\t{json.dumps(fields, separators=(',', ':'))}")
        payload = "\n".join(lines)
        if payload:
            payload += "\n"
        try:
            SYNAPSE_ORDER_INFO_FILE.write_text(payload, encoding="utf-8")
        except Exception as e:
            self.set_status(f"Warning: failed saving Synapse order-info map ({e}).")

    def get_synapse_order_info(self, ship_num: str) -> dict | None:
        ship_key = str(ship_num or "").strip()
        if not ship_key:
            return None
        fields = self.synapse_order_info_by_ship.get(ship_key)
        if fields:
            return dict(fields)
        return None

    def remember_synapse_order_info(self, ship_num: str, response: dict | None):
        ship_key = str(ship_num or "").strip()
        fields = _extract_synapse_order_info_fields(response)
        if not ship_key or not fields:
            return
        if self.synapse_order_info_by_ship.get(ship_key) == fields:
            return
        self.synapse_order_info_by_ship[ship_key] = fields
        self.save_synapse_order_info_map()

    def _load_synapse_last_send_map(self) -> dict[str, dict]:
        if not SYNAPSE_LAST_SEND_FILE.exists():
            return {}
        try:
            lines = SYNAPSE_LAST_SEND_FILE.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for line in lines:
            text = line.strip()
            if not text or "\t" not in text:
                continue
            ship_num, raw_json = text.split("\t", 1)
            ship_key = ship_num.strip()
            if not ship_key:
                continue
            try:
                record = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict) and isinstance(record.get("payload"), dict):
                out[ship_key] = record
        return out

    def save_synapse_last_send_map(self):
        lines = []
        for ship_num in sorted(self.synapse_last_send_by_ship):
            record = self.synapse_last_send_by_ship.get(ship_num) or {}
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            lines.append(f"{ship_num}\t{json.dumps(record, separators=(',', ':'))}")
        payload_text = "\n".join(lines)
        if payload_text:
            payload_text += "\n"
        try:
            SYNAPSE_LAST_SEND_FILE.write_text(payload_text, encoding="utf-8")
        except Exception as e:
            self.set_status(f"Warning: failed saving Synapse last-send map ({e}).")

    def get_synapse_last_send(self, ship_num: str) -> dict | None:
        ship_key = str(ship_num or "").strip()
        if not ship_key:
            return None
        record = self.synapse_last_send_by_ship.get(ship_key)
        if record:
            return dict(record)
        return None

    def remember_synapse_last_send(self, ship_num: str, payload: dict | None, response: dict | None):
        ship_key = str(ship_num or "").strip()
        if not ship_key or not isinstance(payload, dict):
            return
        record = {"payload": payload}
        if response is not None:
            record["response"] = response
        if self.synapse_last_send_by_ship.get(ship_key) == record:
            return
        self.synapse_last_send_by_ship[ship_key] = record
        self.save_synapse_last_send_map()


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
        self._all_shipment_rows: list[dict] = []

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Sales Order Shipments", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Label(top, text="Salesperson:").pack(side="left", padx=(16, 6))
        self.salesperson_filter_var = tk.StringVar(value="All Salespeople")
        self.salesperson_filter = ttk.Combobox(
            top,
            textvariable=self.salesperson_filter_var,
            values=("All Salespeople",),
            state="readonly",
            width=26,
        )
        self.salesperson_filter.pack(side="left")
        self.salesperson_filter.bind("<<ComboboxSelected>>", lambda _e: self._render_shipments())
        ttk.Label(top, text="Search Ship #:").pack(side="left", padx=(16, 6))
        self.ship_search_var = tk.StringVar(value="")
        self.ship_search_entry = ttk.Entry(top, textvariable=self.ship_search_var, width=18)
        self.ship_search_entry.pack(side="left")
        self.ship_search_var.trace_add("write", lambda *_: self._render_shipments())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="Logout", command=self._logout).pack(side="right", padx=(0, 8))

        cols = ("ship_num", "customer", "salesperson", "city", "state", "zip", "carrier", "synapse_status")
        headers = ("Ship #", "Customer", "Salesperson", "City", "State", "Zip", "Carrier", "Synapse")
        self._status_col_idx = len(cols) - 1
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=130, anchor="w")
        self.tree.column("customer", width=200, anchor="w")
        self.tree.column("salesperson", width=180, anchor="w")
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
        if not self.tree.get_children():
            self.refresh()

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
        self.app.set_status(f"Copied ship #: {text}")

    def refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.app.set_status("Loading shipments...")

        def do():
            rows = self.app.fb.data_query(queries.SHIPMENTS_SQL)
            grouped: dict[str, list[dict]] = {}
            for r in rows:
                grouped.setdefault(r["ship_num"], []).append(r)
            collected_rows: list[dict] = []
            for num, pallets in grouped.items():
                first = pallets[0]
                carrier_name = next(
                    (str(p.get("carrier_name") or "").strip() for p in pallets if str(p.get("carrier_name") or "").strip()),
                    "",
                )
                collected_rows.append(
                    {
                        "ship_num": str(num),
                        "customer": self._extract_customer(first),
                        "salesperson": self._extract_salesperson(first),
                        "city": str(first.get("city", "") or ""),
                        "state": str(first.get("state", "") or ""),
                        "zip": str(first.get("zip", "") or ""),
                        "carrier": carrier_name,
                    }
                )
            return grouped, collected_rows

        def ok(payload):
            grouped, collected_rows = payload
            self.app.shipments_by_num = grouped
            self._all_shipment_rows = collected_rows
            self._refresh_salesperson_filter_options()
            shown = self._render_shipments()
            self.app.set_status(f"Loaded {len(grouped)} shipments (showing {shown}).")

        self.app.run_async(do, ok)

    def _extract_customer(self, row: dict) -> str:
        return str(_row_get_any(row, "customer_name", "customerName") or "").strip()

    def _extract_salesperson(self, row: dict) -> str:
        value = _row_get_any(
            row,
            "salesperson",
            "salesPerson",
            "salesman",
            "salesMan",
            "salesperson_name",
            "salesPersonName",
            "salesman_name",
            "salesmanName",
            "csr",
            "csr_name",
        )
        return str(value or "").strip()

    def _status_text_and_tags_for_ship(self, ship_num: str) -> tuple[str, tuple[str, ...]]:
        if ship_num in self.app.synapse_sent_shipments:
            return "SENT", ("synapse_sent",)
        if ship_num in self.app.synapse_failed_shipments:
            return "FAILED", ("synapse_failed",)
        return "", ()

    def _refresh_salesperson_filter_options(self):
        names = sorted(
            {
                str(r.get("salesperson") or "").strip()
                for r in self._all_shipment_rows
                if str(r.get("salesperson") or "").strip()
            }
        )
        values = ("All Salespeople", *names)
        current = str(self.salesperson_filter_var.get() or "").strip()
        self.salesperson_filter.config(values=values)
        if current not in values:
            self.salesperson_filter_var.set("All Salespeople")

    def _render_shipments(self) -> int:
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

        selected = str(self.salesperson_filter_var.get() or "").strip()
        search = str(self.ship_search_var.get() or "").strip().lower()
        shown = 0
        for row in self._all_shipment_rows:
            salesperson = str(row.get("salesperson") or "").strip()
            if selected and selected != "All Salespeople" and salesperson != selected:
                continue
            ship_num = str(row.get("ship_num") or "")
            if search and search not in ship_num.lower():
                continue
            status_text, tags = self._status_text_and_tags_for_ship(ship_num)
            self.tree.insert(
                "",
                "end",
                iid=ship_num,
                values=(
                    ship_num,
                    row.get("customer", ""),
                    salesperson,
                    row.get("city", ""),
                    row.get("state", ""),
                    row.get("zip", ""),
                    row.get("carrier", ""),
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
        self.app.synapse_failed_shipments = {}
        self.app.show("login")


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
        self.ship_type_var = tk.StringVar(value=_ship_type_label_from_code(config.SYNAPSE_SHIP_TYPE))
        ttk.Combobox(
            scac_row,
            textvariable=self.ship_type_var,
            width=24,
            values=tuple(f"{code} - {desc}" for code, desc in SHIP_TYPE_OPTIONS),
            state="readonly",
        ).pack(side="left")
        ttk.Label(scac_row, text="Shipment Terms:").pack(side="left", padx=(18, 6))
        self.shipment_terms_var = tk.StringVar(
            value=_shipment_terms_label_from_code("3RD")
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
        self.order_info_btn = ttk.Button(
            actions,
            text="Send Order Info",
            command=self._send_order_info,
            state="disabled",
        )
        self.order_info_btn.pack(side="left", padx=(8, 0))
        self.email_bol_btn = ttk.Button(
            actions,
            text="Email PDF to NJ",
            command=self._email_bol_to_warehouse,
        )
        self.email_bol_btn.pack(side="left", padx=(8, 0))
        self.toggle_raw_btn = ttk.Button(
            actions,
            text="Show last payload",
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
        self._last_synapse_response: dict | None = None
        self._last_synapse_payload: dict | None = None
        self._last_order_info_payload: dict | None = None
        self._last_order_info_response: dict | None = None

    def on_show(self):
        num = self.app.current_ship_num
        if not num:
            return
        pallets = self.app.shipments_by_num.get(num, [])
        first = pallets[0] if pallets else {}
        self.title_var.set(
            f"Ship #{num}  →  {first.get('city', '')}, {first.get('state', '')}  {first.get('zip', '')}"
        )
        self._set_instructions_text(self.app.hdr_instructions_by_ship.get(num, ""))

        self.item_tree.delete(*self.item_tree.get_children())
        self._review_rows = []
        self.carrier_name_var.set("")
        self._hide_raw()
        self._last_order_info_payload = None
        self._last_order_info_response = None
        self._restore_last_synapse_send_for_ship(num)
        self._update_order_info_btn_state()
        self._update_raw_btn_state()
        self.app.current_items = []
        self.app.current_order_context = {}
        load_seq = self._items_load_seq + 1
        self._items_load_seq = load_seq

        self.app.set_status(f"Loading items for {num}...")

        def do():
            rows, ctx = self._load_shipment_items_and_context(num)
            return load_seq, num, rows, ctx

        def ok(payload):
            load_seq, ship_num, rows, ctx = payload
            if load_seq != self._items_load_seq or ship_num != self.app.current_ship_num:
                return
            self.app.current_items = rows
            self.app.current_order_context = ctx
            self._set_instructions_text(self._resolved_hdr_instructions(ship_num, ctx, rows))
            self._setup_review_ui(ship_num, rows, ctx)
            self.app.set_status(f"Shipment {ship_num}: {len(pallets)} pallets, {len(rows)} items.")

        self.app.run_async(do, ok)

    def _load_shipment_items_and_context(self, ship_num: str) -> tuple[list[dict], dict]:
        rows = self.app.fb.data_query(queries.items_sql_for(ship_num))
        ctx = self._load_order_context(ship_num)
        return rows, ctx

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

    def _ship_to_state_code(self, ship_row: dict, fallback_state: str = "") -> str:
        state = str(_row_get_any(ship_row, "shipToState") or fallback_state or "").strip()
        if not state:
            state_id = _row_get_any(ship_row, "shipToStateId")
            if state_id not in (None, ""):
                state = self._state_code_from_id(state_id)
        # Some Fishbowl schemas expose numeric state IDs in shipToState.
        if state.isdigit():
            mapped = self._state_code_from_id(state)
            if mapped:
                state = mapped
        return state

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

    def _so_note_instructions(self, ctx: dict) -> str:
        so_row = ctx.get("so", {}) or {}
        return str(
            _row_get_any(
                so_row,
                "note",
                "notes",
                "memo",
                "customerMemo",
                "customer_note",
            )
            or ""
        ).strip()

    def _soitem_note_instructions(self, rows: list[dict]) -> str:
        notes: list[str] = []
        for row in rows:
            text = str(
                _row_get_any(
                    row,
                    "soitem_note",
                    "soitemNote",
                    "note",
                )
                or ""
            ).strip()
            if text:
                notes.append(text)
        return "; ".join(notes)

    def _resolved_hdr_instructions(self, ship_num: str, ctx: dict, rows: list[dict] | None = None) -> str:
        manual = str(self.app.hdr_instructions_by_ship.get(ship_num, "") or "")
        if manual.strip():
            return manual
        soitem_note = self._soitem_note_instructions(rows or [])
        if soitem_note:
            return soitem_note
        return self._so_note_instructions(ctx)

    def _ship_date_from_soitem_rows(self, rows: list[dict]) -> str:
        for r in rows:
            raw = _row_get_any(
                r,
                "date_scheduled_fulfillment",
                "dateScheduledFulfillment",
                "datescheduledfulfillment",
            )
            text = str(raw or "").strip()
            if text:
                return text
        return ""

    def _build_review_lines(self, rows: list[dict]) -> list[dict]:
        aggregated: dict[tuple[str, str, str], float] = {}
        passthru_map: dict[tuple[str, str, str], str] = {}
        for r in rows:
            item_num = str(r.get("item_num") or "").strip()
            if self._should_exclude_item(item_num):
                continue
            tracking = _normalize_lot_number(r.get("tracking"))
            uom = normalize_uom(str(r.get("uom") or ""))
            qty = r.get("qty") or 0
            if not item_num or not uom:
                continue
            order_index = str(r.get("order_index") or "").strip()
            key = (item_num, uom, tracking)
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            aggregated[key] = aggregated.get(key, 0.0) + qty_f
            if order_index and key not in passthru_map:
                passthru_map[key] = order_index
        review_lines: list[dict] = []
        for (item_num, uom, tracking), qty in aggregated.items():
            if qty:
                order_index = str(passthru_map.get((item_num, uom, tracking), "")).strip()
                passthru_num = None
                try:
                    passthru_num = int(order_index) if order_index else None
                except (TypeError, ValueError):
                    passthru_num = None
                review_lines.append(
                    {
                        "item": item_num,
                        "tracking": tracking,
                        "fb_uom": uom,
                        "fb_qty": qty,
                        "dtl_pass_thru_num_10": passthru_num,
                    }
                )
        return review_lines

    def _create_synapse_order(self):
        if not config.SYNAPSE_USERNAME or not config.SYNAPSE_PASSWORD:
            messagebox.showerror(
                "Synapse not configured",
                "Set SYNAPSE_USERNAME and SYNAPSE_PASSWORD in your .env (and restart the app).",
            )
            return

        ship_num = str(self.app.current_ship_num or "").strip()
        if not ship_num:
            messagebox.showwarning("No shipment", "No shipment is selected.")
            return
        if not self.app.current_items:
            messagebox.showwarning("No items", "No items are loaded for this shipment yet.")
            return

        rows = self.app.current_items
        ctx = self.app.current_order_context or {}
        first = rows[0]
        ship_row = ctx.get("ship", {}) or {}
        so_row = ctx.get("so", {}) or {}
        customer_row = ctx.get("customer", {}) or {}
        po_number = str(first.get("po_number") or "").strip()
        if not po_number:
            messagebox.showerror("Missing PO", "This shipment has no PO number in Fishbowl.")
            return
        ship_num_value = _synapse_reference_from_ship_num(
            _row_get_any(so_row, "num") or ship_num
        )

        ship_to_name = str(
            _row_get_any(ship_row, "shipToName") or first.get("ship_to_name") or ""
        ).strip() or "SHIP TO"
        ship_to_name = ship_to_name[:40]
        ship_to_address_1, ship_to_address_2 = _split_ship_to_address(
            _row_get_any(ship_row, "shipToAddress", "shipToAddress1") or first.get("address_1") or "",
            str(_row_get_any(customer_row, "name") or "").strip(),
        )
        ship_to_city = str(_row_get_any(ship_row, "shipToCity") or first.get("city") or "").strip()
        ship_to_state = self._ship_to_state_code(ship_row, str(first.get("state") or ""))
        ship_to_postal_code = str(
            _row_get_any(ship_row, "shipToZip", "shipToPostalCode") or first.get("zip") or ""
        ).strip()
        if not all([ship_to_address_1, ship_to_city, ship_to_state, ship_to_postal_code]):
            messagebox.showerror(
                "Missing Ship-To",
                "Fishbowl is missing one or more ship-to fields (address/city/state/zip) for this shipment.",
            )
            return

        carrier_name = next(
            (str(r.get("carrier_name") or "").strip() for r in rows if str(r.get("carrier_name") or "").strip()),
            "",
        )
        header_fields = self._read_order_header_fields()
        if header_fields is None:
            return
        carrier, ship_type, shipment_terms, delivery_service = header_fields
        reviewed = self._collect_reviewed_lines_for_send()
        if reviewed is None:
            return

        details = [
            {
                "item": r["item"],
                "uom_entered": r["send_uom"],
                "qty_entered": r["send_qty"],
                "dtl_pass_thru_num_10": r.get("dtl_pass_thru_num_10"),
                "lot_number": _normalize_lot_number(r.get("lot_number", "")),
            }
            for r in reviewed
        ]
        if not details:
            messagebox.showerror("No order lines", "Could not build any detail lines from Fishbowl items.")
            return

        ship_date = self._ship_date_from_soitem_rows(rows)
        if not ship_date:
            messagebox.showerror(
                "Missing Ship Date",
                "No valid soitem.dateScheduledFulfillment was found for this shipment.",
            )
            return
        shipment_terms = _normalize_terms(shipment_terms) or (config.SYNAPSE_SHIPMENT_TERMS or "").strip().upper()
        ship_type = str(ship_type or config.SYNAPSE_SHIP_TYPE).strip().upper()
        if ship_type not in VALID_SHIP_TYPES:
            ship_type = config.SYNAPSE_SHIP_TYPE
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
                "reference": ship_num_value,
                "from_facility": config.SYNAPSE_FROM_FACILITY,
                "carrier": carrier,
                "ship_type": ship_type,
                "shipment_terms": shipment_terms or config.SYNAPSE_SHIPMENT_TERMS,
                "ship_date": ship_date,
                "ship_to_name": ship_to_name,
                "ship_to_address_1": ship_to_address_1,
                "ship_to_city": ship_to_city,
                "ship_to_state": ship_to_state,
                "ship_to_postal_code": ship_to_postal_code,
                "ship_to_country_code": "USA",
            },
            "details": details,
        }

        if ship_to_address_2:
            order_data["header"]["ship_to_address_2"] = ship_to_address_2
        consignee = _consignee_for_carrier(
            carrier_name, str(_row_get_any(customer_row, "name") or "").strip()
        )
        if consignee:
            order_data["header"]["consignee"] = consignee
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
        if delivery_service:
            order_data["header"]["delivery_service"] = delivery_service
        order_data["header"] = _apply_header_field_limits(order_data["header"])
        instructions_text = str(self.instructions_var.get() or "").strip()
        if instructions_text:
            header_reference = str(order_data.get("header", {}).get("reference") or "").strip()
            header_po_number = str(order_data.get("header", {}).get("po_number") or "").strip()
            order_data["hdrinstruct"] = {
                "custid": "MIRMOS",
                "reference": header_reference,
                "po_number": header_po_number,
                "instructions": instructions_text[:255],
            }

        self._last_synapse_payload = order_data
        self.app.remember_synapse_last_send(ship_num, order_data, None)
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
            self._last_synapse_response = result.get("response")
            self._last_synapse_payload = result.get("sent_payload")
            if self.app.current_ship_num:
                self.app.remember_synapse_last_send(
                    self.app.current_ship_num,
                    self._last_synapse_payload,
                    self._last_synapse_response,
                )
            self.app.remember_scac_for_carrier(carrier_name, carrier)
            self.app.remember_ship_type_for_carrier(carrier_name, ship_type)
            if self.app.current_ship_num:
                self.app.remember_synapse_order_info(self.app.current_ship_num, self._last_synapse_response)
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_sent(self.app.current_ship_num)
            self._update_order_info_btn_state()
            self._update_raw_btn_state()
            self.app.set_status("Synapse order created.")
            messagebox.showinfo("Synapse", "Order created successfully in Synapse.")

        def err(e):
            self.synapse_btn.config(state="normal")
            self._last_synapse_response = {"error": str(e)}
            if isinstance(e, SynapseCreateOrderError) and e.sent_payload is not None:
                self._last_synapse_payload = e.sent_payload
            if self.app.current_ship_num:
                self.app.remember_synapse_last_send(
                    self.app.current_ship_num,
                    self._last_synapse_payload,
                    self._last_synapse_response,
                )
            self._update_raw_btn_state()
            if self.app.current_ship_num:
                shipments_frame = self.app.frames.get("shipments")
                if isinstance(shipments_frame, ShipmentsFrame):
                    shipments_frame.mark_synapse_failed(self.app.current_ship_num, str(e))
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

        ship_num = self.app.current_ship_num
        if not ship_num:
            return
        if current.strip():
            self.app.hdr_instructions_by_ship[ship_num] = current
        else:
            self.app.hdr_instructions_by_ship.pop(ship_num, None)

    def _should_exclude_item(self, item_num: str) -> bool:
        s = (item_num or "").strip().lower()
        if not s:
            return True
        keywords = [k.strip().lower() for k in (config.EXCLUDE_ITEM_KEYWORDS or "").split(",") if k.strip()]
        return any(k in s for k in keywords)

    def _setup_review_ui(self, ship_num: str, rows: list[dict], ctx: dict):
        so_row = ctx.get("so", {}) or {}
        carrier_name = next(
            (str(r.get("carrier_name") or "").strip() for r in rows if str(r.get("carrier_name") or "").strip()),
            "",
        )
        self.carrier_name_var.set(carrier_name)
        self.scac_var.set(self.app.resolve_scac_for_carrier(carrier_name))

        ship_type = self.app.resolve_ship_type_for_carrier(carrier_name)
        if ship_type not in VALID_SHIP_TYPES:
            ship_type = config.SYNAPSE_SHIP_TYPE
        self.ship_type_var.set(_ship_type_label_from_code(ship_type))

        shipment_terms = _normalize_terms(
            str(_row_get_any(so_row, "shipmentTerms", "shipTerms", "freightTerms", "termCode") or "")
        )
        if not shipment_terms:
            shipment_terms = "3RD"
        if _shipment_terms_from_carrier_name(carrier_name):
            shipment_terms = "3RD"
        self.shipment_terms_var.set(_shipment_terms_label_from_code(shipment_terms))

        self._delivery_service_options = _delivery_service_options_for_carrier(carrier_name)
        delivery_enabled = bool(self._delivery_service_options)
        delivery_values = (
            DELIVERY_SERVICE_NONE_LABEL,
            *(f"{code} - {desc}" for code, desc in self._delivery_service_options),
        )
        self.delivery_service_combo.config(
            values=delivery_values,
            state="readonly" if delivery_enabled else "disabled",
        )
        initial_delivery_service = _delivery_service_from_fishbowl_carrier(
            carrier_name, _fishbowl_carrier_service_name(ctx)
        )
        self.delivery_service_var.set(
            _delivery_service_label_from_code(initial_delivery_service, self._delivery_service_options)
            if delivery_enabled
            else DELIVERY_SERVICE_NONE_LABEL
        )
        self.delivery_service_hint.pack_forget()
        if not delivery_enabled:
            self.delivery_service_hint.pack(side="left", padx=(8, 0))

        review_lines = self._build_review_lines(rows)
        hdr_instructions = self._resolved_hdr_instructions(ship_num, ctx, rows)
        self._review_rows = self._build_synapse_review_rows(review_lines, hdr_instructions)
        self._populate_review_item_tree()

    def _build_synapse_review_rows(self, lines: list[dict], hdr_instructions: str = "") -> list[dict]:
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
                    "noted_qty": None,
                }
            )

        if hdr_instructions:
            note_segments = [seg.strip() for seg in hdr_instructions.split(";")]
            for i, row in enumerate(rows):
                if i < len(note_segments):
                    row["noted_qty"] = _parse_noted_qty(note_segments[i])
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

        ship_num = str(self.app.current_ship_num or "").strip()
        rows = self.app.current_items or []

        subject_default = f"NJ - New Order # {ship_num}".strip()

        carrier_name = next(
            (str(r.get("carrier_name") or "").strip() for r in rows if str(r.get("carrier_name") or "").strip()),
            "",
        )

        item_lines = []
        seen = set()
        for r in rows:
            item = str(_row_get_any(r, "item_num") or "").strip()
            if not item:
                continue
            note = str(_row_get_any(r, "soitem_note") or "").strip()
            key = (item, note)
            if key in seen:
                continue
            seen.add(key)
            item_lines.append(f"{item} - {note}" if note else item)
        items_text = "\n".join(item_lines) if item_lines else "*items*"

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

    def _read_order_header_fields(self) -> tuple[str, str, str, str] | None:
        scac = (self.scac_var.get() or "").strip()
        if not scac:
            messagebox.showerror("Missing SCAC", "Enter a SCAC value.")
            return None
        ship_type = _ship_type_code_from_label(self.ship_type_var.get() or "")
        if ship_type not in VALID_SHIP_TYPES:
            messagebox.showerror("Invalid ship type", "Ship Type must be one of A, C, L, P, R, S, T.")
            return None
        shipment_terms = _shipment_terms_code_from_label(self.shipment_terms_var.get() or "")
        if not shipment_terms:
            messagebox.showerror("Missing shipment terms", "Select shipment terms.")
            return None
        delivery_service = (
            _delivery_service_code_from_label(self.delivery_service_var.get() or "", self._delivery_service_options)
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

    def _restore_last_synapse_send_for_ship(self, ship_num: str):
        stored = self.app.get_synapse_last_send(ship_num) or {}
        self._last_synapse_payload = stored.get("payload")
        self._last_synapse_response = stored.get("response")

    def _order_info_fields_for_current_ship(self) -> dict | None:
        ship_num = str(self.app.current_ship_num or "").strip()
        if ship_num:
            stored = self.app.get_synapse_order_info(ship_num)
            if stored:
                return stored
        return _extract_synapse_order_info_fields(self._last_synapse_response)

    def _update_order_info_btn_state(self):
        enabled = self._order_info_fields_for_current_ship() is not None
        self.order_info_btn.config(state="normal" if enabled else "disabled")

    def _update_raw_btn_state(self):
        ship_num = str(self.app.current_ship_num or "").strip()
        stored = self.app.get_synapse_last_send(ship_num) if ship_num else None
        has_data = any(
            (
                self._last_synapse_response,
                self._last_synapse_payload,
                stored and stored.get("payload"),
                self._last_order_info_response,
                self._last_order_info_payload,
                self._order_info_fields_for_current_ship(),
            )
        )
        self.toggle_raw_btn.config(state="normal" if has_data else "disabled")

    def _send_order_info(self):
        fields = self._order_info_fields_for_current_ship()
        if not fields:
            messagebox.showinfo(
                "Order info unavailable",
                "Create a Synapse order first so orderid, shipid, po, and reference are saved.",
            )
            return
        if not config.SYNAPSE_USERNAME or not config.SYNAPSE_PASSWORD:
            messagebox.showerror(
                "Synapse not configured",
                "Set SYNAPSE_USERNAME and SYNAPSE_PASSWORD in your .env (and restart the app).",
            )
            return

        payload = _order_info_payload_from_fields(fields)
        self._last_order_info_payload = payload
        self.order_info_btn.config(state="disabled")
        self.app.set_status("Sending Synapse order-info...")

        def do():
            client = SynapseClient(
                SynapseConfig(
                    base_url=config.SYNAPSE_BASE_URL,
                    username=config.SYNAPSE_USERNAME,
                    password=config.SYNAPSE_PASSWORD,
                )
            )
            client.login()
            return client.order_info(payload)

        def ok(response):
            self._last_order_info_response = response
            self._update_order_info_btn_state()
            self._update_raw_btn_state()
            if not self._raw_visible:
                self._show_raw()
            else:
                self._refresh_raw_text()
            self.app.set_status("Synapse order-info sent.")
            messagebox.showinfo("Synapse order-info", "Order info sent successfully.")

        def err(e):
            self._last_order_info_response = {"error": str(e)}
            self._update_order_info_btn_state()
            self._update_raw_btn_state()
            if self._raw_visible:
                self._refresh_raw_text()
            messagebox.showerror("Synapse order-info error", str(e))
            self.app.set_status("Synapse order-info failed.")

        self.app.run_async(do, ok, err)

    def _toggle_raw(self):
        if self._raw_visible:
            self._hide_raw()
        else:
            self._show_raw()

    def _raw_bundle(self) -> dict:
        return {
            "synapse_payload": self._last_synapse_payload,
            "synapse_response": self._last_synapse_response,
            "order_info_payload": self._last_order_info_payload,
            "order_info_response": self._last_order_info_response,
            "saved_order_info": self._order_info_fields_for_current_ship(),
        }

    def _refresh_raw_text(self):
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", json.dumps(self._raw_bundle(), indent=2, default=str))

    def _show_raw(self):
        bundle = self._raw_bundle()
        if all(v is None for v in bundle.values()):
            return
        self.raw_text.pack(fill="both", expand=True)
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", json.dumps(bundle, indent=2, default=str))
        self.toggle_raw_btn.config(text="Hide last payload")
        self._raw_visible = True

    def _hide_raw(self):
        self.raw_text.pack_forget()
        self.toggle_raw_btn.config(text="Show last payload")
        self._raw_visible = False


if __name__ == "__main__":
    app = App()
    app.mainloop()
