"""GXSUITE-T5 producer. Integration fails because of a field-name mismatch."""


def make_event(user_id: int, action: str) -> dict:
    if not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id")
    if not action:
        raise ValueError("action")
    # BUG: consumer expects snake_case user_id
    return {"userId": user_id, "action": action}
