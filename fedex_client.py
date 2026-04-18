import time
from datetime import date

import requests


DEFAULT_FREIGHT_CLASS = "CLASS_050"
DEFAULT_SERVICE_TYPE = "FEDEX_FREIGHT_PRIORITY"


class FedexError(Exception):
    pass


class FedexClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_number: str,
        base_url: str = "https://apis-sandbox.fedex.com",
        timeout: int = 30,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_number = account_number
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        resp = self._session.post(
            f"{self.base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise FedexError(f"OAuth failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def build_payload(
        self,
        shipper: dict,
        recipient: dict,
        pallets: list[dict],
        ship_date: str | None = None,
        service_type: str = DEFAULT_SERVICE_TYPE,
        freight_class: str = DEFAULT_FREIGHT_CLASS,
    ) -> dict:
        ship_date = ship_date or date.today().isoformat()
        total_units = len(pallets) or 1

        line_items = []
        requested_packages = []
        for i, p in enumerate(pallets, start=1):
            weight = float(p.get("weight") or 0)
            line_items.append({
                "id": f"item-{i}",
                "freightClass": freight_class,
                "pieces": 1,
                "weight": {"units": "LB", "value": weight},
                "subPackagingType": "PALLET",
            })
            requested_packages.append({
                "subPackagingType": "PALLET",
                "weight": {"units": "LB", "value": weight},
            })

        return {
            "accountNumber": {"value": self.account_number},
            "rateRequestControlParameters": {
                "returnTransitTimes": True,
                "rateSortOrder": "SERVICENAMETRADITIONAL",
            },
            "freightRequestedShipment": {
                "shipper": {"address": shipper},
                "recipient": {"address": recipient},
                "serviceType": service_type,
                "shipDateStamp": ship_date,
                "pickupType": "USE_SCHEDULED_PICKUP",
                "shippingChargesPayment": {
                    "paymentType": "SENDER",
                    "payor": {
                        "responsibleParty": {
                            "accountNumber": {"value": self.account_number}
                        }
                    },
                },
                "freightShipmentDetail": {
                    "role": "SHIPPER",
                    "totalHandlingUnits": total_units,
                    "lineItem": line_items,
                },
                "requestedPackageLineItems": requested_packages,
            },
        }

    def rate_quote(self, payload: dict) -> dict:
        token = self._get_token()
        resp = self._session.post(
            f"{self.base_url}/rate/v1/freight/rates/quotes",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-locale": "en_US",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise FedexError(f"Rate quote failed ({resp.status_code}): {resp.text}")
        return resp.json()

    @staticmethod
    def extract_rates(response: dict) -> list[dict]:
        out = []
        details = (
            response.get("output", {}).get("rateReplyDetails")
            or response.get("rateReplyDetails")
            or []
        )
        for d in details:
            service = d.get("serviceName") or d.get("serviceType") or ""
            transit = d.get("commit", {}).get("transitTime") or d.get("transitTime") or ""
            net_charge = ""
            currency = ""
            shipments = d.get("ratedShipmentDetails") or []
            if shipments:
                total = shipments[0].get("totalNetCharge") or shipments[0].get("shipmentRateDetail", {}).get("totalNetCharge")
                if isinstance(total, dict):
                    net_charge = total.get("amount", "")
                    currency = total.get("currency", "")
                else:
                    net_charge = total or ""
            out.append({
                "service": service,
                "transit": transit,
                "net_charge": net_charge,
                "currency": currency,
            })
        return out
