"""In-UI documentation: Markdown files in `control-ui/docs/`, rendered to HTML
on the server by a small, escape-first renderer.

Safety model: every character of the source is HTML-escaped BEFORE any
formatting is applied, and formatting only ever inserts a fixed set of tags
with attributes built here. Links are restricted to http(s), in-page anchors
and relative paths. No raw HTML from a document is ever emitted.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
_ORDER_RE = re.compile(r"^(\d+)-")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:64] or "section"


def _safe_href(url: str) -> str | None:
    url = url.strip()
    if url.startswith("#") and re.fullmatch(r"#[A-Za-z0-9\-_]{1,80}", url):
        return url
    if re.match(r"^https?://[^\s\"'<>]+$", url):
        return url
    if re.match(r"^/?[A-Za-z0-9\-_./#]+$", url) and ".." not in url and not url.startswith("//"):
        return url
    return None


def _inline(escaped: str) -> str:
    """Inline formatting on ALREADY-ESCAPED text."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, escaped)

    def link(m: re.Match) -> str:
        label, raw = m.group(1), html.unescape(m.group(2))
        href = _safe_href(raw)
        if href is None:
            return label
        ext = ' rel="noopener noreferrer" target="_blank"' if href.startswith("http") else ""
        return f'<a href="{html.escape(href, quote=True)}"{ext}>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render(markdown: str) -> tuple[str, list[dict]]:
    """Return (html, toc). toc = [{level, id, title}] for h2/h3."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    toc: list[dict] = []
    used_ids: set[str] = set()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(html.escape(" ".join(para))) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        fence = re.match(r"^```\s*([A-Za-z0-9_+\-]*)\s*$", stripped)
        if fence:
            flush_para()
            lang = fence.group(1)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f'<pre class="code"><code{cls}>{html.escape(chr(10).join(body))}</code></pre>')
            continue

        heading = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", stripped)
        if heading:
            flush_para()
            level = len(heading.group(1))
            title = heading.group(2)
            hid = "d-" + slugify(title)
            base, n = hid, 2
            while hid in used_ids:
                hid, n = f"{base}-{n}", n + 1
            used_ids.add(hid)
            if level in (2, 3):
                toc.append({"level": level, "id": hid, "title": title})
            out.append(f'<h{level} id="{hid}">{_inline(html.escape(title))}</h{level}>')
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,})$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush_para()
            head = _row(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_row(lines[i]))
                i += 1
            parts = ['<div class="table-wrap"><table><thead><tr>']
            parts += [f'<th scope="col">{_inline(html.escape(c))}</th>' for c in head]
            parts.append("</tr></thead><tbody>")
            for r in rows:
                parts.append("<tr>" + "".join(f"<td>{_inline(html.escape(c))}</td>" for c in r) + "</tr>")
            parts.append("</tbody></table></div>")
            out.append("".join(parts))
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            text = " ".join(quote)
            kind = "note"
            m = re.match(r"^\*\*(Warning|Danger|Tip|Note)\*\*", text, re.I)
            if m:
                kind = m.group(1).lower()
            out.append(f'<aside class="callout callout-{kind}"><p>{_inline(html.escape(text))}</p></aside>')
            continue

        list_m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if list_m:
            flush_para()
            ordered = list_m.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if m:
                    if len(m.group(1)) >= 2 and items:
                        items[-1] += f"<ul><li>{_inline(html.escape(m.group(3)))}</li></ul>"
                    else:
                        items.append(_inline(html.escape(m.group(3))))
                    i += 1
                elif lines[i].startswith("  ") and lines[i].strip() and items:
                    items[-1] += " " + _inline(html.escape(lines[i].strip()))
                    i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{it}</li>" for it in items) + f"</{tag}>")
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out), toc


@dataclass
class DocPage:
    slug: str
    title: str
    order: int
    path: Path


class DocLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def pages(self) -> list[DocPage]:
        pages = []
        for path in sorted(self.root.glob("*.md")):
            stem = path.stem
            m = _ORDER_RE.match(stem)
            order = int(m.group(1)) if m else 999
            slug = _ORDER_RE.sub("", stem)
            if not _SLUG_RE.match(slug):
                continue
            title = slug.replace("-", " ").title()
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            pages.append(DocPage(slug, title, order, path))
        return sorted(pages, key=lambda p: (p.order, p.slug))

    def index(self) -> list[dict]:
        out = []
        for page in self.pages():
            _, toc = render(page.path.read_text(encoding="utf-8"))
            out.append({"slug": page.slug, "title": page.title,
                        "sections": [t for t in toc if t["level"] == 2]})
        return out

    def get(self, slug: str) -> dict | None:
        if not _SLUG_RE.match(slug or ""):
            return None
        for page in self.pages():
            if page.slug == slug:
                body, toc = render(page.path.read_text(encoding="utf-8"))
                return {"slug": slug, "title": page.title, "html": body, "toc": toc}
        return None

    def search(self, query: str, limit: int = 30) -> list[dict]:
        q = (query or "").strip().lower()[:100]
        if len(q) < 2:
            return []
        hits = []
        for page in self.pages():
            section, section_id = page.title, ""
            for line in page.path.read_text(encoding="utf-8").splitlines():
                h = re.match(r"^#{2,3}\s+(.+)$", line)
                if h:
                    section, section_id = h.group(1).strip(), "d-" + slugify(h.group(1).strip())
                if q in line.lower():
                    hits.append({"slug": page.slug, "page": page.title, "section": section,
                                 "anchor": section_id, "snippet": line.strip()[:200]})
                    if len(hits) >= limit:
                        return hits
        return hits
