from . import db
from .auth import require_admin


@require_admin
def list_orders(user, conn):
    return [dict(row) for row in db.list_orders(conn)]


@require_admin
def refund_order(user, conn, order_id: int):
    conn.execute("UPDATE orders SET status = 'refunded' WHERE id = ?", (order_id,))
    conn.commit()
    return {"refunded": order_id}


def delete_order(user, conn, order_id: int):
    db.delete_order_row(conn, order_id)
    return {"deleted": order_id}


@require_admin
def search_customers(user, conn, name: str):
    return [dict(row) for row in db.find_customers_by_name(conn, name)]
