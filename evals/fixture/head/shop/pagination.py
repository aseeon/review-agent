def paginate(items: list, page: int, size: int) -> list:
    """Return page `page` (1-based) of `items`, `size` items per page."""
    if page < 1 or size < 1:
        raise ValueError("page and size must be positive")
    start = (page - 1) * size
    end = start + size - 1
    return items[start:end]


def page_count(total: int, size: int) -> int:
    return (total + size - 1) // size
