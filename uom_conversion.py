from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProductCoverage:
    item_code: str
    coverage_sf_per_ea: float | None  # sf per piece/sheet ("EA")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_coverage_map_from_csv(csv_path: str | Path) -> dict[str, ProductCoverage]:
    """
    Builds a map of MIR Product Code -> coverage in square feet per EA (piece/sheet).

    Uses column: "Sheet or piece Coverage sf"
    """
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    with p.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        out: dict[str, ProductCoverage] = {}
        for row in reader:
            code = (row.get("MIR Product Code") or "").strip()
            if not code:
                continue
            code_key = code.upper()
            coverage = _to_float(row.get("Sheet or piece Coverage sf"))
            out[code_key] = ProductCoverage(item_code=code, coverage_sf_per_ea=coverage)
        return out


def normalize_uom(uom: str) -> str:
    s = (uom or "").strip().upper()
    if s in {"SF", "SQFT", "SQ.FT.", "SQ.FT", "SQUARE FEET", "SQUARE FOOT"}:
        return "SF"
    if s in {"EA", "EACH", "EACHES"}:
        return "EA"
    if s in {"PCS", "PC", "PIECE", "PIECES"}:
        return "PCS"
    if s in {"BOX", "BX"}:
        return "BOX"
    return s


def suggest_each_qty(qty_sf: float, coverage_sf_per_ea: float) -> tuple[int, bool]:
    """
    Returns (suggested_ea_qty, was_fractional).
    If the conversion is fractional, we round UP and mark it fractional so the user can review.
    """
    if coverage_sf_per_ea <= 0:
        raise ValueError("coverage_sf_per_ea must be > 0")
    raw = qty_sf / coverage_sf_per_ea
    rounded = int(raw) if float(raw).is_integer() else int(raw) + 1
    return rounded, (not float(raw).is_integer())

