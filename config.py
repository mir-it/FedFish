import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


FB_HOST = _get("FB_HOST", "localhost")
FB_PORT = _get("FB_PORT", "443")
FB_USERNAME = _get("FB_USERNAME")
FB_PASSWORD = _get("FB_PASSWORD")

FEDEX_CLIENT_ID = _get("FEDEX_CLIENT_ID")
FEDEX_CLIENT_SECRET = _get("FEDEX_CLIENT_SECRET")
FEDEX_ACCOUNT_NUMBER = _get("FEDEX_ACCOUNT_NUMBER")
FEDEX_BASE_URL = _get("FEDEX_BASE_URL", "https://apis-sandbox.fedex.com")

SHIPPER_CITY = _get("SHIPPER_CITY", "TAMPA")
SHIPPER_STATE = _get("SHIPPER_STATE", "FL")
SHIPPER_ZIP = _get("SHIPPER_ZIP", "33610")
SHIPPER_COUNTRY = _get("SHIPPER_COUNTRY", "US")

APP_NAME = "BOL_Automator"
APP_DESCRIPTION = "LTL Shipping Automation"
APP_ID = 101
