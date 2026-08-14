"use strict";

/* The grid is rendered the way the profile will actually look once the queue
   has drained: newest at the top-left, so the LAST photo to post sits first and
   the next one to go out sits directly above the already-published block. */

const FLAG_LABELS = { no_date: "no date", too_small: "small", caption_failed: "no caption" };

let state = {
  order: [], photos: {}, posted: [], slots: {}, profile: null, caption_enabled: false,
};
let selected = [];
let lastAnchor = null;
let dragName = null;

const $ = (sel) => document.querySelector(sel);
const grid = $("#grid");
const panel = $("#panel");

/* Queue in display order: reverse of posting order. Already-published photos
   drop out — they stay in `order` so the publisher's history is intact, but
   they're drawn once, in the posted block below, not twice. */
const displayQueue = () =>
  [...state.order].reverse().filter((n) => state.photos[n]?.status !== "posted");
// The photo the publisher will actually pick up next.
const nextReady = () => state.order.find((n) => state.photos[n]?.status === "ready");

// --- server -----------------------------------------------------------------

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}

function apply(data) {
  if (data.order) state = data;
  renderProfile();
  render();
}

// --- profile header ---------------------------------------------------------

function renderProfile() {
  const p = state.profile;
  if (!p) return;
  document.title = `${p.username} — queue`;
  $("#username").textContent = p.username;
  $("#display-name").textContent = p.display_name;
  $("#bio").textContent = p.bio;
  $("#stat-posts").textContent = p.posts;
  $("#stat-followers").textContent = p.followers.toLocaleString();
  $("#stat-following").textContent = p.following.toLocaleString();
  if (p.has_avatar) $("#avatar").style.backgroundImage = "url(/avatar)";

  const list = $("#highlights");
  list.replaceChildren();
  for (const name of p.highlights) {
    const li = document.createElement("li");
    const ring = document.createElement("div");
    ring.className = "ring";
    const label = document.createElement("span");
    label.textContent = name;
    li.append(ring, label);
    list.append(li);
  }
}

// --- grid -------------------------------------------------------------------

function formatSlot(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function shortSlot(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function render() {
  grid.replaceChildren();
  const queue = displayQueue();
  $("#empty").hidden = queue.length + state.posted.length > 0;

  // Next to post: the first photo that isn't on hold. Held ones are skipped by
  // the publisher, so they must not wear the badge either.
  const nextUp = nextReady();

  for (const name of queue) {
    const photo = state.photos[name];
    const cell = baseCell(name, `/img/${encodeURIComponent(name)}`);
    cell.classList.add("queued");
    cell.draggable = true;
    if (photo.status === "hold") cell.classList.add("hold");
    if (name === nextUp) cell.classList.add("next");

    const when = document.createElement("span");
    when.className = "when";
    if (photo.status === "hold") {
      when.textContent = "on hold";
    } else if (name === nextUp) {
      when.textContent = `next · ${shortSlot(state.slots[name])}`;
    } else {
      when.textContent = shortSlot(state.slots[name]) || "—";
    }
    cell.append(when);

    const mark = warnMark(photo);
    if (mark) cell.append(mark);
    if (photo.extra?.length) {
      cell.classList.add("carousel");
      cell.append(stackMark(photo));
    }
    grid.append(cell);
  }

  for (const name of state.posted) {
    const cell = baseCell(name, `/posted/${encodeURIComponent(name)}`);
    cell.classList.add("posted");
    const mark = document.createElement("span");
    mark.className = "mark";
    mark.textContent = "✓";
    mark.title = "already posted";
    cell.append(mark);
    grid.append(cell);
  }

  renderPanel();
}

/* The "needs attention" dot: no caption yet, or a flag from the pipeline. */
function warnMark(photo) {
  const warnings = (photo.flags || []).filter((f) => FLAG_LABELS[f]);
  if (!warnings.length && photo.caption.trim()) return null;
  const mark = document.createElement("span");
  mark.className = "mark warn";
  mark.textContent = "!";
  mark.title = warnings.length
    ? warnings.map((f) => FLAG_LABELS[f]).join(", ")
    : "no caption";
  return mark;
}

/* Grouped by filename: _DSF1234A.jpg + _DSF1234B.jpg are one post. The count
   is the only way to tell from the grid, since only the first one is shown. */
function photoFiles(photo) {
  return [photo.file, ...(photo.extra || [])];
}

function stackMark(photo) {
  const count = photoFiles(photo).length;
  const mark = document.createElement("span");
  mark.className = "mark stack";
  mark.textContent = String(count);
  mark.title = `carousel of ${count}`;
  return mark;
}

/* Keep one cell's dot honest without a full re-render, which would move the
   caret out of the textarea mid-sentence. */
function syncWarn(name) {
  const cell = grid.querySelector(`.cell[data-name="${CSS.escape(name)}"]`);
  if (!cell) return;
  cell.querySelector(".mark.warn")?.remove();
  const mark = warnMark(state.photos[name]);
  if (mark) cell.append(mark);
}

function baseCell(name, src) {
  const cell = document.createElement("div");
  cell.className = "cell";
  cell.dataset.name = name;
  if (selected.includes(name)) cell.classList.add("selected");
  if (name === dragName) cell.classList.add("dragging");
  const img = document.createElement("img");
  img.src = src;
  img.alt = name;
  // Not lazy: a queue is a few dozen local files at most, and lazy loading
  // leaves cells blank whenever the window isn't focused.
  img.draggable = false;
  cell.append(img);
  return cell;
}

// --- side panel -------------------------------------------------------------

function renderPanel() {
  panel.replaceChildren();
  panel.append(actionsBlock());

  const divider = document.createElement("div");
  divider.className = "divider";
  panel.append(divider);

  if (selected.length === 0) {
    panel.append(hint("Click a photo to edit its caption. ⌘-click for several."));
    return;
  }
  if (selected.length > 1) return renderBatch();

  const name = selected[0];
  if (!state.photos[name]) return renderPosted(name);
  renderQueued(name);
}

/* The one queued photo currently selected, or null — the target of "Post this
   photo now". Publishing is irreversible, so the button says which it means. */
function selectedQueued() {
  if (selected.length !== 1) return null;
  const photo = state.photos[selected[0]];
  return photo && photo.status !== "posted" ? photo.file : null;
}

function actionsBlock() {
  const wrap = document.createElement("div");
  const nextUp = nextReady();

  const line = document.createElement("p");
  line.className = "meta";
  line.textContent = nextUp
    ? `Next out: ${nextUp} — ${formatSlot(state.slots[nextUp])}`
    : "Queue is empty.";
  wrap.append(line);

  const target = selectedQueued();
  const row = document.createElement("div");
  row.className = "row";
  row.append(
    button(target ? "Post this photo now" : "Post next photo now",
      () => guard(() => postNow(target))),
    button("Save", () => guard(save), true),
  );
  wrap.append(row);
  return wrap;
}

function renderQueued(name) {
  const photo = state.photos[name];

  const heading = document.createElement("h2");
  heading.textContent = name;
  const meta = document.createElement("p");
  meta.className = "meta";
  const files = photoFiles(photo);
  meta.textContent = [
    photo.date ? photo.date.slice(0, 10) : "no EXIF date",
    files.length > 1 ? `carousel of ${files.length}` : null,
    photo.status === "hold" ? "on hold" : `posts ${formatSlot(state.slots[name])}`,
  ].filter(Boolean).join(" · ");
  panel.append(heading, meta);

  // The grid only ever shows the first photo of a carousel, so the panel is the
  // one place to check the others and their order.
  if (files.length > 1) {
    const strip = document.createElement("div");
    strip.className = "strip";
    for (const file of files) {
      const img = document.createElement("img");
      img.src = `/img/${encodeURIComponent(file)}`;
      img.alt = img.title = file;
      strip.append(img);
    }
    panel.append(strip);
  }

  panel.append(label("Caption"));
  const box = document.createElement("textarea");
  const status = document.createElement("p");
  status.className = "hint";

  const showStatus = (text, cls) => {
    status.className = `hint${cls ? " " + cls : ""}`;
    status.textContent = text ?? (photo.caption_reviewed
      ? "Saved automatically as you type."
      : "Unreviewed draft — publishes as-is if you leave it.");
  };

  box.rows = 3;
  box.value = photo.caption;
  box.placeholder = "Write the caption…";
  if (photo.caption && !photo.caption_reviewed) box.classList.add("draft");
  box.addEventListener("input", () => {
    box.classList.remove("draft");
    photo.caption = box.value;
    photo.caption_reviewed = true;
    syncWarn(name);
    showStatus("Saving…");
    // Autosave: the panel is not re-rendered on keystroke (it would move the
    // cursor), so the status line is updated by hand.
    queueSave(name, { caption: box.value }, () => showStatus("Saved.", "ok"));
  });

  showStatus();
  panel.append(box, status);

  if (state.caption_enabled) panel.append(suggestions(name, box, showStatus));

  // A lettered filename with nothing to pair with is almost always a carousel
  // that didn't take: the part before the letter has to match exactly.
  const lettered = name.match(/^(.*)([A-J])(\.jpe?g)$/i);
  if (files.length === 1 && lettered) {
    const [, base, , ext] = lettered;
    panel.append(hintWarn(
      `Not a carousel: nothing else is named ${base}?${ext}. The name before the ` +
      `letter must be identical — ${base}A${ext}, ${base}B${ext}.`,
    ));
  }

  for (const flag of photo.flags || []) {
    if (flag === "no_date") panel.append(hintWarn("No EXIF date — caption date reads “(?, ?)”."));
    if (flag === "too_small") panel.append(hintWarn("Smaller than 1028×1298 — not upscaled, margins run wide."));
  }

  const row = document.createElement("div");
  row.className = "row";
  row.append(button(photo.status === "hold" ? "Un-hold" : "Hold", () => guard(async () => {
    const next = photo.status === "hold" ? "ready" : "hold";
    await api(`/api/photo/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({ status: next }),
    });
    apply(await api("/api/photos"));
  })));
  row.append(button("Remove", () => guard(() => remove(name, files)), false, true));
  panel.append(row);
}

/* Three drafted lines, offered and never applied on their own. Clicking one
   fills the box and counts as review, exactly like typing would. */
function suggestions(name, box, showStatus) {
  const photo = state.photos[name];
  const options = Object.entries(photo.caption_options || {});
  const wrap = document.createElement("div");
  wrap.append(label("Suggestions"));

  const list = document.createElement("div");
  list.className = "options";
  for (const [voice, text] of options) {
    const el = document.createElement("button");
    el.className = `option${text === box.value ? " on" : ""}`;
    const tag = document.createElement("b");
    tag.textContent = voice;
    el.append(tag, document.createTextNode(text));
    el.addEventListener("click", () => {
      box.value = text;
      box.classList.remove("draft");
      photo.caption = text;
      photo.caption_reviewed = true;
      syncWarn(name);
      for (const other of list.children) other.classList.toggle("on", other === el);
      showStatus("Saving…");
      queueSave(name, { caption: text }, () => showStatus("Saved.", "ok"));
    });
    list.append(el);
  }
  wrap.append(options.length ? list : hint("Nothing drafted for this photo yet."));

  const row = document.createElement("div");
  row.className = "row";
  row.append(button(options.length ? "Suggest three more" : "Suggest three captions",
    () => guard(() => suggest(name))));
  wrap.append(row);
  return wrap;
}

async function suggest(name) {
  toast("Asking Claude…");
  const data = await api("/api/draft", {
    method: "POST",
    body: JSON.stringify({ files: [name] }),
  });
  const errors = data.errors || [];
  apply(data);
  if (errors.length) toast(errors.join("\n"), true);
  else toast("Three suggestions ready — click one to use it.");
}

function renderPosted(name) {
  const heading = document.createElement("h2");
  heading.textContent = name;
  panel.append(heading);
  panel.append(hint("Already posted. Sitting here so you can see the new photos against it."));
}

function renderBatch() {
  const heading = document.createElement("h2");
  const queued = selected.filter((n) => state.photos[n]);
  heading.textContent = `${selected.length} selected`;
  panel.append(heading);

  const row = document.createElement("div");
  row.className = "row";
  row.append(button("Hold all", () => guard(async () => {
    for (const name of queued) {
      await api(`/api/photo/${encodeURIComponent(name)}`, {
        method: "POST",
        body: JSON.stringify({ status: "hold" }),
      });
    }
    apply(await api("/api/photos"));
  })));
  panel.append(row);
}

function label(text) {
  const el = document.createElement("label");
  el.textContent = text;
  return el;
}
function hint(text) {
  const el = document.createElement("p");
  el.className = "hint";
  el.textContent = text;
  return el;
}
function hintWarn(text) {
  const el = hint(text);
  el.classList.add("warn");
  return el;
}
function button(text, onClick, primary = false, danger = false) {
  const el = document.createElement("button");
  el.className = `act${primary ? " primary" : ""}${danger ? " danger" : ""}`;
  el.textContent = text;
  el.addEventListener("click", onClick);
  return el;
}

/* Removing moves the photo to photos/removed/ rather than deleting it, so the
   confirmation says so — this is an undo away, and shouldn't read like a bomb. */
async function remove(name, files) {
  const what = files.length > 1 ? `${files.length} photos:\n${files.join("\n")}` : name;
  if (!confirm(`Move out of the queue?\n\n${what}\n\nThey go to photos/removed/ — drag them back into photos/raw/ to undo.`))
    return;
  const data = await api("/api/remove", {
    method: "POST",
    body: JSON.stringify({ file: name }),
  });
  selected = [];
  apply(data);
  toast(`Moved ${data.moved.length} photo(s) to photos/removed/`);
}

// --- mutations --------------------------------------------------------------

let saveTimer = null;
function queueSave(name, payload, done) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await api(`/api/photo/${encodeURIComponent(name)}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      done?.();
    } catch (e) {
      toast(e.message, true);
    }
  }, 500);
}

async function pushOrder() {
  await api("/api/order", {
    method: "POST",
    body: JSON.stringify({ order: state.order }),
  });
}

async function save() {
  toast("Saving…");
  const result = await api("/api/save", { method: "POST", body: "{}" });
  const lines = result.log.join("\n");
  if (!result.ok) toast(`${lines}\n\n${result.error}`, true);
  else toast(result.warning ? `${lines}\n\n${result.warning}` : lines);
  apply(await api("/api/photos"));
}

/* Two clicks, deliberately. The first is a dry run that proves Meta can fetch
   the image and shows exactly what would go out; only the confirm actually
   publishes. Instagram has no unpublish, so a single misclick shouldn't be
   able to put a photo on the profile. */
async function postNow(file = null) {
  const check = await api("/api/post-now", {
    method: "POST",
    body: JSON.stringify({ file }),
  });
  if (!check.ok) return toast(check.reason, true);

  const warn = (check.warnings || []).map((w) => `\n! ${w}`).join("");
  const caption = check.caption || "(no caption)";
  const what = (check.files || [check.file]).join("\n");
  if (!confirm(`Publish to Instagram right now?\n\n${what}\n${caption}\n${warn}\nThis cannot be undone.`))
    return toast("Cancelled — nothing was posted.");

  toast("Publishing…");
  const result = await api("/api/post-now", {
    method: "POST",
    body: JSON.stringify({ confirm: true, file }),
  });
  if (result.ok) {
    toast(`Live: ${result.permalink || result.file}`);
    apply(await api("/api/photos"));
  } else {
    toast(result.reason, true);
  }
}

// --- selection --------------------------------------------------------------

grid.addEventListener("click", (e) => {
  const cell = e.target.closest(".cell");
  if (!cell) return;
  const name = cell.dataset.name;
  const all = [...displayQueue(), ...state.posted];

  if (e.shiftKey && lastAnchor) {
    const a = all.indexOf(lastAnchor);
    const b = all.indexOf(name);
    selected = all.slice(Math.min(a, b), Math.max(a, b) + 1);
  } else if (e.metaKey || e.ctrlKey) {
    selected = selected.includes(name)
      ? selected.filter((n) => n !== name)
      : [...selected, name];
    lastAnchor = name;
  } else {
    selected = [name];
    lastAnchor = name;
  }
  render();
});

// --- drag to reorder (queued photos only) -----------------------------------

grid.addEventListener("dragstart", (e) => {
  const cell = e.target.closest(".cell.queued");
  if (!cell) return e.preventDefault();
  dragName = cell.dataset.name;
  cell.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragName);
});

/* Reorder by MOVING the existing cell node rather than rebuilding the grid.
   Rebuilding mid-drag detaches the element the drag started on, and a detached
   element's `drop` and `dragend` never bubble — the drag dies silently. Moving
   the node keeps it alive, and a FLIP pass slides its neighbours aside. */
function slideTo(targetCell) {
  const el = grid.querySelector(`.cell[data-name="${CSS.escape(dragName)}"]`);
  if (!el || !targetCell || el === targetCell) return;

  const before = new Map();
  for (const cell of grid.children) before.set(cell, cell.getBoundingClientRect());

  const cells = [...grid.children];
  if (cells.indexOf(targetCell) > cells.indexOf(el)) targetCell.after(el);
  else targetCell.before(el);

  for (const cell of grid.children) {
    const prev = before.get(cell);
    if (!prev) continue;
    const now = cell.getBoundingClientRect();
    const dx = prev.left - now.left;
    const dy = prev.top - now.top;
    if (!dx && !dy) continue;
    cell.animate(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
      { duration: 240, easing: "cubic-bezier(.2,.7,.3,1)" },
    );
  }

  // The DOM is now the source of truth for order; posting order is its reverse.
  state.order = [...grid.querySelectorAll(".cell.queued")]
    .map((c) => c.dataset.name)
    .reverse();
  reassignSlots();
  refreshBadges();
  scheduleOrderPush();
}

/* Slots belong to positions, not photos — after a reorder the same set of
   times is redealt down the new queue. */
function reassignSlots() {
  const times = Object.values(state.slots).sort();
  const out = {};
  let i = 0;
  for (const name of state.order) {
    if (state.photos[name]?.status === "ready") out[name] = times[i++];
  }
  state.slots = out;
}

function refreshBadges() {
  const nextUp = nextReady();
  for (const cell of grid.querySelectorAll(".cell.queued")) {
    const name = cell.dataset.name;
    const when = cell.querySelector(".when");
    cell.classList.toggle("next", name === nextUp);
    if (!when) continue;
    if (state.photos[name].status === "hold") when.textContent = "on hold";
    else if (name === nextUp) when.textContent = `next · ${shortSlot(state.slots[name])}`;
    else when.textContent = shortSlot(state.slots[name]) || "—";
  }
  panel.replaceChildren();
  renderPanel();
}

let orderTimer = null;
function scheduleOrderPush() {
  clearTimeout(orderTimer);
  orderTimer = setTimeout(() => guard(pushOrder), 250);
}

// Live reorder under the cursor, so neighbours slide aside as you drag.
grid.addEventListener("dragover", (e) => {
  if (!dragName) return;
  const cell = e.target.closest(".cell.queued");
  if (!cell) return;
  e.preventDefault();
  slideTo(cell);
});

/* `drop` only fires when a `dragover` handler called preventDefault, so the
   final dragover has already placed the cell. Re-applying the move here would
   shift it one slot too far. */
grid.addEventListener("drop", (e) => {
  e.preventDefault();
  endDrag();
});

document.addEventListener("dragend", endDrag);

function endDrag() {
  if (!dragName) return;
  const el = grid.querySelector(`.cell[data-name="${CSS.escape(dragName)}"]`);
  el?.classList.remove("dragging");
  dragName = null;
  scheduleOrderPush();
}

// --- misc -------------------------------------------------------------------

async function guard(fn) {
  const buttons = panel.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

let toastTimer = null;
function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 14000 : 4000);
}

guard(async () => apply(await api("/api/photos")));
