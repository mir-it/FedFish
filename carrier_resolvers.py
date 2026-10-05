"""Map Fishbowl carrier / service names to Synapse header field values."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import config

# Carrier-name keywords that should force shipment terms to 3RD (third party collect).
SHIPMENT_TERMS_3RD_CARRIER_KEYWORDS: tuple[str, ...] = ("fedex", "ups")

SHIP_TYPE_BY_CARRIER_NAME_KEYWORDS: dict[str, tuple[str, ...]] = {
    # Edit this mapping as needed to add more carrier-name patterns.
    "S": ("ups ground",),
    "A": ("air",),
    "L": ("dhe", "ch robinson"),
    "P": ("will call", "pick up", "customer's own carrier"),
    "C": ("full container", "ocean"),
}

VALID_SHIP_TYPES: set[str] = {"A", "C", "L", "P", "R", "S", "T"}

# Customer hasn't picked whose account to ship on yet; we quote weight/dims as Ground.
# Pinned so a one-off manual pick (e.g. LTL) never becomes the remembered default.
WILL_ADVISE_SHIP_TYPE = "S"

SHIP_TYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("A", "Air"),
    ("C", "Sea"),
    ("L", "LTL"),
    ("P", "Customer p/u"),
    ("R", "Rail"),
    ("S", "Small Package"),
    ("T", "Truckload"),
)

SHIPMENT_TERMS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("3RD", "Third party collect"),
    ("COL", "COLLECT"),
    ("PCK", "CONSIGNEE P/U"),
    ("PPD", "Prepaid"),
)

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


def normalize_terms(raw: str) -> str:
    s = (raw or "").strip()
    u = s.upper().replace("-", "").replace(" ", "")
    if u in {"3RD", "3RDPARTY", "THIRDPARTY", "THIRDPARTYBILL"}:
        return "3RD"
    return s.upper() if s else ""


def normalize_carrier_name(raw_name: str) -> str:
    return " ".join(str(raw_name or "").strip().lower().split())


def scac_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    scac_map = {
        "will advise": "9999",
        "freight": "FXSW",
        "daylight": "DYLT",
        "estes express lines": "EXLA",
        "customer's own carrier": "CSPU",
        "ups": "UPSM",
        "usps": "UPSN",
        "fedex": "FEDM",
        "aaa cooper": "AACT",
    }
    return scac_map.get(name, "")


def shipment_terms_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ""
    if any(k in name for k in SHIPMENT_TERMS_3RD_CARRIER_KEYWORDS):
        return "3RD"
    return ""


def is_ups_carrier(raw_name: str) -> bool:
    # Word-boundary match so "ups" matches UPS / "UPS Ground" but not "USPS".
    return bool(re.search(r"\bups\b", str(raw_name or "").strip().lower()))


def is_fedex_carrier(raw_name: str) -> bool:
    return "fedex" in str(raw_name or "").strip().lower()


def is_will_advise_carrier(raw_name: str) -> bool:
    return bool(re.search(r"\bwill[\s\-_]*advise\b", str(raw_name or "").strip().lower()))


def ship_type_from_carrier_name(raw_name: str) -> str:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ""
    for ship_type, keywords in SHIP_TYPE_BY_CARRIER_NAME_KEYWORDS.items():
        if any(k in name for k in keywords):
            return ship_type
    return ""


def ship_type_label_from_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    for option_code, desc in SHIP_TYPE_OPTIONS:
        if normalized == option_code:
            return f"{option_code} - {desc}"
    default_code, default_desc = SHIP_TYPE_OPTIONS[0]
    return f"{default_code} - {default_desc}"


def ship_type_code_from_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    code = text.split(" - ", 1)[0].strip().upper()
    for option_code, _ in SHIP_TYPE_OPTIONS:
        if code == option_code:
            return option_code
    return ""


def shipment_terms_label_from_code(code: str) -> str:
    normalized = normalize_terms(code)
    for option_code, desc in SHIPMENT_TERMS_OPTIONS:
        if normalized == option_code:
            return f"{option_code} - {desc}"
    default_code, default_desc = SHIPMENT_TERMS_OPTIONS[0]
    return f"{default_code} - {default_desc}"


def shipment_terms_code_from_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    code = text.split(" - ", 1)[0].strip().upper()
    for option_code, _ in SHIPMENT_TERMS_OPTIONS:
        if code == option_code:
            return option_code
    return ""


def delivery_service_from_fishbowl_carrier(carrier_name: str, service_name: str = "") -> str:
    key = " ".join(str(service_name or "").strip().lower().rstrip("?").split())
    if not key:
        return ""
    if is_fedex_carrier(carrier_name):
        return FISHBOWL_FEDEX_SERVICE_TO_DELIVERY_CODE.get(key, "")
    if is_ups_carrier(carrier_name):
        return FISHBOWL_UPS_SERVICE_TO_DELIVERY_CODE.get(key, "")
    return ""


def delivery_service_options_for_carrier(raw_name: str) -> tuple[tuple[str, str], ...]:
    name = str(raw_name or "").strip().lower()
    if not name:
        return ()
    if "fedex" in name:
        return FEDEX_DELIVERY_SERVICE_OPTIONS
    if is_ups_carrier(raw_name):
        return UPS_DELIVERY_SERVICE_OPTIONS
    return ()


def delivery_service_label_from_code(code: str, options: tuple[tuple[str, str], ...]) -> str:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return DELIVERY_SERVICE_NONE_LABEL
    for option_code, desc in options:
        if normalized == option_code.upper():
            return f"{option_code} - {desc}"
    return DELIVERY_SERVICE_NONE_LABEL


def delivery_service_code_from_label(label: str, options: tuple[tuple[str, str], ...]) -> str:
    text = str(label or "").strip()
    if not text or text == DELIVERY_SERVICE_NONE_LABEL:
        return ""
    code = text.split(" - ", 1)[0].strip()
    for option_code, _ in options:
        if code.upper() == option_code.upper():
            return option_code
    return ""


CARRIER_SCAC_MAP_FILE = Path(__file__).with_name("carrier_scac_map.txt")
CARRIER_SHIP_TYPE_MAP_FILE = Path(__file__).with_name("carrier_ship_type_map.txt")


def _load_tab_map(
    path: Path,
    normalize_value: Callable[[str], str],
    *,
    valid: Callable[[str], bool] | None = None,
) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text or "\t" not in text:
            continue
        carrier_key, raw_value = text.split("\t", 1)
        carrier = normalize_carrier_name(carrier_key)
        value = normalize_value(raw_value)
        if carrier and value and (valid is None or valid(value)):
            out[carrier] = value
    return out


def _save_tab_map(
    path: Path,
    data: dict[str, str],
    *,
    include: Callable[[str, str], bool],
    on_error: Callable[[str], None] | None = None,
    error_label: str = "",
) -> None:
    lines = [f"{carrier}\t{value}" for carrier, value in sorted(data.items()) if include(carrier, value)]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    try:
        path.write_text(payload, encoding="utf-8")
    except Exception as e:
        if on_error:
            on_error(f"Warning: failed saving {error_label} ({e}).")


class CarrierPreferenceStore:
    """Persisted carrier -> SCAC / ship-type overrides learned from past sends."""

    def __init__(self, base_dir: Path | None = None):
        base = base_dir or Path(__file__).parent
        self._scac_path = base / CARRIER_SCAC_MAP_FILE.name
        self._ship_type_path = base / CARRIER_SHIP_TYPE_MAP_FILE.name
        self._scac_by_carrier = _load_tab_map(
            self._scac_path,
            lambda raw: str(raw or "").strip().upper(),
        )
        self._ship_type_by_carrier = _load_tab_map(
            self._ship_type_path,
            lambda raw: str(raw or "").strip().upper(),
            valid=lambda value: value in VALID_SHIP_TYPES,
        )
        self.on_save_error: Callable[[str], None] | None = None

    def resolve_scac(self, carrier_name: str) -> str:
        carrier_norm = normalize_carrier_name(carrier_name)
        if carrier_norm and carrier_norm in self._scac_by_carrier:
            return self._scac_by_carrier[carrier_norm]
        return scac_from_carrier_name(carrier_name) or config.SYNAPSE_CARRIER

    def remember_scac(self, carrier_name: str, scac: str) -> None:
        carrier_norm = normalize_carrier_name(carrier_name)
        scac_norm = str(scac or "").strip().upper()
        if not carrier_norm or not scac_norm:
            return
        if self._scac_by_carrier.get(carrier_norm) == scac_norm:
            return
        self._scac_by_carrier[carrier_norm] = scac_norm
        _save_tab_map(
            self._scac_path,
            self._scac_by_carrier,
            include=lambda carrier, value: bool(carrier and value),
            on_error=self.on_save_error,
            error_label="carrier SCAC map",
        )

    def resolve_ship_type(self, carrier_name: str) -> str:
        if is_will_advise_carrier(carrier_name):
            return WILL_ADVISE_SHIP_TYPE
        carrier_norm = normalize_carrier_name(carrier_name)
        if carrier_norm and carrier_norm in self._ship_type_by_carrier:
            return self._ship_type_by_carrier[carrier_norm]
        resolved = str(ship_type_from_carrier_name(carrier_name) or config.SYNAPSE_SHIP_TYPE).strip().upper()
        return resolved if resolved in VALID_SHIP_TYPES else config.SYNAPSE_SHIP_TYPE

    def remember_ship_type(self, carrier_name: str, ship_type: str) -> None:
        if is_will_advise_carrier(carrier_name):
            return
        carrier_norm = normalize_carrier_name(carrier_name)
        ship_type_norm = str(ship_type or "").strip().upper()
        if not carrier_norm or ship_type_norm not in VALID_SHIP_TYPES:
            return
        if self._ship_type_by_carrier.get(carrier_norm) == ship_type_norm:
            return
        self._ship_type_by_carrier[carrier_norm] = ship_type_norm
        _save_tab_map(
            self._ship_type_path,
            self._ship_type_by_carrier,
            include=lambda carrier, value: bool(carrier and value in VALID_SHIP_TYPES),
            on_error=self.on_save_error,
            error_label="carrier ship type map",
        )
