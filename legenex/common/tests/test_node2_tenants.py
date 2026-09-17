import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gxcommon.node2_tenants import DEFAULT_PEERS, PeerTenants, parse_health, parse_peer_map  # noqa: E402

#: Shared fixtures: the media router's copy of the parser must agree (tests/test_contract.py).
FIXTURES = [
    ("gx-voice", {"state": "ready", "memory": {"pending_gib": 0, "resident_gib": 12.0}}, 0.0, True),
    ("gx-call", {"state": "loading", "memory": {"pending_gib": 30.5, "resident_gib": 0}}, 30.5, True),
    ("gx-live", {"state": "loading", "memory": {"estimate_gib": 22}}, 22.0, True),
    ("gx-live", {"state": "unloaded", "memory": {}}, 0.0, False),
    ("gx-music", {"engine": "ready", "busy": True, "memory": {"pending_gib": 4, "loaded_gib": 26}}, 4.0, True),
    ("gx-media-router", {"busy": True, "memory": {"pending_gib": 57}}, 57.0, True),
    ("gx-media-router", {"busy": False, "resident_alias": None, "memory": {"pending_gib": 0}}, 0.0, False),
    ("gx-voice", {"state": "ready", "memory": {"pending_gib": -3}}, 0.0, True),
    ("gx-voice", {"state": "ready", "memory": {"pending_gib": "nan"}}, 0.0, True),
    ("gx-voice", {"state": "ready", "memory": {"pending_gib": True}}, 0.0, True),
    ("gx-voice", ["not", "a", "dict"], 0.0, False),
]


class Parse(unittest.TestCase):
    def test_fixtures(self):
        for name, body, pending, holds in FIXTURES:
            st = parse_health(name, body)
            self.assertEqual(st.pending_gib, pending, (name, body))
            self.assertEqual(st.holds_memory, holds, (name, body))

    def test_active_and_resident(self):
        st = parse_health("gx-call", {"state": "busy", "active_jobs": 1, "active_sessions": 2,
                                      "memory": {"resident_gib": 40}})
        self.assertEqual(st.active, 3)
        self.assertEqual(st.resident_gib, 40.0)
        self.assertTrue(st.public()["holds_memory"])

    def test_peer_map(self):
        self.assertEqual(parse_peer_map("gx-voice=http://192.168.100.11:18830"),
                         {"gx-voice": "http://192.168.100.11:18830/health"})
        for bad in ("voice=http://192.168.100.11:1", "gx-x=https://192.168.100.11:1",
                    "gx-x=http://100.105.214.61:1", "gx-x"):
            with self.assertRaises(ValueError):
                parse_peer_map(bad)

    def test_default_peers_are_fabric_only(self):
        for url in DEFAULT_PEERS.values():
            self.assertTrue(url.startswith("http://192.168.100.11:"), url)


class Peers(unittest.TestCase):
    def test_sum_skips_unreachable_and_self(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            if "18840" in url:
                raise OSError("connection refused")
            if "18850" in url:
                return {"state": "loading", "memory": {"pending_gib": 20}}
            return {"state": "ready", "memory": {"pending_gib": 1.5}}

        peers = PeerTenants(DEFAULT_PEERS, exclude="gx-voice", fetch=fetch)
        self.assertNotIn("gx-voice", peers.peers)
        self.assertEqual(peers.pending_gib(), 23.0)  # router 1.5 + music 1.5 + live 20; call unreachable
        snap = peers.snapshot()
        self.assertFalse(snap["gx-call"]["reachable"])
        self.assertIn("connection refused", snap["gx-call"]["error"])
        n = len(calls)
        peers.snapshot()  # cached
        self.assertEqual(len(calls), n)
        peers.pending_gib(fresh=True)
        self.assertGreater(len(calls), n)

    def test_bad_json_counts_as_unreachable(self):
        def fetch(url, timeout):
            raise ValueError("bad json")

        peers = PeerTenants({"gx-live": "http://127.0.0.1:1/health"}, fetch=fetch)
        self.assertEqual(peers.pending_gib(), 0.0)
        self.assertFalse(peers.states()["gx-live"].reachable)

    def test_from_env(self):
        import os
        old = os.environ.get("GX_NODE2_PEERS")
        os.environ["GX_NODE2_PEERS"] = "gx-live=http://127.0.0.1:9"
        try:
            self.assertEqual(list(PeerTenants.from_env().peers), ["gx-live"])
        finally:
            if old is None:
                os.environ.pop("GX_NODE2_PEERS")
            else:
                os.environ["GX_NODE2_PEERS"] = old
        self.assertEqual(len(PeerTenants.from_env(exclude="gx-music").peers), 4)

    def test_empty(self):
        self.assertEqual(PeerTenants({}).pending_gib(), 0.0)


if __name__ == "__main__":
    unittest.main()
