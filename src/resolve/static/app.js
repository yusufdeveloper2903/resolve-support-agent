// Resolve UI: customer chat (streamed over SSE) and the operator console. No build step.
const $ = (sel) => document.querySelector(sel);
const state = { customerId: null, conversation: null, busy: false, poll: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, { method = "GET", body, customer } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (customer) headers["X-Customer-Id"] = customer;
  const res = await fetch(path, { method, headers, body: body && JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.status === 204 ? null : res.json();
}

// ---------- theme & tabs ----------
$("#theme-toggle").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch {}
});
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch {}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab);
    });
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab.dataset.view}`));
    if (tab.dataset.view === "operator") refreshOperator();
  }),
);

// ---------- chat rendering ----------
const messages = $("#messages");

function scrollDown() { messages.scrollTop = messages.scrollHeight; }

function addBubble(kind, text) {
  messages.querySelector(".empty")?.remove();
  const bubble = el("div", `msg ${kind}`, text);
  messages.append(bubble);
  scrollDown();
  return bubble;
}

function chipClass(outcome) {
  if (outcome === "error" || outcome === "rejected_by_specialist") return "error";
  if (outcome === "pending_approval" || outcome === "running") return "pending";
  return "ok";
}

function addChip(id, name, outcome) {
  messages.querySelector(".empty")?.remove();
  let row = messages.lastElementChild;
  if (!row?.classList.contains("tools")) messages.append((row = el("div", "tools")));
  const chip = el("span", `chip ${chipClass(outcome)}`, `${name} · ${outcome}`);
  chip.dataset.id = id;
  chip.dataset.name = name;
  row.append(chip);
  scrollDown();
}

function updateChip(id, outcome) {
  const chip = messages.querySelector(`.chip[data-id="${CSS.escape(id)}"]`);
  if (!chip) return;
  chip.className = `chip ${chipClass(outcome)}`;
  chip.textContent = `${chip.dataset.name} · ${outcome}`;
}

function renderTranscript(entries) {
  messages.replaceChildren();
  for (const e of entries) {
    if (e.kind === "tool") addChip(e.id, e.name, e.outcome);
    else addBubble(e.kind, e.text);
  }
  if (!entries.length) messages.append(emptyState);
}
const emptyState = messages.querySelector(".empty");
emptyState.querySelectorAll(".suggestion").forEach((b) =>
  b.addEventListener("click", () => { $("#input").value = b.textContent; $("#composer").requestSubmit(); }),
);

// ---------- streaming ----------
async function stream(path, body) {
  setBusy(true);
  let bubble = null;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Customer-Id": state.customerId },
      body: body && JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const event = raw.match(/^event: (.*)$/m)?.[1];
        const data = raw.match(/^data: (.*)$/m)?.[1];
        if (event && data) bubble = handleEvent(event, JSON.parse(data), bubble);
      }
    }
  } catch (err) {
    addBubble("error", err.message);
  } finally {
    bubble?.classList.remove("typing");
    setBusy(false);
    await refreshConversation();
  }
}

function handleEvent(type, data, bubble) {
  switch (type) {
    case "text":
      if (!bubble) { bubble = addBubble("assistant", ""); bubble.classList.add("typing"); }
      bubble.textContent += data.delta;
      scrollDown();
      return bubble;
    case "tool_call":
      bubble?.classList.remove("typing");
      addChip(data.id, data.name, "running");
      return null; // text after tools starts a new bubble
    case "tool_result":
      updateChip(data.id, data.status);
      return bubble;
    case "error":
      addBubble("error", data.message);
      return bubble;
    default: // approval_required, done
      return bubble;
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("#composer button").disabled = busy || state.conversation?.status !== "active";
}

// ---------- conversation lifecycle ----------
async function newConversation() {
  clearInterval(state.poll);
  state.conversation = await api("/api/conversations", { method: "POST", body: { customer_id: state.customerId } });
  renderTranscript([]);
  await refreshConversation();
}

async function refreshConversation() {
  if (!state.conversation) return;
  const conv = await api(`/api/conversations/${state.conversation.id}`, { customer: state.customerId });
  state.conversation = conv;
  const waiting = conv.status === "awaiting_approval";
  $("#approval-banner").hidden = !waiting;
  setBusy(state.busy);
  clearInterval(state.poll);
  if (waiting && conv.ready_to_resume && !state.busy) {
    renderTranscript(conv.transcript);
    await stream(`/api/conversations/${conv.id}/resume`);
  } else if (waiting) {
    state.poll = setInterval(refreshConversation, 2000);
  }
}

$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  addBubble("user", text);
  await stream(`/api/conversations/${state.conversation.id}/messages`, { text });
});
$("#input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); }
});
$("#new-chat").addEventListener("click", newConversation);
$("#customer-select").addEventListener("change", (event) => { state.customerId = event.target.value; newConversation(); });

// ---------- operator console ----------
async function refreshOperator() {
  const [pending, audit] = await Promise.all([api("/api/approvals?status=pending"), api("/api/audit?limit=200")]);
  const badge = $("#pending-count");
  badge.hidden = !pending.length;
  badge.textContent = pending.length;

  const list = $("#approvals");
  list.replaceChildren(...pending.map(approvalCard));
  if (!pending.length) list.append(el("div", "muted", "Nothing waiting. Refunds over $100 appear here."));

  $("#audit-rows").replaceChildren(
    ...audit.map((row) => {
      const tr = el("tr");
      tr.append(
        el("td", "", new Date(row.ts).toLocaleTimeString()),
        el("td", "", row.actor),
        el("td", "", row.customer_id ?? ""),
        el("td", "", row.tool),
        el("td", `outcome ${row.outcome}`, row.outcome),
        el("td", "args", row.arguments),
      );
      return tr;
    }),
  );
}

function approvalCard(approval) {
  const card = el("div", "approval");
  const note = el("input");
  note.placeholder = "Note to the customer (optional)";
  const actions = el("div", "actions");
  for (const [label, approve] of [["Approve", true], ["Reject", false]]) {
    const button = el("button", `btn small ${approve ? "approve" : "reject"}`, label);
    button.addEventListener("click", async () => {
      try {
        await api(`/api/approvals/${approval.id}/decision`, {
          method: "POST",
          body: { approve, operator: $("#operator-name").value || "operator", note: note.value || null },
        });
      } catch (err) { alert(err.message); }
      refreshOperator();
    });
    actions.append(button);
  }
  card.append(
    el("div", "summary", approval.summary),
    el("div", "meta", `${approval.customer_id} · ${approval.tool_name} · ${new Date(approval.created_at).toLocaleString()}`),
    note,
    actions,
  );
  return card;
}

$("#reset-demo").addEventListener("click", async () => {
  if (!confirm("Reset all demo data? Conversations, approvals and the audit log are cleared.")) return;
  await api("/api/reset", { method: "POST" });
  await refreshOperator();
  await newConversation();
});

// ---------- boot ----------
(async function boot() {
  const [health, customers] = await Promise.all([api("/api/health"), api("/api/customers")]);
  const status = $("#agent-status");
  status.textContent = health.agent_online ? `${health.model} · online` : "agent offline (no API key)";
  status.classList.add(health.agent_online ? "online" : "offline");
  const select = $("#customer-select");
  for (const c of customers) select.append(new Option(`${c.name} (${c.id})`, c.id));
  state.customerId = customers[0]?.id;
  await newConversation();
  setInterval(() => { if ($("#view-operator").classList.contains("active")) refreshOperator(); }, 3000);
  api("/api/approvals?status=pending").then((p) => { $("#pending-count").hidden = !p.length; $("#pending-count").textContent = p.length; });
})();
