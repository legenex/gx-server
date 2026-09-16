"""Performance budget for the hot API paths (offline, cached readings)."""

from __future__ import annotations

import statistics
import time
import unittest

from test_server import ServerBase


class TestLatencyBudget(ServerBase):
    BUDGET_MS = 250

    def test_hot_paths_are_fast(self):
        self.login()
        for path in ("/api/overview", "/api/models", "/api/nodes", "/api/jobs", "/api/system", "/api/docs"):
            self.req("GET", path)  # warm the caches
            samples = []
            for _ in range(15):
                t0 = time.perf_counter()
                status, _, _ = self.req("GET", path)
                samples.append((time.perf_counter() - t0) * 1000)
                self.assertEqual(status, 200, path)
            p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
            self.assertLess(p95, self.BUDGET_MS, f"{path} p95 {p95:.1f} ms (median {statistics.median(samples):.1f})")

    def test_static_assets_are_compressed(self):
        status, headers, _ = self.req("GET", "/js/pages/playground.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")


if __name__ == "__main__":
    unittest.main()
