from decimal import ROUND_HALF_UP, Decimal


def format_money(cents: int, currency: str = "EUR") -> str:
    """Format an integer amount of cents, e.g. 123456 -> '1,234.56 EUR'."""
    amount = (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:,} {currency}"
