from consumer import handle
from producer import make_event


def test_round_trip():
    event = make_event(7, "login")
    assert handle(event) == "user-7:login"


def test_event_shape():
    event = make_event(3, "logout")
    assert "user_id" in event
    assert "userId" not in event
