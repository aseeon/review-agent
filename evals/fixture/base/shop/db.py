import sqlite3


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_customer(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def list_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()


def delete_order_row(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
