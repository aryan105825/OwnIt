// @exports: window.OwnIt = {h, api, post, stateBox, btn, guard, toast, code, register, show}   // shared UI toolkit; boots the tabs on DOMContentLoaded
// @exports: tab "dashboard" (ownership headline, module table, rescan), tab "functions" (function cards), tab "proof" (live PASS/FAIL claims)
// @imports: ownit/server.py:GET /api/report, POST /api/rescan, GET /api/functions, GET /api/card, GET /api/source, GET /api/proof
// @schema: OwnIt.stateBox(kind, title, body, actions) renders section[data-state=kind]; kinds: loading, empty, error, stale, all-owned
// @schema: OwnIt.register(name, async (view: HTMLElement, params: object) => void); a thrown error is shown as the error state with a retry button
// @schema: OwnIt.show(name, params) switches tab; OwnIt.code([{n, text}]) renders a numbered source block
(function () {
  "use strict";

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "style") node.style.cssText = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid);
    }
    return node;
  }

  async function api(path, options) {
    let res;
    try {
      res = await fetch(path, options);
    } catch (err) {
      throw new Error("Cannot reach the OwnIt server (" + err.message + "). Check that `python -m ownit.server PATH` is still running, then try again.");
    }
    const text = await res.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch (err) {
      throw new Error(path + " answered with something that is not JSON (HTTP " + res.status + "). Restart the OwnIt server.");
    }
    if (!res.ok) {
      throw new Error(path + " failed: " + ((body && body.error) || "HTTP " + res.status));
    }
    return body;
  }

  function post(path, payload) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function btn(label, onclick, cls) {
    return h("button", { type: "button", class: "btn" + (cls ? " " + cls : ""), onclick: onclick, text: label });
  }

  let toastTimer = null;
  function toast(message, isError) {
    let node = document.getElementById("toast");
    if (!node) {
      node = h("div", { id: "toast", role: "status" });
      document.body.append(node);
    }
    node.textContent = message;
    node.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      node.className = "toast";
    }, isError ? 9000 : 4500);
  }

  function guard(fn) {
    return async function (event) {
      try {
        await fn(event);
      } catch (err) {
        toast(err.message, true);
      }
    };
  }

  function stateBox(kind, title, body, actions) {
    return h(
      "section",
      { class: "state state-" + kind, "data-state": kind, role: kind === "error" ? "alert" : "status" },
      h("h2", { text: title }),
      body ? h("p", { text: body }) : null,
      actions && actions.length ? h("div", { class: "actions" }, actions) : null
    );
  }

  function code(lines) {
    return h(
      "pre",
      { class: "code" },
      lines.map(function (line) {
        return h("span", { class: "ln" }, h("span", { class: "n", text: String(line.n) }), h("span", { class: "t", text: line.text === "" ? " " : line.text }));
      })
    );
  }

  const renderers = {};
  function register(name, fn) {
    renderers[name] = fn;
  }

  async function show(name, params) {
    for (const button of document.querySelectorAll("[data-tab]")) {
      const on = button.dataset.tab === name;
      button.setAttribute("aria-selected", on ? "true" : "false");
      button.classList.toggle("active", on);
    }
    const view = document.getElementById("view");
    view.replaceChildren(stateBox("loading", "Loading", "Reading the call graph."));
    try {
      if (!renderers[name]) throw new Error("There is no tab called " + name + ".");
      await renderers[name](view, params || {});
    } catch (err) {
      view.replaceChildren(
        stateBox("error", "Something went wrong", err.message, [btn("Try again", function () { show(name, params); })])
      );
    }
  }

  function pct(part, total) {
    return total ? Math.round((100 * part) / total) : 0;
  }

  function ownershipBar(o) {
    const total = o.total || 1;
    function seg(cls, n, label) {
      return n ? h("span", { class: "seg " + cls, style: "width:" + (100 * n) / total + "%", title: n + " " + label }) : null;
    }
    return h(
      "div",
      { class: "bar", role: "img", "aria-label": o.owned + " owned, " + o.stale + " stale, " + o.missed + " missed, " + o.new + " not answered yet" },
      seg("owned", o.owned, "owned"),
      seg("stale", o.stale, "stale"),
      seg("missed", o.missed, "missed"),
      seg("new", o.new, "not answered yet")
    );
  }

  function legend() {
    function item(cls, text) {
      return h("li", null, h("span", { class: "swatch " + cls }), text);
    }
    return h("ul", { class: "legend" }, item("owned", "Owned and still valid"), item("stale", "Stale: code changed"), item("missed", "Missed"), item("new", "Not answered yet"));
  }

  async function rescan() {
    const result = await post("/api/rescan", {});
    toast("Rescanned: " + result.questions + " questions, " + result.stale + " stale, " + result.orphans + " retired.");
    await show("dashboard");
  }

  async function renderDashboard(view) {
    const report = await api("/api/report");
    document.getElementById("target").textContent = report.target;
    const o = report.ownership.overall;
    const stats = report.stats;

    if (stats.functions === 0) {
      view.replaceChildren(
        stateBox(
          "empty",
          "No functions to ask about",
          "OwnIt found no Python functions in " + report.target + ". Start it on a folder or file that contains Python code: python -m ownit.server PATH",
          [btn("Check again", function () { show("dashboard"); })]
        )
      );
      return;
    }
    if (o.total === 0) {
      view.replaceChildren(
        stateBox(
          "empty",
          "Nothing is certain enough to ask yet",
          "OwnIt found " + stats.functions + " functions, but every call was dynamic or ambiguous (" + stats.uncertain_calls + " uncertain calls), so no question can be answered with certainty. Anything uncertain is left out on purpose.",
          [btn("Rescan", guard(rescan))]
        )
      );
      return;
    }

    const nodes = [];
    nodes.push(
      h(
        "section",
        { class: "hero" },
        h("p", { class: "kicker", text: "Your grip on this code" }),
        h("h2", { class: "headline" }, "You understand ", h("span", { class: "figure", text: pct(o.owned, o.total) + "%" }), " of this code"),
        h("p", { class: "sub", text: o.owned + " of " + o.total + " questions answered correctly and still valid." }),
        ownershipBar(o),
        legend()
      )
    );

    if (o.stale > 0) {
      nodes.push(
        stateBox(
          "stale",
          o.stale + (o.stale === 1 ? " answer has" : " answers have") + " gone stale",
          "The code behind " + (o.stale === 1 ? "it" : "them") + " changed after you answered, so what you knew may no longer be true.",
          [btn("Re-answer the stale ones", function () { show("quiz", { status: "stale" }); }, "primary"), btn("Rescan", guard(rescan))]
        )
      );
    } else if (o.owned === o.total) {
      nodes.push(
        stateBox(
          "all-owned",
          "Everything is owned",
          "Every question is answered and still valid. After the next AI edit, rescan and anything affected will come back here.",
          [btn("Rescan after an edit", guard(rescan))]
        )
      );
    } else {
      const left = o.new + o.missed;
      nodes.push(
        h(
          "div",
          { class: "actions", style: "margin:0 0 20px" },
          btn(left + (left === 1 ? " question" : " questions") + " to go: start quiz", function () { show("quiz", {}); }, "primary"),
          btn("Rescan after an edit", guard(rescan))
        )
      );
    }

    if (report.orphans > 0) {
      nodes.push(
        h("p", { class: "note", text: report.orphans + " saved answer(s) belong to questions that no longer exist because functions were renamed or removed. They are ignored." })
      );
    }

    const rows = Object.keys(report.ownership.by_module).map(function (moduleName) {
      const mo = report.ownership.by_module[moduleName];
      return h(
        "tr",
        null,
        h("th", { scope: "row", text: moduleName }),
        h("td", null, ownershipBar(mo)),
        h("td", { class: "num", text: pct(mo.owned, mo.total) + "%" }),
        h("td", null, mo.stale ? h("span", { class: "stale-note", text: mo.stale + " stale" }) : ""),
        h("td", null, btn("Quiz me", function () { show("quiz", { module: moduleName }); }, "small"))
      );
    });
    nodes.push(
      h("h3", { class: "page-title", text: "By module" }),
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          null,
          h("thead", null, h("tr", null, h("th", { text: "Module" }), h("th", { text: "Understanding" }), h("th", { class: "num", text: "Owned" }), h("th", { text: "" }), h("th", { text: "" }))),
          h("tbody", null, rows)
        )
      )
    );

    function fact(label, value) {
      return h("div", { class: "fact" }, h("dt", { text: label }), h("dd", { text: String(value) }));
    }
    nodes.push(
      h(
        "dl",
        { class: "facts" },
        fact("Functions", stats.functions),
        fact("Calls traced", stats.resolved_calls),
        fact("Calls left out as uncertain", stats.uncertain_calls),
        fact("Questions", stats.questions),
        fact("Files skipped", stats.skipped_files)
      ),
      h("p", { class: "note", text: "A question is only asked when the call graph is certain about the answer. Dynamic calls, shared names and unresolved attributes are left out instead of guessed." })
    );

    view.replaceChildren(...nodes);
  }

  async function renderFunctions(view) {
    const data = await api("/api/functions");
    const list = data.functions;
    if (!list.length) {
      view.replaceChildren(stateBox("empty", "No functions found", "Start OwnIt on a folder that contains Python code: python -m ownit.server PATH"));
      return;
    }
    const byId = {};
    for (const fn of list) byId[fn.id] = fn;

    const input = h("input", { type: "search", placeholder: "Filter by name or file", "aria-label": "Filter functions" });
    const listEl = h("ul", { class: "fn-list" });
    const detail = h("div", { "aria-live": "polite" }, h("p", { class: "muted", text: "Pick a function to see who uses it, what it calls, and its source." }));
    let activeId = null;

    function draw() {
      const needle = input.value.trim().toLowerCase();
      const matches = list.filter(function (fn) {
        return (fn.id + " " + fn.relpath).toLowerCase().includes(needle);
      });
      if (!matches.length) {
        listEl.replaceChildren(h("li", null, h("p", { class: "muted", style: "padding:10px 12px", text: "No function matches that filter." })));
        return;
      }
      listEl.replaceChildren(
        ...matches.slice(0, 400).map(function (fn) {
          return h(
            "li",
            null,
            h(
              "button",
              { type: "button", class: "fn-item" + (fn.id === activeId ? " active" : ""), onclick: guard(function () { return openCard(fn.id); }) },
              h("span", { class: "fn-name", text: fn.id }),
              h("span", { class: "fn-file", text: fn.relpath + ":" + fn.line })
            )
          );
        })
      );
    }

    function linkList(ids) {
      if (!ids.length) return h("p", { class: "muted", text: "None" });
      return h(
        "ul",
        { class: "links" },
        ids.map(function (id) {
          return h("li", null, h("button", { type: "button", class: "link", onclick: guard(function () { return openCard(id); }), text: id }));
        })
      );
    }

    async function openCard(id) {
      const card = await api("/api/card?id=" + encodeURIComponent(id));
      const fn = byId[id];
      const last = Math.min(fn.end_line, fn.line + 199);
      const source = await api("/api/source?path=" + encodeURIComponent(fn.relpath) + "&start=" + fn.line + "&end=" + last);
      activeId = id;
      draw();
      detail.replaceChildren(
        h(
          "article",
          { class: "fcard" },
          h("h3", { text: card.id }),
          h("p", { class: "usage", text: card.summary }),
          h("p", { class: "sublabel", text: "Used by" }),
          linkList(card.callers),
          h("p", { class: "sublabel", text: "Calls" }),
          linkList(card.callees),
          card.uncertain_count > 0
            ? h("p", { class: "note", text: card.uncertain_count + " call name(s) in this function could not be resolved with certainty, so OwnIt leaves them out of questions." })
            : null,
          h("p", { class: "sublabel", text: card.relpath + " · lines " + fn.line + "–" + last }),
          code(source.lines)
        )
      );
    }

    input.addEventListener("input", draw);
    draw();
    view.replaceChildren(
      h("h2", { class: "page-title", text: "Function cards" }),
      h("div", { class: "fn-layout" }, h("div", null, input, listEl), detail)
    );
  }

  async function renderProof(view) {
    const proof = await api("/api/proof");
    if (!proof.claims.length) {
      view.replaceChildren(
        stateBox(
          "empty",
          "No proof results yet",
          "Run scripts/verify.sh from the repository root. It checks every claim against real measurements, writes results/summary.json, and this tab then shows the outcome."
        )
      );
      return;
    }
    const failing = proof.claims.filter(function (claim) { return !claim.pass; });
    const banner = proof.all_pass
      ? stateBox("all-owned", "All " + proof.claims.length + " claims pass", "Measured by the last run of scripts/verify.sh.")
      : stateBox("stale", failing.length + (failing.length === 1 ? " claim is" : " claims are") + " failing", "See the details below. Re-run scripts/verify.sh after fixing them.");
    const items = proof.claims.map(function (claim) {
      return h(
        "li",
        { class: "claim" + (claim.pass ? "" : " failing") },
        h("span", { class: "cid", text: claim.id }),
        h("span", { class: "cname", text: claim.name }),
        h("span", { class: "chip " + (claim.pass ? "pass" : "fail"), text: claim.pass ? "PASS" : "FAIL" }),
        h("span", { class: "cdetail", text: claim.detail })
      );
    });
    view.replaceChildren(h("h2", { class: "page-title", text: "Proof" }), banner, h("ol", { class: "claims" }, items));
  }

  register("dashboard", renderDashboard);
  register("functions", renderFunctions);
  register("proof", renderProof);

  window.OwnIt = { h: h, api: api, post: post, stateBox: stateBox, btn: btn, guard: guard, toast: toast, code: code, register: register, show: show };

  document.addEventListener("DOMContentLoaded", function () {
    for (const button of document.querySelectorAll("[data-tab]")) {
      button.addEventListener("click", function () {
        show(button.dataset.tab);
      });
    }
    show("dashboard");
  });
})();
