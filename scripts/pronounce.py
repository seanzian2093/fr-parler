#!/usr/bin/env python3
"""Build self-contained .html pages from .md notes.

Reads a Markdown notes file whose French words are wrapped in a
``{{word, gloss}}`` marker (the gloss is optional) and emits a single,
self-contained HTML file that:

* renders the Markdown "nicely" (headings, nested lists, emphasis, code),
* turns every ``{{word, gloss}}`` into a pronounce button,
* speaks the word with the browser's French text-to-speech when clicked,
* links every page together through a left-sidebar nav (built from the other
  ``*.md`` files in the source directory), the current page highlighted,
* in ``--all`` builds also writes an ``index.html`` that redirects the site root
  to the main page (``--main``; default: the first page), so GitHub Pages has a
  landing page to serve.

With PWA on (default; disable with ``--no-pwa``) every page links a web
manifest and registers a service worker, and the build also emits
``manifest.webmanifest``, ``sw.js``, and three PNG icons into the output dir —
making the site an installable, offline app.  No third-party dependencies: the
icons are drawn from a rounded gradient + a white speaker glyph with only the
stdlib, so the output stays self-contained except for those sibling PWA files
(a lone .html degrades gracefully if they are absent).

Usage:
    python3 scripts/pronounce.py                 # notes/Complete-French-All-in-One.md -> build/Complete-French-All-in-One.html
    python3 scripts/pronounce.py IN.md OUT.html  # one page, explicit output
    python3 scripts/pronounce.py --all           # notes/*.md -> build/*.html + index.html
    python3 scripts/pronounce.py --all --main Vocabulary   # redirect to a named page

Each page's ``<title>`` is taken from its first ``# H1`` (falling back to the
filename), so a glob build produces distinct tabs for every page.
"""

import argparse
import glob
import html
import json
import math
import os
import re
import struct
import sys
import zlib

# --------------------------------------------------------------------------- #
# The fixed, self-contained click handler.
#
# A user clicks a <button class="pronounce" data-say="...">.  We read the
# French text to speak, pick a French voice if the browser has one, and hand
# it to the Web Speech API.  Kept deliberately tiny and dependency-free.
# --------------------------------------------------------------------------- #
JS_CLICK_HANDLER = r"""
<script>
(function () {
  "use strict";

  // Voices may load asynchronously; keep the latest list handy.
  var frenchVoice = null;
  function pickVoice() {
    var voices = window.speechSynthesis.getVoices();
    for (var i = 0; i < voices.length; i++) {
      if (voices[i].lang && voices[i].lang.toLowerCase().indexOf("fr") === 0) {
        frenchVoice = voices[i];
        break;
      }
    }
  }
  pickVoice();
  if (window.speechSynthesis.onvoiceschanged !== undefined) {
    window.speechSynthesis.onvoiceschanged = pickVoice;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button.pronounce") : null;
    if (!button) {
      return;
    }
    var text = button.getAttribute("data-say");
    if (!text) {
      return;
    }
    if (!("speechSynthesis" in window)) {
      alert("Your browser does not support text-to-speech.");
      return;
    }
    var utter = new SpeechSynthesisUtterance(text);
    utter.lang = "fr-FR";
    if (frenchVoice) {
      utter.voice = frenchVoice;
    }
    utter.rate = 0.9; // slightly slower is friendlier for learners
    window.speechSynthesis.cancel(); // stop any word already playing
    window.speechSynthesis.speak(utter);
  });
})();
</script>
"""

# --------------------------------------------------------------------------- #
# The marker: {{word, gloss}}  with gloss optional.
# --------------------------------------------------------------------------- #
MARKER_RE = re.compile(
    r"\{\{\s*"
    r"(?P<word>[^{}]+?)"                       # the French word/phrase
    r"(?:\s*,\s*(?P<gloss>[^{}]+?))?"          # optional ", gloss"
    r"\s*\}\}"
)

# A marker may be wrapped in backticks, e.g. `{{le canard}}`.  Strip both
# surrounding backticks so it becomes a button instead of a code span.
BACKTICKED_MARKER_RE = re.compile(r"`(?P<m>\{\{[^{}]+\}\})`")


# --------------------------------------------------------------------------- #
# Inline rendering: code, emphasis, then the pronouncers.
# --------------------------------------------------------------------------- #
def render_inline(text):
    """Render inline Markdown (code, **bold**, _em_ / *em*) to HTML.

    Pronounce markers have already been pulled out into placeholders, so this
    never sees them.
    """
    # 1. Inline code: protect the contents, then style.
    code_spans = []

    def stash_code(m):
        code_spans.append(html.escape(m.group(1)))
        return "\x00CODE%d\x00" % (len(code_spans) - 1)

    text = re.sub(r"`([^`]+)`", stash_code, text)

    # 2. Escape the remaining plain text.
    text = html.escape(text)

    # 3. Emphasis.  Bold before italic so "**x**" is not split by "*x*".
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<em>\1</em>", text)

    # 4. Restore code spans.
    for i, code in enumerate(code_spans):
        text = text.replace("\x00CODE%d\x00" % i, "<code>%s</code>" % code)

    return text


# --------------------------------------------------------------------------- #
# Block rendering: headings, nested lists, paragraphs.
# --------------------------------------------------------------------------- #
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
INDENT_UNIT = 2  # this notes file indents nested lists by two spaces


def render_blocks(lines):
    """Turn Markdown block lines into HTML."""
    out = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Blank line -> skip (paragraph breaks are handled by the paragraph path).
        if not line.strip():
            i += 1
            continue

        # Heading.
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            content = render_inline(m.group(2).strip())
            out.append("<h%d>%s</h%d>" % (level, content, level))
            i += 1
            continue

        # Horizontal rule.
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # List (possibly nested): gather the run of list lines and build the tree.
        if LIST_ITEM_RE.match(line):
            start = i
            while i < n and (LIST_ITEM_RE.match(lines[i]) or
                             (lines[i].startswith(" ") and
                              i > start and LIST_ITEM_RE.match(lines[i - 1]))):
                i += 1
            out.append(render_list(lines[start:i]))
            continue

        # Paragraph: gather consecutive plain lines.
        start = i
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not HEADING_RE.match(lines[i]) \
                and not LIST_ITEM_RE.match(lines[i]) \
                and not re.match(r"^\s*([-*_])\1{2,}\s*$", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % render_inline(" ".join(para)))

    return "\n".join(out)


def render_list(item_lines):
    """Build nested <ul> HTML from a run of list-item lines.

    Indentation depth (in INDENT_UNIT steps) determines nesting.  A small stack
    tracks the open <ul> at each depth.
    """
    items = []  # (level, text)
    for line in item_lines:
        m = LIST_ITEM_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1).expandtabs())
        level = indent // INDENT_UNIT
        items.append((level, m.group(2)))

    if not items:
        return ""

    html_parts = []
    stack = [0]  # levels of the currently-open <ul> elements

    def open_ul(level):
        html_parts.append("<ul>")
        stack.append(level)

    def close_to(level):
        # Close <ul>s that sit at a deeper level than the item we are about to add.
        while stack[-1] > level:
            stack.pop()
            html_parts.append("</ul>")

    first = True
    for level, text in items:
        if first:
            open_ul(level)
            first = False
        else:
            if level == stack[-1]:
                pass  # sibling under the same <ul>
            elif level > stack[-1]:
                open_ul(level)
            else:
                close_to(level)
        html_parts.append("<li>%s</li>" % render_inline(text))

    while len(stack) > 1:
        stack.pop()
        html_parts.append("</ul>")

    return "\n".join(html_parts)


# --------------------------------------------------------------------------- #
# Pronounce markers.
# --------------------------------------------------------------------------- #
def pronouncers(markers):
    """Render collected (word, gloss) markers to button HTML, HTML-escaped."""
    buttons = []
    for word, gloss in markers:
        w = html.escape(word.strip())
        if gloss:
            g = html.escape(gloss.strip())
            label = w + ' <span class="gloss">%s</span>' % g
            title = "%s — %s" % (w, g)
        else:
            label = w
            title = w
        # A speaker glyph makes the button read as "press to hear this".
        buttons.append(
            '<button type="button" class="pronounce" '
            'data-say="%s" title="%s">\U0001F50A %s</button>'
            % (w, title, label)
        )
    return buttons


def extract_markers(text):
    """Pull every {{word, gloss}} out of the text, leaving safe placeholders.

    Returns (text_with_placeholders, markers) where markers is the list of
    (word, gloss) tuples in document order; the placeholder for markers[i] is
    the token "\x00PRONi\x00".
    """
    # First, drop backticks that wrap a marker so it isn't treated as code.
    text = BACKTICKED_MARKER_RE.sub(r"\g<m>", text)

    markers = []

    def stash(m):
        word = m.group("word")
        gloss = m.group("gloss")
        token = "\x00PRON%d\x00" % len(markers)
        markers.append((word, gloss))
        return token

    text = MARKER_RE.sub(stash, text)
    return text, markers


# --------------------------------------------------------------------------- #
# The page shell.
# --------------------------------------------------------------------------- #
CSS = """
:root {
  --fg: #e6e6e6;
  --muted: #9ca3af;
  --line: #333842;
  --code-bg: #2a2f3a;
  --btn-bg: #26304a;
  --btn-border: #3b4a73;
  --btn-fg: #c7d2fe;
  --accent: #818cf8;
  --bg: #0f1115;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
               Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.6;
  max-width: 46rem;
  margin: 3rem auto;
  padding: 0 1.25rem;
}
/* When a page carries a nav, widen and split it into two columns: a sticky
   sidebar on the left, the content on the right.  The single-page case keeps
   the centered layout above. */
body.has-nav {
  display: flex;
  align-items: flex-start;
  gap: 2.5rem;
  max-width: 64rem;
}
.site-nav {
  position: sticky;
  top: 3rem;
  align-self: flex-start;
  flex: 0 0 12rem;
  max-height: calc(100vh - 6rem);
  overflow-y: auto;
  font-size: .95rem;
}
.site-nav ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.site-nav li {
  margin: .1em 0;
}
.site-nav a {
  display: block;
  padding: .35em .6em;
  color: var(--muted);
  text-decoration: none;
  border: 1px solid transparent;
  border-radius: 6px;
}
.site-nav a:hover {
  color: var(--fg);
  background: var(--code-bg);
}
.site-nav a.current {
  color: var(--btn-fg);
  background: var(--btn-bg);
  border-color: var(--btn-border);
  font-weight: 600;
}
.site-nav a:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.content {
  flex: 1 1 auto;
  min-width: 0;
  max-width: 46rem;
}
/* On narrow screens the sidebar stacks above the content instead of the side. */
@media (max-width: 720px) {
  body.has-nav {
    flex-direction: column;
    gap: 1.5rem;
  }
  body.has-nav .site-nav {
    position: static;
    flex-basis: auto;
    max-height: none;
  }
}
h1, h2, h3, h4 { line-height: 1.25; margin-top: 1.8em; }
h1 { font-size: 2rem; border-bottom: 2px solid var(--line); padding-bottom: .3em; }
h2 { font-size: 1.5rem; border-bottom: 1px solid var(--line); padding-bottom: .25em; }
h3 { font-size: 1.2rem; }
code {
  background: var(--code-bg);
  padding: .12em .35em;
  border-radius: 4px;
  font-size: .9em;
  font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
}
ul { padding-left: 1.4em; }
li { margin: .15em 0; }
button.pronounce {
  font: inherit;
  font-size: .9em;
  line-height: 1;
  color: var(--btn-fg);
  background: var(--btn-bg);
  border: 1px solid var(--btn-border);
  border-radius: 999px;
  padding: .15em .5em;
  margin: 0 .05em;
  cursor: pointer;
  vertical-align: baseline;
}
button.pronounce:hover { filter: brightness(.96); }
button.pronounce:active { transform: translateY(1px); }
button.pronounce:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button.pronounce .gloss { color: var(--muted); font-style: italic; font-size: .85em; }
@media (prefers-color-scheme: light) {
  :root {
    --fg: #1b1f24; --muted: #6b7280; --line: #e5e7eb;
    --code-bg: #f3f4f6; --btn-bg: #eef2ff; --btn-border: #c7d2fe;
    --btn-fg: #3730a3; --accent: #4f46e5; --bg: #ffffff;
  }
}
"""


def render_nav(pages, current_href):
    """Render the left-sidebar navigation, or "" when there is nothing to link to.

    ``pages`` is a list of (href, label) in display order; the item whose href
    equals ``current_href`` is marked as the current page (highlighted and tagged
    ``aria-current="page"``).  With fewer than two pages the nav is omitted, so a
    lone page is rendered without a sidebar.
    """
    if not pages or len(pages) < 2:
        return ""

    items = []
    for href, label in pages:
        e_href = html.escape(href, quote=True)
        e_label = html.escape(label)
        if href == current_href:
            items.append(
                '<li><a class="current" href="%s" aria-current="page">%s</a></li>'
                % (e_href, e_label)
            )
        else:
            items.append(
                '<li><a href="%s">%s</a></li>' % (e_href, e_label)
            )

    return (
        '<nav class="site-nav" aria-label="Pages">\n'
        '  <ul>\n'
        "    " + "\n    ".join(items) + "\n"
        "  </ul>\n"
        "</nav>"
    )


# --------------------------------------------------------------------------- #
# The landing page: an index.html that redirects the site root to the main page.
# --------------------------------------------------------------------------- #
def render_redirect(href, title, pwa=True):
    """A minimal, no-JS-needed redirect from ``index.html`` to a page.

    Uses ``location.replace`` so the redirect doesn't linger in browser history,
    with a ``<meta http-equiv="refresh">`` fallback and a visible link for
    no-JS clients.  The link target is a generated same-stem filename, so it is
    safe; it is still JSON-encoded for the JS literal and HTML-escaped for the
    attributes to be correct for any character a filename might hold.  With PWA
    on it also carries the manifest + service-worker registration so a visitor who
    lands on the root still boots the offline shell.
    """
    js_target = json.dumps(href)
    attr_href = html.escape(href, quote=True)
    e_title = html.escape(title)
    pwa_meta = PWA_META if pwa else ""
    sw_reg = JS_SW_REGISTRATION if pwa else ""
    # Build the full template first, then format it.  A trailing `% (...)` binds
    # to the last literal only, so the PWA pieces must be spliced in before the
    # format args are applied (otherwise the final "%s" line mis-formats).
    template = (
        '<!doctype html>\n'
        '<html lang="fr">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + pwa_meta +
        "  <title>Redirecting…</title>\n"
        '  <meta http-equiv="refresh" content="0; url=%s">\n'
        '  <script>location.replace(%s);</script>\n'
        "</head>\n"
        "<body>\n"
        '  <p>Redirecting to <a href="%s">%s</a>…</p>\n'
        + sw_reg +
        "</body>\n"
        "</html>\n"
    )
    return template % (attr_href, js_target, attr_href, e_title)


def resolve_main(pages, main_name):
    """Pick the (href, title) landing page to redirect ``index.html`` to.

    ``main_name`` may be a filename or stem (``.md``/``.html`` optional, case
    insensitive); when omitted the first page in nav order is used.  Returns the
    matching ``(href, title)``, or None when there are no pages.  A miss warns
    and falls back to the first page rather than failing the build.
    """
    if not pages:
        return None
    if main_name is None:
        return pages[0]

    key = os.path.splitext(main_name)[0].strip().lower()
    for href, title in pages:
        if os.path.splitext(href)[0].lower() == key:
            return (href, title)

    print("warning: --main %r not found among pages; using %s"
          % (main_name, pages[0][0]))
    return pages[0]


def build_html(markdown_body, title="French notes", pages=None, current_href=None,
               pwa=True):
    text, markers = extract_markers(markdown_body)

    body_html = render_blocks(text.splitlines())

    # Re-insert pronouncers where the placeholders sit.
    buttons = pronouncers(markers)
    for i, button in enumerate(buttons):
        body_html = body_html.replace("\x00PRON%d\x00" % i, button)

    nav = render_nav(pages, current_href) if pages else ""
    has_nav = bool(nav)

    pwa_head = PWA_META if pwa else ""
    title_tag = "  <title>%s</title>\n" % html.escape(title)
    head = (
        "<!doctype html>\n"
        '<html lang="fr">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + pwa_head +
        title_tag +
        "  <style>\n" + CSS + "  </style>\n"
        "</head>\n"
    )

    # Page body: an optional sidebar nav, then the content column.  The body's
    # "has-nav" class switches on the two-column layout; without a nav it falls
    # back to the centered single-column look.
    body_tag = '<body class="has-nav">' if has_nav else "<body>"
    body_section = body_tag + "\n"
    if has_nav:
        body_section += nav + "\n"
    body_section += '  <main class="content">\n'
    body_section += "  " + body_html.replace("\n", "\n  ") + "\n"
    body_section += "  </main>\n"
    sw_reg = JS_SW_REGISTRATION if pwa else ""
    body_section += JS_CLICK_HANDLER + sw_reg + "</body>\n</html>\n"

    return head + body_section


# --------------------------------------------------------------------------- #
# PWA: manifest, service worker, and a generated app icon.
#
# With PWA on, every page links a web manifest and registers sw.js, and the build
# also emits manifest.webmanifest, sw.js, and three PNG icons into the output
# dir.  The result is an installable, offline app:
#   * the manifest declares the app name / icon / start page for the OS home
#     screen (Android Chrome and iOS "Add to Home Screen");
#   * sw.js precaches the whole static site and serves it offline (cache-first,
#     falling back to the main page when the network is down);
#   * the icons are drawn here from a rounded indigo gradient + a white speaker
#     glyph using only the stdlib (zlib/struct) — no image library, no binary
#     blob checked into the repo.
#
# Every asset URL is relative ("scope": "./", "src": "icon-…png",
# register("sw.js")), so the same build works whether GitHub Pages serves it at a
# domain root or under a /repo/ sub-path.  The .html pages degrade gracefully if
# the sibling PWA files are absent (a missing manifest/worker just no-ops).
# ---------------------------------------------------------------------------
PWA_ICONS = [
    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
]
PWA_ASSET_FILES = [
    "manifest.webmanifest", "sw.js",
    "icon-192.png", "icon-512.png", "icon-maskable-512.png",
]

# Injected into every page's <head>: manifest + install metadata.  The icon is the
# full-bleed maskable one, which iOS will round for the home screen.
PWA_META = (
    '  <meta name="theme-color" content="#0f1115">\n'
    '  <link rel="manifest" href="manifest.webmanifest">\n'
    '  <link rel="apple-touch-icon" href="icon-maskable-512.png">\n'
    '  <meta name="apple-mobile-web-app-capable" content="yes">\n'
    '  <meta name="apple-mobile-web-app-title" content="fr-parler">\n'
    '  <meta name="mobile-web-app-capable" content="yes">\n'
)

# Injected at the end of every page's <body>: register the offline app shell.
# No-ops silently where service workers are unavailable.
JS_SW_REGISTRATION = (
    "\n<script>\n"
    "// Install the offline app shell; no-ops where service workers are absent.\n"
    "if ('serviceWorker' in navigator) {\n"
    "  window.addEventListener('load', function () {\n"
    "    navigator.serviceWorker.register('sw.js').catch(function () {});\n"
    "  });\n"
    "}\n"
    "</script>\n"
)


def render_manifest(main_href, icons):
    """The web app manifest, pointing at the main page and the generated icons."""
    manifest = {
        "name": "fr-parler",
        "short_name": "fr-parler",
        "description": "Self-teach French: read the notes, tap any word to hear it.",
        "start_url": main_href,
        "scope": "./",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": icons,
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def render_service_worker(precache, main_href):
    """sw.js: precache every output file, serve it cache-first, and fall back to
    the main page when the network is unavailable."""
    return (
        '// Precached by scripts/pronounce.py.  Cache-first, offline fallback.\n'
        'const CACHE = "fr-parler-v1";\n'
        'const PRECACHE = %s;\n'
        'const OFFLINE = %s;\n'
        'self.addEventListener("install", function (e) {\n'
        '  e.waitUntil(caches.open(CACHE).then(function (c) {\n'
        '    return c.addAll(PRECACHE);\n'
        '  }).then(function () { return self.skipWaiting(); }));\n'
        '});\n'
        'self.addEventListener("activate", function (e) {\n'
        '  e.waitUntil(caches.keys().then(function (keys) {\n'
        '    return Promise.all(keys.filter(function (k) { return k !== CACHE; })\n'
        '        .map(function (k) { return caches.delete(k); }));\n'
        '  }).then(function () { return self.clients.claim(); }));\n'
        '});\n'
        'self.addEventListener("fetch", function (e) {\n'
        '  var req = e.request;\n'
        '  if (req.method !== "GET") { return; }\n'
        '  e.respondWith(caches.match(req).then(function (hit) {\n'
        '    if (hit) { return hit; }\n'
        '    return fetch(req).then(function (res) {\n'
        '      var copy = res.clone();\n'
        '      caches.open(CACHE).then(function (c) { c.put(req, copy); });\n'
        '      return res;\n'
        '    }).catch(function () { return caches.match(OFFLINE); });\n'
        '  }));\n'
        '});\n'
        % (json.dumps(precache), json.dumps(main_href))
    )


# --- icon drawing, pure stdlib (zlib + struct) ---------------------------- #

def _png_encode(rgba, size):
    """Encode a flat RGBA byte buffer (length size*size*4) to PNG bytes."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # per-scanline filter byte (None)
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", ihdr)
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += chunk(b"IEND", b"")
    return out


def _downsample(src, M, size):
    """Box-average an M×M RGBA buffer down to size×size for antialiasing."""
    out = bytearray(size * size * 4)
    scale = M / size
    for oy in range(size):
        y0 = int(oy * scale)
        y1 = max(y0 + 1, int((oy + 1) * scale))
        for ox in range(size):
            x0 = int(ox * scale)
            x1 = max(x0 + 1, int((ox + 1) * scale))
            rs = gs = bs = asum = 0.0
            for y in range(y0, y1):
                base = y * M * 4
                for x in range(x0, x1):
                    i = base + x * 4
                    a = src[i + 3] / 255.0
                    if a:
                        rs += src[i] * a
                        gs += src[i + 1] * a
                        bs += src[i + 2] * a
                    asum += a
            op = (oy * size + ox) * 4
            if asum:
                area = (y1 - y0) * (x1 - x0)
                out[op] = int(rs / asum + 0.5)
                out[op + 1] = int(gs / asum + 0.5)
                out[op + 2] = int(bs / asum + 0.5)
                out[op + 3] = int(asum / area * 255 + 0.5)
            else:
                out[op + 3] = 0
    return bytes(out)


def _npixel(n, box, M):
    """Map normalized [0,1] -> pixel, centered in an M-sized canvas via `box`."""
    return M * (0.5 + (n - 0.5) * box)


def _fill_rect(buf, M, x0, y0, x1, y1, color):
    x0, x1 = max(0, int(x0)), min(M, int(x1) + 1)
    y0, y1 = max(0, int(y0)), min(M, int(y1) + 1)
    r, g, b = color[0], color[1], color[2]
    for y in range(y0, y1):
        base = y * M * 4
        for x in range(x0, x1):
            i = base + x * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = r, g, b, 255


def _fill_polygon(buf, M, poly, color):
    px = [(int(x), int(y)) for x, y in poly]
    ys = [p[1] for p in px]
    r, g, b = color[0], color[1], color[2]
    for y in range(min(ys), max(ys) + 1):
        xints = []
        n = len(px)
        for i in range(n):
            x1, y1 = px[i]
            x2, y2 = px[(i + 1) % n]
            if (y1 <= y < y2) or (y2 <= y < y1):
                xints.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xints.sort()
        for k in range(0, len(xints) - 1, 2):
            xa = max(0, int(xints[k]))
            xb = min(M, int(xints[k + 1]) + 1)
            base = y * M * 4
            for x in range(xa, xb):
                i = base + x * 4
                buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = r, g, b, 255


def _fill_arc(buf, M, cx, cy, radius, half_ang, thickness, color):
    r0, r1 = radius - thickness / 2, radius + thickness / 2
    r02, r12 = r0 * r0, r1 * r1
    r, g, b = color[0], color[1], color[2]
    for y in range(max(0, int(cy - radius)), min(M, int(cy + radius) + 1)):
        dy = y - cy
        for x in range(max(0, int(cx - radius)), min(M, int(cx + radius) + 1)):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if r02 <= d2 <= r12 and -half_ang <= math.atan2(dy, dx) <= half_ang:
                i = (y * M + x) * 4
                buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = r, g, b, 255


def _draw_background(buf, M):
    """A vertical indigo gradient (accent -> indigo-600)."""
    top = (129, 140, 248)
    bot = (79, 70, 229)
    for y in range(M):
        t = y / (M - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        buf[y * M * 4:(y + 1) * M * 4] = bytes((r, g, b, 255)) * M


def _round_mask(buf, M, radius):
    """Zero the alpha of the four corners outside a rounded rect (radius)."""
    r2 = radius * radius
    for y in range(M):
        dy = y if y < M - 1 - y else M - 1 - y
        base = y * M * 4
        for x in range(M):
            dx = x if x < M - 1 - x else M - 1 - x
            if dx < radius and dy < radius and dx * dx + dy * dy > r2:
                buf[base + x * 4 + 3] = 0


def _draw_speaker(buf, M, box):
    """A white speaker + two sound waves, centered, sized by `box` (0..1)."""
    w = (255, 255, 255)
    # back block of the speaker
    _fill_rect(buf, M, _npixel(0.25, box, M), _npixel(0.42, box, M),
               _npixel(0.39, box, M), _npixel(0.58, box, M), w)
    # flaring cone opening to the right
    _fill_polygon(buf, M, [
        (_npixel(0.39, box, M), _npixel(0.34, box, M)),
        (_npixel(0.55, box, M), _npixel(0.16, box, M)),
        (_npixel(0.55, box, M), _npixel(0.84, box, M)),
        (_npixel(0.39, box, M), _npixel(0.66, box, M)),
    ], w)
    # two sound waves to the right of the cone
    cx, cy = _npixel(0.57, box, M), _npixel(0.50, box, M)
    for rnorm in (0.10, 0.17):
        _fill_arc(buf, M, cx, cy, rnorm * box * M, math.radians(40),
                  0.03 * box * M, w)


def _make_icon_png(size, maskable):
    """Draw the app icon at `size` px.  `maskable` is full-bleed (the platform
    masks it) vs. a rounded corner, with the glyph kept inside the safe zone."""
    M = 1024
    buf = bytearray(M * M * 4)
    _draw_background(buf, M)
    _draw_speaker(buf, M, 0.70 if not maskable else 0.56)
    if not maskable:
        _round_mask(buf, M, int(M * 0.22))
    return _png_encode(_downsample(buf, M, size), size)


def write_pwa_assets(out_dir, out_files, main_href):
    """Emit manifest.webmanifest, sw.js, and the icon PNGs into `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    precache = out_files + PWA_ASSET_FILES
    with open(os.path.join(out_dir, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        f.write(render_manifest(main_href, PWA_ICONS))
    with open(os.path.join(out_dir, "sw.js"), "w", encoding="utf-8") as f:
        f.write(render_service_worker(precache, main_href))
    for size, maskable, name in ((192, False, "icon-192.png"),
                                 (512, False, "icon-512.png"),
                                 (512, True, "icon-maskable-512.png")):
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(_make_icon_png(size, maskable))
        print("Wrote %s" % os.path.join(out_dir, name))
    print("Wrote PWA assets into %s" % out_dir)


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
# Files to skip when globbing a directory: README.md is not a page.
SKIP_STEMS = {"README"}


def title_from_name(path):
    """Fallback <title> when the Markdown has no H1: the filename, pretty-cased."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace("-", " ").replace("_", " ").title() or "Notes"


def derive_title(body, fallback):
    """<title> from the first ``# H1`` line, reducing any ``{{word, gloss}}``
    markers to just the word; falls back to the filename when there's no H1."""
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:  # only top-level headings
            title = MARKER_RE.sub(lambda mm: mm.group("word").strip(), m.group(2).strip())
            title = re.sub(r"[*_`]", "", title).strip()
            return title or fallback
    return fallback


def default_out(in_path):
    """Sibling .html with the same stem, e.g. notes/Food.md -> notes/Food.html."""
    base = os.path.basename(in_path)
    return os.path.splitext(base)[0] + ".html"


def discover_pages(src_dir):
    """Return [(href, title)] for every page in ``src_dir``, in sorted filename
    order, for use as the shared sidebar navigation.

    ``href`` is the same-stem ``.html`` that this page builds to (a relative link
    that works between pages that all land in the output directory); ``title`` is
    its H1, falling back to the filename.  README and other non-pages are skipped.
    """
    pages = []
    for in_path in sorted(glob.glob(os.path.join(src_dir, "*.md"))):
        stem = os.path.splitext(os.path.basename(in_path))[0]
        if stem in SKIP_STEMS:
            continue
        with open(in_path, "r", encoding="utf-8") as f:
            body = f.read()
        title = derive_title(body, title_from_name(in_path))
        pages.append((default_out(in_path), title))
    return pages


def build_one(in_path, out_path, pages=None, pwa=True):
    """Read one Markdown page and write its self-contained HTML twin.

    When ``pages`` is given (as ``(href, title)`` for the sibling pages) the page
    renders a sidebar linking to them, the current one highlighted; otherwise the
    sibling pages are discovered from the source directory.  With PWA on the page
    links the manifest and registers the service worker.
    """
    with open(in_path, "r", encoding="utf-8") as f:
        body = f.read()

    title = derive_title(body, title_from_name(in_path))

    if pages is None:
        pages = discover_pages(os.path.dirname(in_path) or ".")

    current_href = default_out(in_path)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(body, title=title, pages=pages,
                           current_href=current_href, pwa=pwa))

    print("Wrote %s from %s (title: %s)" % (out_path, in_path, title))


def build_all(src, out, main_name=None, pwa=True):
    """Glob every *.md in ``notes`` and write a same-stem .html into ``build``,
    skipping README and other non-pages, plus an ``index.html`` that redirects
    the site root to the main page.  With PWA on, also emits manifest, service
    worker, and icons into ``out`` so the site is installable and offline."""
    md_files = sorted(glob.glob(os.path.join(src, "*.md")))
    if not md_files:
        print("No .md files found in %s" % src)
        return 1

    # One shared sidebar: every page links to every other, built in the same run.
    pages = discover_pages(src)

    out_files = []
    built = 0
    for in_path in md_files:
        stem = os.path.splitext(os.path.basename(in_path))[0]
        if stem in SKIP_STEMS:
            continue
        out_name = default_out(in_path)
        build_one(in_path, os.path.join(out, out_name), pages=pages, pwa=pwa)
        out_files.append(out_name)
        built += 1

    # Landing page so GitHub Pages has an index.html to serve at the root.
    main = resolve_main(pages, main_name)
    main_href = main[0] if main else "index.html"
    if main:
        os.makedirs(out, exist_ok=True)
        index_path = os.path.join(out, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(render_redirect(*main, pwa=pwa))
        out_files.append("index.html")
        print("Wrote %s -> %s" % (index_path, main[0]))

    print("Built %d page(s) from %s into %s" % (built, src, out))
    if pwa:
        write_pwa_assets(out, out_files, main_href)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build self-contained .html pages from .md notes."
    )
    parser.add_argument("in_path", nargs="?", default="notes/Complete-French-All-in-One.md",
                       help="Markdown source (default: notes/Complete-French-All-in-One.md)")
    parser.add_argument("out_path", nargs="?",
                       help="Explicit HTML output path (overrides --out for one page)")
    parser.add_argument("--all", action="store_true",
                       help="build every *.md in --src into a shared-nav site + index.html")
    parser.add_argument("--main", default=None,
                       help="landing page index.html redirects to: a filename or "
                            "stem (default: the first page)")
    parser.add_argument("--src", default="notes",
                       help="source directory for --all (default: notes)")
    parser.add_argument("--out", default="build",
                       help="output directory for .html (default: build)")
    parser.add_argument("--no-pwa", action="store_true",
                        help="skip the PWA wiring (manifest, service worker, "
                             "icons) and emit plain HTML only")
    args = parser.parse_args(argv)
    pwa = not args.no_pwa

    if args.all:
        return build_all(args.src, args.out, main_name=args.main, pwa=pwa)

    out_path = args.out_path or os.path.join(args.out, default_out(args.in_path))
    build_one(args.in_path, out_path, pwa=pwa)
    if pwa:
        out_name = os.path.basename(out_path)
        write_pwa_assets(os.path.dirname(out_path) or ".", [out_name], out_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
