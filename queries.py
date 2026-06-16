LOCATION_GROUP = "Jersey City, NJ"
PACKED_STATUS_ID = 20

SHIPMENTS_SQL = f"""
SELECT
    so.num AS ship_num,
    so.*,
    COALESCE(ship.shipToCity, '') AS city,
    COALESCE(stateconst.code, '') AS state,
    COALESCE(ship.shipToZip, '') AS zip,
    shipcarton.freightweight AS weight,
    shipcarton.len AS length,
    shipcarton.width AS width,
    shipcarton.height AS height,
    (
        SELECT c.name
        FROM shipcarton sc
        LEFT JOIN carrier c ON c.id = sc.carrierId
        WHERE sc.shipId = ship.id
        ORDER BY sc.id
        LIMIT 1
    ) AS carrier_name,
    COALESCE(customer.name, '') AS customer_name,
    lg.name AS location_group
FROM so
JOIN locationgroup lg ON so.locationGroupId = lg.id
LEFT JOIN customer ON customer.id = so.customerId
LEFT JOIN ship ON ship.id = (
    SELECT s.id
    FROM ship s
    WHERE s.soId = so.id
      AND s.statusId = {PACKED_STATUS_ID}
    ORDER BY s.id DESC
    LIMIT 1
)
LEFT JOIN shipcarton ON ship.id = shipcarton.shipId
LEFT JOIN stateconst ON ship.shipToStateId = stateconst.id
WHERE EXISTS (
    SELECT 1
    FROM ship sx
    WHERE sx.soId = so.id
      AND sx.statusId = {PACKED_STATUS_ID}
)
  AND lg.name = '{LOCATION_GROUP}'
ORDER BY so.num
"""

SHIPMENT_ITEMS_SQL_TEMPLATE = f"""
SELECT
    ship.num AS ship_num,
    so.customerPO AS po_number,
    ship.shipToName AS ship_to_name,
    ship.shipToAddress AS address_1,
    ship.shipToCity AS city,
    stateconst.code AS state,
    ship.shipToZip AS zip,
    product.num AS item_num,
    (
        SELECT sc.carrierId
        FROM shipcarton sc
        WHERE sc.shipId = ship.id
        ORDER BY sc.id
        LIMIT 1
    ) AS carrier_id,
    (
        SELECT c.name
        FROM shipcarton sc
        LEFT JOIN carrier c ON c.id = sc.carrierId
        WHERE sc.shipId = ship.id
        ORDER BY sc.id
        LIMIT 1
    ) AS carrier_name,
    COALESCE(
        (
            SELECT ttv.info
            FROM shipitem si
            LEFT JOIN tagtrackingview ttv ON ttv.tagId = si.tagId
            WHERE si.shipId = ship.id
              AND si.soItemId = soitem.id
              AND ttv.info IS NOT NULL
              AND ttv.info <> ''
            ORDER BY si.id
            LIMIT 1
        ),
        ''
    ) AS tracking,
    soitem.id AS order_index,
    COALESCE(soitem.note, '') AS soitem_note,
    soitem.dateScheduledFulfillment AS date_scheduled_fulfillment,
    soitem.qtyOrdered AS qty,
    uom.code AS uom
FROM ship
JOIN so ON ship.soId = so.id
JOIN locationgroup lg ON so.locationGroupId = lg.id
JOIN stateconst ON ship.shipToStateId = stateconst.id
JOIN soitem ON so.id = soitem.soId
JOIN product ON soitem.productId = product.id
JOIN uom ON soitem.uomId = uom.id
WHERE ship.statusId = {PACKED_STATUS_ID}
  AND lg.name = '{LOCATION_GROUP}'
  AND so.num = '{{ship_num}}'
ORDER BY product.num
"""


def items_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SHIPMENT_ITEMS_SQL_TEMPLATE.format(ship_num=safe)


SHIP_SQL_TEMPLATE = """
SELECT
    ship.*,
    cs.name AS carrier_service_name
FROM ship
LEFT JOIN carrierservice cs ON cs.id = ship.carrierServiceId
WHERE soId = (
    SELECT id
    FROM so
    WHERE num = '{ship_num}'
    LIMIT 1
)
  AND statusId = {PACKED_STATUS_ID}
ORDER BY id DESC
LIMIT 1
"""


SO_SQL_TEMPLATE = """
SELECT *
FROM so
WHERE num = '{ship_num}'
LIMIT 1
"""


CUSTOMER_SQL_TEMPLATE = """
SELECT *
FROM customer
WHERE id = {customer_id}
LIMIT 1
"""


STATE_CODE_SQL_TEMPLATE = """
SELECT code
FROM stateconst
WHERE id = {state_id}
LIMIT 1
"""


def ship_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SHIP_SQL_TEMPLATE.format(ship_num=safe, PACKED_STATUS_ID=PACKED_STATUS_ID)


def so_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SO_SQL_TEMPLATE.format(ship_num=safe)


def customer_sql_for(customer_id: int) -> str:
    return CUSTOMER_SQL_TEMPLATE.format(customer_id=int(customer_id))


def state_code_sql_for(state_id: int) -> str:
    return STATE_CODE_SQL_TEMPLATE.format(state_id=int(state_id))
