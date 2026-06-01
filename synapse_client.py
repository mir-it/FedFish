from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests
import re


def _to_int_qty(value: Any) -> int:
    if value is None:
        raise ValueError("qty_entered is required")
    if isinstance(value, bool):
        raise ValueError("qty_entered must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"qty_entered must be an integer (got {value})")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("qty_entered is required")
        try:
            f = float(s)
        except ValueError:
            raise ValueError(f"qty_entered must be an integer (got {value!r})")
        if f.is_integer():
            return int(f)
        raise ValueError(f"qty_entered must be an integer (got {value!r})")
    raise ValueError(f"qty_entered must be an integer (got {type(value).__name__})")


_SYNAPSE_MDY_DATE_RE = re.compile(r"^(0?[1-9]|1[0-2])[/-](0?[1-9]|[12][0-9]|3[01])[/-](19|20)\d\d")


def _normalize_ship_date(value: Any) -> str:
    """Normalize incoming ship_date to Synapse-friendly MM/DD/YYYY."""
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    s = str(value or "").strip()
    if not s:
        raise ValueError("header.ship_date is required")
    if _SYNAPSE_MDY_DATE_RE.match(s):
        return s
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    # Handle ISO values like 2026-04-23T07:00:00.000+00:00 or trailing Z.
    iso_candidate = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).strftime("%m/%d/%Y")
    except ValueError as e:
        raise ValueError(f"header.ship_date has unsupported format: {s}") from e


@dataclass(frozen=True)
class SynapseConfig:
    base_url: str
    username: str
    password: str


class SynapseCreateOrderError(ValueError):
    def __init__(self, message: str, sent_payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.sent_payload = sent_payload


class SynapseClient:
    def __init__(self, cfg: SynapseConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.last_payload_sent: dict[str, Any] | None = None

    def login(self) -> None:
        resp = self.session.post(
            f"{self.cfg.base_url}/api/login",
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=30,
        )
        resp.raise_for_status()

    def _xsrf_headers(self) -> dict[str, str]:
        token = self.session.cookies.get("XSRF-TOKEN")
        if not token:
            return {"Content-Type": "application/json"}
        return {"X-XSRF-TOKEN": token, "Content-Type": "application/json"}

    def create_order(self, order_data: dict[str, Any]) -> dict[str, Any]:
        """
        Creates an order via Synapse: POST /api/orders/create-order

        Expects order_data shaped like:
          {
            "header": {...},
            "details": [{"item": "...", "uom_entered": "...", "qty_entered": 1, ...}, ...]
          }

        This method uses Synapse schema field names (func/custid/ship_to_* etc).
        """
        hdr_in = order_data.get("header") or {}
        dtl_in = order_data.get("details") or []

        po_number = (hdr_in.get("po_number") or hdr_in.get("reference") or "").strip()
        if not po_number:
            raise ValueError("header.po_number is required")

        custid = (hdr_in.get("custid") or "").strip()
        if not custid:
            raise ValueError("header.custid is required")

        from_facility = (hdr_in.get("from_facility") or "").strip()
        if not from_facility:
            raise ValueError("header.from_facility is required")

        ship_to_name = (hdr_in.get("ship_to_name") or "").strip()[:40]
        ship_to_address_1 = (hdr_in.get("ship_to_address_1") or "").strip()
        ship_to_city = (hdr_in.get("ship_to_city") or "").strip()
        ship_to_state = (hdr_in.get("ship_to_state") or "").strip()
        ship_to_postal_code = (hdr_in.get("ship_to_postal_code") or "").strip()
        ship_to_country_code = (hdr_in.get("ship_to_country_code") or "USA").strip()
        if not all(
            [ship_to_name, ship_to_address_1, ship_to_city, ship_to_state, ship_to_postal_code, ship_to_country_code]
        ):
            raise ValueError(
                "Ship-to fields required: ship_to_name, ship_to_address_1, ship_to_city, "
                "ship_to_state, ship_to_postal_code, ship_to_country_code"
            )
        ship_type = str(hdr_in.get("ship_type") or "S").strip().upper()
        if ship_type not in {"A", "C", "L", "P", "R", "S", "T"}:
            ship_type = "S"
        ship_date = _normalize_ship_date(hdr_in.get("ship_date"))

        # Minimal-but-valid header aligned to the provided schema keys.
        header: dict[str, Any] = {
            "func": hdr_in.get("func", "A"),  # A=add (per your sheet: A/U/R/D)
            "custid": custid,
            "order_type": "O",
            "ship_date": ship_date,
            "po_number": po_number,
            "from_facility": from_facility,
            "priority": hdr_in.get("priority", "A"),
            "ship_type": ship_type,
            "carrier": (hdr_in.get("carrier") or "").strip(),
            "reference": (hdr_in.get("reference") or po_number).strip(),
            "shipment_terms": hdr_in.get("shipment_terms", "3RD"),  # common default in your sheet
            "ship_to_name": ship_to_name,
            "ship_to_address_1": ship_to_address_1,
            "ship_to_city": ship_to_city,
            "ship_to_state": ship_to_state,
            "ship_to_postal_code": ship_to_postal_code,
            "ship_to_country_code": ship_to_country_code,
        }

        # Pass through optional header fields if provided.
        for key in [
            # shipper
            "shipper_name",
            "shipper_contact",
            "shipper_address_1",
            "shipper_address_2",
            "shipper_city",
            "shipper_state",
            "shipper_postal_code",
            "shipper_country_code",
            "shipper_phone",
            "shipper_email",
            "consignee",
            "shipper",
            # bill-to / 3rd party
            "bill_to_name",
            "bill_to_contact",
            "bill_to_address_1",
            "bill_to_city",
            "bill_to_state",
            "bill_to_postal_code",
            "bill_to_country_code",
            "bill_to_phone",
            "bill_to_email",
        ]:
            if key in hdr_in and str(hdr_in.get(key) or "").strip():
                value = str(hdr_in.get(key)).strip()
                if key in {"bill_to_name"}:
                    value = value[:40]
                header[key] = value

        # carrier is marked required in your sheet; enforce non-empty here.
        if not header["carrier"]:
            raise ValueError("header.carrier is required")

        details: list[dict[str, Any]] = []
        for idx, item in enumerate(dtl_in, start=1):
            item_num = (item.get("item") or "").strip()
            uom = (item.get("uom_entered") or "").strip()
            qty = _to_int_qty(item.get("qty_entered"))
            if not item_num or not uom or qty is None:
                raise ValueError("Each detail requires: item, uom_entered, qty_entered")

            lot_number = (item.get("lot_number") or "").strip()
            line: dict[str, Any] = {
                "item": item_num,
                "uom_entered": uom,
                "qty_entered": qty,
                "dtl_pass_thru_num_10": int(item.get("dtl_pass_thru_num_10") or idx),
            }
            # Synapse validation rejects empty string lots; only send if we have a value.
            if lot_number:
                line["lot_number"] = lot_number
            details.append(line)

        payload = {"header": header, "details": details}
        hdrinstruct_in = order_data.get("hdrinstruct") or {}
        hdrinstruct_custid = str(hdrinstruct_in.get("custid") or "").strip()
        hdrinstruct_reference = str(hdrinstruct_in.get("reference") or header.get("reference") or "").strip()
        hdrinstruct_po_number = str(hdrinstruct_in.get("po_number") or header.get("po_number") or "").strip()
        instructions = str(hdrinstruct_in.get("instructions") or "").strip()
        if instructions:
            payload["hdrinstruct"] = {
                "custid": hdrinstruct_custid or "MIRMOS",
                "reference": hdrinstruct_reference,
                "po_number": hdrinstruct_po_number,
                "instructions": instructions[:255],
            }

        self.last_payload_sent = payload
        resp = self.session.post(
            f"{self.cfg.base_url}/api/orders/create-order",
            json=payload,
            headers=self._xsrf_headers(),
            timeout=60,
        )
        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise SynapseCreateOrderError(
                f"Synapse create-order failed ({resp.status_code}): {body}",
                sent_payload=payload,
            )
        try:
            return resp.json()
        except ValueError:
            return {"status_code": resp.status_code, "text": resp.text}

    def order_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/orders/order-info with orderid, shipid, custid, po, reference."""
        resp = self.session.post(
            f"{self.cfg.base_url}/api/orders/order-info",
            json=payload,
            headers=self._xsrf_headers(),
            timeout=60,
        )
        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise ValueError(f"Synapse order-info failed ({resp.status_code}): {body}")
        try:
            return resp.json()
        except ValueError:
            return {"status_code": resp.status_code, "text": resp.text}

