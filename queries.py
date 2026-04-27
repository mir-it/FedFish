LOCATION_GROUP = "Jersey City, NJ"
PACKED_STATUS_ID = 20

SHIPMENTS_SQL = f"""
SELECT
    ship.num AS ship_num,
    ship.shipToCity AS city,
    stateconst.code AS state,
    ship.shipToZip AS zip,
    shipcarton.freightweight AS weight,
    shipcarton.len AS length,
    shipcarton.width AS width,
    shipcarton.height AS height,
    lg.name AS location_group
FROM ship
JOIN shipcarton ON ship.id = shipcarton.shipId
JOIN locationgroup lg ON ship.locationGroupId = lg.id
JOIN stateconst ON ship.shipToStateId = stateconst.id
WHERE ship.statusId = {PACKED_STATUS_ID}
  AND lg.name = '{LOCATION_GROUP}'
ORDER BY ship.num
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
    '' AS tracking,
    soitem.qtyOrdered AS qty,
    uom.code AS uom
FROM ship
JOIN so ON ship.soId = so.id
JOIN locationgroup lg ON ship.locationGroupId = lg.id
JOIN stateconst ON ship.shipToStateId = stateconst.id
JOIN soitem ON so.id = soitem.soId
JOIN product ON soitem.productId = product.id
JOIN uom ON soitem.uomId = uom.id
WHERE ship.statusId = {PACKED_STATUS_ID}
  AND lg.name = '{LOCATION_GROUP}'
  AND ship.num = '{{ship_num}}'
ORDER BY product.num
"""


def items_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SHIPMENT_ITEMS_SQL_TEMPLATE.format(ship_num=safe)


SHIP_SQL_TEMPLATE = """
SELECT *
FROM ship
WHERE num = '{ship_num}'
LIMIT 1
"""


SO_SQL_TEMPLATE = """
SELECT *
FROM so
WHERE id = (
    SELECT soId
    FROM ship
    WHERE num = '{ship_num}'
    LIMIT 1
)
LIMIT 1
"""


CUSTOMER_SQL_TEMPLATE = """
SELECT *
FROM customer
WHERE id = {customer_id}
LIMIT 1
"""


LOT_CANDIDATES_SQL_TEMPLATE = """
SELECT
    product.num AS item_num,
    tag.num AS tracking,
    shipitem.qtyShipped AS qty_shipped
FROM ship
JOIN shipitem ON ship.id = shipitem.shipId
JOIN product ON shipitem.productId = product.id
LEFT JOIN tag ON shipitem.tagId = tag.id
WHERE ship.num = '{ship_num}'
"""


STATE_CODE_SQL_TEMPLATE = """
SELECT code
FROM stateconst
WHERE id = {state_id}
LIMIT 1
"""


def ship_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SHIP_SQL_TEMPLATE.format(ship_num=safe)


def so_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return SO_SQL_TEMPLATE.format(ship_num=safe)


def customer_sql_for(customer_id: int) -> str:
    return CUSTOMER_SQL_TEMPLATE.format(customer_id=int(customer_id))


def lot_candidates_sql_for(ship_num: str) -> str:
    safe = ship_num.replace("'", "''")
    return LOT_CANDIDATES_SQL_TEMPLATE.format(ship_num=safe)


def state_code_sql_for(state_id: int) -> str:
    return STATE_CODE_SQL_TEMPLATE.format(state_id=int(state_id))
