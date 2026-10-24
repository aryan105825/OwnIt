// @exports: tab "quiz" registered on window.OwnIt (filters: status, module, kind; pick-one, pick-many and ordering questions; evidence after each answer)
// @imports: dashboard.js:window.OwnIt.{h, api, post, stateBox, btn, guard, code, register, show}
// @imports: ownit/server.py:GET /api/report, GET /api/questions, POST /api/answer
// @schema: show("quiz", {status?: "new"|"stale"|"missed"|"owned"|"all", module?: string, kind?: "callee"|"impact"|"chain"}); the default status is stale, else new, else missed, else all
// @schema: question select modes from the API: "one" (single choice), "many" (multi choice, exact set), "order" (click every option in call order)
(function () {
  "use strict";

  const O = window.OwnIt;
  const h = O.h;
  const api = O.api;
  const post = O.post;
  const stateBox = O.stateBox;
  const btn = O.btn;
  const guard = O.guard;
  const code = O.code;

  const HINTS = {
    one: "Pick one.",
    many: "Pick every one that applies. All of them must be right.",
    order: "Click the functions in call order, first caller to last callee. Click a chosen one again to take it out.",
  };
  const KIND_NAMES = { callee: "Who gets called", impact: "What could break", chain: "Call chain" };
  const STATUS_NAMES = { new: "New", owned: "Owned", stale: "Stale", missed: "Missed last time" };

  function defaultStatus(o) {
    if (o.stale > 0) return "stale";
    if (o.new > 0) return "new";
    if (o.missed > 0) return "missed";
    return "all";
  }

  function select(labelText, name, choices, current, onChange) {
    const control = h(
      "select",
      { name: name, "aria-label": labelText },
      choices.map(function (choice) {
        return h("option", { value: choice[0], selected: choice[0] === current, text: choice[1] });
      })
    );
    control.addEventListener("change", function () { onChange(control.value); });
    return h("label", null, labelText, control);
  }

  function evidenceBlock(ev) {
    return h(
      "figure",
      { class: "evidence" },
      h("figcaption", null, h("span", { text: ev.note }), h("span", { class: "ev-where", text: ev.relpath + " · lines " + ev.start + "–" + ev.end })),
      code(ev.lines.map(function (text, i) { return { n: ev.start + i, text: text }; }))
    );
  }

  function questionCard(q, index, total, onNext) {
    const picked = [];
    let answered = false;
    const labels = {};
    for (const opt of q.options) labels[opt.key] = opt.label;

    const submit = btn("Check my answer", guard(submitAnswer), "primary");
    submit.disabled = true;
    const optionsEl = h("ul", { class: "options", "aria-label": "Answer choices" });
    const resultEl = h("div", { "aria-live": "polite" });
    const buttons = {};

    function isReady() {
      return q.select === "order" ? picked.length === q.options.length : picked.length > 0;
    }

    function mark(key) {
      const at = picked.indexOf(key);
      if (q.select === "order") return at >= 0 ? String(at + 1) : "";
      return at >= 0 ? "✓" : "";
    }

    function refresh() {
      for (const opt of q.options) {
        const node = buttons[opt.key];
        node.setAttribute("aria-pressed", picked.includes(opt.key) ? "true" : "false");
        node.querySelector(".mark").textContent = mark(opt.key);
      }
      submit.disabled = answered || !isReady();
    }

    function toggle(key) {
      if (answered) return;
      const at = picked.indexOf(key);
      if (q.select === "one") {
        picked.length = 0;
        picked.push(key);
      } else if (at >= 0) {
        picked.splice(at, 1);
      } else {
        picked.push(key);
      }
      refresh();
    }

    for (const opt of q.options) {
      const node = h(
        "button",
        { type: "button", class: "option", "aria-pressed": "false", onclick: function () { toggle(opt.key); } },
        h("span", { class: "mark" + (q.select === "many" ? "" : " round"), "aria-hidden": "true" }),
        h("span", { class: "label", text: opt.label })
      );
      buttons[opt.key] = node;
      optionsEl.append(h("li", null, node));
    }

    async function submitAnswer() {
      submit.disabled = true;
      const res = await post("/api/answer", { id: q.id, answer: picked.slice() });
      answered = true;
      for (const opt of q.options) {
        const node = buttons[opt.key];
        node.disabled = true;
        if (q.select !== "order") {
          if (res.answer.includes(opt.key)) node.classList.add("is-right");
          else if (picked.includes(opt.key)) node.classList.add("is-wrong");
        }
      }
      submit.remove();

      const answerItems = res.answer.map(function (key) { return h("li", { text: labels[key] }); });
      const verdict = h(
        "div",
        { class: "verdict" + (res.correct ? "" : " wrong") },
        h("h3", { text: res.correct ? "Correct." : "Not quite." }),
        h("p", { text: res.correct ? "This answer is now owned until the code behind it changes." : q.select === "order" ? "The shortest call chain, in order:" : "The right answer:" }),
        res.correct && q.select !== "order" ? null : h(q.select === "order" ? "ol" : "ul", { class: "answer-list" }, answerItems)
      );
      const last = index + 1 >= total;
      resultEl.replaceChildren(
        verdict,
        h("h3", { class: "sublabel", text: "Where this comes from in the source" }),
        res.explain.map(evidenceBlock),
        h("div", { class: "actions" }, btn(last ? "Finish" : "Next question", function () { onNext(res.correct); }, "primary"))
      );
    }

    const meta = h(
      "div",
      { class: "qmeta" },
      h("span", { class: "chip" + (q.status === "stale" ? " stale" : ""), text: STATUS_NAMES[q.status] || q.status }),
      h("span", { text: KIND_NAMES[q.kind] || q.kind }),
      h("span", { text: q.module }),
      h("span", { class: "progress", text: "Question " + (index + 1) + " of " + total })
    );

    return h(
      "article",
      { class: "qcard" },
      meta,
      q.status === "stale" && q.changed.length
        ? h("p", { class: "changed", text: "You answered this before, but the code changed in: " + q.changed.join(", ") + ". Answer again to own it once more." })
        : null,
      h("h2", { class: "qprompt", text: q.prompt }),
      h("p", { class: "qhint", text: HINTS[q.select] || "" }),
      optionsEl,
      h("div", { class: "actions", style: "margin:0 0 14px" }, submit),
      resultEl
    );
  }

  function runSession(stage, list, filters) {
    let index = 0;
    let right = 0;

    function step() {
      if (index >= list.length) {
        stage.replaceChildren(
          h(
            "section",
            { class: "summary" },
            h("h2", { text: "Batch finished" }),
            h("p", { text: right + " of " + list.length + " answered correctly." }),
            h(
              "div",
              { class: "actions" },
              btn("Keep going", function () { O.show("quiz", filters); }, "primary"),
              btn("Back to dashboard", function () { O.show("dashboard"); })
            )
          )
        );
        return;
      }
      stage.replaceChildren(
        questionCard(list[index], index, list.length, function (wasRight) {
          if (wasRight) right += 1;
          index += 1;
          step();
        })
      );
    }
    step();
  }

  async function loadStage(stage, filters, overall) {
    stage.replaceChildren(stateBox("loading", "Fetching questions", "Picking from the call graph."));
    const params = new URLSearchParams({ status: filters.status, limit: "20" });
    if (filters.module) params.set("module", filters.module);
    if (filters.kind) params.set("kind", filters.kind);
    const data = await api("/api/questions?" + params.toString());
    if (!data.questions.length) {
      if (overall.owned === overall.total) {
        stage.replaceChildren(
          stateBox("all-owned", "Everything is owned", "All " + overall.total + " questions are answered and still valid.", [btn("Back to dashboard", function () { O.show("dashboard"); })])
        );
      } else {
        stage.replaceChildren(
          stateBox("empty", "No questions match these filters", "Try another status, module or kind above.")
        );
      }
      return;
    }
    runSession(stage, data.questions, filters);
  }

  async function renderQuiz(view, params) {
    const report = await api("/api/report");
    document.getElementById("target").textContent = report.target;
    const overall = report.ownership.overall;
    if (report.stats.functions === 0 || overall.total === 0) {
      view.replaceChildren(
        stateBox(
          "empty",
          "No questions yet",
          report.stats.functions === 0
            ? "OwnIt found no Python functions to ask about. Start it on a folder with Python code."
            : "Every call in this code is dynamic or ambiguous, so no question can be answered with certainty.",
          [btn("Back to dashboard", function () { O.show("dashboard"); })]
        )
      );
      return;
    }

    const filters = {
      status: params.status || defaultStatus(overall),
      module: params.module || "",
      kind: params.kind || "",
    };
    function reload() { O.show("quiz", filters); }

    const statusChoices = [["all", "All"], ["stale", "Stale (" + overall.stale + ")"], ["new", "New (" + overall.new + ")"], ["missed", "Missed (" + overall.missed + ")"], ["owned", "Owned (" + overall.owned + ")"]];
    const kindChoices = [["", "Any kind"], ["callee", KIND_NAMES.callee], ["impact", KIND_NAMES.impact], ["chain", KIND_NAMES.chain]];
    const moduleChoices = [["", "All modules"]].concat(Object.keys(report.ownership.by_module).map(function (m) { return [m, m]; }));

    const stage = h("div", { class: "quiz-stage" });
    view.replaceChildren(
      h("h2", { class: "page-title", text: "Quiz" }),
      h(
        "div",
        { class: "filters" },
        select("Show", "status", statusChoices, filters.status, function (v) { filters.status = v; reload(); }),
        select("Kind", "kind", kindChoices, filters.kind, function (v) { filters.kind = v; reload(); }),
        select("Module", "module", moduleChoices, filters.module, function (v) { filters.module = v; reload(); })
      ),
      stage
    );
    await loadStage(stage, filters, overall);
  }

  O.register("quiz", renderQuiz);
})();
