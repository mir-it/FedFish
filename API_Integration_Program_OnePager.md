# API Integration Function One-Pager (FedFish)

## How I Implemented It

I built a Python desktop integration (`app.py`) that orchestrates two systems:
- **Fishbowl API** as the source of packed shipment data.
- **Synapse API** as the destination for outbound orders.

The app has three functional layers:
1. **Data extraction** from Fishbowl using SQL via API.
2. **Transformation/validation** into Synapse-required schema.
3. **Submission and status tracking** for successful or failed posts.

## End-to-End Data Flow

1. User logs in from the UI; app calls Fishbowl auth and stores bearer token in session.
2. App pulls all packed shipments for the configured location group (`queries.SHIPMENTS_SQL`).
3. When a shipment is selected (or bulk send runs), app pulls line-level shipment items (`queries.items_sql_for(ship_num)`).
4. App also pulls supporting records for header completion:
   - `queries.ship_sql_for(ship_num)`
   - `queries.so_sql_for(ship_num)`
   - `queries.customer_sql_for(customer_id)`
   - `queries.state_code_sql_for(state_id)` when state normalization is needed.
5. App aggregates and normalizes lines:
   - Excludes non-shippable item keywords.
   - Normalizes lot strings.
   - Converts/validates UOM and quantity (SF->EA using product coverage map when needed).
   - Applies carrier->SCAC and carrier->ship type mappings.
6. App builds `order_data = {header, details, optional hdrinstruct}`.
7. App logs in to Synapse, POSTs create-order payload, and records `SENT`/`FAILED` by shipment.

## Fishbowl Queries Used

- **Shipment list query** (`SHIPMENTS_SQL`):
  - Filters by `ship.statusId = 20` (packed).
  - Filters by `locationgroup` (currently Jersey City, NJ).
  - Joins `ship`, `shipcarton`, `locationgroup`, `stateconst`.
  - Returns ship number, destination city/state/zip, and pallet dimensions/weight.

- **Item detail query** (`SHIPMENT_ITEMS_SQL_TEMPLATE` via `items_sql_for`):
  - Joins `ship`, `so`, `soitem`, `product`, `uom`, and state.
  - Returns PO, ship-to fields, item, qty, UOM, order index, schedule date.
  - Pulls first carton carrier and first non-empty lot/tracking value.

- **Context queries**:
  - `ship_sql_for` -> complete ship header record.
  - `so_sql_for` -> sales order record tied to shipment.
  - `customer_sql_for` -> customer defaults for billing fallback.
  - `state_code_sql_for` -> converts numeric state IDs to state codes.

## API Endpoints and Request Flow

### Fishbowl (source)
- **POST** `/api/login`
  - Payload: app metadata + Fishbowl username/password.
  - Response: bearer token saved in `FishbowlClient`.

- **GET** `/api/data-query`
  - Headers: `Authorization: Bearer <token>`, `Content-Type: application/sql`
  - Body (`data`): SQL statement string from `queries.py`.
  - Used repeatedly for shipment list, item detail, and context lookups.

- **POST** `/api/logout`
  - Called on app close/logout to cleanly close session.

### Synapse (destination)
- **POST** `/api/login`
  - Payload: Synapse username/password.
  - Establishes authenticated session/cookies.

- **POST** `/api/orders/create-order`
  - Payload:
    - `header`: custid, po/reference, facility, carrier, ship_type, shipment_terms, ship-to, optional bill-to.
    - `details`: item, uom_entered, qty_entered, lot_number (when present/required), pass-through index.
    - `hdrinstruct` (optional): shipment instructions.
  - Headers include XSRF token when cookie exists.
  - Non-2xx responses raise structured error with sent payload attached for troubleshooting.

## Controls Built Into the Flow

- Required field checks before submit: PO, ship-to address fields, ship date, carrier, integer quantities.
- Lot enforcement when `SYNAPSE_REQUIRE_LOT` is enabled.
- Header field normalization and max-length enforcement before POST.
- Local memory files prevent duplicate sends and preserve carrier mapping decisions:
  - `synapse_sent_shipments.txt`
  - `carrier_scac_map.txt`
  - `carrier_ship_type_map.txt`

## Operational Result

The integration now runs as a repeatable pipeline: **Fishbowl packed shipment -> SQL extraction -> mapped/validated payload -> Synapse create-order POST -> tracked outcome per shipment**.

