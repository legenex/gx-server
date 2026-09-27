"""Hermetic netview tests: the parsers are pure and fixture-tested; the view
is offline-honest. No network access anywhere in this file."""

from __future__ import annotations

import unittest

from support import TempEnv

from gx_control_ui import netview
from gx_control_ui.netview import NetView, parse_ethtool, parse_ip_br, parse_show_gids

IP_BR = """lo               UNKNOWN        127.0.0.1/8 ::1/128
rocep1s0f0       UP             192.168.100.10/24 fe80::1/64
enP7s7           UP             10.60.21.37/24
tailscale0       UNKNOWN        100.105.214.61/32
"""

ETHTOOL = """Settings for rocep1s0f0:
        Supported ports: [ Backplane ]
        Supported link modes: Not reported
        Port: Direct Attach Copper
        Speed: 200000Mb/s
        Link detected: yes
"""

SHOW_GIDS = """DEV      PORT  INDEX  GID  GID_TYPE        STATE
rocep1s0f0  1  0  fe80:0000:0000:0000:0000:0000:0000:0001  RoCE v2  ACTIVE
rocep1s0f0  1  1  fe80:0000:0000:0000:2222:0000:0000:0002  RoCE v2
rocep1s0f0  1  3  0000:0000:0000:0000:2222:0a00:0000:000b  RoCE v2  ACTIVE
rocep1s0f0  1  4  fe80:0000:0000:0000:2222:0000:0000:0004  RoCE v2
"""


class TestParsers(unittest.TestCase):
    def test_parse_ip_br(self):
        out = parse_ip_br(IP_BR)
        self.assertEqual(out["rocep1s0f0"]["state"], "UP")
        self.assertIn("192.168.100.10/24", out["rocep1s0f0"]["ips"])
        self.assertNotIn("lo", out)
        self.assertEqual(out["tailscale0"]["ips"], ["100.105.214.61/32"])

    def test_parse_ethtool(self):
        out = parse_ethtool(ETHTOOL)
        self.assertEqual(out["speed_mbps"], 200000)
        self.assertTrue(out["link"])
        self.assertEqual(out["port"], "direct")

    def test_parse_show_gids(self):
        rows = parse_show_gids(SHOW_GIDS)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["dev"], "rocep1s0f0")
        self.assertEqual(rows[2]["gid_index"], 3)
        self.assertTrue(rows[2]["gid"].endswith("000b"))
        # the registry pin (NCCL_IB_GID_INDEX=3) selects the ACTIVE RoCE entry
        pinned = next(r for r in rows if r["gid_index"] == 3)
        self.assertIn("RoCE v2", pinned["gid_type"])
        self.assertIn("ACTIVE", pinned["gid_type"])

    def test_parsers_tolerate_garbage(self):
        self.assertEqual(parse_ip_br(""), {})
        self.assertEqual(parse_ethtool("nothing here"), {"speed_mbps": None, "link": None, "port": None})
        self.assertEqual(parse_show_gids("DEV PORT INDEX GID GID_TYPE"), [])


class TestView(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.nv = NetView(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()

    def test_rails_come_from_the_registry(self):
        rails = self.nv.rails()
        self.assertEqual(len(rails), 2)
        self.assertEqual(rails[0]["head"]["ip"], "192.168.100.10")
        self.assertEqual(rails[0]["worker"]["ip"], "192.168.100.11")
        self.assertEqual(rails[1]["head"]["ip"], "192.168.101.10")

    def test_offline_view_is_honest_and_read_only(self):
        out = self.nv.view(fresh=True)
        self.assertTrue(out["offline"])
        self.assertEqual(len(out["rails"]), 2)
        for rail in out["rails"]:
            self.assertEqual(rail["ping_worker"], {"skipped": "offline mode"})
            self.assertEqual(rail["level"], "unknown")
        self.assertIn("read-only", out["diagnostics_note"])

    def test_diagnostics_unavailable_offline(self):
        out = self.nv.diagnostics()
        self.assertEqual(out["available"], False)
        self.assertEqual(out["pairs"], [])


if __name__ == "__main__":
    unittest.main()
