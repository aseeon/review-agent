from . import db


def order_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def orders_with_customers(conn) -> list[dict]:
    """Every order with its customer's name and email, newest first."""
    rows = []
    for order in db.list_orders(conn):
        customer = db.get_customer(conn, order["customer_id"])
        rows.append({
            "order_id": order["id"],
            "total": order["total"],
            "customer": customer["name"] if customer else None,
            "email": customer["email"] if customer else None,
        })
    return rows
