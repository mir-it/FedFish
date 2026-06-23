LOCATION_GROUP = "Jersey City, NJ"
PACKED_STATUS_ID = 20

SHIPMENTS_SQL = f"""
SELECT
    so.num AS so_num,
    so.customerPO,
    so.shipToName,
    so.shipToAddress,
    so.shipToCity,
    so.shipToStateId,
    so.shipToZip,
    so.billToName,
    so.billToAddress,
    so.billToCity,
    so.billToStateId,
    so.billToZip,
    so.note,
    so.dateFirstShip,
    COALESCE(carrier.name, '') AS carrier_name,
    COALESCE(cs.name, '') AS carrier_service_name,
    COALESCE(customer.name, '') AS customer_name,
    COALESCE(ship_state.code, '') AS ship_to_state,
    COALESCE(bill_state.code, '') AS bill_to_state,
    lg.name AS location_group
FROM so
JOIN locationgroup lg ON so.locationGroupId = lg.id
LEFT JOIN customer ON customer.id = so.customerId
LEFT JOIN carrier ON carrier.id = so.carrierId
LEFT JOIN carrierservice cs ON cs.id = so.carrierServiceId
LEFT JOIN stateconst ship_state ON so.shipToStateId = ship_state.id
LEFT JOIN stateconst bill_state ON so.billToStateId = bill_state.id
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
    shipitem.qtyShipped AS qty,
    uom.code AS uom
FROM ship
JOIN so ON ship.soId = so.id
JOIN locationgroup lg ON so.locationGroupId = lg.id
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


STATE_CODE_SQL_TEMPLATE = """
SELECT code
FROM stateconst
WHERE id = {state_id}
LIMIT 1
"""


def state_code_sql_for(state_id: int) -> str:
    return STATE_CODE_SQL_TEMPLATE.format(state_id=int(state_id))
