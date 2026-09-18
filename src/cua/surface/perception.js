// Perception layer injected into every frame (context.add_init_script).
//
// One module, three jobs, one shared notion of "what is this control":
//   snapshot()  -> compact text view of the frame for the LLM, with refs
//   describe()  -> candidate locator strategies for an element (recording)
//   resolve()   -> which elements match a strategy (replay)
//
// Because recording and replay compute role / name / label / table position
// with the *same* functions, a strategy that was unique at record time means
// the same thing at replay time. Nothing here depends on ids or test ids —
// legacy surfaces don't have them. The ordering of strategies mirrors what a
// human operator relies on: visible role + caption first, layout position
// (row/column in a table, "the box next to 'Search Value:'") next, raw
// structure last.
(() => {
  if (window.__cua) return;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const INTERACTIVE = "a[href], button, input, select, textarea, [onclick], [role=button], [role=link]";

  function visible(el) {
    if (!(el instanceof Element)) return false;
    if (el.tagName === "INPUT" && el.type === "hidden") return false;
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === "A") return "link";
    if (tag === "BUTTON") return "button";
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox";
    }
    if (tag === "TD" || tag === "TH") return "cell";
    if (el.hasAttribute("onclick")) return "button";
    return "text";
  }

  // Accessible name, simplified from the ARIA name computation.
  function accName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((n) => n.textContent).join(" ");
      if (norm(t)) return norm(t);
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && norm(lab.textContent)) return norm(lab.textContent);
    }
    const wrap = el.closest("label");
    if (wrap && norm(wrap.textContent)) return norm(wrap.textContent);
    const tag = el.tagName;
    if (tag === "INPUT" && ["submit", "button", "reset"].includes((el.type || "").toLowerCase())) return norm(el.value);
    if (tag === "IMG" || (tag === "INPUT" && el.type === "image")) return norm(el.alt);
    if (["A", "BUTTON", "TD", "TH"].includes(tag) || el.hasAttribute("onclick") || el.getAttribute("role")) {
      const t = norm(el.innerText || el.textContent);
      if (t) return t.slice(0, 120);
    }
    if (el.title) return norm(el.title);
    if (el.placeholder) return norm(el.placeholder);
    return "";
  }

  // Visual caption for unlabeled form fields: the nearest preceding text in
  // the same table row, or the preceding text in the same parent. This is
  // how a human reads "User ID [______]" on a table-layout legacy form.
  function visualLabel(el) {
    const cell = el.closest("td, th");
    if (cell) {
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = norm(prev.innerText || prev.textContent);
        if (t) return t.slice(0, 80);
        prev = prev.previousElementSibling;
      }
    }
    let n = el.previousSibling;
    while (n) {
      const t = norm(n.textContent);
      if (t) return t.slice(0, 80);
      n = n.previousSibling;
    }
    return "";
  }

  // ---- tables ------------------------------------------------------------
  function isHeaderRow(tr) {
    const cells = Array.from(tr.cells);
    if (!cells.length) return false;
    if (cells.every((c) => c.tagName === "TH")) return true;
    if (tr.getAttribute("bgcolor") || tr.closest("thead")) {
      return cells.every((c) => norm(c.innerText));
    }
    return cells.every((c) => c.querySelector("b, strong") && norm(c.innerText) === norm(c.querySelector("b, strong").innerText));
  }

  function tableInfo(cell) {
    const tr = cell.parentElement;
    const table = cell.closest("table");
    if (!tr || !table) return null;
    const rows = Array.from(table.rows).filter((r) => r.closest("table") === table);
    const header = rows.length > 1 && isHeaderRow(rows[0]) ? rows[0] : null;
    const colName = (i) => (header && header.cells[i] ? norm(header.cells[i].innerText) : `#${i}`);
    return { tr, table, rows, header, colName, index: cell.cellIndex };
  }

  // Pick a cell in this row that identifies the row by *meaning* rather than
  // by position: alphabetic, no digits, unique within its column. Share IDs
  // like S0001 differ per member; "Share Savings" does not.
  function rowKey(info) {
    const { tr, rows, header, colName } = info;
    const body = rows.filter((r) => r !== header);
    const cells = Array.from(tr.cells);
    const candidates = [];
    cells.forEach((c, i) => {
      const t = norm(c.innerText);
      if (!t || i === info.index) return;
      const same = body.filter((r) => r.cells[i] && norm(r.cells[i].innerText) === t).length;
      if (same !== 1) return;
      const score = (/\d/.test(t) ? 2 : 0) + (i === 0 ? 0 : 1) * 0.1;
      candidates.push({ column: colName(i), equals: t, score });
    });
    candidates.sort((a, b) => a.score - b.score);
    return candidates[0] || null;
  }

  function cellFor(el) {
    return el.tagName === "TD" || el.tagName === "TH" ? el : el.closest("td, th");
  }

  // ---- structural path (last resort) -------------------------------------
  function cssPath(el) {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && n.tagName !== "BODY" && n.tagName !== "HTML") {
      const tag = n.tagName.toLowerCase();
      const sibs = n.parentElement ? Array.from(n.parentElement.children).filter((c) => c.tagName === n.tagName) : [n];
      parts.unshift(sibs.length > 1 ? `${tag}:nth-of-type(${sibs.indexOf(n) + 1})` : tag);
      n = n.parentElement;
    }
    return "body > " + parts.join(" > ");
  }

  // ---- candidates + matching ---------------------------------------------
  function candidates() {
    const set = new Set();
    document.querySelectorAll(INTERACTIVE + ", td, th").forEach((el) => {
      if (visible(el)) set.add(el);
    });
    return Array.from(set);
  }

  function matches(el, s) {
    switch (s.by) {
      case "role_name":
        return role(el) === s.role && accName(el) === s.name;
      case "label":
        return role(el) === s.role && !accName(el) && visualLabel(el) === s.label;
      case "table_cell": {
        const cell = cellFor(el);
        if (cell !== el) return false;
        const info = tableInfo(cell);
        if (!info || info.tr === info.header) return false;
        if (info.colName(info.index) !== s.column) return false;
        const ci = Array.from(info.header ? info.header.cells : info.tr.cells).findIndex((c, i) => info.colName(i) === s.row.column);
        return ci >= 0 && info.tr.cells[ci] && norm(info.tr.cells[ci].innerText) === s.row.equals;
      }
      case "attr":
        return el.tagName.toLowerCase() === s.tag && el.getAttribute(s.attr) === s.value;
      case "css":
        return document.querySelector(s.path) === el;
      default:
        return false;
    }
  }

  let seq = 0;
  function refFor(el, prefix) {
    let r = el.getAttribute("data-cua-ref");
    if (!r || !r.startsWith(prefix)) {
      r = `${prefix}e${++seq}`;
      el.setAttribute("data-cua-ref", r);
    }
    return r;
  }

  function resolve(strategy, prefix) {
    // A half-parsed document can make a structural fallback match while the
    // semantic strategy (which needs the header row) cannot yet. Never
    // resolve against a document that is still loading.
    if (document.readyState !== "complete") return [];
    return candidates().filter((el) => matches(el, strategy)).map((el) => refFor(el, prefix));
  }

  function byRef(ref) {
    return document.querySelector(`[data-cua-ref="${CSS.escape(ref)}"]`);
  }

  // Strategies ordered by expected robustness; each tagged with whether it is
  // unique *right now* so the recorder only keeps unambiguous ones.
  function describe(el) {
    const out = [];
    const r = role(el);
    const name = accName(el);
    if (name && r !== "cell" && r !== "text") out.push({ by: "role_name", role: r, name });
    if (!name && r !== "cell" && r !== "text") {
      const lab = visualLabel(el);
      if (lab) out.push({ by: "label", role: r, label: lab });
    }
    const cell = cellFor(el);
    if (cell === el) {
      const info = tableInfo(cell);
      if (info && info.tr !== info.header) {
        const key = rowKey(info);
        if (key) out.push({ by: "table_cell", column: info.colName(info.index), row: { column: key.column, equals: key.equals } });
      }
    }
    for (const a of ["name", "value"]) {
      const v = el.getAttribute(a);
      if (v && ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName) && !(a === "value" && r === "textbox")) {
        out.push({ by: "attr", tag: el.tagName.toLowerCase(), attr: a, value: v });
        break;
      }
    }
    out.push({ by: "css", path: cssPath(el) });
    for (const s of out) s.unique = candidates().filter((c) => matches(c, s)).length === 1;
    return {
      role: r,
      name,
      label: visualLabel(el),
      tag: el.tagName.toLowerCase(),
      inputType: el.tagName === "INPUT" ? (el.type || "text").toLowerCase() : null,
      href: el.tagName === "A" ? el.href : null,
      formAction: el.form ? el.form.action : null,
      formMethod: el.form ? (el.form.method || "get").toLowerCase() : null,
      strategies: out,
    };
  }

  // ---- snapshot for the model ------------------------------------------
  // opaque=true is the evidence variant: data cells (non-header rows, not
  // "Caption:" cells) are replaced by their shape, so a failure snapshot shows
  // structure and labels without persisting member data.
  function snapshot(prefix, opaque) {
    const lines = [];
    const shown = (c, t) => {
      if (!opaque) return t;
      const info = tableInfo(c);
      if (/:$/.test(t) || (info && info.tr === info.header)) return t;
      return `‹${/\d/.test(t) ? "data" : "text"}:${t.length}›`;
    };
    const seen = new Set();
    const fieldLine = (el) => {
      const r = role(el);
      const ref = refFor(el, prefix);
      const name = accName(el);
      let line = `[${ref}] ${r}`;
      if (name) line += ` "${name}"`;
      const lab = visualLabel(el);
      if (!name && lab) line += ` label="${lab}"`;
      if (el.tagName === "INPUT" && el.type === "password") line += " (password)";
      else if (r === "textbox") line += ` value="${opaque && el.value ? "‹" + el.value.length + "›" : norm(el.value)}"`;
      if (el.tagName === "SELECT") {
        const opts = Array.from(el.options).map((o) => o.text);
        line += ` selected="${el.selectedOptions[0] ? el.selectedOptions[0].text : ""}" options=${JSON.stringify(opts)}`;
      }
      return line;
    };
    const walk = (node, depth) => {
      if (node.nodeType === 3) {
        const t = norm(node.textContent);
        const p = node.parentElement;
        if (t && p && visible(p) && !p.closest("td, th, a, button, select, option, script, style")) lines.push("  ".repeat(depth) + `text "${t}"`);
        return;
      }
      if (node.nodeType !== 1) return;
      const el = node;
      if (["SCRIPT", "STYLE", "HEAD", "OPTION"].includes(el.tagName)) return;
      if (!visible(el) && el.tagName !== "TBODY" && el.tagName !== "FORM") return;
      if (el.tagName === "TR") {
        const cells = Array.from(el.cells).filter(visible);
        // Layout row (legacy pages nest tables for positioning): descend instead
        // of flattening a whole form into one "cell".
        if (cells.some((c) => c.querySelector("table"))) {
          cells.forEach((c) => Array.from(c.childNodes).forEach((n) => walk(n, depth)));
          return;
        }
        const parts = [];
        const inner = [];
        for (const c of cells) {
          const fields = Array.from(c.querySelectorAll(INTERACTIVE)).filter(visible);
          if (fields.length) {
            fields.forEach((f) => { if (!seen.has(f)) { seen.add(f); inner.push(fieldLine(f)); } });
            const t = norm(c.innerText);
            if (t && !fields.some((f) => norm(f.innerText) === t)) parts.push(`[${refFor(c, prefix)}] "${shown(c, t)}"`);
          } else {
            const t = norm(c.innerText);
            if (t) parts.push(`[${refFor(c, prefix)}] "${shown(c, t)}"`);
          }
        }
        if (parts.length) lines.push("  ".repeat(depth) + "row: " + parts.join(" | "));
        inner.forEach((l) => lines.push("  ".repeat(depth + 1) + l));
        return;
      }
      if (el.matches(INTERACTIVE) && !seen.has(el)) {
        seen.add(el);
        lines.push("  ".repeat(depth) + fieldLine(el));
        return;
      }
      if (el.tagName === "TABLE") {
        lines.push("  ".repeat(depth) + "table:");
        Array.from(el.childNodes).forEach((c) => walk(c, depth + 1));
        return;
      }
      Array.from(el.childNodes).forEach((c) => walk(c, depth));
    };
    if (document.body) walk(document.body, 0);
    return lines.join("\n");
  }

  function readText(ref) {
    const el = byRef(ref);
    return el ? norm(el.innerText || el.value || el.textContent) : null;
  }

  function pageText() {
    return document.body ? norm(document.body.innerText) : "";
  }

  // Mark leaf elements whose text matches a sensitive pattern so screenshots
  // can mask them (Playwright `mask=`). Patterns come from the redactor.
  function markSensitive(patterns, literals) {
    document.querySelectorAll("[data-cua-sensitive]").forEach((e) => e.removeAttribute("data-cua-sensitive"));
    const res = patterns.map((p) => new RegExp(p));
    const hit = (t) => res.some((r) => r.test(t)) || literals.some((l) => l && t.includes(l));
    // Structural rule first: every data cell of a data table, and the value
    // cell beside a "Caption:" cell, is treated as sensitive — names don't
    // match any regex, so pattern-matching alone would leak them.
    document.querySelectorAll("td").forEach((c) => {
      const info = tableInfo(c);
      if (!info || info.table.querySelector("table")) return;
      const first = info.tr.cells[0] ? norm(info.tr.cells[0].innerText) : "";
      const dataRow = info.header && info.tr !== info.header;
      const kvValue = c.cellIndex > 0 && /:$/.test(first) && !c.querySelector(INTERACTIVE);
      if ((dataRow || kvValue) && norm(c.innerText)) c.setAttribute("data-cua-sensitive", "1");
    });
    document.querySelectorAll("td, th, span, font, b, i, a, div, input").forEach((el) => {
      const t = el.tagName === "INPUT" ? (el.type === "password" ? "x" : el.value || "") : norm(el.innerText);
      const leaf = el.tagName === "INPUT" || !Array.from(el.children).some((c) => norm(c.innerText));
      if ((el.tagName === "INPUT" && el.type === "password") || (leaf && t && hit(t))) el.setAttribute("data-cua-sensitive", "1");
    });
  }

  // ---- human capture ------------------------------------------------------
  // Every real user event in every frame is described with the same
  // describe() used by the recorder, so a human's manual steps can be
  // turned into artifact steps. Typed values are never reported — only
  // which field and how many characters.
  function report(ev) {
    try {
      if (typeof window.__cuaHumanEvent === "function") window.__cuaHumanEvent(ev);
    } catch (e) {}
  }
  document.addEventListener("click", (e) => {
    const el = e.target.closest(INTERACTIVE + ", td, th, span, div") || e.target;
    report({ type: "click", frame: window.name || "_top", target: describe(el), x: e.clientX, y: e.clientY });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    const isSelect = el.tagName === "SELECT";
    report({
      type: isSelect ? "select" : "fill",
      frame: window.name || "_top",
      target: describe(el),
      option: isSelect && el.selectedOptions[0] ? el.selectedOptions[0].text : null,
      length: isSelect ? null : (el.value || "").length,
    });
  }, true);

  window.__cua = { snapshot, describe: (ref) => describe(byRef(ref)), resolve, readText, pageText, markSensitive, byRef };
})();
