import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


FB_HOST = _get("FB_HOST", "192.168.0.132")
FB_PORT = _get("FB_PORT", "80")
FB_USERNAME = _get("FB_USERNAME", "bayel_nj")
FB_PASSWORD = _get("FB_PASSWORD", "Tile2026@")

SHIPPER_CITY = _get("SHIPPER_CITY", "TAMPA")
SHIPPER_STATE = _get("SHIPPER_STATE", "FL")
SHIPPER_ZIP = _get("SHIPPER_ZIP", "33610")
SHIPPER_COUNTRY = _get("SHIPPER_COUNTRY", "US")
SHIPPER_NAME = _get("SHIPPER_NAME", "SHIPPER")
SHIPPER_ADDRESS_1 = _get("SHIPPER_ADDRESS_1", "")

SYNAPSE_BASE_URL = _get("SYNAPSE_BASE_URL", "https://phoenix.zethconapp.com/test")
SYNAPSE_USERNAME = _get("SYNAPSE_USERNAME", "MIRAPI")
SYNAPSE_PASSWORD = _get("SYNAPSE_PASSWORD", "1wHt6cpg4PpL")
SYNAPSE_CUSTID = _get("SYNAPSE_CUSTID", "MIRMOS")
SYNAPSE_FROM_FACILITY = _get("SYNAPSE_FROM_FACILITY", "PNP")
SYNAPSE_TO_FACILITY = _get("SYNAPSE_TO_FACILITY", "PNP")
SYNAPSE_CARRIER = _get("SYNAPSE_CARRIER", "9999")
SYNAPSE_GENERATE_LABELS = _get("SYNAPSE_GENERATE_LABELS", "N").strip().upper() in {"Y", "YES", "TRUE", "1"}
_ship_type = _get("SYNAPSE_SHIP_TYPE", "S").strip().upper()
SYNAPSE_SHIP_TYPE = _ship_type if _ship_type in {"A", "C", "L", "P", "R", "S", "T"} else "S"
SYNAPSE_SHIPMENT_TERMS = _get("SYNAPSE_SHIPMENT_TERMS", "PPD")
SYNAPSE_INVENTORY_STATUS = _get("SYNAPSE_INVENTORY_STATUS", "20")
SYNAPSE_REQUIRE_LOT = _get("SYNAPSE_REQUIRE_LOT", "Y").strip().upper() in {"Y", "YES", "TRUE", "1"}
SYNAPSE_CANCEL_AFTER_DAYS = int(_get("SYNAPSE_CANCEL_AFTER_DAYS", "7") or "7")

# Third-party billing (required when shipment terms are 3RD)
SYNAPSE_BILLTO_ACCOUNT = _get("SYNAPSE_BILLTO_ACCOUNT", "")
SYNAPSE_BILLTO_NAME = _get("SYNAPSE_BILLTO_NAME", "")
SYNAPSE_BILLTO_ADDRESS_1 = _get("SYNAPSE_BILLTO_ADDRESS_1", "")
SYNAPSE_BILLTO_ADDRESS_2 = _get("SYNAPSE_BILLTO_ADDRESS_2", "")
SYNAPSE_BILLTO_CITY = _get("SYNAPSE_BILLTO_CITY", "")
SYNAPSE_BILLTO_STATE = _get("SYNAPSE_BILLTO_STATE", "")
SYNAPSE_BILLTO_POSTAL_CODE = _get("SYNAPSE_BILLTO_POSTAL_CODE", "")
SYNAPSE_BILLTO_COUNTRY_CODE = _get("SYNAPSE_BILLTO_COUNTRY_CODE", "USA")

# Local product master export used to convert SF -> EA for Synapse
_product_csv = _get("PRODUCT_COVERAGE_CSV", "").strip()
if not _product_csv:
    # Convenience: if user drops the file in the project folder
    # and names it PRODUCT_COVERAGE_CSV or PRODUCT_COVERAGE_CSV.csv
    _here = os.path.dirname(__file__)
    for name in ("PRODUCT_COVERAGE_CSV", "PRODUCT_COVERAGE_CSV.csv"):
        candidate = os.path.join(_here, name)
        if os.path.exists(candidate):
            _product_csv = candidate
            break
PRODUCT_COVERAGE_CSV = _product_csv

# Filter out non-shippable/misc fee lines that appear as SO items in Fishbowl.
# Comma-separated keywords matched against item number (case-insensitive).
EXCLUDE_ITEM_KEYWORDS = _get("EXCLUDE_ITEM_KEYWORDS", "shipping,freight,fee")

APP_NAME = "BOL_Automator"
APP_DESCRIPTION = "LTL Shipping Automation"
APP_ID = 101
