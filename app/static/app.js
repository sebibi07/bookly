const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const badge = document.getElementById("authBadge");
let sessionId = null;

const GREETING =
  "Hi, I'm Bookly's support assistant. I can check on an order, start a return, " +
  "or answer questions about how we ship and handle refunds. What's going on?";

// Models reach for markdown even when asked not to. Render the bit they
// actually use rather than showing a customer literal asterisks.
function lightMarkdown(text) {
  return text
    .replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<![*\w])\*([^*\n]+)\*(?![*\w])/g, "<em>$1</em>")
    .replace(/`([^`\n]+)`/g, '<code>$1</code>');
}

function bubble(text, who) {
  const row = document.createElement("div");
  row.className = "row" + (who === "user" ? " user" : "");
  const el = document.createElement("div");
  el.className = "msg " + who;
  el.innerHTML = lightMarkdown(text);
  row.appendChild(el);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function thinking() {
  const row = bubble("", "agent");
  row.querySelector(".msg").innerHTML =
    '<span class="dots"><span></span><span></span><span></span></span>';
  return row;
}

// Collapsed disclosure under each reply. Handy on a screen recording, and
// deletable in one block if you want a pure customer view.
function trace(t) {
  const d = document.createElement("details");
  d.className = "trace";
  const called = t.tool_calls.map((c) => c.tool);
  d.innerHTML =
    `<summary><span class="pill">${t.intent} · ${t.intent_confidence}</span>` +
    `<span class="pill">${t.auth.verified ? "verified" : "anonymous"}</span>` +
    `<span class="pill">${called.length ? called.join(" → ") : "no tools"}</span>` +
    `<span class="pill">${Math.round(t.total_ms)} ms</span></summary>` +
    `<pre>${JSON.stringify(t, null, 2).replace(/</g, "&lt;")}</pre>`;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function setBadge(auth) {
  if (auth.locked) {
    badge.dataset.state = "locked";
    badge.textContent = "Verification locked";
  } else if (auth.verified) {
    badge.dataset.state = "verified";
    badge.textContent = "Verified · " + auth.customer_name;
  } else {
    badge.dataset.state = "anon";
    badge.textContent = "Not verified";
  }
}

async function submit(text) {
  bubble(text, "user");
  input.value = "";
  send.disabled = true;
  const pending = thinking();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    sessionId = data.session_id;
    pending.querySelector(".msg").innerHTML = lightMarkdown(data.reply);
    setBadge(data.trace.auth);
    lastTrace = data.trace;
    if (plated) renderPlate();
    trace(data.trace);
  } catch (err) {
    pending.querySelector(".msg").textContent =
      "Something went wrong reaching the agent: " + err.message;
  } finally {
    send.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (text) submit(text);
});

document.querySelectorAll(".personas button").forEach((b) =>
  b.addEventListener("click", () => {
    input.value = b.dataset.fill;
    input.focus();
  })
);

document.getElementById("resetBtn").addEventListener("click", async () => {
  await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "reset", session_id: sessionId }),
  });
  sessionId = null;
  log.innerHTML = "";
  lastTrace = null;
  if (plated) renderPlate();
  setBadge({ verified: false, locked: false });
  bubble(GREETING, "agent");
});

// --- live machinery panel -------------------------------------------------
const plate = document.getElementById("plate");
const plateBtn = document.getElementById("plateBtn");
let plated = false;
let lastTrace = null;

const esc = (v) => String(v).replace(/[<&]/g, (c) => (c === "<" ? "&lt;" : "&amp;"));

function prow(label, inner) {
  return `<div class="prow"><div class="plabel">${label}</div>${inner}</div>`;
}

function renderPlate() {
  if (!lastTrace) {
    plate.innerHTML =
      '<h3><span>What the system did</span></h3>' +
      '<p class="plate-empty">Send a message and this fills in — intent, which tools ' +
      'the model was allowed to see, the token it acted under, and every database ' +
      'statement that ran.</p>';
    return;
  }
  const t = lastTrace;
  let h = `<h3><span>What the system did</span><span>${Math.round(t.total_ms)} ms</span></h3>`;

  h += prow("Router", `<div class="pmono"><strong>${t.intent}</strong> · confidence ${t.intent_confidence}</div>` +
    (t.llm_error ? `<div class="pmono" style="color:#9c3025">fell back — ${esc(t.llm_error)}</div>` : ""));

  h += prow("Tools sent to the model",
    `<div class="tchips">${t.tools_exposed.map((n) => `<span class="tchip">${n}</span>`).join("")}` +
    `${(t.tools_withheld || []).map((n) => `<span class="tchip off">${n}</span>`).join("")}</div>`);

  h += prow("Tool calls", t.tool_calls.length
    ? `<div class="pmono">${t.tool_calls.map((c) =>
        `${c.tool}(${Object.entries(c.arguments).map(([k, v]) => `${k}=${esc(v)}`).join(", ")})` +
        `${c.error ? "  ← error" : ""}`).join("\n")}</div>`
    : '<div class="pmono" style="opacity:.55">— none —</div>');

  const tok = t.token;
  h += prow("Identity", tok
    ? `<div class="pmono">customer <strong>${tok.sub}</strong> · expires in ${tok.expires_in}s` +
      `\nscope: ${tok.scope.join(", ")}\nverified by: ${tok.amr.join(" + ")}</div>`
    : '<div class="pmono" style="opacity:.55">anonymous — no token issued</div>');

  if (t.queries && t.queries.length) {
    h += prow("Database", t.queries.map((q) =>
      `<pre class="psql">${esc(q.sql)}</pre>` +
      `<span class="prows${q.rows === 0 ? " zero" : ""}">${q.rows} row${q.rows === 1 ? "" : "s"} · ${q.ms} ms</span>`
    ).join(""));
  }
  if (t.escalated) {
    h += prow("Effect", '<div class="pmono">escalated to a human · handoff email sent</div>');
  }
  plate.innerHTML = h;
}

function togglePlate() {
  plated = !plated;
  document.body.classList.toggle("plated", plated);
  plateBtn.setAttribute("aria-pressed", String(plated));
  document.getElementById("plateLabel").textContent =
    plated ? "Hide the machinery" : "Show the machinery";
  if (plated) renderPlate();
}

plateBtn.addEventListener("click", togglePlate);
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return;
  if (e.key === "t" || e.key === "T") togglePlate();
});

// --- model picker ---------------------------------------------------------
const overlay = document.getElementById("modelOverlay");
const engineBtn = document.getElementById("engineBtn");
const apiKey = document.getElementById("apiKey");
const keyErr = document.getElementById("keyErr");
const saveKey = document.getElementById("saveKey");
let canSetKey = false;

function showEngine(h) {
  engineBtn.textContent =
    h.engine === "mock"
      ? "scripted engine — click to connect a model"
      : `${h.model} · routing on ${h.router_model}`;
}

function openPanel() {
  if (!canSetKey) return;
  keyErr.hidden = true;
  overlay.hidden = false;
  apiKey.focus();
}

function closePanel() {
  overlay.hidden = true;
  // Never leave the credential sitting in the DOM.
  apiKey.value = "";
}

async function chooseEngine(key) {
  saveKey.disabled = true;
  keyErr.hidden = true;
  try {
    const res = await fetch("/api/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not switch model.");
    showEngine(data);
    closePanel();
    bubble(
      data.engine === "mock"
        ? "Switched to the scripted engine."
        : `Connected to ${data.model}. Starting a fresh conversation.`,
      "system"
    );
    // A transcript built by one engine should not be handed to another.
    sessionId = null;
    setBadge({ verified: false, locked: false });
  } catch (err) {
    keyErr.textContent = err.message;
    keyErr.hidden = false;
  } finally {
    saveKey.disabled = false;
  }
}

engineBtn.addEventListener("click", openPanel);
document.getElementById("cancelKey").addEventListener("click", closePanel);
document.getElementById("useMock").addEventListener("click", () => chooseEngine(null));
saveKey.addEventListener("click", () => {
  const key = apiKey.value.trim();
  if (!key) {
    keyErr.textContent = "Paste a key, or choose the scripted engine.";
    keyErr.hidden = false;
    return;
  }
  chooseEngine(key);
});
apiKey.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveKey.click();
});
overlay.addEventListener("click", (e) => {
  if (e.target === overlay) closePanel();
});

fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    canSetKey = h.can_set_key;
    showEngine(h);
    if (!canSetKey) engineBtn.classList.remove("linky");
  });

bubble(GREETING, "agent");
input.focus();
