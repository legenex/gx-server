from pathlib import Path

ROOT = Path(__file__).resolve().parent
html = ROOT.joinpath("index.html").read_text()
css = ROOT.joinpath("styles.css").read_text()
js = ROOT.joinpath("app.js").read_text()


def test_status_pill_markup():
    assert 'data-testid="status-pill"' in html
    assert "gx-status-pill" in html
    assert "<header" in html
    # pill lives in the existing header, not a new page
    header = html.split("<header", 1)[1].split("</header>", 1)[0]
    assert "gx-status-pill" in header


def test_no_inline_styles():
    assert "style=" not in html


def test_uses_existing_classes_not_bootstrap():
    assert "gx-btn" in html
    assert "gx-card" in html
    assert "btn-primary" not in html
    assert "MuiButton" not in html


def test_js_sets_online_state():
    assert "gx-status-pill--ok" in js or "gx-status-pill--ok" in html
    assert "online" in js.lower() or "online" in html.lower()


def test_css_keeps_modifiers():
    assert ".gx-status-pill--ok" in css
    assert ".gx-status-pill--bad" in css
