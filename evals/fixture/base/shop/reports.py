from . import db


def order_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
