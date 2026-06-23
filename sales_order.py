from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import config
import queries
from carrier_resolvers import is_fedex_carrier, is_ups_carrier, normalize_terms


def _row_get_any(row: dict | None, *keys: str):
    if not row:
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        k = key.lower()
        if k in lowered and lowered[k] not in (None, ""):
            return lowered[k]
    return None


def _query_first(fb, sql: str) -> dict:
    try:
        rows = fb.data_query(sql)
    except Exception:
        return {}
    return rows[0] if rows else {}


def _synapse_reference_from_so_num(raw_so_num) -> str:
    so_num = str(raw_so_num or "").strip()
    if not so_num:
        return ""
    if so_num[:1].upper() == "S":
        return so_num[1:].strip()
    return so_num


def _split_ship_to_address(raw: str, customer_name: str = "") -> tuple[str, str]:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if str(customer_name or "").strip() != "Ecom HD":
        return " ".join(line.strip() for line in text.split("\n") if line.strip()), ""
    if "\n" not in text:
        return text, ""
    first_line, rest = text.split("\n", 1)
    other_lines = [part.strip() for part in rest.split("\n") if part.strip()]
    return " ".join(other_lines), first_line.strip()


def _consignee_for_carrier(raw_name: str, customer_name: str = "") -> str:
    customer = str(customer_name or "").strip()
    if is_ups_carrier(raw_name):
        if customer == "Ecom HD":
            return "HDMIR"
        return "MIRUPS"
    if is_fedex_carrier(raw_name):
        if customer == "Ecom LS":
            return "LOWMIR"
        return "MIRFEX"
    return ""


_HEADER_FIELD_LIMITS: dict[str, int] = {
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

_HEADER_UPPERCASE_FIELDS: set[str] = {
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


def _apply_header_field_limits(header: dict) -> dict:
    normalized: dict[str, str] = {}
    for key, value in header.items():
        text = str(value or "").strip()
        if key in _HEADER_UPPERCASE_FIELDS:
            text = text.upper()
        max_len = _HEADER_FIELD_LIMITS.get(key)
        if max_len is not None:
            text = text[:max_len]
        normalized[key] = text
    return normalized


def make_state_resolver(fb) -> Callable[[object], str]:
    cache: dict[int, str] = {}

    def resolve_state_id(state_id) -> str:
        try:
            state_int = int(str(state_id).strip())
        except (TypeError, ValueError):
            return ""
        if state_int in cache:
            return cache[state_int]
        row = _query_first(fb, queries.state_code_sql_for(state_int))
        code = str(_row_get_any(row, "code") or "").strip()
        cache[state_int] = code
        return code

    return resolve_state_id


@dataclass(frozen=True)
class SalesOrderSummary:
    so_num: str
    customer_name: str
    city: str
    state: str
    zip_code: str
    carrier_name: str

    @classmethod
    def from_row(cls, row: dict) -> SalesOrderSummary:
        return cls(
            so_num=str(row.get("so_num") or "").strip(),
            customer_name=str(row.get("customer_name") or "").strip(),
            city=str(row.get("shipToCity") or "").strip(),
            state=str(row.get("ship_to_state") or "").strip(),
            zip_code=str(row.get("shipToZip") or "").strip(),
            carrier_name=str(row.get("carrier_name") or "").strip(),
        )

    @property
    def location_title(self) -> str:
        return f"{self.city}, {self.state}  {self.zip_code}".strip()


@dataclass(frozen=True)
class OrderLine:
    item_num: str
    qty: float
    uom: str
    tracking: str
    order_index: str
    soitem_note: str

    @classmethod
    def from_row(cls, row: dict) -> OrderLine:
        qty_raw = row.get("qty") or 0
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 0.0
        return cls(
            item_num=str(row.get("item_num") or "").strip(),
            qty=qty,
            uom=str(row.get("uom") or "").strip(),
            tracking=str(row.get("tracking") or "").strip(),
            order_index=str(row.get("order_index") or "").strip(),
            soitem_note=str(row.get("soitem_note") or "").strip(),
        )


@dataclass(frozen=True)
class SynapseHeaderOverrides:
    carrier_scac: str
    ship_type: str
    shipment_terms: str
    delivery_service: str = ""


@dataclass(frozen=True)
class SynapseDetailLine:
    item: str
    send_uom: str
    send_qty: int
    lot_number: str
    dtl_pass_thru_num_10: int | None = None


@dataclass
class SalesOrder:
    so_num: str
    po_number: str
    carrier_name: str
    carrier_service_name: str
    customer_name: str
    ship_to_name: str
    ship_to_address_raw: str
    ship_to_city: str
    ship_to_state: str
    ship_to_postal_code: str
    bill_to_name: str
    bill_to_address_1: str
    bill_to_city: str
    bill_to_state: str
    bill_to_postal_code: str
    bill_to_country_code: str
    so_note: str
    ship_date: str = ""
    lines: list[OrderLine] = field(default_factory=list)

    def resolved_instructions(self, manual_override: str = "") -> str:
        manual = str(manual_override or "").strip()
        if manual:
            return manual
        notes = [line.soitem_note for line in self.lines if line.soitem_note]
        if notes:
            return "; ".join(notes)
        return self.so_note

    def aggregated_review_lines(
        self,
        *,
        should_exclude_item: Callable[[str], bool],
        normalize_lot,
        normalize_uom,
    ) -> list[dict]:
        aggregated: dict[tuple[str, str, str], float] = {}
        passthru_map: dict[tuple[str, str, str], str] = {}
        note_map: dict[tuple[str, str, str], str] = {}
        for line in self.lines:
            item_num = line.item_num
            if should_exclude_item(item_num):
                continue
            tracking = normalize_lot(line.tracking)
            uom = normalize_uom(line.uom)
            if not item_num or not uom:
                continue
            key = (item_num, uom, tracking)
            aggregated[key] = aggregated.get(key, 0.0) + line.qty
            if line.order_index and key not in passthru_map:
                passthru_map[key] = line.order_index
            if line.soitem_note and key not in note_map:
                note_map[key] = line.soitem_note
        review_lines: list[dict] = []
        for (item_num, uom, tracking), qty in aggregated.items():
            if not qty:
                continue
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
                    "soitem_note": note_map.get((item_num, uom, tracking), ""),
                }
            )
        return review_lines

    def email_item_lines(self) -> list[str]:
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for line in self.lines:
            if not line.item_num:
                continue
            key = (line.item_num, line.soitem_note)
            if key in seen:
                continue
            seen.add(key)
            if line.soitem_note:
                lines.append(f"{line.item_num} - {line.soitem_note}")
            else:
                lines.append(line.item_num)
        return lines

    def to_debug_dict(self) -> dict:
        data = asdict(self)
        data["lines"] = [asdict(line) for line in self.lines]
        return data

    def build_synapse_payload(
        self,
        *,
        header: SynapseHeaderOverrides,
        details: list[SynapseDetailLine],
        instructions: str = "",
        normalize_lot,
    ) -> dict:
        if not self.po_number:
            raise ValueError("This sales order has no PO number in Fishbowl.")
        if not all([self.ship_to_address_raw, self.ship_to_city, self.ship_to_state, self.ship_to_postal_code]):
            raise ValueError(
                "Fishbowl is missing one or more ship-to fields (address/city/state/zip) for this sales order."
            )
        if not details:
            raise ValueError("Could not build any detail lines from Fishbowl items.")
        if not self.ship_date:
            raise ValueError("No dateFirstShip was found for this sales order.")

        ship_to_address_1, ship_to_address_2 = _split_ship_to_address(
            self.ship_to_address_raw,
            self.customer_name,
        )
        shipment_terms = normalize_terms(header.shipment_terms) or (
            config.SYNAPSE_SHIPMENT_TERMS or ""
        ).strip().upper()
        ship_type = str(header.ship_type or config.SYNAPSE_SHIP_TYPE).strip().upper()

        order_data: dict = {
            "header": {
                "func": "A",
                "custid": config.SYNAPSE_CUSTID,
                "po_number": self.po_number,
                "order_type": "O",
                "reference": _synapse_reference_from_so_num(self.so_num),
                "from_facility": config.SYNAPSE_FROM_FACILITY,
                "carrier": header.carrier_scac,
                "ship_type": ship_type,
                "shipment_terms": shipment_terms or config.SYNAPSE_SHIPMENT_TERMS,
                "ship_date": self.ship_date,
                "ship_to_name": (self.ship_to_name or "SHIP TO")[:40],
                "ship_to_address_1": ship_to_address_1,
                "ship_to_city": self.ship_to_city,
                "ship_to_state": self.ship_to_state,
                "ship_to_postal_code": self.ship_to_postal_code,
                "ship_to_country_code": "USA",
            },
            "details": [
                {
                    "item": d.item,
                    "uom_entered": d.send_uom,
                    "qty_entered": d.send_qty,
                    "dtl_pass_thru_num_10": d.dtl_pass_thru_num_10,
                    "lot_number": normalize_lot(d.lot_number),
                }
                for d in details
            ],
        }

        if ship_to_address_2:
            order_data["header"]["ship_to_address_2"] = ship_to_address_2
        consignee = _consignee_for_carrier(self.carrier_name, self.customer_name)
        if consignee:
            order_data["header"]["consignee"] = consignee
        if shipment_terms == "3RD":
            order_data["header"].update(
                {
                    "bill_to_name": self.bill_to_name,
                    "bill_to_address_1": self.bill_to_address_1,
                    "bill_to_city": self.bill_to_city,
                    "bill_to_state": self.bill_to_state,
                    "bill_to_postal_code": self.bill_to_postal_code,
                    "bill_to_country_code": self.bill_to_country_code,
                }
            )
        if header.delivery_service:
            order_data["header"]["delivery_service"] = header.delivery_service
        order_data["header"] = _apply_header_field_limits(order_data["header"])

        instructions_text = str(instructions or "").strip()
        if instructions_text:
            order_data["hdrinstruct"] = {
                "custid": config.SYNAPSE_CUSTID,
                "reference": str(order_data["header"].get("reference") or "").strip(),
                "po_number": str(order_data["header"].get("po_number") or "").strip(),
                "instructions": instructions_text[:255],
            }
        return order_data

    @classmethod
    def load(cls, fb, so_num: str, *, so_row: dict) -> SalesOrder:
        if not so_row:
            raise ValueError(
                f"No cached sales-order row for SO {so_num}. Refresh the shipments list and open the order again."
            )

        resolve_state_id = make_state_resolver(fb)
        item_rows = fb.data_query(queries.items_sql_for(so_num))
        lines = [OrderLine.from_row(row) for row in item_rows]

        ship_to_state = str(so_row.get("ship_to_state") or "").strip()
        if not ship_to_state:
            ship_to_state_id = so_row.get("shipToStateId")
            if ship_to_state_id not in (None, ""):
                ship_to_state = resolve_state_id(ship_to_state_id)

        bill_to_state = str(so_row.get("bill_to_state") or "").strip()
        if not bill_to_state:
            bill_to_state_id = so_row.get("billToStateId")
            if bill_to_state_id not in (None, ""):
                bill_to_state = resolve_state_id(bill_to_state_id)

        return cls(
            so_num=str(so_row.get("so_num") or so_num).strip(),
            po_number=str(so_row.get("customerPO") or "").strip(),
            carrier_name=str(so_row.get("carrier_name") or "").strip(),
            carrier_service_name=str(so_row.get("carrier_service_name") or "").strip(),
            customer_name=str(so_row.get("customer_name") or "").strip(),
            ship_to_name=str(so_row.get("shipToName") or "").strip(),
            ship_to_address_raw=str(so_row.get("shipToAddress") or "").strip(),
            ship_to_city=str(so_row.get("shipToCity") or "").strip(),
            ship_to_state=ship_to_state,
            ship_to_postal_code=str(so_row.get("shipToZip") or "").strip(),
            bill_to_name=str(so_row.get("billToName") or so_row.get("customer_name") or "").strip()[:40],
            bill_to_address_1=str(so_row.get("billToAddress") or "").strip(),
            bill_to_city=str(so_row.get("billToCity") or "").strip(),
            bill_to_state=bill_to_state,
            bill_to_postal_code=str(so_row.get("billToZip") or "").strip(),
            bill_to_country_code="USA",
            so_note=str(so_row.get("note") or "").strip(),
            ship_date=str(so_row.get("dateFirstShip") or "").strip(),
            lines=lines,
        )


def load_order_summaries(fb) -> tuple[list[SalesOrderSummary], dict[str, dict]]:
    rows = fb.data_query(queries.SHIPMENTS_SQL)
    summaries: list[SalesOrderSummary] = []
    cached_so_rows: dict[str, dict] = {}
    for row in rows:
        summary = SalesOrderSummary.from_row(row)
        if not summary.so_num:
            continue
        summaries.append(summary)
        cached_so_rows[summary.so_num] = row
    return summaries, cached_so_rows


SYNAPSE_LAST_SEND_FILE = "synapse_last_send_map.txt"


class SynapseLastSendStore:
    """Persist last Synapse payload/response per SO for the Show payload debug panel."""

    def __init__(self, base_dir: Path | None = None):
        base = base_dir or Path(__file__).parent
        self._path = base / SYNAPSE_LAST_SEND_FILE
        self._by_so: dict[str, dict] = self._load()
        self.on_save_error: Callable[[str], None] | None = None

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for line in lines:
            text = line.strip()
            if not text or "\t" not in text:
                continue
            so_num, raw_json = text.split("\t", 1)
            so_key = so_num.strip()
            if not so_key:
                continue
            try:
                record = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict) and isinstance(record.get("payload"), dict):
                out[so_key] = record
        return out

    def _save(self) -> None:
        lines = []
        for so_num in sorted(self._by_so):
            record = self._by_so.get(so_num) or {}
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            lines.append(f"{so_num}\t{json.dumps(record, separators=(',', ':'))}")
        payload_text = "\n".join(lines)
        if payload_text:
            payload_text += "\n"
        try:
            self._path.write_text(payload_text, encoding="utf-8")
        except Exception as e:
            if self.on_save_error:
                self.on_save_error(f"Warning: failed saving Synapse last-send map ({e}).")

    def apply_to(self, session: SalesOrderSession) -> None:
        record = self._by_so.get(session.so_num)
        if not record:
            return
        session.synapse_payload = record.get("payload")
        session.synapse_response = record.get("response")

    def remember(self, so_num: str, payload: dict | None, response=None) -> None:
        if not isinstance(payload, dict):
            return
        so_key = str(so_num or "").strip()
        if not so_key:
            return
        record: dict = {"payload": payload}
        if response is not None:
            record["response"] = response
        if self._by_so.get(so_key) == record:
            return
        self._by_so[so_key] = record
        self._save()


@dataclass
class SalesOrderSession:
    """Fishbowl sales order plus last Synapse send payload/response for one SO."""

    order: SalesOrder
    synapse_payload: dict | None = None
    synapse_response: dict | None = None

    @property
    def so_num(self) -> str:
        return self.order.so_num

    @classmethod
    def load(
        cls,
        fb,
        so_num: str,
        *,
        so_row: dict,
        last_send_store: SynapseLastSendStore | None = None,
    ) -> SalesOrderSession:
        session = cls(order=SalesOrder.load(fb, so_num, so_row=so_row))
        if last_send_store:
            last_send_store.apply_to(session)
        return session

    def record_synapse_send(
        self,
        payload: dict | None,
        response=None,
        *,
        store: SynapseLastSendStore | None = None,
    ) -> None:
        if isinstance(payload, dict):
            self.synapse_payload = payload
        if response is not None:
            self.synapse_response = response
        if store is not None and isinstance(self.synapse_payload, dict):
            store.remember(self.so_num, self.synapse_payload, self.synapse_response)

    def has_raw_display_data(self, draft_payload: dict | None = None) -> bool:
        return any((self.synapse_payload, draft_payload, self.synapse_response))

    def debug_bundle(self, *, draft_payload: dict | None = None) -> dict:
        return {
            "so_num": self.so_num,
            "sales_order": self.order.to_debug_dict(),
            "synapse_payload": self.synapse_payload or draft_payload,
            "synapse_response": self.synapse_response,
        }
