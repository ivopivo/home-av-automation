"use strict";

// ---- connection --------------------------------------------------------

const devices = new Map(); // id -> {id, name, state, actions}
const pending = new Map(); // call id -> {resolve, reject}
let ws = null;
let nextId = 1;
let cameraId = null;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onmessage = (event) => handle(JSON.parse(event.data));
  ws.onclose = () => {
    for (const { reject } of pending.values()) reject(new Error("Connection to hub lost"));
    pending.clear();
    render();
    setTimeout(connect, 2000);
  };
}

function handle(msg) {
  if (msg.type === "devices") {
    devices.clear();
    for (const d of msg.devices) devices.set(d.id, d);
    fillCameraList();
    loadSnapshot();
  } else if (msg.type === "state") {
    const device = devices.get(msg.device);
    if (device) device.state[msg.name] = msg.value;
    if (msg.device !== cameraId) return;
  } else if (msg.type === "result") {
    const call = pending.get(msg.id);
    pending.delete(msg.id);
    if (call) msg.ok ? call.resolve() : call.reject(new Error(msg.error));
    return;
  }
  render();
}

function call(action, args = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !cameraId) {
    return Promise.reject(new Error("Not connected to the hub"));
  }
  const id = nextId++;
  ws.send(JSON.stringify({ type: "call", id, device: cameraId, action, args }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

function run(action, args) {
  showMessage("");
  return call(action, args).catch((err) => showMessage(err.message));
}

// ---- camera selection --------------------------------------------------

const $ = (id) => document.getElementById(id);

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private window etc. */ }
  return null;
}

function fillCameraList() {
  const cameras = [...devices.values()].filter((d) => d.actions.includes("move"));
  const select = $("camera");
  select.replaceChildren(...cameras.map((d) => new Option(d.name, d.id)));
  select.hidden = cameras.length < 2;
  const saved = storage("camera");
  cameraId = cameras.some((d) => d.id === saved) ? saved : cameras[0]?.id ?? null;
  select.value = cameraId ?? "";
}

$("camera").addEventListener("change", (e) => {
  stopMove();
  cameraId = e.target.value;
  storage("camera", cameraId);
  render();
});

// ---- rendering ---------------------------------------------------------

function render() {
  const online = ws?.readyState === WebSocket.OPEN;
  const camera = devices.get(cameraId);
  const s = camera?.state ?? {};
  const ready = online && camera && s.connected;

  const link = $("link");
  link.dataset.ok = String(Boolean(ready));
  link.textContent = !online ? "Hub offline" : !camera ? "No camera" : s.connected ? "Connected" : "Core offline";
  document.title = camera ? `${camera.name} · Camera Control` : "Camera Control";

  $("pan").textContent = s.pan_deg == null ? "–" :
    s.pan_deg === 0 ? "0°" : `${Math.abs(s.pan_deg).toFixed(1)}° ${s.pan_deg < 0 ? "left" : "right"}`;
  $("tilt").textContent = s.tilt == null ? "–" : s.tilt.toFixed(2);
  $("zoom").textContent = s.zoom == null ? "–" : `${Math.round(s.zoom * 100)}%`;
  $("status").textContent = s.status ?? "–";
  $("moving").hidden = !s.moving;
  $("privacy-chip").hidden = !s.privacy;
  $("privacy").checked = Boolean(s.privacy);

  for (const el of document.querySelectorAll(".controls button, .controls input")) {
    if (el.id !== "save-mode") el.disabled = !ready;
  }
}

function showMessage(text) {
  $("message").textContent = text;
}

// ---- hold to move ------------------------------------------------------

let activeMove = null; // {direction, button?}

function startMove(direction, button) {
  if (activeMove?.direction === direction) return;
  activeMove?.button?.classList.remove("active");
  activeMove = { direction, button };
  button?.classList.add("active");
  run("move", { direction });
}

function stopMove() {
  if (!activeMove) return;
  activeMove.button?.classList.remove("active");
  activeMove = null;
  run("stop_moving");
}

for (const button of document.querySelectorAll("[data-move]")) {
  button.addEventListener("pointerdown", (e) => {
    if (button.disabled) return;
    button.setPointerCapture(e.pointerId);
    startMove(button.dataset.move, button);
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(type, stopMove);
  }
  button.addEventListener("contextmenu", (e) => e.preventDefault());
}

// Arrow keys (two at once for diagonals), + / - to zoom, H for home.
const KEYS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
const held = new Set();

function keyDirection() {
  if (held.has("+")) return "zoom_in";
  if (held.has("-")) return "zoom_out";
  const v = held.has("up") ? "up" : held.has("down") ? "down" : "";
  const h = held.has("left") ? "left" : held.has("right") ? "right" : "";
  return v && h ? `${v}_${h}` : v || h || null;
}

function typing(e) {
  return e.target instanceof HTMLInputElement && e.target.type !== "checkbox";
}

document.addEventListener("keydown", (e) => {
  if (typing(e) || e.repeat) return;
  const key = KEYS[e.key] ?? (e.key === "+" || e.key === "=" ? "+" : e.key === "-" ? "-" : null);
  if (key) {
    e.preventDefault();
    held.add(key);
    const direction = keyDirection();
    if (direction) startMove(direction);
  } else if (e.key === "h" || e.key === "H") {
    run("recall_preset", { preset: "home" });
  }
});

document.addEventListener("keyup", (e) => {
  const key = KEYS[e.key] ?? (e.key === "+" || e.key === "=" ? "+" : e.key === "-" ? "-" : null);
  if (!key) return;
  held.delete(key);
  const direction = keyDirection();
  direction ? startMove(direction) : stopMove();
});

window.addEventListener("blur", () => { held.clear(); stopMove(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { held.clear(); stopMove(); }
});

// ---- turn, presets, privacy --------------------------------------------

for (const button of document.querySelectorAll("[data-turn]")) {
  button.addEventListener("click", () => run("pan_by", { degrees: Number(button.dataset.turn) }));
}

$("turn-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const degrees = Number($("turn-deg").value);
  if (Number.isFinite(degrees) && degrees !== 0) run("pan_by", { degrees });
});

$("save-mode").addEventListener("change", (e) => {
  document.querySelector(".presets").classList.toggle("saving", e.target.checked);
});

for (const button of document.querySelectorAll("[data-preset]")) {
  button.addEventListener("click", () => {
    const preset = button.dataset.preset;
    const saving = $("save-mode").checked && button.closest(".presets");
    if (!saving) return run("recall_preset", { preset });
    if (!confirm(`Overwrite preset "${preset}" with the current position?`)) return;
    run("save_preset", { preset }).then(() => {
      $("save-mode").checked = false;
      document.querySelector(".presets").classList.remove("saving");
    });
  });
}

$("privacy").addEventListener("change", (e) => run("privacy", { on: e.target.checked }));

// ---- live picture ------------------------------------------------------

const img = $("snapshot");
let snapshotTimer = null;

function nextSnapshot(delay) {
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(loadSnapshot, delay);
}

function showPicture(ok, text = "No picture") {
  img.hidden = !ok;
  $("no-picture").hidden = ok;
  $("no-picture").textContent = text;
}

function loadSnapshot() {
  clearTimeout(snapshotTimer);
  const s = devices.get(cameraId)?.state;
  if (document.hidden) return nextSnapshot(1000);
  if (!s?.snapshot_url) {
    if (devices.size) showPicture(false);
    return nextSnapshot(1000);
  }
  img.src = `/api/devices/${encodeURIComponent(cameraId)}/snapshot?t=${Date.now()}`;
}

img.addEventListener("load", () => { showPicture(true); nextSnapshot(300); });
img.addEventListener("error", () => { showPicture(false); nextSnapshot(2000); });
$("camera").addEventListener("change", () => { showPicture(false, "Loading…"); loadSnapshot(); });

connect();
render();
loadSnapshot();
