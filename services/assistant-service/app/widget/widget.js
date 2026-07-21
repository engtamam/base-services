/*
 * assistant-widget — a drop-in help panel.
 *
 *   <script src=".../widget.js"></script>
 *   <assistant-widget surface="admin" locale="ar" base-url="https://..." token="..."></assistant-widget>
 *
 * A WEB COMPONENT RATHER THAN A FRAMEWORK PACKAGE, for three reasons that each cost real money
 * otherwise. It works identically in React, Vue, Angular or plain HTML, so an adopter running four
 * UIs ships one widget instead of four. It renders into a SHADOW ROOT, so the host page's CSS
 * cannot break the panel and the panel's CSS cannot leak into the host — the usual first support
 * ticket for an embedded widget. And it has no dependencies, so there is nothing to audit, nothing
 * to keep patched, and no version conflict with whatever the host already loads.
 *
 * IT HOLDS NO SECRET. The host application mints a short-lived token and passes it in; the widget
 * never carries an API key. A key shipped to a browser is a key published.
 *
 * NOTHING IS EVER PARSED AS MARKUP. Answers arrive in markdown and are rendered by BUILDING DOM
 * NODES — createElement plus textContent — never innerHTML. A `<script>` inside an entry renders
 * as the visible characters `<script>`: inert, and obviously wrong to whoever wrote it. "It is our
 * own content" is exactly the assumption that turns a help panel into a stored-XSS carrier the
 * first time an adopter lets a customer edit an entry.
 */
(function () {
  "use strict";


  /*
   * A SMALL MARKDOWN RENDERER THAT NEVER PARSES HTML.
   *
   * The model answers in markdown, so rendering with textContent shows a user literal "**VAT
   * number**" and a line of dashes where a list should be — help that looks broken. The obvious
   * fix is innerHTML with a markdown library, and that is exactly the wrong trade: it re-opens
   * script injection through KB content and drags in a dependency to audit.
   *
   * So this builds DOM NODES. Every piece of text goes in through textContent and every element
   * is created explicitly, which means a `<script>` in an entry renders as the characters
   * `<script>` — visible, inert, and obviously wrong to whoever wrote it. There is no code path
   * that turns a string into markup.
   *
   * The subset is what help text actually uses: headings, bullet and numbered lists, code blocks,
   * inline code, bold, italic, and links. Anything else falls through as text rather than being
   * silently dropped — losing a user's content is worse than showing an unsupported marker.
   */
  // Each pattern captures its own leading offset so the slice math is trivial and cannot
  // double-count. An earlier version adjusted the index after matching and duplicated text — the
  // kind of arithmetic that looks right and is not, which is why the renderer has its own tests.
  var INLINE = [
    { name: "code", re: /`([^`]+)`/ },
    { name: "bold", re: /\*\*([^*]+)\*\*/ },
    { name: "link", re: /\[([^\]]+)\]\(([^)\s]+)\)/ },
    { name: "ital", re: /\*([^*]+)\*/ }
  ];

  /** Only http(s), root-relative or in-page links become anchors. Anything else stays TEXT — that
   *  one rule keeps `javascript:` out of an href without enumerating every scheme. */
  function safeHref(url) {
    var u = String(url || "").trim();
    return /^https?:\/\//i.test(u) || u.charAt(0) === "/" || u.charAt(0) === "#" ? u : null;
  }

  /** Inline markers -> nodes appended to `parent`. */
  function inline(parent, text) {
    while (text) {
      var best = null;
      for (var i = 0; i < INLINE.length; i++) {
        var m = INLINE[i].re.exec(text);
        // Strictly earlier wins, so the order above breaks ties: code before bold before italic,
        // which is what stops `**a**` being read as an italic `*` pair.
        if (m && (!best || m.index < best.m.index)) best = { kind: INLINE[i].name, m: m };
      }
      if (!best) { parent.appendChild(document.createTextNode(text)); return; }

      var m0 = best.m;
      if (m0.index > 0) parent.appendChild(document.createTextNode(text.slice(0, m0.index)));

      var el;
      if (best.kind === "code") {
        el = document.createElement("code");
        el.textContent = m0[1];                      // never re-parsed
      } else if (best.kind === "bold") {
        el = document.createElement("strong");
        inline(el, m0[1]);
      } else if (best.kind === "ital") {
        el = document.createElement("em");
        inline(el, m0[1]);
      } else {
        var href = safeHref(m0[2]);
        if (href) {
          el = document.createElement("a");
          el.setAttribute("href", href);
          el.setAttribute("rel", "noopener noreferrer");   // the opened page cannot reach back
          el.setAttribute("target", "_blank");
        } else {
          el = document.createElement("span");             // refused scheme: shown, never linked
        }
        inline(el, m0[1]);
      }
      parent.appendChild(el);

      // Advance past the WHOLE match. One expression, no per-kind adjustment to get wrong.
      text = text.slice(m0.index + m0[0].length);
    }
  }

  /** Blocks -> a fragment. */
  function markdown(src) {
    var frag = document.createDocumentFragment();
    var lines = String(src || "").split(/\r?\n/);
    var i = 0, list = null;

    function endList() { list = null; }

    while (i < lines.length) {
      var line = lines[i];

      if (/^```/.test(line)) {                                   // fenced code
        endList();
        var buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        var pre = document.createElement("pre");
        var code = document.createElement("code");
        code.textContent = buf.join("\n");                       // never parsed
        pre.appendChild(code);
        frag.appendChild(pre);
        continue;
      }

      var h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        endList();
        var hEl = document.createElement("h" + Math.min(6, h[1].length + 2));
        inline(hEl, h[2]);
        frag.appendChild(hEl);
        i++;
        continue;
      }

      var bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      var numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        var want = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== want) {
          list = document.createElement(want);
          frag.appendChild(list);
        }
        var li = document.createElement("li");
        inline(li, (bullet || numbered)[1]);
        list.appendChild(li);
        i++;
        continue;
      }

      if (!line.trim()) { endList(); i++; continue; }

      endList();                                                  // paragraph
      var para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !/^```|^#{1,4}\s|^\s*[-*+]\s|^\s*\d+[.)]\s/.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      var p = document.createElement("p");
      inline(p, para.join(" "));
      frag.appendChild(p);
    }
    return frag;
  }

  var DEFAULTS = {
    name: "Assistant",
    primary: "#2563eb",
    surface: "#ffffff",
    text: "#111827",
    position: "bottom-right",
    direction: "ltr",
    greeting: "",
    logo: ""
  };

  class AssistantWidget extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: "open" });
      this._open = false;
      this._busy = false;
      this._brand = Object.assign({}, DEFAULTS);
    }

    static get observedAttributes() {
      return ["surface", "locale", "token", "base-url"];
    }

    connectedCallback() {
      this._render();
      this._loadBranding();
    }

    attributeChangedCallback(name) {
      // A token change must not rebuild the panel and lose the conversation on screen.
      if (this.isConnected && name !== "token") this._render();
    }

    get _base() {
      return (this.getAttribute("base-url") || "").replace(/\/+$/, "");
    }

    _headers() {
      // Read at call time, never cached: the host rotates a short-lived token and the widget must
      // pick up the new one without being torn down.
      var h = { "Content-Type": "application/json" };
      var t = this.getAttribute("token") || "";
      if (t) h["Authorization"] = "Bearer " + t;
      return h;
    }

    _q(name) {
      return encodeURIComponent(this.getAttribute(name) || "");
    }

    async _loadBranding() {
      try {
        var url = this._base + "/api/v1/assistant/branding?surface=" + this._q("surface") +
          "&locale=" + this._q("locale");
        var r = await fetch(url, { headers: this._headers() });
        if (!r.ok) return;                     // branding is decoration; never block the panel on it
        var b = await r.json();
        this._brand = {
          name: b.name || DEFAULTS.name,
          greeting: b.greeting || "",
          primary: b.primary_color || DEFAULTS.primary,
          surface: b.surface_color || DEFAULTS.surface,
          text: b.text_color || DEFAULTS.text,
          logo: b.logo_url || "",
          position: b.position || DEFAULTS.position,
          direction: b.direction || DEFAULTS.direction
        };
        this._render();
      } catch (e) {
        /* offline or blocked: the panel still works, unbranded */
      }
    }

    _render() {
      var b = this._brand;
      var parts = (b.position || "bottom-right").split("-");
      var vpos = parts[0], hpos = parts[1];
      var rtl = b.direction === "rtl";
      this._root.textContent = "";             // clear; every node below is built, never parsed

      var style = document.createElement("style");
      // The interpolated values are validated server-side as strict hex colours and an enum
      // position, so none of them can close this rule and open another.
      style.textContent = [
        ":host { all: initial; }",
        ".wrap { position: fixed; " + vpos + ": 20px; " + hpos + ": 20px; z-index: 2147483000;",
        "  font: 14px/1.5 system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;",
        "  direction: " + b.direction + "; }",
        ".launch { width: 56px; height: 56px; border-radius: 50%; border: 0; cursor: pointer;",
        "  background: " + b.primary + "; color: #fff; font-size: 22px; }",
        ".panel { width: min(380px, calc(100vw - 40px)); height: min(560px, calc(100vh - 120px));",
        "  background: " + b.surface + "; color: " + b.text + "; border-radius: 12px;",
        "  box-shadow: 0 10px 40px rgba(0,0,0,.18); display: flex; flex-direction: column;",
        "  overflow: hidden; }",
        ".head { display: flex; align-items: center; gap: 8px; padding: 12px 14px;",
        "  background: " + b.primary + "; color: #fff; }",
        ".head img { height: 22px; width: auto; }",
        ".head .t { font-weight: 600; flex: 1; }",
        ".head button { background: transparent; border: 0; color: #fff; font-size: 20px; cursor: pointer; }",
        ".log { flex: 1; overflow-y: auto; padding: 12px 14px; }",
        ".msg { margin: 0 0 12px; }",
        ".msg p { margin: 0 0 8px; }",
        ".msg p:last-child { margin-bottom: 0; }",
        ".msg ul, .msg ol { margin: 6px 0; padding-inline-start: 20px; }",
        ".msg li { margin: 2px 0; }",
        ".msg h3, .msg h4, .msg h5, .msg h6 { margin: 10px 0 4px; font-size: 1em; font-weight: 600; }",
        ".msg code { background: rgba(0,0,0,.06); border-radius: 4px; padding: 1px 4px;",
        "  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }",
        ".msg pre { background: rgba(0,0,0,.06); border-radius: 8px; padding: 8px 10px;",
        "  overflow-x: auto; margin: 6px 0; }",
        ".msg pre code { background: none; padding: 0; }",
        ".msg a { color: " + b.primary + "; }",
        ".msg.me { text-align: " + (rtl ? "left" : "right") + "; opacity: .85; }",
        ".cites { margin: 4px 0 0; padding: 0; list-style: none; font-size: 12px; opacity: .75; }",
        ".acts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }",
        ".acts a { font-size: 12px; border: 1px solid " + b.primary + "; color: " + b.primary + ";",
        "  border-radius: 999px; padding: 2px 10px; text-decoration: none; }",
        ".ask { display: flex; gap: 6px; border-top: 1px solid rgba(0,0,0,.08); padding: 10px; }",
        ".ask input { flex: 1; padding: 8px 10px; border: 1px solid rgba(0,0,0,.2);",
        "  border-radius: 8px; font: inherit; color: inherit; background: transparent; }",
        ".ask button { background: " + b.primary + "; color: #fff; border: 0; border-radius: 8px;",
        "  padding: 8px 14px; cursor: pointer; }",
        "[hidden] { display: none !important; }"
      ].join("\n");
      this._root.appendChild(style);

      var wrap = document.createElement("div");
      wrap.className = "wrap";

      var launch = document.createElement("button");
      launch.className = "launch";
      launch.setAttribute("aria-label", "Open " + b.name);
      launch.textContent = "?";
      launch.addEventListener("click", this._toggle.bind(this));

      var panel = document.createElement("div");
      panel.className = "panel";
      panel.hidden = !this._open;
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", b.name);

      var head = document.createElement("div");
      head.className = "head";
      if (b.logo) {
        var img = document.createElement("img");
        img.src = b.logo;                      // validated http(s) server-side
        img.alt = "";
        head.appendChild(img);
      }
      var title = document.createElement("div");
      title.className = "t";
      title.textContent = b.name;               // textContent, never innerHTML
      var close = document.createElement("button");
      close.setAttribute("aria-label", "Close");
      close.textContent = "×";
      close.addEventListener("click", this._toggle.bind(this));
      head.appendChild(title);
      head.appendChild(close);

      this._log = document.createElement("div");
      this._log.className = "log";
      this._log.setAttribute("aria-live", "polite");

      var form = document.createElement("form");
      form.className = "ask";
      this._input = document.createElement("input");
      this._input.type = "text";
      this._input.setAttribute("aria-label", "Your question");
      var send = document.createElement("button");
      send.type = "submit";
      send.textContent = "→";
      form.appendChild(this._input);
      form.appendChild(send);
      form.addEventListener("submit", function (e) { e.preventDefault(); this._ask(); }.bind(this));

      panel.appendChild(head);
      panel.appendChild(this._log);
      panel.appendChild(form);
      wrap.appendChild(panel);
      wrap.appendChild(launch);
      this._root.appendChild(wrap);

      this._panel = panel;
      this._launch = launch;
      launch.hidden = this._open;
      if (b.greeting) this._say(b.greeting, "bot");
    }

    _toggle() {
      this._open = !this._open;
      if (this._panel) this._panel.hidden = !this._open;
      if (this._launch) this._launch.hidden = this._open;
      if (this._open && this._input) this._input.focus();
    }

    _say(text, who, citations, actions) {
      if (!this._log) return;
      var p = document.createElement("p");
      p.className = "msg" + (who === "me" ? " me" : "");
      if (who === "me") {
        // The user's own words are shown exactly as typed. Rendering them as markdown would let a
        // question containing an asterisk come back looking edited.
        p.textContent = text;
      } else {
        p.appendChild(markdown(text));          // nodes, never parsed markup
      }
      this._log.appendChild(p);

      if (citations && citations.length) {
        var ul = document.createElement("ul");
        ul.className = "cites";
        citations.forEach(function (c) {
          var li = document.createElement("li");
          li.textContent = "— " + (c.title || c.entry_id || "");
          ul.appendChild(li);
        });
        this._log.appendChild(ul);
      }
      if (actions && actions.length) {
        var box = document.createElement("div");
        box.className = "acts";
        actions.forEach(function (a) {
          // A LINK, never a call. The widget points at where the action lives; performing it on a
          // misread question is a large surface for a small gain.
          var link = document.createElement("a");
          link.textContent = a.label || a.id || "";
          link.setAttribute("href", a.path || "#");
          box.appendChild(link);
        });
        this._log.appendChild(box);
      }
      this._log.scrollTop = this._log.scrollHeight;
    }

    async _ask() {
      var q = (this._input.value || "").trim();
      if (!q || this._busy) return;
      this._busy = true;
      this._input.value = "";
      this._say(q, "me");
      try {
        var r = await fetch(this._base + "/api/v1/assistant/ask", {
          method: "POST",
          headers: this._headers(),
          body: JSON.stringify({
            question: q,
            surface: this.getAttribute("surface") || "",
            lang: this.getAttribute("locale") || ""
          })
        });
        if (!r.ok) {
          // Wording, not a raw status: a user reading "429" learns nothing they can act on.
          this._say(r.status === 429
            ? "You have reached today's limit. Please try again later."
            : "Something went wrong. Please try again.", "bot");
          return;
        }
        var d = await r.json();
        this._say(d.answer || "", "bot", d.citations, d.actions);
      } catch (e) {
        this._say("Could not reach the assistant.", "bot");
      } finally {
        this._busy = false;
      }
    }

    /** Open the panel showing one entry — what a `?` icon calls. No ranking, no model, no cost. */
    async help(anchor) {
      if (!this._open) this._toggle();
      try {
        var url = this._base + "/api/v1/assistant/help?anchor=" + encodeURIComponent(anchor) +
          "&surface=" + this._q("surface") + "&lang=" + this._q("locale");
        var r = await fetch(url, { headers: this._headers() });
        if (!r.ok) { this._say("No help for this yet.", "bot"); return; }
        var d = await r.json();
        this._say((d.title ? d.title + "\n\n" : "") + (d.body || ""), "bot");
      } catch (e) {
        this._say("Could not reach the assistant.", "bot");
      }
    }
  }

  if (!customElements.get("assistant-widget")) {
    customElements.define("assistant-widget", AssistantWidget);
  }
})();
