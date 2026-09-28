// BizAgent Workbench UI — static, no build step. All dynamic text uses textContent.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { session: null, streaming: false };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// ---------------------------------------------------------------- SSE over fetch(POST)
async function streamSSE(path, body, onEvent) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.search(/\r?\n\r?\n/)) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx).replace(/^\r?\n\r?\n/, "");
      const data = raw.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
      if (data) onEvent(JSON.parse(data));
    }
  }
}

// ---------------------------------------------------------------- rendering
function chat(kind, text) {
  const node = el("div", { class: `msg ${kind}` }, text);
  $("chat").append(node);
  node.scrollIntoView({ block: "end" });
}

function badge(cls, text) {
  return el("span", { class: `badge ${cls}` }, text);
}

function timeline(e) {
  const t = new Date((e.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  const li = el("li", { class: e.type }, el("span", { class: "t" }, `${t} `), el("b", {}, e.type), " ");
  if (e.type === "tool_call") {
    li.append(e.tool, " ", badge(e.status, e.status), " ", badge(e.decision === "allowed" ? "allowed" : "denied", e.decision));
    if (e.policy && e.policy.rule) li.append(" ", ruleBadge(e.policy));
    if (e.approver) li.append(isPolicyApprover(e.approver) ? " auto-approved by the approval policy" : ` approved by ${e.approver}`);
    // an auto-approved call has no preview by design (D32): a neutral badge, not the red 未预演 one
    if (e.preview_check) li.append(" ", isPolicyApprover(e.approver) ? badge("empty", "no preview") : checkBadge(e.preview_check));
    if (e.preview_check && e.preview_check.actual) li.append(el("span", { class: "muted" }, ` changed: ${e.preview_check.actual}`));
    if (e.policy && e.policy.guard) li.append(" ", badge("mismatch", "guard"), ` ${e.policy.guard}`);
    li.append(el("pre", {}, (e.text || "").slice(0, 400)));
  } else if (e.type === "approval_requested") {
    li.append(e.tool, " ", e.policy && e.policy.rule ? ruleBadge(e.policy) : badge("rule default", "default rule"));
  } else if (e.type === "preview") {
    li.append(e.tool, " ", badge(e.status === "ok" ? "ok" : "unpreviewed", e.status), ` ${e.summary || ""}`);
  } else if (e.type === "preview_mismatch") {
    li.append(e.tool, " ", badge("mismatch", "preview_mismatch"), el("pre", {}, (e.differences || []).join("\n")));
  } else if (e.type === "approval_refused") {
    li.append(e.tool, ` ${e.reason}`);
  } else if (e.type === "node") {
    li.append(e.node, e.tool ? ` -> ${e.tool}` : "", e.steps ? ` (${e.steps.length} plan steps)` : "");
  } else if (e.type === "llm") {
    li.append(`${e.purpose}: ${e.tokens} tokens`);
  } else if (e.type === "final") {
    li.append(e.termination ? `stopped: ${e.termination.reason}` : "completed", ` · steps ${e.steps} · tokens ${e.tokens_used}`);
  } else {
    li.append(e.tool || e.key || e.reason || e.detail || "");
  }
  $("timeline").append(li);
}

function handleEvent(e) {
  timeline(e);
  if (e.type === "tool_call") {
    const rule = e.policy && e.policy.rule ? ` · rule ${e.policy.rule}` : "";
    const auto = isPolicyApprover(e.approver);
    const check = e.preview_check && !auto ? ` · preview check: ${e.preview_check.result}` : "";
    const changed = auto && e.preview_check && e.preview_check.actual ? ` · changed: ${e.preview_check.actual}` : "";
    const how = auto ? " · auto-approved, no preview" : "";
    chat(e.decision === "denied_by_rule" ? "step warn" : "step", `tool ${e.tool} → ${e.status}${rule}${how}${changed}${check}`);
  }
  if (e.type === "node" && e.node === "plan") chat("step", `plan: ${e.steps.map((s) => s.description).join(" → ")}`);
  if (e.type === "preview") chat("step", `preview in a shadow environment: ${e.status} · ${e.summary || ""}`);
  if (e.type === "approval_required" && e.thread_id) showApproval(e);
  if (e.type === "approval_refused") chat("step warn", e.reason);
  if (e.type === "preview_mismatch") chat("step warn", `⚠ preview_mismatch: the real change differs from the preview (${(e.differences || []).join("; ")})`);
  if (e.type === "memory_rejected") chat("step", `memory not stored (${e.source}): ${e.key}`);
  if (e.type === "done") {
    chat("agent", e.final_answer || "(no answer)");
    refreshMemory();
  }
}

// ---------------------------------------------------------------- approval policy (ADR-030)
function isPolicyApprover(approver) {
  return typeof approver === "string" && approver.startsWith("policy:");
}

function ruleBadge(policy) {
  return badge("rule", `rule ${policy.rule}`);
}

function policySection(p) {
  if (!p) return [];
  const why = p.rule ? p.reason.replace(`rule ${p.rule}`, "").replace(/^: /, "") : p.reason;
  const out = [el("div", { class: "policy-line" }, el("b", {}, "审批策略"), " approval policy: ",
    p.rule ? ruleBadge(p) : badge("rule default", "default"), why ? ` ${why}` : "")];
  if (p.guard) out.push(el("div", { class: "blocked small" }, p.guard));
  return out;
}

// ---------------------------------------------------------------- approval card + preview (ADR-029)
function checkBadge(check) {
  const cls = { match: "match", preview_mismatch: "mismatch", preview_unavailable: "unpreviewed" }[check.result] || "empty";
  return badge(cls, `preview ${check.result}`);
}

function fmtValue(v) {
  return v === null || v === undefined ? "null" : typeof v === "string" ? v : JSON.stringify(v);
}

function fmtRow(row, volatile) {
  // time columns are recorded but not compared: shown muted
  return Object.entries(row).flatMap(([col, v], i) => [
    i ? ", " : "",
    el("span", volatile[col] ? { class: "volatile", title: `${volatile[col]}: recorded, not compared` } : {}, `${col}=${fmtValue(v)}`),
  ]);
}

function tableChanges(name, t) {
  const volatile = t.volatile || {};
  const key = (k) => el("td", { class: "key" }, fmtValue(k));
  const rows = [
    ...t.added.map((a) => el("tr", { class: "added" }, el("td", {}, "+"), key(a.key), el("td", {}, ...fmtRow(a.row, volatile)))),
    ...t.removed.map((r) => el("tr", { class: "removed" }, el("td", {}, "−"), key(r.key), el("td", {}, ...fmtRow(r.row, volatile)))),
    ...t.changed.map((c) => el("tr", { class: "changed" }, el("td", {}, "~"), key(c.key),
      el("td", {}, ...c.columns.flatMap((col, i) => [i ? ", " : "", el("span", volatile[col] ? { class: "volatile" } : {}, `${col}: ${fmtValue(c.before[col])} → ${fmtValue(c.after[col])}`)])))),
  ];
  const shown = t.added.length + t.removed.length + t.changed.length;
  const total = t.counts.added + t.counts.removed + t.counts.changed;
  return el("div", { class: "preview-table" },
    el("div", {}, el("b", {}, name), el("span", { class: "muted small" }, ` ${t.rows_before} → ${t.rows_after} rows`)),
    el("table", { class: "rows" }, el("tbody", {}, ...rows)),
    total > shown ? el("div", { class: "muted small" }, `… and ${total - shown} more`) : "");
}

function previewSection(p, risk) {
  if (!p) return [];
  if (p.status === "ok") {
    const secs = p.timings_ms && p.timings_ms.total ? `shadow environment run: ${(p.timings_ms.total / 1000).toFixed(1)} s; the session's own database was not touched` : "";
    const out = [el("div", { class: "preview-head" }, el("b", {}, "将要改动的行"), " rows this call will change ", badge("ok", "previewed")), el("div", { class: "muted small" }, secs)];
    const tables = Object.entries((p.changes && p.changes.tables) || {});
    if (!tables.length) out.push(el("p", { class: "muted" }, "In the preview this call changed no rows."));
    if (p.call && p.call.is_error) out.push(el("p", { class: "blocked" }, `In the preview the call returned an error: ${p.call.text.slice(0, 200)}`));
    for (const [name, t] of tables) out.push(tableChanges(name, t));
    return [el("div", { class: "preview-box" }, ...out)];
  }
  const out = [el("div", { class: "preview-head" }, badge("unpreviewed big", "未预演 · not previewed")), el("p", {}, `Preview ${p.status}: ${p.error || ""}`)];
  if (!p.approvable) out.push(el("p", { class: "blocked" }, `A ${risk} call needs a successful preview before approval: only rejection is possible.`));
  return [el("div", { class: "preview-box unpreviewed" }, ...out)];
}

function showApproval(e) {
  const body = $("approval-body");
  body.replaceChildren(
    el("div", {}, "Tool: ", el("b", {}, e.tool), " ", badge(e.risk, e.risk)),
    ...policySection(e.policy),
    el("pre", {}, JSON.stringify(e.arguments, null, 2)),
    ...previewSection(e.preview, e.risk),
  );
  $("approve").disabled = Boolean(e.preview) && e.preview.approvable === false;
  $("approval").classList.remove("hidden");
}

async function decide(approved) {
  if (!state.session) return;
  $("approval").classList.add("hidden");
  const body = { approved, approver: $("approver").value || "reviewer", reason: $("reason").value };
  chat("step", approved ? `approved by ${body.approver}` : `rejected by ${body.approver}`);
  await run(`/approvals/${state.session.session_id}`, body);
}

async function run(path, body) {
  state.streaming = true;
  setComposer(false);
  try {
    await streamSSE(path, body, handleEvent);
  } catch (err) {
    chat("step", `error: ${err.message}`);
  } finally {
    state.streaming = false;
    setComposer(true);
  }
}

function setComposer(enabled) {
  $("message").disabled = !enabled || !state.session;
  $("send").disabled = !enabled || !state.session;
}

// ---------------------------------------------------------------- session / data
async function loadScenarios() {
  const rows = await api("/scenarios");
  $("scenario").replaceChildren(...rows.map((r) => el("option", { value: r.name }, `${r.name} (${r.tools} tools, ${r.tasks} tasks)`)));
}

async function createSession() {
  $("create-session").disabled = true;
  try {
    const s = await api("/sessions", { method: "POST", body: JSON.stringify({ scenario: $("scenario").value }) });
    state.session = s;
    $("session-info").textContent = `session ${s.session_id} · env ${s.env_url}`;
    $("tools").replaceChildren(...s.tools.map((t) => el("tr", {}, el("td", {}, t.name.split("__")[1] || t.name), el("td", {}, badge(t.risk, t.risk)))));
    $("chat").replaceChildren();
    $("timeline").replaceChildren();
    setComposer(true);
    refreshMemory();
  } catch (err) {
    $("session-info").textContent = `failed: ${err.message}`;
  } finally {
    $("create-session").disabled = false;
  }
}

async function refreshMemory() {
  const items = await api("/memory");
  $("memory").replaceChildren(...items.map((m) => el("li", {}, `${m.key}: ${m.value} (${m.source})`)));
}

async function refreshDiff() {
  if (!state.session) return;
  const d = await api(`/sessions/${state.session.session_id}/diff`);
  const box = $("diff");
  if (!d.changed) {
    box.replaceChildren(el("p", { class: "muted" }, "No changes vs the initial DB."));
    return;
  }
  const rows = Object.entries(d.tables).map(([name, t]) =>
    el("tr", {}, el("td", {}, name), el("td", {}, `${t.rows_before} → ${t.rows_after}`),
      el("td", {}, JSON.stringify(t.added)), el("td", {}, JSON.stringify(t.removed)), el("td", {}, JSON.stringify(t.changed))));
  box.replaceChildren(el("table", {}, el("thead", {}, el("tr", {}, ...["table", "rows", "added pk", "removed pk", "changed pk"].map((h) => el("th", {}, h)))), el("tbody", {}, ...rows)));
}

// ---------------------------------------------------------------- trajectory viewer
function renderAwmTrajectory(data) {
  // AWM `awm agent` output format: third_party/agent-world-model/awm/core/agent.py:578-590
  const out = [el("p", {}, `AWM trajectory · scenario ${data.scenario} · task ${data.task_id} · ${data.total_iterations} iterations`), el("p", { class: "muted" }, data.task || "")];
  for (const step of data.trajectory || []) {
    const box = el("div", { class: "viewer-step" }, el("b", {}, `#${step.iteration} `));
    for (const tc of step.tool_calls || []) box.append(el("code", {}, `${tc.name} ${JSON.stringify(tc.arguments)}`));
    if (step.is_final) box.append(el("div", {}, "final: ", (step.content || "").slice(0, 600)));
    if (step.tool_response) box.append(el("pre", {}, String(step.tool_response.content).slice(0, 600)));
    out.push(box);
  }
  return out;
}

function renderWorkbenchTrace(events) {
  return [el("p", {}, `workbench trace · ${events.length} events`), el("ol", { class: "timeline" }, ...events.map((e) => {
    const li = el("li", { class: e.type }, el("b", {}, e.type), " ", e.tool || e.node || e.key || e.reason || "");
    if (e.type === "tool_call") li.append(" ", badge(e.status, e.status));
    return li;
  }))];
}

function renderAny(text) {
  const trimmed = text.trim();
  let nodes;
  try {
    const obj = JSON.parse(trimmed);
    if (Array.isArray(obj)) nodes = renderWorkbenchTrace(obj);
    else if (obj.trajectory && obj.messages) nodes = renderAwmTrajectory(obj);
    else nodes = [el("pre", {}, JSON.stringify(obj, null, 2).slice(0, 5000))];
  } catch {
    const events = trimmed.split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l));
    nodes = renderWorkbenchTrace(events);
  }
  $("viewer").replaceChildren(...nodes);
}

// ---------------------------------------------------------------- wiring
$("create-session").addEventListener("click", createSession);
$("composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = $("message").value.trim();
  if (!text || !state.session || state.streaming) return;
  $("message").value = "";
  chat("user", text);
  run(`/sessions/${state.session.session_id}/messages`, { content: text });
});
$("approve").addEventListener("click", () => decide(true));
$("reject").addEventListener("click", () => decide(false));
$("refresh-diff").addEventListener("click", refreshDiff);
$("traj-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (file) renderAny(await file.text());
});
$("load-trace").addEventListener("click", async () => {
  if (state.session) renderAny(JSON.stringify(await api(`/sessions/${state.session.session_id}/trace`)));
});
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "diff") refreshDiff();
}));

loadScenarios().catch((err) => { $("session-info").textContent = `cannot load scenarios: ${err.message}`; });
