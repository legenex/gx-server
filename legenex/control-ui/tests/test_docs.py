from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import UI_DIR

from gx_control_ui.docs import DocLibrary, render

REQUIRED_SECTIONS = [
    "Getting started", "Architecture", "Which model should I use?", "gx-mini", "gx-fast", "gx-reason",
    "gx-max", "gx-auto", "gx-image", "gx-video", "API quickstart", "curl examples", "Python examples",
    "JavaScript examples", "Vision input", "Tool calling", "Image generation", "Video generation",
    "Model loading / unloading", "gx-max explained", "Resource safety", "Queueing",
    "Errors / troubleshooting", "Git sync", "Remote access", "Log locations", "Admin / recovery", "FAQ",
]


class TestRenderer(unittest.TestCase):
    def test_escapes_raw_html(self):
        html, _ = render('# T\n\n<script>alert(1)</script> <img src=x onerror="alert(2)">')
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_escapes_code_blocks(self):
        html, _ = render("```bash\necho '<b>' && rm -rf /tmp/x\n```")
        self.assertIn("&lt;b&gt;", html)
        self.assertIn('<pre class="code"><code class="lang-bash">', html)

    def test_rejects_dangerous_links(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example/x", "../../etc/passwd",
                    'http://x" onmouseover="y'):
            html, _ = render(f"[click]({bad})")
            self.assertNotIn("href", html, bad)
            self.assertIn("click", html)

    def test_allows_safe_links(self):
        html, _ = render("[a](https://example.com/x?y=1&z=2) [b](#d-faq) [c](/#/docs/models)")
        self.assertIn('href="https://example.com/x?y=1&amp;z=2"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn('href="#d-faq"', html)
        self.assertIn('href="/#/docs/models"', html)

    def test_headings_toc_and_unique_ids(self):
        html, toc = render("# Title\n## Same\n### Sub\n## Same")
        self.assertEqual([t["id"] for t in toc], ["d-same", "d-sub", "d-same-2"])
        self.assertIn('<h2 id="d-same">', html)

    def test_table_list_callout_inline(self):
        md = ("| A | B |\n|---|---|\n| `x<y` | **bold** |\n\n- one\n  - nested\n- two\n\n1. first\n\n"
              "> **Warning** careful *now*\n\n---\n")
        html, _ = render(md)
        self.assertIn("<table>", html)
        self.assertIn("<code>x&lt;y</code>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>", html)
        self.assertIn("<ol><li>first</li></ol>", html)
        self.assertIn('class="callout callout-warning"', html)
        self.assertIn("<em>now</em>", html)
        self.assertIn("<hr>", html)


class TestLibrary(unittest.TestCase):
    def test_real_docs_cover_every_required_section(self):
        lib = DocLibrary(UI_DIR / "docs")
        titles = {s["title"].lower() for p in lib.index() for s in p["sections"]}
        missing = [s for s in REQUIRED_SECTIONS if s.lower() not in titles]
        self.assertEqual(missing, [], f"docs are missing sections: {missing}")

    def test_real_docs_contain_no_secret_values(self):
        for path in (UI_DIR / "docs").glob("*.md"):
            text = path.read_text()
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]{20,}", path.name)

    def test_get_search_and_slug_validation(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "02-second.md").write_text("# Second\n\n## Alpha\nfind the needle here\n")
            Path(d, "01-first.md").write_text("# First\n\n## Beta\n")
            Path(d, "bad name.md").write_text("# ignored")
            lib = DocLibrary(Path(d))
            self.assertEqual([p["slug"] for p in lib.index()], ["first", "second"])
            page = lib.get("second")
            self.assertEqual(page["title"], "Second")
            self.assertIn("needle", page["html"])
            self.assertIsNone(lib.get("../etc/passwd"))
            self.assertIsNone(lib.get("missing"))
            hits = lib.search("NEEDLE")
            self.assertEqual(hits[0]["anchor"], "d-alpha")
            self.assertEqual(lib.search("x"), [])


if __name__ == "__main__":
    unittest.main()
