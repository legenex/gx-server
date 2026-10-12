"""GXSUITE-T5 consumer. Contract: event["user_id"] is a positive int."""


def handle(event: dict) -> str:
    uid = event["user_id"]
    action = event["action"]
    return f"user-{uid}:{action}"
