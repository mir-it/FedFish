from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests


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


def _synapse_date(value: Any) -> str:
    """
    Synapse expects dates as MM/DD/YYYY (per API validation pattern).
    Accepts date/datetime/ISO-string (YYYY-MM-DD) or already-formatted strings.
    Falls back to today's date.
    """
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, str) and value.strip():
        s = value.strip()
        # Convert ISO date -> Synapse date
        try:
            d = date.fromisoformat(s)
            return d.strftime("%m/%d/%Y")
        except ValueError:
            return s
    return date.today().strftime("%m/%d/%Y")


@dataclass(frozen=True)
class SynapseConfig:
    base_url: str
    username: str
    password: str


class SynapseClient:
    def __init__(self, cfg: SynapseConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()

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

        ship_to_name = (hdr_in.get("ship_to_name") or "").strip()
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

        # Minimal-but-valid header aligned to the provided schema keys.
        header: dict[str, Any] = {
            "func": hdr_in.get("func", "A"),  # A=add (per your sheet: A/U/R/D)
            "custid": custid,
            "order_type": hdr_in.get("order_type", "A"),
            "appointment_date": _synapse_date(hdr_in.get("appointment_date")),
            "ship_date": _synapse_date(hdr_in.get("ship_date")),
            "po_number": po_number,
            "from_facility": from_facility,
            "to_facility": (hdr_in.get("to_facility") or from_facility).strip(),
            "priority": hdr_in.get("priority", "A"),
            "ship_type": hdr_in.get("ship_type", "N"),
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
            "bill_to_account",
            "bill_to_name",
            "bill_to_contact",
            "bill_to_address_1",
            "bill_to_address_2",
            "bill_to_city",
            "bill_to_state",
            "bill_to_postal_code",
            "bill_to_country_code",
            "bill_to_phone",
            "bill_to_email",
            # dates/windows commonly used for allocation
            "cancel_after",
            "requested_ship",
            "ship_not_before",
            "ship_no_later",
        ]:
            if key in hdr_in and str(hdr_in.get(key) or "").strip():
                # date-like fields must be MM/DD/YYYY
                if key in {"cancel_after", "requested_ship", "ship_not_before", "ship_no_later"}:
                    header[key] = _synapse_date(hdr_in.get(key))
                else:
                    header[key] = str(hdr_in.get(key)).strip()

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

            inventory_status = (item.get("inventory_status") or "").strip()
            lot_number = (item.get("lot_number") or "").strip()
            line: dict[str, Any] = {
                "item": item_num,
                "uom_entered": uom,
                "qty_entered": qty,
                # Your prior code used this as the line number passthrough.
                "dtl_pass_thru_num_10": int(item.get("dtl_pass_thru_num_10") or idx),
            }
            # Synapse validation rejects empty strings; only send inventory status if present.
            if inventory_status:
                line["inventory_status_ind"] = "Y"
                line["inventory_status"] = inventory_status
            # Synapse validation rejects empty string lots; only send if we have a value.
            if lot_number:
                line["lot_number"] = lot_number
            details.append(line)

        payload = {"header": header, "details": details}

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
            raise ValueError(f"Synapse create-order failed ({resp.status_code}): {body}")
        try:
            return resp.json()
        except ValueError:
            return {"status_code": resp.status_code, "text": resp.text}

