"""Minimal static site builder for Chano documentation (chanochano.com)."""

from __future__ import annotations

import argparse
import contextlib
import html
import http.server
import re
import shutil
import socketserver
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
STATIC_DIR = ROOT / "static"
DIST_DIR = ROOT / "dist"

VBG_CSS_URL = "https://vercel.com/geist/vercel-brand.css"
GITHUB_URL = "https://github.com/scotttrinh/chano"
SITE_NAME = "Chano"

_FORMATTER = HtmlFormatter(nowrap=True)


@dataclass(frozen=True)
class TocItem:
    id: str
    text: str


@dataclass(frozen=True)
class Page:
    src: Path
    out: Path
    url: str
    title: str
    description: str
    lede: str
    body_html: str
    toc: list[TocItem]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    meta: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, raw[end + 5 :]


def highlight_code(code: str, lang: str, _attrs: str) -> str:
    language = (lang or "").strip().split()[0] if lang else ""
    try:
        lexer = get_lexer_by_name(language) if language else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    highlighted = highlight(code, lexer, _FORMATTER)
    lang_attr = f' data-lang="{html.escape(language)}"' if language else ""
    return (
        f'<pre class="vbg-custom-code"{lang_attr}>'
        f"<code>{highlighted}</code></pre>\n"
    )


def render_markdown(source: str) -> tuple[str, list[TocItem]]:
    md = MarkdownIt(
        "commonmark",
        {"html": False, "typographer": False, "highlight": highlight_code},
    )
    md.enable("table")

    tokens = md.parse(source)
    toc: list[TocItem] = []
    seen_slugs: dict[str, int] = {}

    in_thead = False
    for i, token in enumerate(tokens):
        if token.type == "thead_open":
            in_thead = True
        elif token.type == "thead_close":
            in_thead = False
        elif token.type == "th_open" and in_thead:
            token.attrSet("scope", "col")
        elif token.type == "heading_open" and token.tag in ("h2", "h3"):
            heading_text = tokens[i + 1].content if i + 1 < len(tokens) else ""
            plain_text = re.sub(r"`([^`]*)`", r"\1", heading_text).strip()
            base_slug = slugify(plain_text)
            count = seen_slugs.get(base_slug, 0)
            seen_slugs[base_slug] = count + 1
            slug = base_slug if count == 0 else f"{base_slug}-{count + 1}"
            token.attrSet("id", slug)
            if token.tag == "h2":
                token.attrJoin("class", "vbg-heading-24")
                toc.append(TocItem(id=slug, text=plain_text))
            else:
                token.attrJoin("class", "vbg-heading-16")

    rendered = md.renderer.render(tokens, md.options, {})
    rendered = rendered.replace("<table>", '<div class="vbg-table-wrap"><table>')
    rendered = rendered.replace("</table>", "</table></div>")
    return rendered, toc


def render_page(page: Page) -> str:
    page_title = (
        f"{page.title} — {SITE_NAME}" if page.title != SITE_NAME else SITE_NAME
    )
    toc_html = ""
    if page.toc:
        items = "\n".join(
            f'          <li><a href="#{html.escape(item.id)}">'
            f"{html.escape(item.text)}</a></li>"
            for item in page.toc
        )
        toc_html = f"""      <aside class="vbg-custom-toc" aria-label="On this page">
        <p class="vbg-custom-toc-title">On this page</p>
        <ol class="vbg-custom-toc-list">
{items}
        </ol>
      </aside>"""

    lede_html = (
        f'        <p class="vbg-lede">{html.escape(page.lede)}</p>' if page.lede else ""
    )
    fonts_url = (
        "https://fonts.googleapis.com/css2"
        "?family=Geist:wght@400..600&family=Geist+Mono:wght@400..600&display=swap"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <meta name="description" content="{html.escape(page.description)}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="{fonts_url}" rel="stylesheet" referrerpolicy="no-referrer">
  <link href="{VBG_CSS_URL}" rel="stylesheet">
  <link href="/site.css" rel="stylesheet">
</head>
<body class="vbg-report">
  <div class="vbg-shell">
    <a class="vbg-skip-link" href="#main">Skip to content</a>
    <header class="vbg-custom-header">
      <a class="vbg-custom-brand" href="/">{SITE_NAME}</a>
      <nav class="vbg-custom-nav" aria-label="Primary">
        <a href="/" aria-current="page">Guide</a>
        <a href="{GITHUB_URL}">GitHub</a>
        <span class="vbg-custom-nav-meta">Python 3.12+</span>
      </nav>
    </header>
    <main id="main">
      <section class="vbg-custom-opening">
        <h1 class="vbg-title">{html.escape(page.title)}</h1>
{lede_html}
      </section>
      <div class="vbg-custom-doc-layout">
{toc_html}
        <article class="vbg-custom-prose">
{page.body_html}
        </article>
      </div>
    </main>
    <footer class="vbg-custom-footer">
      <span class="vbg-custom-footer-brand">{SITE_NAME}</span>
      <span>
        Serverless-first Python web framework ·
        <a href="{GITHUB_URL}">github.com/scotttrinh/chano</a>
      </span>
    </footer>
  </div>
</body>
</html>
"""


def build() -> list[Page]:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    if STATIC_DIR.exists():
        for src in STATIC_DIR.rglob("*"):
            if src.is_file():
                dest = DIST_DIR / src.relative_to(STATIC_DIR)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    pages: list[Page] = []
    for md_path in sorted(CONTENT_DIR.rglob("*.md")):
        rel = md_path.relative_to(CONTENT_DIR)
        meta, body = parse_frontmatter(md_path.read_text(encoding="utf-8"))
        body_html, toc = render_markdown(body)

        if rel.name == "index.md":
            out_path = DIST_DIR / rel.with_suffix(".html")
            url = "/" if rel.parent == Path(".") else f"/{rel.parent.as_posix()}/"
        else:
            out_path = DIST_DIR / rel.with_suffix("") / "index.html"
            url = f"/{rel.with_suffix('').as_posix()}"

        page = Page(
            src=md_path,
            out=out_path,
            url=url,
            title=meta.get("title", SITE_NAME),
            description=meta.get("description", ""),
            lede=meta.get("lede", ""),
            body_html=body_html,
            toc=toc,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_page(page), encoding="utf-8")
        pages.append(page)

    return pages


def serve(port: int) -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DIST_DIR), **kwargs)

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Serving {DIST_DIR} at http://127.0.0.1:{port}")
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Chano static docs site.")
    parser.add_argument(
        "--serve", action="store_true", help="Serve dist/ locally after building."
    )
    parser.add_argument(
        "--port", type=int, default=4321, help="Port for --serve (default: 4321)."
    )
    args = parser.parse_args()

    pages = build()
    print(f"Built {len(pages)} page(s) into {DIST_DIR.relative_to(ROOT)}/")
    for page in pages:
        print(f"  {page.url} -> {page.out.relative_to(ROOT)}")

    if args.serve:
        serve(args.port)


if __name__ == "__main__":
    main()
