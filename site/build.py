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

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
STATIC_DIR = ROOT / "static"
DIST_DIR = ROOT / "dist"

VBG_CSS_URL = "https://vercel.com/geist/vercel-brand.css"
FONTS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Geist:wght@400..600&family=Geist+Mono:wght@400..600&display=swap"
)
HLJS_VERSION = "11.11.1"
HLJS_JS_URL = (
    f"https://unpkg.com/@highlightjs/cdn-assets@{HLJS_VERSION}/highlight.min.js"
)
HLJS_LIGHT_CSS_URL = (
    f"https://unpkg.com/@highlightjs/cdn-assets@{HLJS_VERSION}/styles/github.min.css"
)
HLJS_DARK_CSS_URL = f"https://unpkg.com/@highlightjs/cdn-assets@{HLJS_VERSION}/styles/github-dark.min.css"
GITHUB_URL = "https://github.com/scotttrinh/chano"
SITE_NAME = "Chano"

HERO_CODE_SAMPLE = """\
from typing import Annotated
from msgspec import Meta, Struct
from chano import Depends, HTTPBearer
from chano.status import Unauthorized

class ItemInput(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]

class Item(ItemInput):
    id: int

bearer = HTTPBearer()

def current_user(token: str = Depends(bearer)) -> str | Unauthorized:
    if token != "secret":
        return Unauthorized("Not authenticated")
    return "alice"

def GET(item_id: int, verbose: bool = False):
    return {"item_id": item_id, "verbose": verbose}

async def PUT(
    item_id: int,
    item: ItemInput,
    user: str = Depends(current_user),
) -> Item:
    return Item(id=item_id, name=item.name, price=item.price)
"""

DEPS_CODE_SAMPLE = """\
from chano import Boundary, Depends, HTTPBearer
from chano.status import Forbidden

bearer = HTTPBearer()

def require_admin(token: str = Depends(bearer)) -> None | Forbidden:
    if token != "admin-secret":
        return Forbidden("Admin access required")
    return None

boundary = Boundary(guards=[require_admin])
"""

MIDDLEWARE_CODE_SAMPLE = """\
import time
from chano import Boundary, CallNext, Request, Response

async def timing(request: Request, call_next: CallNext) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["x-response-time"] = f"{elapsed_ms:.2f}ms"
    return response

boundary = Boundary(wrap=[timing])
"""


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
    nav_title: str
    description: str
    lede: str
    layout: str
    section: str
    order: int
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


def highlight_code(code: str, lang: str, _attrs: str = "") -> str:
    language = (lang or "").strip().split()[0].lower() if lang else ""
    escaped = html.escape(code)
    if not language or language in ("text", "plaintext"):
        return (
            '<pre class="vbg-custom-code">'
            f'<code class="nohighlight">{escaped}</code></pre>\n'
        )
    hljs_lang = "bash" if language == "sh" else language
    return (
        f'<pre class="vbg-custom-code" data-lang="{html.escape(language)}">'
        f'<code class="language-{html.escape(hljs_lang)}">{escaped}</code></pre>\n'
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


def render_header(active_section: str) -> str:
    guides_current = ' aria-current="page"' if active_section == "guides" else ""
    ref_current = ' aria-current="page"' if active_section == "reference" else ""
    return f"""    <header class="vbg-custom-header">
      <a class="vbg-custom-brand" href="/">{SITE_NAME}</a>
      <nav class="vbg-custom-nav" aria-label="Primary">
        <a href="/guides/getting-started"{guides_current}>Guides</a>
        <a href="/reference/routing"{ref_current}>Reference</a>
        <a href="{GITHUB_URL}">GitHub</a>
      </nav>
    </header>"""


def render_footer() -> str:
    return f"""    <footer class="vbg-custom-footer">
      <span class="vbg-custom-footer-brand">{SITE_NAME}</span>
      <span>
        Python web framework for Vercel Functions ·
        <a href="/guides/getting-started">Guides</a> ·
        <a href="/reference/routing">Reference</a> ·
        <a href="{GITHUB_URL}">GitHub</a>
      </span>
    </footer>"""


def render_document(page: Page, main_html: str) -> str:
    page_title = f"{page.title} | {SITE_NAME}" if page.title != SITE_NAME else SITE_NAME
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <meta name="description" content="{html.escape(page.description)}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="{FONTS_URL}" rel="stylesheet" referrerpolicy="no-referrer">
  <link href="{VBG_CSS_URL}" rel="stylesheet">
  <link href="{HLJS_LIGHT_CSS_URL}" rel="stylesheet">
  <link
    href="{HLJS_DARK_CSS_URL}"
    rel="stylesheet"
    media="(prefers-color-scheme: dark)"
  >
  <link href="/site.css" rel="stylesheet">
</head>
<body class="vbg-report">
  <div class="vbg-shell">
    <a class="vbg-skip-link" href="#main">Skip to content</a>
{render_header(page.section)}
    <main id="main">
{main_html}
    </main>
{render_footer()}
  </div>
  <script src="{HLJS_JS_URL}"></script>
  <script>hljs.highlightAll();</script>
</body>
</html>
"""


def render_landing(page: Page) -> str:
    hero_code_html = highlight_code(HERO_CODE_SAMPLE, "python")
    install_html = highlight_code('uv add chano && uv add --dev "chano[dev]"', "sh")
    deps_code_html = highlight_code(DEPS_CODE_SAMPLE, "python")
    middleware_code_html = highlight_code(MIDDLEWARE_CODE_SAMPLE, "python")
    main_html = f"""      <section class="vbg-custom-hero">
        <div class="vbg-custom-hero-copy">
          <h1 class="vbg-display">
            Python at Vercel scale
          </h1>
          <p class="vbg-lede">{html.escape(page.lede)}</p>
          <div class="vbg-custom-actions">
            <a class="vbg-custom-btn-primary" href="/guides/getting-started">
              Get started
            </a>
            <a class="vbg-custom-btn-secondary" href="/reference/routing">
              API reference
            </a>
          </div>
          <div class="vbg-custom-install">
            <p class="vbg-label">Install with uv</p>
            {install_html.strip()}
          </div>
        </div>
        <div class="vbg-custom-hero-code">
          <div class="vbg-custom-code-bar">
            <span class="vbg-mono">app/api/items/[item_id]/route.py</span>
            <span class="vbg-mono">/api/items/{{item_id}}</span>
          </div>
          {hero_code_html.strip()}
        </div>
      </section>

      <section class="vbg-custom-landing-section">
        <div class="vbg-custom-section-head">
          <h2 class="vbg-heading-24">Built for Vercel Functions</h2>
          <p class="vbg-custom-section-lead">
            Chano keeps familiar typed Python handlers and dependency injection
            while compiling each route into its own Vercel Function for infinite
            serverless scaling.
          </p>
        </div>
        <div class="vbg-custom-pillar-grid">
          <div class="vbg-custom-pillar">
            <h3 class="vbg-heading-16">One function per route</h3>
            <p>
              Each <code>route.py</code> compiles into its own Vercel Function with
              independent <code>memory</code>, <code>max_duration</code>,
              <code>regions</code>, and AST tree-shaken dependencies.
            </p>
          </div>
          <div class="vbg-custom-pillar">
            <h3 class="vbg-heading-16">Small runtime</h3>
            <p>
              Built directly on ASGI and <code>msgspec</code> for single-pass JSON
              decoding and validation, with automatic
              <code>pydantic.BaseModel</code> support when imported.
            </p>
          </div>
          <div class="vbg-custom-pillar">
            <h3 class="vbg-heading-16">Folder-scoped conventions</h3>
            <p>
              Place <code>boundary.py</code> in any folder under
              <code>app/</code> to scope guards, error recovery, and HTTP
              wrappers to that subtree without central app wiring.
            </p>
          </div>
          <div class="vbg-custom-pillar">
            <h3 class="vbg-heading-16">Static docs and function lifecycles</h3>
            <p>
              OpenAPI 3.1 and Swagger UI pre-render to the Vercel CDN at build time.
              Use <code>lazy()</code> for warm singletons and <code>after()</code>
              for background work.
            </p>
          </div>
          <div class="vbg-custom-pillar">
            <h3 class="vbg-heading-16">Seamless Vercel</h3>
            <p>
              Get access to Vercel's whole ecosystem of platform services like
              Queues, Workflows, Sandboxes, Daemons, Schedules, and more.
            </p>
          </div>
        </div>
      </section>

      <section class="vbg-custom-landing-section">
        <div class="vbg-custom-section-head">
          <h2 class="vbg-heading-24">Familiar patterns, scoped to folders</h2>
          <p class="vbg-custom-section-lead">
            Share auth checks, middleware, and error handling across a route
            subtree by placing special files in any directory under
            <code>app/</code>.
            <a href="/guides/application-structure">Explore application structure.</a>
          </p>
        </div>
        <div class="vbg-custom-example-grid">
          <div class="vbg-custom-hero-code">
            <div class="vbg-custom-code-bar">
              <span class="vbg-mono">app/(admin)/boundary.py</span>
              <span class="vbg-mono">Subtree auth</span>
            </div>
            {deps_code_html.strip()}
          </div>
          <div class="vbg-custom-hero-code">
            <div class="vbg-custom-code-bar">
              <span class="vbg-mono">app/boundary.py</span>
              <span class="vbg-mono">Root-to-leaf wrapper</span>
            </div>
            {middleware_code_html.strip()}
          </div>
        </div>
      </section>

      <section class="vbg-custom-landing-section">
        <div class="vbg-custom-section-head">
          <h2 class="vbg-heading-24">Develop locally, deploy to Vercel</h2>
          <p class="vbg-custom-section-lead">
            Three commands take a project from local hot reload to prebuilt
            Vercel-compatible functions.
          </p>
        </div>
        <div class="vbg-custom-steps">
          <div class="vbg-custom-step">
            <p class="vbg-custom-step-cmd"><code>uv run chano dev</code></p>
            <p>
              Run all routes, <code>/docs</code>, <code>/openapi.json</code>, and
              <code>public/</code> locally with reload.
            </p>
          </div>
          <div class="vbg-custom-step">
            <p class="vbg-custom-step-cmd"><code>uv run chano build</code></p>
            <p>
              Emit deterministic Build Output API functions with tree-shaken
              <code>_vendor/</code> bundles.
            </p>
          </div>
          <div class="vbg-custom-step">
            <p class="vbg-custom-step-cmd"><code>vercel deploy --prebuilt</code></p>
            <p>
              Upload prebuilt functions and static CDN assets directly without a
              remote build step.
            </p>
          </div>
        </div>
        <p class="vbg-custom-section-lead">
          Deploying outside Vercel? Call <code>create_app()</code> to run the
          entire application as a standard ASGI app on Uvicorn or any ASGI
          platform.
          <a href="/guides/deploying-to-vercel#running-on-other-platforms-asgi">
            See ASGI hosting.
          </a>
        </p>
      </section>

      <section class="vbg-custom-landing-section">
        <div class="vbg-custom-next-grid">
          <a class="vbg-custom-next-link" href="/guides/getting-started">
            <span class="vbg-heading-20">Guides</span>
            <span class="vbg-custom-next-desc">
              Walk through project setup, the Vercel Functions model, folder
              conventions, and deployment.
            </span>
          </a>
          <a class="vbg-custom-next-link" href="/reference/routing">
            <span class="vbg-heading-20">Reference</span>
            <span class="vbg-custom-next-desc">
              Look up exact routing rules, parameter markers, response classes,
              pyproject.toml keys, and CLI flags.
            </span>
          </a>
        </div>
      </section>"""
    return render_document(page, main_html)


def render_sidebar(current: Page, sections: dict[str, list[Page]]) -> str:
    groups: list[str] = []
    for key, heading in (("guides", "Guides"), ("reference", "Reference")):
        pages = sections.get(key, [])
        if not pages:
            continue
        items = []
        for p in pages:
            aria = ' aria-current="page"' if p.url == current.url else ""
            items.append(
                f'            <li><a href="{html.escape(p.url)}"{aria}>'
                f"{html.escape(p.nav_title)}</a></li>"
            )
        groups.append(
            f"""        <div class="vbg-custom-sidebar-group">
          <p class="vbg-custom-sidebar-title">{html.escape(heading)}</p>
          <ul class="vbg-custom-sidebar-list">
{chr(10).join(items)}
          </ul>
        </div>"""
        )
    return f"""      <nav class="vbg-custom-sidebar" aria-label="Documentation">
{chr(10).join(groups)}
      </nav>"""


def render_pager(current: Page, sections: dict[str, list[Page]]) -> str:
    siblings = sections.get(current.section, [])
    try:
        idx = next(i for i, p in enumerate(siblings) if p.url == current.url)
    except StopIteration:
        return ""

    prev_page = siblings[idx - 1] if idx > 0 else None
    next_page = siblings[idx + 1] if idx + 1 < len(siblings) else None
    if not prev_page and not next_page:
        return ""

    prev_url = html.escape(prev_page.url) if prev_page else ""
    prev_title = html.escape(prev_page.nav_title) if prev_page else ""
    next_url = html.escape(next_page.url) if next_page else ""
    next_title = html.escape(next_page.nav_title) if next_page else ""

    prev_html = (
        f'          <a class="vbg-custom-pager-link" href="{prev_url}">\n'
        f'            <span class="vbg-custom-pager-label">Previous</span>\n'
        f'            <span class="vbg-custom-pager-title">{prev_title}</span>\n'
        f"          </a>"
        if prev_page
        else "          <span></span>"
    )
    next_html = (
        f'          <a class="vbg-custom-pager-link vbg-custom-pager-next"'
        f' href="{next_url}">\n'
        f'            <span class="vbg-custom-pager-label">Next</span>\n'
        f'            <span class="vbg-custom-pager-title">{next_title}</span>\n'
        f"          </a>"
        if next_page
        else "          <span></span>"
    )
    return f"""        <nav class="vbg-custom-pager" aria-label="Pagination">
{prev_html}
{next_html}
        </nav>"""


def render_doc(page: Page, sections: dict[str, list[Page]]) -> str:
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
        f'          <p class="vbg-lede">{html.escape(page.lede)}</p>'
        if page.lede
        else ""
    )
    sidebar_html = render_sidebar(page, sections)
    pager_html = render_pager(page, sections)
    layout_class = (
        "vbg-custom-docs-layout vbg-custom-docs-with-toc"
        if page.toc
        else "vbg-custom-docs-layout"
    )

    main_html = f"""      <div class="{layout_class}">
{sidebar_html}
        <div class="vbg-custom-doc-main">
          <section class="vbg-custom-opening">
            <h1 class="vbg-title">{html.escape(page.title)}</h1>
{lede_html}
          </section>
          <article class="vbg-custom-prose">
{page.body_html}
          </article>
{pager_html}
        </div>
{toc_html}
      </div>"""
    return render_document(page, main_html)


def load_pages() -> list[Page]:
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

        title = meta.get("title", SITE_NAME)
        pages.append(
            Page(
                src=md_path,
                out=out_path,
                url=url,
                title=title,
                nav_title=meta.get("nav_title", title),
                description=meta.get("description", ""),
                lede=meta.get("lede", ""),
                layout=meta.get("layout", "doc"),
                section=meta.get("section", ""),
                order=int(meta.get("order", "99")),
                body_html=body_html,
                toc=toc,
            )
        )
    return pages


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

    pages = load_pages()
    sections: dict[str, list[Page]] = {}
    for page in pages:
        if page.section:
            sections.setdefault(page.section, []).append(page)
    for group in sections.values():
        group.sort(key=lambda p: (p.order, p.nav_title))

    for page in pages:
        html_output = (
            render_landing(page)
            if page.layout == "landing"
            else render_doc(page, sections)
        )
        page.out.parent.mkdir(parents=True, exist_ok=True)
        page.out.write_text(html_output, encoding="utf-8")

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
