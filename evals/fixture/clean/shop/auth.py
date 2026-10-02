from functools import wraps


class Forbidden(Exception):
    pass


def require_admin(handler):
    """Reject the request unless the caller is an admin."""

    @wraps(handler)
    def wrapper(user, *args, **kwargs):
        if not user.get("is_admin"):
            raise Forbidden(f"{user.get('name', 'anonymous')} is not an admin")
        return handler(user, *args, **kwargs)

    return wrapper
