# FedFish — Fishbowl → FedEx Freight Integration

Desktop app that pulls packed shipments from Fishbowl Server and requests LTL
freight rate quotes from the FedEx Freight sandbox.

## Setup

```
pip install -r requirements.txt
cp .env.example .env
# fill in Fishbowl host/creds and FedEx API key / secret / account number
python app.py
```

## Flow

1. **Login** — enter Fishbowl host/port/username/password (prefilled from `.env`).
2. **Shipments** — table of packed shipments (statusId=20) for the configured
   location group.
3. **Pallet detail** — select a shipment to see its pallets and line items.
4. **Get FedEx Rates** — builds the FedEx payload from the pallet data and
   POSTs to `/rate/v1/freight/rates/quotes`. Rates render in a table; toggle
   "Show raw response" to see the full JSON.

## Notes

- Location group filter and packed-status ID live at the top of `queries.py`.
- Default freight class is `CLASS_050` (`fedex_client.py`).
- FedEx token is cached in-memory for the session.
- Package for Windows with `pyinstaller --onefile --windowed app.py`.
