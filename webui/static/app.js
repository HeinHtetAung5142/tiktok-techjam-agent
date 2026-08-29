/* Client for the local shopping-agent UI.
 *
 * The random target lives here and only here. The server draws it, hands it over, and
 * keeps no record of it; deciding whether the agent surfaced it is done below by
 * comparing ids against the response. Nothing about the target is ever sent back --
 * /api/message carries { session_id, message } and nothing else.
 */

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  maxTurns: 10,
  topK: 10,
  turn: 0,
  target: null,      // { parent_asin, intent_card[], details{} } - browser-only
  view: "card",      // "card" | "full"
  found: null,       // { rank, turn } once the target shows up in the scored window
  busy: false,
  // Last turn's ranking, kept so a reroll can re-tag the list without asking the agent
  // anything again.
  last: { results: [], disclosed: 0, turn: 0 },
};

/* ---------- tiny DOM helpers (textContent everywhere: catalog text is untrusted) ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function price(value) {
  return typeof value === "number" ? "$" + value.toFixed(2) : "price not listed";
}

function rating(row) {
  if (typeof row.average_rating !== "number") return null;
  const count = typeof row.rating_number === "number" ? ` (${row.rating_number.toLocaleString()})` : "";
  return `${row.average_rating.toFixed(1)}★${count}`;
}

async function api(path, body) {
  const options = { method: body === undefined ? "GET" : "POST" };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({ error: "bad response from server" }));
  if (!response.ok) throw Object.assign(new Error(payload.error || response.statusText), payload);
  return payload;
}

/* ---------- target panel ---------- */

function renderTarget() {
  const host = $("target-body");
  host.replaceChildren();
  if (!state.target) {
    host.append(el("p", "muted", "Drawing a product…"));
    return;
  }

  if (state.view === "card") {
    const lines = state.target.intent_card || [];
    if (!lines.length) {
      host.append(el("p", "muted", "This product discloses nothing useful — try another."));
      return;
    }
    const list = el("ul", "card-lines");
    lines.forEach((line) => list.append(el("li", null, line)));
    host.append(list);
    host.append(el("p", "asin", "these are the only things the simulated customer would ever tell you"));
    return;
  }

  const d = state.target.details || {};
  host.append(el("p", "full-title", d.title));
  const meta = el("p", "meta");
  const bits = [d.store, price(d.price), rating(d), (d.categories || []).join(" › ")].filter(Boolean);
  bits.forEach((bit) => meta.append(el("span", null, bit)));
  host.append(meta);
  if ((d.features || []).length) {
    const list = el("ul", "card-lines");
    d.features.forEach((f) => list.append(el("li", null, f)));
    host.append(list);
  }
  host.append(el("p", "asin", d.parent_asin));
}

function renderFound() {
  const banner = $("found");
  if (!state.found) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent =
    state.found.rank === 1
      ? `🎯 Found it at the top of the list — rank 1 on turn ${state.found.turn}.`
      : `🎯 Found it — rank ${state.found.rank} on turn ${state.found.turn}.`;
}

async function reroll() {
  try {
    state.target = await api("/api/target", {});
    state.found = null;
    renderTarget();
    // Re-tag the ranking already on screen against the new target: same rows, no new
    // agent call, and `found` re-detects if the new draw is already sitting in the list.
    if (state.last.results.length) {
      renderRanking(state.last.results, state.last.disclosed, state.last.turn);
    }
    renderFound();
  } catch (err) {
    $("target-body").replaceChildren(el("p", "muted", "Could not draw a product: " + err.message));
  }
}

/* ---------- ranking ---------- */

function renderRanking(results, disclosedCount, turn) {
  state.last = { results: results, disclosed: disclosedCount, turn: turn };
  const host = $("rank-list");
  host.replaceChildren();

  if (!results.length) {
    host.append(el("p", "empty", "The agent returned nothing for this turn."));
    $("rank-note").textContent = "no results";
    return;
  }

  results.forEach((row) => {
    if (row.rank === state.topK + 1) {
      host.append(el("div", "divider", `below the scored top ${state.topK}`));
    }
    host.append(rowNode(row, turn));
  });

  const withheld = Math.max(results.length - disclosedCount, 0);
  $("rank-note").textContent =
    `agent disclosed ${disclosedCount} of ${state.topK} · ${withheld} more shown for you`;
}

function rowNode(row, turn) {
  const isTarget = state.target && row.parent_asin === state.target.parent_asin;

  const node = el("div", "row");
  if (row.scored) node.classList.add("scored");
  if (!row.disclosed) node.classList.add("withheld");
  if (isTarget) node.classList.add("is-target");

  node.append(el("div", "rank", "#" + row.rank));

  const body = el("div");
  const title = el("p", "title");
  title.append(document.createTextNode(row.title));
  if (!row.disclosed) title.append(el("span", "badge withheld", "withheld this turn"));
  if (isTarget) title.append(el("span", "badge target", "TARGET"));
  body.append(title);

  const bits = [row.store, price(row.price), rating(row)].filter(Boolean);
  body.append(el("p", "sub", bits.join(" · ")));
  if ((row.features || []).length) {
    body.append(el("p", "feat", row.features.join(" · ")));
  }
  node.append(body);

  // First time the target lands inside the scored window, that is the result the
  // evaluator would have recorded -- freeze it, exactly as the scorer does.
  if (isTarget && row.scored && !state.found) {
    state.found = { rank: row.rank, turn: turn };
  }
  return node;
}

/* ---------- chat ---------- */

function say(who, text, askAttribute) {
  const node = el("div", "msg " + who);
  if (who !== "sys") node.append(el("span", "who", who));
  node.append(document.createTextNode(text));
  if (askAttribute) node.append(el("span", "ask", "asking about: " + askAttribute));
  $("transcript").append(node);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  $("input").disabled = busy;
  if (busy) $("composer-note").textContent = "thinking…";
}

async function send(event) {
  event.preventDefault();
  if (state.busy) return;
  const text = $("input").value.trim();
  if (!text) return;

  $("input").value = "";
  say("you", text);
  setBusy(true);

  try {
    // Only the typed text goes out. The target is deliberately not in this payload.
    const reply = await api("/api/message", { session_id: state.sessionId, message: text });

    state.turn = reply.turn;
    say("agent", reply.message, reply.ask_attribute);
    // The turn-cap reply carries no ranking; keep the last one on screen rather than
    // replacing a good list with "nothing found".
    if ((reply.results || []).length) {
      renderRanking(reply.results, reply.disclosed_count || 0, reply.turn);
      renderFound();
    }

    $("stat-turn").textContent = reply.turn;
    $("stat-latency").textContent = reply.latency_ms + " ms";
    const usage = reply.usage || {};
    $("stat-tokens").textContent = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);

    const note = $("composer-note");
    if (reply.done) {
      note.textContent = `Turn ${state.maxTurns} reached — the evaluator stops here. Reset to play again.`;
      note.className = "composer-note warn";
    } else {
      note.textContent = reply.ask_attribute
        ? "Answer the agent's question to give it more to work with."
        : "";
      note.className = "composer-note";
    }
  } catch (err) {
    if (err.expired) {
      say("sys", "Session expired on the server — starting a new one.");
      await startSession();
    } else {
      say("sys", "Error: " + err.message);
    }
    $("composer-note").textContent = "";
  } finally {
    setBusy(false);
    if (state.turn >= state.maxTurns) {
      // Past the cap the agent has nothing left to give; steer the user to Reset chat.
      $("send").disabled = true;
      $("input").disabled = true;
    } else {
      $("input").focus();
    }
  }
}

/* ---------- session ---------- */

async function startSession() {
  const info = await api("/api/session", {});
  state.sessionId = info.session_id;
  state.maxTurns = info.max_turns;
  state.topK = info.top_k;
  state.turn = 0;
  state.found = null;
  $("stat-maxturn").textContent = info.max_turns;
  $("stat-turn").textContent = "0";
  $("stat-latency").textContent = "—";
  renderFound();
}

async function resetChat() {
  $("transcript").replaceChildren();
  $("rank-list").replaceChildren(el("p", "empty", "Send a message to see what the agent retrieves."));
  $("rank-note").textContent = "no results yet";
  state.last = { results: [], disclosed: 0, turn: 0 };
  $("composer-note").textContent = "";
  $("composer-note").className = "composer-note";
  await startSession();
  setBusy(false);                       // re-enable if the turn cap had locked the box
  $("composer-note").textContent = "";
  say("sys", "New session. The agent remembers nothing from before.");
  $("input").focus();
}

function setView(view) {
  state.view = view;
  $("view-card").classList.toggle("on", view === "card");
  $("view-full").classList.toggle("on", view === "full");
  renderTarget();
}

async function main() {
  $("composer").addEventListener("submit", send);
  $("reroll").addEventListener("click", reroll);
  $("reset").addEventListener("click", resetChat);
  $("view-card").addEventListener("click", () => setView("card"));
  $("view-full").addEventListener("click", () => setView("full"));

  try {
    await startSession();
    await reroll();
    say("sys", "Tell the agent what you're shopping for. It will ask one attribute per turn.");
    $("input").focus();
  } catch (err) {
    say("sys", "Could not reach the server: " + err.message);
  }
}

main();
