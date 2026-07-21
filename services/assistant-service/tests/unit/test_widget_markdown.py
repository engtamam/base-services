"""The widget's markdown renderer — it must format, and it must not become an injection.

The model answers in markdown, so rendering with textContent shows a user literal "**VAT number**"
and a row of dashes where a list belongs: help that looks broken. The obvious fix — innerHTML plus
a markdown library — is the wrong trade: it re-opens script injection through KB content and adds a
dependency to audit.

So the renderer builds DOM NODES. These tests assert that in two ways, because the second is the
one that actually matters:

  * the OUTPUT formats correctly (bold, lists, code, links)
  * the NODE TYPES are safe — a `<script>` in an entry must produce a text node, never an element,
    and a `javascript:` URL must produce no anchor at all

Checking a serialized string would pass for both a text node and a real element, so the assertions
below inspect what was constructed rather than how it prints.

Node is used because the renderer is JavaScript; the test SKIPS if node is absent rather than
failing, since a missing toolchain is not a defect in the widget.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

WIDGET = pathlib.Path(__file__).resolve().parents[2] / "app" / "widget" / "widget.js"

# A DOM small enough to be obviously correct, so a passing test cannot be an artefact of a clever
# stub. Elements record their tag; text nodes record their text and nothing else.
HARNESS = r"""
class N {
  constructor(tag){ this.tagName=tag; this.children=[]; this._text=""; this.attrs={}; }
  appendChild(c){ this.children.push(c); return c; }
  setAttribute(k,v){ this.attrs[k]=v; }
  set textContent(v){ this._text=v; this.children=[]; }
  get textContent(){ return this._text || this.children.map(c=>c.textContent).join(""); }
  get html(){
    if(this.tagName==="#text") return this._text;
    const inner = this._text || this.children.map(c=>c.html).join("");
    const a = Object.entries(this.attrs).map(([k,v])=>` ${k}="${v}"`).join("");
    return this.tagName==="#frag" ? inner : `<${this.tagName}${a}>${inner}</${this.tagName}>`;
  }
}
globalThis.document = {
  createElement: t => new N(t),
  createTextNode: t => { const n = new N("#text"); n._text = t; return n; },
  createDocumentFragment: () => new N("#frag"),
};
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };

const fs = require("fs");
const src = fs.readFileSync(SRC_PATH, "utf8");
eval(src.replace("})();", "globalThis.__md = markdown; })();"));

function walk(n, out=[]) { out.push(String(n.tagName)); (n.children||[]).forEach(c=>walk(c,out)); return out; }

const frag = globalThis.__md(INPUT);
console.log(JSON.stringify({ html: frag.html, tags: walk(frag) }));
"""


def _render(markdown_text: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the renderer is JavaScript")
    script = (HARNESS
              .replace("SRC_PATH", json.dumps(str(WIDGET)))
              .replace("INPUT", json.dumps(markdown_text)))
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"renderer failed: {out.stderr[-500:]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_widget_is_syntactically_valid():
    """Guards a real incident: an escaping bug once left a broken regex in the shipped file, and a
    mis-read exit code reported it as fine."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(WIDGET)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]


# ── it formats ────────────────────────────────────────────────────────────────
def test_bold_and_inline_code_render_without_mangling_the_text():
    r = _render("Your **VAT number** is `15` digits.")
    assert "<strong>VAT number</strong>" in r["html"]
    assert "<code>15</code>" in r["html"]
    assert r["html"].count("digits.") == 1, "text was duplicated"


def test_bullet_lists_become_a_list():
    r = _render("Steps:\n\n- Open Settings\n- Enter the URL\n- Save")
    assert r["tags"].count("li") == 3 and "ul" in r["tags"]


def test_numbered_lists_become_an_ordered_list():
    r = _render("1. First\n2. Second")
    assert "ol" in r["tags"] and r["tags"].count("li") == 2


def test_headings_render():
    assert "Refunds" in _render("## Refunds\n\nTake ten days.")["html"]


def test_a_code_fence_becomes_pre_code():
    r = _render("Run:\n\n```\nnpm install\n```")
    assert "pre" in r["tags"] and "npm install" in r["html"]


def test_plain_prose_is_a_paragraph():
    r = _render("Just a sentence.")
    assert r["tags"].count("p") == 1 and "Just a sentence." in r["html"]


def test_unsupported_markers_survive_as_text():
    # Losing a user's content is worse than showing a marker we do not style.
    assert "~~struck~~" in _render("A ~~struck~~ word.")["html"]


# ── it cannot inject ──────────────────────────────────────────────────────────
def test_html_in_the_content_becomes_a_text_node_not_an_element():
    """THE PROPERTY. A serialized string would look the same either way, so this asserts on the
    node types that were constructed."""
    r = _render("Hello <script>alert(1)</script> world")
    assert "script" not in [t.lower() for t in r["tags"]], "a script element was created"
    assert "#text" in r["tags"]


def test_a_javascript_url_produces_no_anchor():
    r = _render("Click [here](javascript:alert(1)).")
    assert r["tags"].count("a") == 0, "javascript: reached an href"


def test_a_data_url_produces_no_anchor():
    r = _render("Click [here](data:text/html;base64,PHNjcmlwdD4=).")
    assert r["tags"].count("a") == 0


def test_an_https_link_is_allowed_and_cannot_reach_back():
    r = _render("See [docs](https://ok.example/x).")
    assert r["tags"].count("a") == 1
    assert 'rel="noopener noreferrer"' in r["html"], "the opened page could reach window.opener"


def test_a_relative_link_is_allowed():
    # Deep links into the host application are the common, useful case.
    assert _render("Open [settings](/settings/billing).")["tags"].count("a") == 1
