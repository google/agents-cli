/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Lifecycle page interactive visualizations.
 *
 * Vanilla JS, no dependencies. Each animation is self-contained, mounts
 * lazily on scroll-into-view, and inherits theme colors from the CSS vars
 * mkdocs-material exposes (--md-primary-fg-color, --md-default-fg-color,
 * --md-code-bg-color, etc.) so light/dark works automatically.
 *
 *   #lifecycle-anim-transcript   Auto-playing 4-turn outage transcript
 *   #lifecycle-anim-loop         Four CLI verbs in a continuous loop
 *   #lifecycle-anim-scaffold     Interactive scaffold wizard
 *   #lifecycle-anim-team         Orchestrator + 3 sub-agents with packet
 *   #lifecycle-anim-eval         Eval-fix loop + animated score chart
 *   #lifecycle-anim-trace        Multi-agent trace waterfall draw-in
 *
 * MkDocs Material's `navigation.instant` swaps page content via JS; this
 * script re-runs on every navigation by listening for `document$` if it's
 * around (a Material-internal RxJS subject), and falls back to DOMContentLoaded.
 */
(function () {
  "use strict";

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function init() {
    initTranscript(document.getElementById("lifecycle-anim-transcript"));
    initLoop(document.getElementById("lifecycle-anim-loop"));
    initScaffold(document.getElementById("lifecycle-anim-scaffold"));
    initModelCompare(document.getElementById("lifecycle-anim-models"));
    initPlayground(document.getElementById("lifecycle-anim-playground"));
    initTeam(document.getElementById("lifecycle-anim-team"));
    initEvalLoop(document.getElementById("lifecycle-anim-eval"));
    initFailureMuseum(document.getElementById("lifecycle-anim-failures"));
    initDeploy(document.getElementById("lifecycle-anim-deploy"));
    initPublish(document.getElementById("lifecycle-anim-publish"));
    initTrace(document.getElementById("lifecycle-anim-trace"));
    initRolling(document.getElementById("lifecycle-anim-rolling"));
  }

  // Material's instant navigation exposes a document$ observable. If present,
  // re-run on every nav; otherwise use the standard DOMContentLoaded.
  if (window.document$) {
    window.document$.subscribe(init);
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }

  // ── Lazy: only fire when the element scrolls into view ──────────────────
  function whenInView(el, fn) {
    if (!el) return;
    if (REDUCED) {
      fn();
      return;
    }
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            obs.disconnect();
            fn();
          }
        });
      },
      { rootMargin: "0px 0px -10% 0px" }
    );
    obs.observe(el);
  }

  // Helper: build an SVG element from a string template.
  function svgEl(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  // Helper: build an HTML element from a tag + attrs object.
  function htmlEl(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs)
      for (const k in attrs) {
        if (k === "style" && typeof attrs[k] === "object") {
          Object.assign(el.style, attrs[k]);
        } else if (k === "class") {
          el.className = attrs[k];
        } else if (k === "html") {
          el.innerHTML = attrs[k];
        } else {
          el.setAttribute(k, attrs[k]);
        }
      }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach((c) => {
        if (c == null) return;
        el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return el;
  }

  // ─────────────────────────────────────────── 1. TRANSCRIPT ──────────────

  const TRANSCRIPT_TURNS = [
    { kind: "user", text: "payments error rate just crossed 1%. Triage." },
    {
      kind: "tool",
      tool: "query_logs",
      args: 'service="payments", severity="ERROR"',
      result: "9 entries — top: 'connection refused: postgres-primary' (5 in last 2 min)",
    },
    {
      kind: "tool",
      tool: "search_runbook",
      args: 'query="payments connection refused postgres-primary"',
      result: "Runbook §5.1 — patch-window pattern. Sanctioned remediation: db-failover to read-replicas. Page DBA on-call.",
    },
    {
      kind: "agent",
      text:
        "Diagnosis: postgres-primary is refusing connections — matches the §5.1 patch-window pattern. " +
        "Recommended: run `db-failover` to shift writes to a read-replica and page the DBA on-call. " +
        "Will not auto-execute — needs a human confirm.",
    },
  ];

  function initTranscript(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-transcript");
    const head = htmlEl("div", { class: "lc-transcript__head" });
    head.appendChild(htmlEl("span", { class: "lc-transcript__title" }, "$ agents-cli playground · root_agent"));
    const status = htmlEl("span", { class: "lc-transcript__status" }, "auto · idle");
    head.appendChild(status);
    const body = htmlEl("div", { class: "lc-transcript__body" });
    const foot = htmlEl("div", { class: "lc-transcript__foot" });
    const replayBtn = htmlEl("button", { class: "lc-btn lc-btn--ghost", type: "button" }, "↺ Replay");
    replayBtn.style.display = "none";
    replayBtn.addEventListener("click", () => play());
    foot.appendChild(replayBtn);
    foot.appendChild(htmlEl("span", { class: "lc-muted" }, "a recorded session — different scenario in /lifecycle"));
    host.appendChild(head);
    host.appendChild(body);
    host.appendChild(foot);

    function turnNode(t) {
      if (t.kind === "user") {
        const wrap = htmlEl("div", { class: "lc-row lc-row--right" });
        wrap.appendChild(htmlEl("div", { class: "lc-bubble lc-bubble--user" }, t.text));
        return wrap;
      }
      if (t.kind === "tool") {
        const wrap = htmlEl("div", { class: "lc-row" });
        const card = htmlEl("div", { class: "lc-tool" });
        card.appendChild(
          htmlEl("div", { class: "lc-tool__sig" }, [
            htmlEl("span", { class: "lc-tool__name" }, t.tool),
            htmlEl("span", { class: "lc-muted" }, "(" + t.args + ")"),
          ])
        );
        card.appendChild(htmlEl("div", { class: "lc-tool__result" }, "↳ " + t.result));
        wrap.appendChild(card);
        return wrap;
      }
      const wrap = htmlEl("div", { class: "lc-row" });
      wrap.appendChild(htmlEl("div", { class: "lc-bubble lc-bubble--agent" }, t.text));
      return wrap;
    }

    function play() {
      body.innerHTML = "";
      replayBtn.style.display = "none";
      status.textContent = "auto · playing";
      let i = 0;
      function step() {
        if (i >= TRANSCRIPT_TURNS.length) {
          status.textContent = "auto · done";
          replayBtn.style.display = "";
          return;
        }
        const node = turnNode(TRANSCRIPT_TURNS[i]);
        node.classList.add("lc-fade-in");
        body.appendChild(node);
        body.scrollTop = body.scrollHeight;
        i += 1;
        setTimeout(step, i === 1 ? 600 : 1100);
      }
      setTimeout(step, 400);
    }

    whenInView(host, play);
  }

  // ─────────────────────────────────────────── 2. LIFECYCLE LOOP ──────────
  // Four verbs as HTML cards; a CSS-pulsed "active" card cycles through them
  // every ~1.6s. Cleaner and more honest than the previous SVG/orbit version,
  // which had visual collisions between the pulse and the box text.

  const LOOP_VERBS = [
    { label: "scaffold", desc: "spec → 72 files" },
    { label: "eval",     desc: "score before merge" },
    { label: "deploy",   desc: "ship to prod" },
    { label: "observe",  desc: "trace + analytics" },
  ];

  function initLoop(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-loop");

    const grid = htmlEl("div", { class: "lc-loop__grid" });
    const cards = LOOP_VERBS.map((v, i) => {
      const card = htmlEl("div", { class: "lc-loop__card", "aria-label": v.label });
      card.appendChild(htmlEl("div", { class: "lc-loop__step" }, "step " + (i + 1)));
      card.appendChild(htmlEl("div", { class: "lc-loop__verb" }, [
        htmlEl("span", { class: "lc-loop__prompt" }, "$ "),
        v.label,
      ]));
      card.appendChild(htmlEl("div", { class: "lc-loop__desc" }, v.desc));
      grid.appendChild(card);
      return card;
    });
    host.appendChild(grid);

    host.appendChild(htmlEl("div", { class: "lc-loop__foot" }, [
      htmlEl("span", { class: "lc-loop__arrow" }, "↻"),
      htmlEl("span", { class: "lc-muted" }, "scaffold → eval → deploy → observe → repeat"),
    ]));

    // Cycle the active class through cards.
    let active = 0;
    function setActive(i) {
      cards.forEach((c, idx) => c.classList.toggle("lc-loop__card--active", idx === i));
    }
    setActive(0);

    let timer = null;
    function tick() {
      active = (active + 1) % cards.length;
      setActive(active);
    }
    function start() {
      if (timer) return;
      timer = setInterval(tick, 1600);
    }
    function restart() {
      if (timer) clearInterval(timer);
      timer = setInterval(tick, 1600);
    }

    // Click any card to jump to it (and restart the rotation from there).
    cards.forEach((card, idx) => {
      card.style.cursor = "pointer";
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.addEventListener("click", () => {
        active = idx;
        setActive(active);
        restart();
      });
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          active = idx;
          setActive(active);
          restart();
        }
      });
    });

    whenInView(host, start);
  }

  // ─────────────────────────────────────────── 5b. FAILURE MUSEUM ─────────
  // A small gallery of common agent failures + the eval rubric that catches
  // each. Cards reveal on scroll-into-view with a subtle wobble.

  const FAILURES = [
    {
      name: "Hallucinated tool result",
      what: "Agent claims it called a tool it never invoked, then quotes a fabricated result.",
      caught: "tool_trajectory_avg_score — compares actual tool calls to the expected trajectory",
    },
    {
      name: "Ungrounded answer",
      what: "Agent's final response invents facts that aren't supported by what its tools returned.",
      caught: "hallucinations_v1 — checks each claim in the response against tool-returned context",
    },
    {
      name: "Prompt-injection bypass",
      what: "User message smuggles instructions to override the safety rule.",
      caught: "safety_v1 — pair with Model Armor on the prompt path for runtime defense",
    },
    {
      name: "Persona drift",
      what: "Agent starts answering off-topic — chit-chat, generic advice — instead of investigating.",
      caught: "rubric_based_final_response_quality_v1 — the 'relevance' rubric flags off-topic responses",
    },
  ];

  function initFailureMuseum(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-failures");

    host.appendChild(htmlEl("div", { class: "lc-failures__head" },
      htmlEl("span", { class: "lc-mono lc-muted" }, "common failure modes · and the rubric that catches each")));

    const grid = htmlEl("div", { class: "lc-failures__grid" });
    FAILURES.forEach((f, i) => {
      const card = htmlEl("div", { class: "lc-failures__card", "data-i": i });
      card.style.opacity = "0";
      card.style.transform = "translateY(8px)";
      card.style.transition = "opacity 300ms ease-out, transform 300ms ease-out";
      card.appendChild(htmlEl("p", { class: "lc-failures__eyebrow" }, "failure mode"));
      card.appendChild(htmlEl("h4", { class: "lc-failures__name" }, f.name));
      card.appendChild(htmlEl("p", { class: "lc-failures__what" }, f.what));
      card.appendChild(htmlEl("p", { class: "lc-failures__caught" }, [
        htmlEl("span", { class: "lc-failures__caughtLabel" }, "caught by: "),
        f.caught,
      ]));
      grid.appendChild(card);
    });
    host.appendChild(grid);

    function reveal() {
      grid.querySelectorAll(".lc-failures__card").forEach((card, i) => {
        setTimeout(() => {
          card.style.opacity = "1";
          card.style.transform = "translateY(0)";
        }, 90 * i);
      });
    }
    whenInView(host, reveal);
  }

  // ─────────────────────────────────────────── 3. SCAFFOLD WIZARD ─────────

  const SCAFFOLD_STEPS = [
    {
      key: "target",
      q: "Where will this run in production?",
      help: "Managed runtime is the fastest path; pick Cloud Run or GKE for more control.",
      options: [
        { v: "agent_runtime", l: "Agent Runtime" },
        { v: "cloud_run",     l: "Cloud Run" },
        { v: "gke",           l: "GKE" },
      ],
      default: "agent_runtime",
    },
    {
      key: "datastore",
      q: "Does it ground answers in your data?",
      help: "Vector Search for embeddings; Search for keyword + facets; None for no RAG.",
      options: [
        { v: "agent_platform_vector_search", l: "Vector Search" },
        { v: "agent_platform_search",         l: "Search" },
        { v: "none",                           l: "None" },
      ],
      default: "agent_platform_vector_search",
    },
    {
      key: "session",
      q: "Where do conversations live?",
      help: "In-memory loses state on restart; managed scales without DB ops.",
      options: [
        { v: "in_memory",                  l: "In-memory" },
        { v: "cloud_sql",                  l: "Cloud SQL" },
        { v: "agent_platform_sessions",    l: "Managed" },
      ],
      default: "in_memory",
    },
    {
      key: "cicd",
      q: "Who runs the deploys?",
      help: "GitHub Actions if you live on GitHub; Cloud Build to keep it inside GCP.",
      options: [
        { v: "github_actions",      l: "GitHub Actions" },
        { v: "google_cloud_build",  l: "Cloud Build" },
        { v: "skip",                l: "Skip" },
      ],
      default: "github_actions",
    },
    {
      key: "bq",
      q: "Log every prompt + response to BigQuery?",
      help: "Optional — but you'll want this in production for analytics and replay.",
      options: [
        { v: "yes", l: "Yes" },
        { v: "no",  l: "No" },
      ],
      default: "yes",
    },
  ];

  function initScaffold(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-wizard");

    const state = {};
    SCAFFOLD_STEPS.forEach((s) => (state[s.key] = s.default));

    // Header
    host.appendChild(htmlEl("div", { class: "lc-wizard__head" }, [
      htmlEl("strong", null, "scaffold wizard"),
      htmlEl("span", { class: "lc-muted" }, "every choice updates the command"),
    ]));

    // Steps
    SCAFFOLD_STEPS.forEach((s, i) => {
      const step = htmlEl("div", { class: "lc-wizard__step" });
      step.appendChild(htmlEl("span", { class: "lc-wizard__num" }, String(i + 1)));
      const main = htmlEl("div", {});
      main.appendChild(htmlEl("p", { class: "lc-wizard__q" }, s.q));
      main.appendChild(htmlEl("p", { class: "lc-wizard__help" }, s.help));
      const toggle = htmlEl("div", { class: "lc-toggle" });
      s.options.forEach((o) => {
        const btn = htmlEl("button", { type: "button", "data-v": o.v, class: "lc-toggle__btn" }, o.l);
        if (o.v === state[s.key]) btn.classList.add("lc-toggle__btn--on");
        btn.addEventListener("click", () => {
          state[s.key] = o.v;
          toggle.querySelectorAll(".lc-toggle__btn").forEach((b) => b.classList.remove("lc-toggle__btn--on"));
          btn.classList.add("lc-toggle__btn--on");
          render();
        });
        toggle.appendChild(btn);
      });
      main.appendChild(toggle);
      step.appendChild(main);
      host.appendChild(step);
    });

    // Output: the live command
    const cmdBox = htmlEl("pre", { class: "lc-wizard__cmd" });
    const cmdCode = htmlEl("code", null, "");
    cmdBox.appendChild(cmdCode);
    host.appendChild(cmdBox);

    // Output: file count
    const summary = htmlEl("div", { class: "lc-wizard__summary" });
    host.appendChild(summary);

    function render() {
      const c = state;
      const lines = [
        "agents-cli scaffold create outage-recovery-bot \\",
        "  --agent agentic_rag \\",
      ];
      if (c.datastore !== "none") lines.push("  --datastore " + c.datastore + " \\");
      lines.push("  --deployment-target " + c.target + " \\");
      if (c.session !== "in_memory") lines.push("  --session-type " + c.session + " \\");
      if (c.cicd !== "skip") lines.push("  --cicd-runner " + c.cicd + " \\");
      lines.push(c.bq === "yes" ? "  --bq-analytics" : "  --auto-approve");
      cmdCode.textContent = lines.join("\n");

      // Crude file-count estimate using the deltas we know about.
      let fileCount = 72;
      if (c.datastore === "none") fileCount -= 14;     // strip data_ingestion + vector_search.tf
      if (c.cicd === "skip")      fileCount -= 18;     // strip .github/* + cicd terraform
      if (c.target !== "agent_runtime") fileCount -= 1; // strip agent_runtime_app.py
      if (c.bq === "no")          fileCount -= 2;
      const pieces = [
        c.datastore !== "none" && "RAG pipeline",
        c.cicd !== "skip" && (c.cicd === "github_actions" ? "GitHub Actions CI/CD" : "Cloud Build CI/CD"),
        c.bq === "yes" && "BQ Analytics",
        c.target === "agent_runtime" ? "Agent Runtime entrypoint" : c.target === "cloud_run" ? "Cloud Run service" : "GKE manifests",
      ].filter(Boolean);
      summary.innerHTML = "<strong>" + fileCount + " files</strong> · " + pieces.join(" · ");
    }
    render();
  }

  // ─────────────────────────────────────────── 4. TEAM DIAGRAM ────────────

  const TEAM_SUBS = [
    { id: "investigator", x: 20,  label: "investigator", tools: "query_logs · check_metrics" },
    { id: "diagnoser",    x: 215, label: "diagnoser",    tools: "search_runbook" },
    { id: "remediator",   x: 410, label: "remediator",   tools: "propose_remediation" },
  ];
  const TEAM_W = 600;
  const TEAM_H = 320;
  const ORCH_BOTTOM = { x: 300, y: 90 };
  const SUB_TOP_Y = 200;
  const J_Y = 150;

  const TEAM_SEQ = [
    { packet: [{ x: 300, y: 90 }],                                          dur: 250 },
    { packet: [{ x: 300, y: 90 }, { x: 300, y: J_Y }, { x: 105, y: J_Y }, { x: 105, y: SUB_TOP_Y }], dur: 750, hl: "investigator" },
    { packet: [{ x: 105, y: SUB_TOP_Y }],                                    dur: 350, hl: "investigator" },
    { packet: [{ x: 105, y: SUB_TOP_Y }, { x: 105, y: J_Y }, { x: 300, y: J_Y }, { x: 300, y: 90 }], dur: 750, hl: "investigator" },
    { packet: [{ x: 300, y: 90 }, { x: 300, y: SUB_TOP_Y }],                 dur: 500, hl: "diagnoser" },
    { packet: [{ x: 300, y: SUB_TOP_Y }],                                    dur: 350, hl: "diagnoser" },
    { packet: [{ x: 300, y: SUB_TOP_Y }, { x: 300, y: 90 }],                 dur: 500, hl: "diagnoser" },
    { packet: [{ x: 300, y: 90 }, { x: 300, y: J_Y }, { x: 495, y: J_Y }, { x: 495, y: SUB_TOP_Y }], dur: 750, hl: "remediator" },
    { packet: [{ x: 495, y: SUB_TOP_Y }],                                    dur: 350, hl: "remediator" },
    { packet: [{ x: 495, y: SUB_TOP_Y }, { x: 495, y: J_Y }, { x: 300, y: J_Y }, { x: 300, y: 90 }], dur: 750, hl: "remediator" },
  ];

  function initTeam(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-team");

    const head = htmlEl("div", { class: "lc-team__head" });
    head.appendChild(htmlEl("span", { class: "lc-muted" }, "orchestrator routes work to specialists, then synthesizes the answer"));
    const replayBtn = htmlEl("button", { class: "lc-btn lc-btn--ghost", type: "button" }, "▶ replay one investigation");
    head.appendChild(replayBtn);
    host.appendChild(head);

    const svg = svgEl("svg", { viewBox: `0 0 ${TEAM_W} ${TEAM_H}`, width: "100%", role: "img", "aria-label": "Team diagram" });
    host.appendChild(svg);

    // Connectors
    const connectors = {};
    [
      { id: "investigator", d: `M 300 90 V ${J_Y} H 105 V 200` },
      { id: "diagnoser",    d: "M 300 90 V 200" },
      { id: "remediator",   d: `M 300 90 V ${J_Y} H 495 V 200` },
    ].forEach((c) => {
      const path = svgEl("path", {
        d: c.d, fill: "none",
        stroke: "var(--md-default-fg-color--lightest)",
        "stroke-width": "1.4",
        "stroke-linecap": "round",
      });
      svg.appendChild(path);
      connectors[c.id] = path;
    });

    // Orchestrator box
    const orch = svgEl("g", {});
    orch.appendChild(svgEl("rect", { x: 200, y: 20, width: 200, height: 70, rx: 12, fill: "var(--md-default-bg-color)", stroke: "var(--md-primary-fg-color)", "stroke-width": "1.2", "stroke-opacity": "0.45" }));
    addBoxLabels(orch, 200 + 100, 20, 70, "ORCHESTRATOR", "orchestrator", "routes & synthesizes");
    svg.appendChild(orch);

    // Sub-agents
    const subBoxes = {};
    TEAM_SUBS.forEach((s) => {
      const g = svgEl("g", {});
      const rect = svgEl("rect", { x: s.x, y: SUB_TOP_Y, width: 170, height: 100, rx: 12, fill: "var(--md-default-bg-color)", stroke: "var(--md-default-fg-color--light)", "stroke-width": "1.2", "stroke-opacity": "0.4" });
      g.appendChild(rect);
      addBoxLabels(g, s.x + 85, SUB_TOP_Y, 100, "SUB-AGENT", s.label, s.tools);
      svg.appendChild(g);
      subBoxes[s.id] = rect;
    });

    // Packet
    const packet = svgEl("circle", { r: 8, fill: "var(--md-primary-fg-color)", stroke: "var(--md-default-bg-color)", "stroke-width": 2, opacity: 0 });
    svg.appendChild(packet);

    function addBoxLabels(g, cx, top, h, eyebrow, label, sub) {
      const e = svgEl("text", { x: cx, y: top + 22, "text-anchor": "middle", "font-family": "var(--md-code-font-family)", "font-size": "10", "letter-spacing": "1.5", fill: "var(--md-primary-fg-color)", opacity: "0.85" });
      e.textContent = eyebrow;
      g.appendChild(e);
      const l = svgEl("text", { x: cx, y: top + h / 2 + 6, "text-anchor": "middle", "font-family": "var(--md-text-font-family)", "font-size": "14", "font-weight": "600", fill: "var(--md-default-fg-color)" });
      l.textContent = label;
      g.appendChild(l);
      const s = svgEl("text", { x: cx, y: top + h - 12, "text-anchor": "middle", "font-family": "var(--md-code-font-family)", "font-size": "10.5", fill: "var(--md-default-fg-color--light)" });
      s.textContent = sub;
      g.appendChild(s);
    }

    function setHighlight(id) {
      Object.entries(subBoxes).forEach(([k, rect]) => {
        const isOn = k === id;
        rect.setAttribute("stroke", isOn ? "var(--md-primary-fg-color)" : "var(--md-default-fg-color--light)");
        rect.setAttribute("stroke-width", isOn ? "2.5" : "1.2");
        rect.setAttribute("stroke-opacity", isOn ? "1" : "0.4");
      });
      Object.entries(connectors).forEach(([k, path]) => {
        const isOn = k === id;
        path.setAttribute("stroke", isOn ? "var(--md-primary-fg-color)" : "var(--md-default-fg-color--lightest)");
        path.setAttribute("stroke-width", isOn ? "2.2" : "1.4");
      });
    }

    let stepIdx = -1;
    let timer = null;
    let rafId = null;

    function clearTimers() {
      if (timer) clearTimeout(timer);
      if (rafId) cancelAnimationFrame(rafId);
      timer = null;
      rafId = null;
    }

    function tweenPacket(waypoints, durMs, onDone) {
      const start = performance.now();
      function step(now) {
        const t = Math.min(1, (now - start) / durMs);
        // Move along waypoints linearly
        const segs = waypoints.length - 1;
        if (segs === 0) {
          packet.setAttribute("cx", waypoints[0].x);
          packet.setAttribute("cy", waypoints[0].y);
        } else {
          const segT = t * segs;
          const segIdx = Math.min(segs - 1, Math.floor(segT));
          const local = segT - segIdx;
          const a = waypoints[segIdx];
          const b = waypoints[segIdx + 1];
          packet.setAttribute("cx", a.x + (b.x - a.x) * local);
          packet.setAttribute("cy", a.y + (b.y - a.y) * local);
        }
        if (t < 1) {
          rafId = requestAnimationFrame(step);
        } else {
          onDone();
        }
      }
      rafId = requestAnimationFrame(step);
    }

    function runStep(i) {
      if (i >= TEAM_SEQ.length) {
        packet.setAttribute("opacity", "0");
        setHighlight(null);
        replayBtn.disabled = false;
        replayBtn.textContent = "▶ replay one investigation";
        return;
      }
      const seg = TEAM_SEQ[i];
      packet.setAttribute("opacity", "1");
      setHighlight(seg.hl || null);
      tweenPacket(seg.packet, seg.dur, () => {
        timer = setTimeout(() => runStep(i + 1), 50);
      });
    }

    function start() {
      clearTimers();
      replayBtn.disabled = true;
      replayBtn.textContent = "▸ playing…";
      runStep(0);
    }

    replayBtn.addEventListener("click", start);
    whenInView(host, () => setTimeout(start, 200));
  }

  // ─────────────────────────────────────────── 5. EVAL-FIX LOOP ───────────

  const EVAL_CASES_INITIAL = [
    { id: "api_gateway_5xx_spike",                prompt: "api-gateway 500s in us-east1, p99 latency spiking past 2s.", passing: true },
    { id: "ambiguous_pager_alert",                prompt: "Pager fired for 'payments error rate above threshold'.",      passing: true },
    { id: "no_runbook_match_should_escalate",    prompt: "checkout flow stuck on /confirm — symptom is unfamiliar.",   passing: false, reason: "Response invented a 'restart payment-svc' remediation. No runbook section was cited. Violates safety rubric." },
  ];
  const EVAL_HISTORY = [
    { iter: 1, score: 0.42 },
    { iter: 2, score: 0.61 },
    { iter: 3, score: 0.74 },
    { iter: 4, score: 0.83 },
    { iter: 5, score: 0.91 },
  ];

  function initEvalLoop(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-eval");

    let cases = JSON.parse(JSON.stringify(EVAL_CASES_INITIAL));
    let revealed = 1; // chart points visible

    // Header
    host.appendChild(htmlEl("div", { class: "lc-eval__head" }, [
      htmlEl("span", { class: "lc-mono" }, "$ agents-cli eval run --evalset basic.evalset.json"),
      htmlEl("span", { class: "lc-muted" }, "3 cases"),
    ]));

    // Cases
    const list = htmlEl("div", { class: "lc-eval__list" });
    host.appendChild(list);

    function renderCases() {
      list.innerHTML = "";
      cases.forEach((c) => {
        const card = htmlEl("div", { class: "lc-eval__card" });
        const dot = htmlEl("span", { class: "lc-eval__dot lc-eval__dot--" + (c.passing ? "pass" : "fail") });
        const main = htmlEl("div", { class: "lc-eval__main" });
        main.appendChild(htmlEl("div", { class: "lc-eval__meta" }, [
          htmlEl("span", { class: "lc-mono lc-muted" }, "eval_id=" + c.id),
          htmlEl("span", { class: "lc-eval__pill lc-eval__pill--" + (c.passing ? "pass" : "fail") }, c.passing ? "PASS" : "FAIL"),
        ]));
        main.appendChild(htmlEl("p", { class: "lc-eval__prompt" }, '"' + c.prompt + '"'));
        if (!c.passing && c.reason) {
          main.appendChild(htmlEl("div", { class: "lc-eval__judge" }, [
            htmlEl("span", { class: "lc-eval__judgeLabel" }, "rationale ›"),
            c.reason,
          ]));
        }
        if (c.passing && c.id === "no_runbook_match_should_escalate" && fixed) {
          main.appendChild(htmlEl("div", { class: "lc-eval__judge lc-eval__judge--ok" }, [
            htmlEl("span", { class: "lc-eval__judgeLabel lc-eval__judgeLabel--ok" }, "rationale ›"),
            "Response correctly escalated. Runbook citation present. Safety rubric satisfied.",
          ]));
        }
        card.appendChild(dot);
        card.appendChild(main);
        list.appendChild(card);
      });
    }

    let fixed = false;
    renderCases();

    // Footer with action button
    const foot = htmlEl("div", { class: "lc-eval__foot" });
    const actionBtn = htmlEl("button", { type: "button", class: "lc-btn" }, "Apply fix → re-run");
    const note = htmlEl("span", { class: "lc-mono lc-muted" }, "Iteration #1 — 1 case failing, 2 passing");
    foot.appendChild(actionBtn);
    foot.appendChild(note);
    host.appendChild(foot);

    // Score chart
    const chartWrap = htmlEl("div", { class: "lc-eval__chartwrap" });
    host.appendChild(chartWrap);
    const W = 480, H = 160, padL = 28, padR = 28, padT = 18, padB = 24;
    const xMin = 1, xMax = EVAL_HISTORY.length;
    const xScale = (i) => padL + ((i - xMin) / (xMax - xMin)) * (W - padL - padR);
    const yScale = (s) => padT + (1 - s) * (H - padT - padB);
    const fullPath = EVAL_HISTORY.map((p, i) => (i === 0 ? "M" : "L") + " " + xScale(p.iter) + " " + yScale(p.score)).join(" ");
    const yT = yScale(0.8);

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Eval score across fix iterations" });
    chartWrap.appendChild(svg);

    // Grid lines
    [0, 0.25, 0.5, 0.75, 1].forEach((g) => {
      svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: yScale(g), y2: yScale(g), stroke: "var(--md-default-fg-color--lightest)", "stroke-width": "0.6" }));
    });
    // Threshold
    svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: yT, y2: yT, stroke: "var(--md-default-fg-color--light)", "stroke-dasharray": "3 4", "stroke-width": "1" }));
    const thLabel = svgEl("text", { x: W - padR, y: yT - 4, "text-anchor": "end", "font-family": "var(--md-code-font-family)", "font-size": "9", fill: "var(--md-default-fg-color--light)" });
    thLabel.textContent = "threshold 0.80";
    svg.appendChild(thLabel);

    // Iteration tick labels (always all 5)
    EVAL_HISTORY.forEach((p) => {
      const t = svgEl("text", { x: xScale(p.iter), y: H - 6, "text-anchor": "middle", "font-family": "var(--md-code-font-family)", "font-size": "9", fill: "var(--md-default-fg-color--light)" });
      t.textContent = "#" + p.iter;
      svg.appendChild(t);
    });

    // The line — always full extent, revealed via stroke-dashoffset
    const line = svgEl("path", {
      d: fullPath,
      fill: "none",
      stroke: "var(--md-primary-fg-color)",
      "stroke-width": "2.5",
      "stroke-linecap": "round",
      pathLength: "1",
      "stroke-dasharray": "1",
      "stroke-dashoffset": "1",
    });
    line.style.transition = "stroke-dashoffset 380ms ease-out";
    svg.appendChild(line);

    // Dots
    const dots = EVAL_HISTORY.map((p) => {
      const c = svgEl("circle", { cx: xScale(p.iter), cy: yScale(p.score), r: 0, fill: "var(--md-default-bg-color)", stroke: "var(--md-primary-fg-color)", "stroke-width": "2" });
      c.style.transition = "r 280ms ease-out";
      svg.appendChild(c);
      return c;
    });

    // Score label that slides to the latest revealed dot
    const lastInit = EVAL_HISTORY[0];
    const scoreLabel = svgEl("text", { x: xScale(lastInit.iter) + 8, y: yScale(lastInit.score) + 4, "font-family": "var(--md-code-font-family)", "font-size": "11", "font-weight": "600", fill: "var(--md-primary-fg-color)" });
    scoreLabel.textContent = lastInit.score.toFixed(2);
    scoreLabel.style.transition = "transform 380ms ease-out";
    svg.appendChild(scoreLabel);

    function setRevealed(n) {
      revealed = Math.max(1, Math.min(EVAL_HISTORY.length, n));
      const frac = (revealed - 1) / (EVAL_HISTORY.length - 1);
      line.setAttribute("stroke-dashoffset", String(1 - frac));
      dots.forEach((c, i) => {
        const isVisible = i < revealed;
        const isLast = i === revealed - 1;
        c.setAttribute("r", isVisible ? (isLast ? "5.5" : "4.5") : "0");
      });
      const last = EVAL_HISTORY[revealed - 1];
      // Slide the score label
      const targetX = xScale(last.iter) + 8;
      const targetY = yScale(last.score) + 4;
      scoreLabel.setAttribute("x", targetX);
      scoreLabel.setAttribute("y", targetY);
      scoreLabel.textContent = last.score.toFixed(2);
    }
    setRevealed(1);

    let revealTimer = null;
    function clearReveal() {
      if (revealTimer) {
        clearInterval(revealTimer);
        revealTimer = null;
      }
    }
    function animateReveal() {
      clearReveal();
      let i = 1;
      setRevealed(i);
      revealTimer = setInterval(() => {
        i += 1;
        setRevealed(i);
        if (i >= EVAL_HISTORY.length) clearReveal();
      }, 380);
    }

    function applyFix() {
      cases = cases.map((c) =>
        c.id === "no_runbook_match_should_escalate"
          ? Object.assign({}, c, { passing: true, reason: undefined })
          : c
      );
      fixed = true;
      renderCases();
      note.textContent = "Iteration #2 — all rubrics satisfied";
      actionBtn.textContent = "↺ Replay the fix loop";
      actionBtn.classList.add("lc-btn--ghost");
      animateReveal();
    }
    function reset() {
      cases = JSON.parse(JSON.stringify(EVAL_CASES_INITIAL));
      fixed = false;
      renderCases();
      note.textContent = "Iteration #1 — 1 case failing, 2 passing";
      actionBtn.textContent = "Apply fix → re-run";
      actionBtn.classList.remove("lc-btn--ghost");
      setRevealed(1);
    }
    actionBtn.addEventListener("click", () => (fixed ? reset() : applyFix()));
  }

  // ─────────────────────────────────────────── 6. TRACE WATERFALL ─────────

  // Span names follow ADK's OTEL semantic conventions:
  //   invoke_agent <agent.name>            (per trace_agent_invocation)
  //   generate_content <model.name>        (per use_generate_content_span)
  //   execute_tool <tool.name>             (per trace_tool_call)
  const TRACE_SPANS = [
    { id: "01", parent: null, name: "invoke_agent orchestrator", start: 0, dur: 5800, attrs: { "gen_ai.conversation.id": "sess_3f0b…", "gen_ai.agent.name": "orchestrator", "gen_ai.operation.name": "invoke_agent" } },
    { id: "02", parent: "01", name: "generate_content gemini-flash-latest", start: 30, dur: 320, attrs: { "gen_ai.usage.input_tokens": 220, "gen_ai.usage.output_tokens": 38 } },
    { id: "10", parent: "01", name: "invoke_agent investigator", start: 360, dur: 1900, attrs: { "gen_ai.agent.name": "investigator", "gen_ai.operation.name": "invoke_agent" } },
    { id: "11", parent: "10", name: "generate_content gemini-flash-latest", start: 380, dur: 220, attrs: { "gen_ai.usage.input_tokens": 320, "gen_ai.usage.output_tokens": 42 } },
    { id: "12", parent: "10", name: "execute_tool query_logs", start: 610, dur: 720, attrs: { "gen_ai.tool.name": "query_logs", "gcp.vertex.agent.tool_call_args": '{"service":"api-gateway"}' } },
    { id: "13", parent: "10", name: "execute_tool check_metrics", start: 1340, dur: 540, attrs: { "gen_ai.tool.name": "check_metrics", "gcp.vertex.agent.tool_call_args": '{"service":"auth-service"}' } },
    { id: "20", parent: "01", name: "invoke_agent diagnoser", start: 2280, dur: 1700, attrs: { "gen_ai.agent.name": "diagnoser", "gen_ai.operation.name": "invoke_agent" } },
    { id: "21", parent: "20", name: "generate_content gemini-flash-latest", start: 2300, dur: 280, attrs: { "gen_ai.usage.input_tokens": 412, "gen_ai.usage.output_tokens": 64 } },
    { id: "22", parent: "20", name: "execute_tool search_runbook", start: 2590, dur: 1260, attrs: { "gen_ai.tool.name": "search_runbook", "gcp.vertex.agent.tool_call_args": '{"query":"api-gateway 5xx auth-service latency"}' } },
    { id: "30", parent: "01", name: "invoke_agent remediator", start: 4000, dur: 1280, attrs: { "gen_ai.agent.name": "remediator", "gen_ai.operation.name": "invoke_agent" } },
    { id: "31", parent: "30", name: "generate_content gemini-flash-latest", start: 4020, dur: 290, attrs: { "gen_ai.usage.input_tokens": 380, "gen_ai.usage.output_tokens": 58 } },
    { id: "32", parent: "30", name: "execute_tool propose_remediation", start: 4320, dur: 580, attrs: { "gen_ai.tool.name": "propose_remediation", "gcp.vertex.agent.tool_call_args": '{"action":"traffic-shift"}' } },
    { id: "40", parent: "01", name: "generate_content gemini-flash-latest", start: 5300, dur: 480, attrs: { "gen_ai.usage.input_tokens": 680, "gen_ai.usage.output_tokens": 92 } },
  ];

  function initTrace(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-trace");

    // Compute depth from parent chain
    const byId = {};
    TRACE_SPANS.forEach((s) => (byId[s.id] = s));
    function depthOf(id, memo) {
      if (!memo) memo = {};
      if (memo[id] != null) return memo[id];
      const s = byId[id];
      if (!s || s.parent == null) memo[id] = 0;
      else memo[id] = depthOf(s.parent, memo) + 1;
      return memo[id];
    }
    const memo = {};
    const depths = {};
    TRACE_SPANS.forEach((s) => (depths[s.id] = depthOf(s.id, memo)));
    const maxDepth = Math.max.apply(null, Object.values(depths));
    const totalMs = Math.max.apply(null, TRACE_SPANS.map((s) => s.start + s.dur));

    const ROW_H = 26;
    const PAD_TOP = 14;
    const PAD_RIGHT = 16;
    const INDENT = 14;
    const padLeft = 200 + maxDepth * INDENT;
    const W = 720;
    const H = PAD_TOP * 2 + ROW_H * TRACE_SPANS.length + 24;

    host.appendChild(htmlEl("div", { class: "lc-trace__head" }, [
      htmlEl("span", { class: "lc-mono" }, "trace · session sess_3f0b… · orchestrator + 3 sub-agents"),
      htmlEl("span", { class: "lc-mono lc-muted" }, totalMs.toLocaleString() + " ms total"),
    ]));

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Multi-agent trace waterfall" });
    host.appendChild(svg);

    // Axis ticks
    const tickCount = 5;
    for (let i = 0; i <= tickCount; i++) {
      const ms = (totalMs / tickCount) * i;
      const x = padLeft + (ms / totalMs) * (W - padLeft - PAD_RIGHT);
      svg.appendChild(svgEl("line", { x1: x, x2: x, y1: PAD_TOP, y2: H - 4, stroke: "var(--md-default-fg-color--lightest)", "stroke-width": "0.6" }));
      const t = svgEl("text", { x, y: H - 6, "text-anchor": "middle", "font-family": "var(--md-code-font-family)", "font-size": "9", fill: "var(--md-default-fg-color--light)" });
      t.textContent = Math.round(ms) + " ms";
      svg.appendChild(t);
    }

    function spanColor(name) {
      if (name.indexOf("execute_tool") === 0) return "var(--md-primary-fg-color)";
      if (name.indexOf("generate_content") === 0) return "#fbbc04";
      if (name.indexOf("invoke_agent") === 0) return "var(--md-primary-fg-color--dark)";
      return "var(--md-default-fg-color--light)";
    }

    // Hover detail strip below the SVG.
    const detail = htmlEl("div", { class: "lc-trace__detail" });
    detail.innerHTML = "<span class='lc-muted'>hover any span to see attributes</span>";

    const bars = [];
    TRACE_SPANS.forEach((s, i) => {
      const x = padLeft + (s.start / totalMs) * (W - padLeft - PAD_RIGHT);
      const w = Math.max(2, (s.dur / totalMs) * (W - padLeft - PAD_RIGHT));
      const y = PAD_TOP + i * ROW_H;
      const d = depths[s.id];

      // Wrap each row in a <g> so hover targets the whole row (label + bar).
      const row = svgEl("g", { class: "lc-trace__row", "data-id": s.id });
      // Invisible hit area for hover — full row width.
      const hit = svgEl("rect", { x: 0, y, width: W, height: ROW_H, fill: "transparent", style: "pointer-events: all;" });
      row.appendChild(hit);

      // Indented, left-aligned label so the parent → child hierarchy reads
      // as a visual tree.
      const indentPx = 6 + d * INDENT;
      const lbl = svgEl("text", { x: indentPx, y: y + 16, "text-anchor": "start", "font-family": "var(--md-code-font-family)", "font-size": "11", fill: d === 0 ? "var(--md-default-fg-color)" : "var(--md-default-fg-color--light)", "font-weight": d === 0 ? "600" : "400" });
      const maxChars = 32 - d * 2;
      lbl.textContent = s.name.length > maxChars ? s.name.slice(0, maxChars - 1) + "…" : s.name;
      row.appendChild(lbl);

      const bar = svgEl("rect", { x, y: y + 5, width: 0, height: ROW_H - 10, rx: 3, fill: spanColor(s.name), opacity: 0.85 });
      bar.style.transition = "width 350ms ease-out, opacity 150ms ease";
      row.appendChild(bar);
      const dur = svgEl("text", { x: x + w + 6, y: y + 16, "font-family": "var(--md-code-font-family)", "font-size": "10", fill: "var(--md-default-fg-color--light)", opacity: 0 });
      dur.textContent = s.dur + "ms";
      dur.style.transition = "opacity 280ms ease-out";
      row.appendChild(dur);

      // Hover handlers
      row.addEventListener("mouseenter", () => {
        bar.setAttribute("opacity", 1);
        const attrs = Object.entries(s.attrs || {})
          .map(([k, v]) => `${k}=${v}`)
          .join(" · ");
        detail.innerHTML = "";
        const left = htmlEl("span", { class: "lc-mono" }, [
          htmlEl("strong", { style: "color: var(--md-primary-fg-color);" }, s.name),
          " · ",
          s.dur + "ms",
          attrs ? " · " : "",
          htmlEl("span", { class: "lc-muted" }, attrs),
        ]);
        detail.appendChild(left);
      });
      row.addEventListener("mouseleave", () => {
        bar.setAttribute("opacity", 0.85);
        detail.innerHTML = "<span class='lc-muted'>hover any span to see attributes</span>";
      });

      svg.appendChild(row);
      bars.push({ bar, dur, finalW: w });
    });

    host.appendChild(detail);

    function play() {
      bars.forEach((b, i) => {
        setTimeout(() => {
          b.bar.setAttribute("width", b.finalW);
          setTimeout(() => b.dur.setAttribute("opacity", 1), 280);
        }, i * 60);
      });
    }
    whenInView(host, play);
  }

  // ─────────────────────────────────────────── 7. MODEL COMPARE ───────────
  // Three side-by-side cards showing the same prompt + a different model
  // identifier per provider. Mock responses are clearly labeled illustrative.

  const MODELS = [
    {
      provider: "Google",
      badge:    "Gemini 3.1 Pro",
      swap:     'Gemini(model="gemini-3.1-pro")',
      accent:   "#4285f4",
      response: "Diagnosis: api-gateway 5xx is downstream of auth-service degradation. Per runbook §3.2, the sanctioned remediation is `traffic-shift` away from the auth-service canary — NOT a restart. Recommend executing now and paging the auth-service on-call.",
    },
    {
      provider: "Anthropic",
      badge:    "Claude Sonnet 4.6",
      swap:     'LiteLlm(model="anthropic/claude-sonnet-4-6")',
      accent:   "#cc7d54",
      response: "Looking at the logs and metrics for api-gateway: the 5xx errors correlate with elevated p99 latency on auth-service. Per runbook §3.2, for this exact symptom the documented remediation is `traffic-shift` away from the auth-service canary; restarts are explicitly NOT sanctioned. I'd run the shift and page auth-service on-call.",
    },
    {
      provider: "OpenAI",
      badge:    "GPT-5",
      swap:     'LiteLlm(model="openai/gpt-5")',
      accent:   "#10a37f",
      response: "Root cause: auth-service degradation. 8 upstream timeouts in the last 4 minutes; auth-service p99 at 920 ms vs. ~110 ms baseline. Runbook §3.2 covers this pattern — remediation is a `traffic-shift` off the canary, not a restart. Suggest executing the shift and notifying auth-service on-call.",
    },
  ];

  function initModelCompare(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-models");

    host.appendChild(htmlEl("div", { class: "lc-models__head" }, [
      htmlEl("strong", null, "Same prompt, three models"),
      htmlEl("span", { class: "lc-models__pill" }, "illustrative · responses are sample text"),
    ]));
    host.appendChild(htmlEl("pre", { class: "lc-models__prompt" },
      htmlEl("code", null, "user > api-gateway is returning 500s in us-east1, p99 latency spiking past 2s. What's going on?")));

    const grid = htmlEl("div", { class: "lc-models__grid" });
    MODELS.forEach((m) => {
      const card = htmlEl("div", { class: "lc-models__card" });
      card.style.borderColor = "color-mix(in srgb, " + m.accent + " 28%, var(--md-default-fg-color--lightest))";
      card.appendChild(htmlEl("p", { class: "lc-models__provider" }, m.provider));
      const badge = htmlEl("p", { class: "lc-models__badge" }, m.badge);
      badge.style.color = m.accent;
      card.appendChild(badge);
      card.appendChild(htmlEl("p", { class: "lc-models__response" }, m.response));
      // span (not code) — sidesteps mkdocs-material's .md-typeset code rules
      // which override our padding/font/background by specificity.
      const swap = htmlEl("span", { class: "lc-models__swap", title: m.swap }, m.swap);
      card.appendChild(swap);
      const passRow = htmlEl("div", { class: "lc-models__pass" }, [
        htmlEl("span", { class: "lc-models__dot" }),
        htmlEl("span", { class: "lc-mono" }, "eval · 0.91 · all rubrics pass"),
      ]);
      card.appendChild(passRow);
      grid.appendChild(card);
    });
    host.appendChild(grid);

    host.appendChild(htmlEl("p", { class: "lc-models__foot lc-mono lc-muted" },
      "Swap one line in agent.py — eval, deploy, observability all behave the same regardless of provider."));
  }

  // ─────────────────────────────────────────── 8. INLINE PLAYGROUND ───────
  // A second, click-to-step-through scenario for the Build phase. Uses a
  // different incident from the auto-playing transcript at the top so the
  // reader sees two distinct investigations.

  const PLAYGROUND_TURNS = [
    { kind: "user", text: "ALERT: payments service error_rate_5xx > 1% sustained for 5 min. Triage." },
    { kind: "tool", tool: "query_logs", args: 'service="payments", severity="ERROR"',
      result: "9 entries — top: 'connection refused: postgres-primary' (5 in last 2 min)" },
    { kind: "tool", tool: "check_metrics", args: 'service="postgres-primary", metric="connections_active"',
      result: "active=0 (baseline ~140), refused_connections=12 in last 2 min — primary is down or unreachable" },
    { kind: "tool", tool: "search_runbook", args: 'query="payments postgres-primary refused"',
      result: "Runbook §5.1 — patch-window pattern. Sanctioned remediation: db-failover to read-replicas. Page DBA on-call." },
    { kind: "agent", text:
      "Diagnosis: postgres-primary is refusing connections — matches the §5.1 patch-window pattern. " +
      "Recommended: run `db-failover` to shift writes to a read-replica and page the DBA on-call. " +
      "Will not auto-execute — needs a human confirm." },
  ];

  function initPlayground(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-transcript");
    const head = htmlEl("div", { class: "lc-transcript__head" });
    head.appendChild(htmlEl("span", { class: "lc-transcript__title" }, "$ agents-cli playground · root_agent"));
    const counter = htmlEl("span", { class: "lc-mono lc-muted" }, "0/" + PLAYGROUND_TURNS.length);
    head.appendChild(counter);
    const body = htmlEl("div", { class: "lc-transcript__body" });
    const foot = htmlEl("div", { class: "lc-transcript__foot" });
    const advance = htmlEl("button", { class: "lc-btn", type: "button" }, "▶ Send the first prompt");
    foot.appendChild(advance);
    foot.appendChild(htmlEl("span", { class: "lc-muted" }, "click to step through · payments triage · 5 turns"));
    host.appendChild(head);
    host.appendChild(body);
    host.appendChild(foot);

    function renderTurn(t) {
      if (t.kind === "user") {
        const w = htmlEl("div", { class: "lc-row lc-row--right" });
        w.appendChild(htmlEl("div", { class: "lc-bubble lc-bubble--user" }, t.text));
        return w;
      }
      if (t.kind === "tool") {
        const w = htmlEl("div", { class: "lc-row" });
        const c = htmlEl("div", { class: "lc-tool" });
        c.appendChild(htmlEl("div", { class: "lc-tool__sig" }, [
          htmlEl("span", { class: "lc-tool__name" }, t.tool),
          htmlEl("span", { class: "lc-muted" }, "(" + t.args + ")"),
        ]));
        c.appendChild(htmlEl("div", { class: "lc-tool__result" }, "↳ " + t.result));
        w.appendChild(c);
        return w;
      }
      const w = htmlEl("div", { class: "lc-row" });
      w.appendChild(htmlEl("div", { class: "lc-bubble lc-bubble--agent" }, t.text));
      return w;
    }

    let step = 0;
    function next() {
      if (step >= PLAYGROUND_TURNS.length) {
        // reset
        step = 0;
        body.innerHTML = "";
        counter.textContent = "0/" + PLAYGROUND_TURNS.length;
        advance.textContent = "▶ Send the first prompt";
        return;
      }
      const node = renderTurn(PLAYGROUND_TURNS[step]);
      node.classList.add("lc-fade-in");
      body.appendChild(node);
      body.scrollTop = body.scrollHeight;
      step += 1;
      counter.textContent = step + "/" + PLAYGROUND_TURNS.length;
      advance.textContent = step >= PLAYGROUND_TURNS.length ? "↺ Replay" : "→ Next turn";
    }
    advance.addEventListener("click", next);
  }

  // ─────────────────────────────────────────── 9. DEPLOY TARGETS ──────────

  const DEPLOY_TARGETS = [
    {
      id:    "agent_runtime",
      label: "Agent Runtime",
      blurb: "Fastest path to prod. Sessions, eval, traces built-in.",
      cmd:   "agents-cli deploy --dry-run\n  Would deploy to Agent Runtime: project=outage-recovery-prod, region=us-east1",
      pipeline: [
        { tool: "uv build",                what: "package the agent into a Python wheel" },
        { tool: "vertexai.agent_engines",  what: "upload the wheel to a staging bucket" },
        { tool: "Agent Runtime",           what: "create a managed runtime instance" },
        { tool: "telemetry",               what: "wire Cloud Trace + sessions automatically" },
      ],
    },
    {
      id:    "cloud_run",
      label: "Cloud Run",
      blurb: "When you need a custom HTTP surface around the agent.",
      cmd: "agents-cli deploy --dry-run\n  Would run: gcloud beta run deploy outage-recovery-bot --project outage-recovery-prod --region us-east1 --source . --memory 4Gi --no-allow-unauthenticated --no-cpu-throttling --port 8080 --labels created-by=adk",
      pipeline: [
        { tool: "Cloud Build", what: "buildpack-build the container" },
        { tool: "Artifact Registry", what: "push the image" },
        { tool: "gcloud beta run deploy", what: "create the service · 4Gi · port 8080" },
        { tool: "IAP / secrets", what: "wire identity, env vars, and scaling" },
      ],
    },
    {
      id:    "gke",
      label: "GKE",
      blurb: "When you need cluster-level isolation, sidecars, or VPC-bound services.",
      cmd: "agents-cli deploy --dry-run\n  Would run: terraform -chdir=deployment/terraform/single-project apply -auto-approve\n  Would run: gcloud builds submit --tag ...\n  Would run: gcloud container clusters get-credentials ...\n  Would run: kubectl set image ... us-east1-docker.pkg.dev/outage-recovery-prod/outage-recovery-bot/outage-recovery-bot:latest\n  Would run: kubectl rollout status ...",
      pipeline: [
        { tool: "terraform apply",  what: "cluster + workload identity + namespace" },
        { tool: "docker build",     what: "build the agent container locally" },
        { tool: "docker push",      what: "push to Artifact Registry" },
        { tool: "kubectl apply",    what: "deployment + service + HPA manifests" },
        { tool: "rollout wait",     what: "block until the new revision is healthy" },
      ],
    },
  ];

  function initDeploy(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-deploy");

    let active = 0;

    // Three target picker cards
    const grid = htmlEl("div", { class: "lc-deploy__grid" });
    const cards = DEPLOY_TARGETS.map((t, i) => {
      const card = htmlEl("button", { type: "button", class: "lc-deploy__card", "data-i": i });
      card.appendChild(htmlEl("div", { class: "lc-deploy__flag lc-mono" }, "--deployment-target " + t.id));
      card.appendChild(htmlEl("div", { class: "lc-deploy__label" }, t.label));
      card.appendChild(htmlEl("div", { class: "lc-deploy__blurb" }, t.blurb));
      card.addEventListener("click", () => { active = i; render(); });
      grid.appendChild(card);
      return card;
    });
    host.appendChild(grid);

    // Dry-run output
    const cmdBlock = htmlEl("pre", { class: "lc-deploy__cmd" });
    const cmdCode = htmlEl("code");
    cmdBlock.appendChild(cmdCode);
    host.appendChild(cmdBlock);

    // Per-target pipeline
    const pipelineWrap = htmlEl("div", { class: "lc-deploy__pipeline" });
    const pipelineHead = htmlEl("p", { class: "lc-mono lc-muted" }, "");
    const pipelineList = htmlEl("ol", { class: "lc-deploy__steps" });
    pipelineWrap.appendChild(pipelineHead);
    pipelineWrap.appendChild(pipelineList);
    host.appendChild(pipelineWrap);

    function render() {
      cards.forEach((c, i) => c.classList.toggle("lc-deploy__card--on", i === active));
      const t = DEPLOY_TARGETS[active];
      cmdCode.textContent = "$ " + t.cmd;
      pipelineHead.textContent = "what `agents-cli deploy` runs · target = " + t.id;
      pipelineList.innerHTML = "";
      t.pipeline.forEach((p, i) => {
        const li = htmlEl("li");
        li.appendChild(htmlEl("span", { class: "lc-deploy__num lc-mono" }, String(i + 1)));
        const detail = htmlEl("div");
        detail.appendChild(htmlEl("code", { class: "lc-deploy__tool" }, p.tool));
        detail.appendChild(htmlEl("span", { class: "lc-deploy__what" }, " — " + p.what));
        li.appendChild(detail);
        pipelineList.appendChild(li);
      });
    }
    render();
  }

  // ─────────────────────────────────────────── 10. PUBLISH CARD ───────────

  function initPublish(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-publish");

    host.appendChild(htmlEl("p", { class: "lc-mono lc-publish__eyebrow" }, "GEMINI ENTERPRISE · agent catalog"));
    host.appendChild(htmlEl("h4", { class: "lc-publish__name" }, "outage-recovery-bot"));
    host.appendChild(htmlEl("p", { class: "lc-publish__sub" },
      "SRE-facing assistant. Investigates incidents using logs, metrics, and runbook RAG. Cites every recommendation."));
    const facts = htmlEl("ul", { class: "lc-publish__facts lc-mono" });
    [
      "mode = ADK",
      "runtime = Agent Runtime",
      "region = us-east1",
      "tools = 3",
    ].forEach((f) => facts.appendChild(htmlEl("li", null, "· " + f)));
    host.appendChild(facts);

    const status = htmlEl("div", { class: "lc-publish__status" });
    status.appendChild(htmlEl("span", { class: "lc-publish__pulse" }));
    status.appendChild(htmlEl("span", { class: "lc-mono lc-publish__statusLabel" }, "READY · receiving traffic"));
    host.appendChild(status);

    const cmd = htmlEl("pre", { class: "lc-publish__cmd" }, htmlEl("code", null,
      "$ agents-cli publish gemini-enterprise --registration-type adk\n✅ Successfully registered agent to Gemini Enterprise!"));
    host.appendChild(cmd);
  }

  // ─────────────────────────────────────────── 11. ROLLING EVAL ───────────

  const ROLLING_DATA = [
    { day: "D-9", score: 0.92 },
    { day: "D-8", score: 0.93 },
    { day: "D-7", score: 0.91 },
    { day: "D-6", score: 0.78, ann: { kind: "regression", label: "regression caught" } },
    { day: "D-5", score: 0.86 },
    { day: "D-4", score: 0.91 },
    { day: "D-3", score: 0.93, ann: { kind: "deploy",     label: "v1.4 deployed" } },
    { day: "D-2", score: 0.94 },
    { day: "D-1", score: 0.93 },
    { day: "Today", score: 0.95 },
  ];

  function initRolling(host) {
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("lc-rolling");

    // Header with live indicator
    const head = htmlEl("div", { class: "lc-rolling__head" });
    head.appendChild(htmlEl("span", { class: "lc-mono lc-muted" }, "ROLLING EVAL SCORE · last 10 days · production"));
    const live = htmlEl("span", { class: "lc-rolling__live" });
    live.appendChild(htmlEl("span", { class: "lc-rolling__livepulse" }));
    live.appendChild(htmlEl("span", { class: "lc-mono", style: "color: #34a853;" }, "LIVE"));
    head.appendChild(live);
    host.appendChild(head);

    // SVG chart
    const W = 560, H = 170;
    const PAD_L = 18, PAD_R = 18, PAD_T = 24, PAD_B = 26;
    const dx = (W - PAD_L - PAD_R) / (ROLLING_DATA.length - 1);
    const yScale = (s) => PAD_T + (1 - s) * (H - PAD_T - PAD_B);

    const pts = ROLLING_DATA.map((p, i) => Object.assign({}, p, { cx: PAD_L + i * dx, cy: yScale(p.score) }));
    const linePath = pts.map((p, i) => (i === 0 ? "M " : "L ") + p.cx + " " + p.cy).join(" ");
    const areaPath = "M " + pts[0].cx + " " + (H - PAD_B) + " " +
                     pts.map((p) => "L " + p.cx + " " + p.cy).join(" ") +
                     " L " + pts[pts.length - 1].cx + " " + (H - PAD_B) + " Z";
    const yT = yScale(0.8);

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", role: "img", "aria-label": "Rolling production eval score, last 10 days" });
    host.appendChild(svg);

    // Threshold band
    svg.appendChild(svgEl("rect", { x: PAD_L, y: yT, width: W - PAD_L - PAD_R, height: H - PAD_B - yT, fill: "color-mix(in srgb, #ea4335 10%, transparent)" }));
    svg.appendChild(svgEl("line", { x1: PAD_L, x2: W - PAD_R, y1: yT, y2: yT, stroke: "var(--md-default-fg-color--light)", "stroke-dasharray": "3 4", "stroke-width": 1 }));
    const thLabel = svgEl("text", { x: W - PAD_R - 2, y: yT - 4, "text-anchor": "end", "font-family": "var(--md-code-font-family)", "font-size": "9", fill: "var(--md-default-fg-color--light)" });
    thLabel.textContent = "threshold 0.80";
    svg.appendChild(thLabel);

    // Faint area beneath the line
    const area = svgEl("path", { d: areaPath, fill: "color-mix(in srgb, #34a853 14%, transparent)", opacity: "0" });
    area.style.transition = "opacity 600ms ease";
    svg.appendChild(area);

    // The line — stroke-dashoffset reveal
    const line = svgEl("path", {
      d: linePath, fill: "none", stroke: "#34a853", "stroke-width": "2", "stroke-linecap": "round",
      pathLength: "1", "stroke-dasharray": "1", "stroke-dashoffset": "1",
    });
    line.style.transition = "stroke-dashoffset 1100ms ease-out";
    svg.appendChild(line);

    // Dots + annotations
    pts.forEach((p, i) => {
      const isLast = i === pts.length - 1;
      const annotated = !!p.ann;
      const annColor = annotated && p.ann.kind === "regression" ? "#ea4335" : "var(--md-primary-fg-color)";

      // Live pulse halo on last point
      if (isLast) {
        const halo = svgEl("circle", { cx: p.cx, cy: p.cy, r: 4, fill: "#34a853", opacity: "0" });
        halo.style.transformOrigin = p.cx + "px " + p.cy + "px";
        halo.style.animation = "lc-rolling-pulse 1.6s ease-out infinite 1.2s";
        svg.appendChild(halo);
      }

      const dot = svgEl("circle", {
        cx: p.cx, cy: p.cy, r: 0,
        fill: annotated ? annColor : "#34a853",
        stroke: "var(--md-default-bg-color)", "stroke-width": "1.5",
      });
      dot.style.transition = "r 280ms ease-out";
      dot.dataset.targetR = String(annotated ? 4 : (isLast ? 4 : 2.8));
      svg.appendChild(dot);

      if (annotated) {
        // Leader line + label above
        const leader = svgEl("line", {
          x1: p.cx, x2: p.cx, y1: p.cy - 6, y2: PAD_T - 2,
          stroke: annColor, "stroke-width": "0.8", "stroke-dasharray": "2 2", opacity: "0",
        });
        leader.style.transition = "opacity 300ms ease";
        leader.dataset.reveal = "1";
        svg.appendChild(leader);
        const annText = svgEl("text", {
          x: p.cx, y: PAD_T - 8, "text-anchor": "middle",
          "font-family": "var(--md-code-font-family)", "font-size": "9",
          fill: annColor, "font-weight": "600", opacity: "0",
        });
        annText.textContent = p.ann.label;
        annText.style.transition = "opacity 300ms ease";
        annText.dataset.reveal = "1";
        svg.appendChild(annText);
      }
    });

    // Day labels
    pts.forEach((p) => {
      const t = svgEl("text", {
        x: p.cx, y: H - 6, "text-anchor": "middle",
        "font-family": "var(--md-code-font-family)", "font-size": "9",
        fill: p.day === "Today" ? "#34a853" : "var(--md-default-fg-color--light)",
        "font-weight": p.day === "Today" ? "600" : "400",
        opacity: "0",
      });
      t.textContent = p.day;
      t.style.transition = "opacity 280ms ease";
      t.dataset.reveal = "1";
      svg.appendChild(t);
    });

    // Footer summary
    const foot = htmlEl("div", { class: "lc-rolling__foot lc-mono" });
    foot.innerHTML = "<span class='lc-muted'>median 0.91 · 1 regression caught · 1 deploy</span>" +
                     " <span style='color: #34a853; float: right;'>now 0.95</span>";
    host.appendChild(foot);

    function play() {
      // Reveal line
      line.setAttribute("stroke-dashoffset", "0");
      area.setAttribute("opacity", "1");
      // Reveal dots
      svg.querySelectorAll("circle").forEach((c, i) => {
        const targetR = c.dataset.targetR;
        if (!targetR) return;
        setTimeout(() => c.setAttribute("r", targetR), 1000 + i * 40);
      });
      // Reveal annotations + day labels
      svg.querySelectorAll("[data-reveal='1']").forEach((el, i) => {
        setTimeout(() => el.setAttribute("opacity", "1"), 1100 + i * 30);
      });
    }
    whenInView(host, play);
  }
})();
