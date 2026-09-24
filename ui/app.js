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
    if (e.approver) li.append(` approved by ${e.approver}`);
    li.append(el("pre", {}, (e.text || "").slice(0, 400)));
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
  if (e.type === "tool_call") chat("step", `tool ${e.tool} → ${e.status}`);
  if (e.type === "node" && e.node === "plan") chat("step", `plan: ${e.steps.map((s) => s.description).join(" → ")}`);
  if (e.type === "approval_required" && e.thread_id) showApproval(e);
  if (e.type === "memory_rejected") chat("step", `memory not stored (${e.source}): ${e.key}`);
  if (e.type === "done") {
    chat("agent", e.final_answer || "(no answer)");
    refreshMemory();
  }
}

function showApproval(e) {
  const body = $("approval-body");
  body.replaceChildren(
    el("div", {}, "Tool: ", el("b", {}, e.tool), " ", badge(e.risk, e.risk)),
    el("pre", {}, JSON.stringify(e.arguments, null, 2)),
  );
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
