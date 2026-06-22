LOCATION_GROUP = "Jersey City, NJ"
PACKED_STATUS_ID = 20

SHIPMENTS_SQL = f"""
SELECT
    so.num AS so_num,
    so.*,
    COALESCE(so.shipToCity, '') AS city,
    COALESCE(stateconst.code, '') AS state,
    COALESCE(so.shipToZip, '') AS zip,
    COALESCE(carrier.name, '') AS carrier_name,
    COALESCE(customer.name, '') AS customer_name,
    lg.name AS location_group
FROM so
JOIN locationgroup lg ON so.locationGroupId = lg.id
LEFT JOIN customer ON customer.id = so.customerId
LEFT JOIN carrier ON carrier.id = so.carrierId
LEFT JOIN stateconst ON so.shipToStateId = stateconst.id
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
    so.num AS so_num,
    ship.num AS ship_num,
    so.customerPO AS po_number,
    so.shipToName AS ship_to_name,
    so.shipToAddress AS address_1,
    so.shipToCity AS city,
    stateconst.code AS state,
    so.shipToZip AS zip,
    COALESCE(carrier.name, '') AS carrier_name,
    product.num AS item_num,
    COALESCE(
        (
            SELECT ttv.info
            FROM tagtrackingview ttv
            WHERE ttv.tagId = shipitem.tagId
              AND ttv.info IS NOT NULL
              AND ttv.info <> ''
            LIMIT 1
        ),
        ''
    ) AS tracking,
    soitem.id AS order_index,
    COALESCE(soitem.note, '') AS soitem_note,
    soitem.dateScheduledFulfillment AS date_scheduled_fulfillment,
    shipitem.qtyShipped AS qty,
    uom.code AS uom
FROM ship
JOIN so ON ship.soId = so.id
JOIN locationgroup lg ON so.locationGroupId = lg.id
LEFT JOIN carrier ON carrier.id = so.carrierId
LEFT JOIN stateconst ON so.shipToStateId = stateconst.id
JOIN shipitem ON shipitem.shipId = ship.id
JOIN soitem ON soitem.id = shipitem.soItemId
JOIN product ON soitem.productId = product.id
JOIN uom ON soitem.uomId = uom.id
WHERE ship.statusId = {PACKED_STATUS_ID}
  AND lg.name = '{LOCATION_GROUP}'
  AND so.num = '{{so_num}}'
ORDER BY product.num
"""


def items_sql_for(so_num: str) -> str:
    safe = so_num.replace("'", "''")
    return SHIPMENT_ITEMS_SQL_TEMPLATE.format(so_num=safe)


SO_SQL_TEMPLATE = """
SELECT
    so.*,
    cs.name AS carrier_service_name
FROM so
LEFT JOIN carrierservice cs ON cs.id = so.carrierServiceId
WHERE so.num = '{so_num}'
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


def so_sql_for(so_num: str) -> str:
    safe = so_num.replace("'", "''")
    return SO_SQL_TEMPLATE.format(so_num=safe)


def customer_sql_for(customer_id: int) -> str:
    return CUSTOMER_SQL_TEMPLATE.format(customer_id=int(customer_id))


def state_code_sql_for(state_id: int) -> str:
    return STATE_CODE_SQL_TEMPLATE.format(state_id=int(state_id))
