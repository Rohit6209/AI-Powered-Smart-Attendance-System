const HOLD_MS = 5000;       // how long a profile stays on screen after the person leaves
let scanner = null, remoteId = null, pollTimer = null, audio = null;
let announced = new Set(), holdUntil = 0, shownId = null, shownStatus = "", devices = [], devIdx = 0, todayCount = 0;

// ---------- clock ----------
function tick() {
  const d = new Date();
  $("#clock").firstChild.nodeValue = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("#date").textContent = d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}
setInterval(tick, 1000); tick();

// ---------- start screen ----------
(async function init() {
  const qCam = new URLSearchParams(location.search).get("cam");
  try { await api("/api/me"); } catch {
    $("#startMsg").textContent = "Please sign in first (admin or teacher account).";
    $("#startBtn").hidden = true; $("#srcSel").hidden = true; $("#adminLink").hidden = false; return;
  }
  try {
    const cams = (await api("/api/cameras")).filter(c => c.mode === "gate");
    $("#srcSel").insertAdjacentHTML("beforeend", cams.map(c => `<option value="${c.id}">${esc(c.name)} (CCTV, ${c.running ? "running" : "stopped"})</option>`).join(""));
    if (qCam) $("#srcSel").value = qCam;
  } catch {}
})();
$("#startBtn").onclick = async () => {
  $("#startErr").textContent = "";
  const v = $("#srcSel").value;
  try {
    if (v === "local") await startLocal(); else await startRemote(+v);
    $("#startOverlay").hidden = true; $("#shell").hidden = false;
    audio = new (window.AudioContext || window.webkitAudioContext)();
    loadToday(); hideProfile();
  } catch (e) { $("#startErr").textContent = e.name === "NotAllowedError" ? "Camera permission denied. Allow it (HTTPS or localhost required)." : e.message; }
};
$("#fsBtn").onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {});

// ---------- sources ----------
async function startLocal() {
  $("#remote").hidden = true; $("#vid").hidden = false;
  scanner = new FaceScanner({ video: $("#vid"), canvas: $("#ov"), stage: $("#stage"), width: 640, gap: 80,
    payload: () => ({ mode: "gate" }), onFaces, onError: e => setChip(e.message, false) });
  await scanner.openCamera();
  devices = await listVideoDevices(); $("#swapBtn").hidden = devices.length < 2;
  scanner.start(); setChip("Scanning — please look at the camera", true);
}
$("#swapBtn").onclick = async () => {
  devIdx = (devIdx + 1) % devices.length; scanner.deviceId = devices[devIdx].deviceId;
  scanner.stop(); await scanner.openCamera().catch(() => {}); scanner.start();
};
async function startRemote(id) {            // server-side CCTV/IP camera: server draws the boxes in the stream
  remoteId = id;
  const s = await api(`/api/cameras/${id}/state`);
  if (!s.running) await post(`/api/cameras/${id}/start`, {});
  $("#vid").hidden = true; $("#ov").hidden = true; $("#remote").hidden = false;
  $("#remote").src = `/api/cameras/${id}/stream?t=${Date.now()}`;
  pollTimer = setInterval(async () => {
    try { const r = await api(`/api/cameras/${id}/state`); onFaces(r.faces); if (r.error) setChip(r.error, false); }
    catch (e) { setChip(e.message, false); }
  }, 600);
  setChip("CCTV connected", true);
}
function setChip(t, ok) { $("#chipTxt").textContent = t; $("#chip").classList.toggle("live", ok); }

// ---------- face events ----------
const rank = { marked: 3, already: 2, verifying: 1, unknown: 0 };
function onFaces(faces) {
  const now = Date.now();
  if (!faces.length) { setChip("Waiting for someone...", true); if (now > holdUntil) hideProfile(); return; }
  const area = f => f.box[2] * f.box[3];
  const best = [...faces].sort((a, b) => rank[b.status] - rank[a.status] || area(b) - area(a))[0];

  for (const f of faces) if (f.status === "marked" && !announced.has(f.id)) newEntry(f);

  if (best.status === "marked" || best.status === "already") {
    setChip(best.status === "marked" ? "Attendance marked" : "Already marked today", true);
    renderProfile(best); holdUntil = now + HOLD_MS;           // profile card appears right next to the face
  } else {
    setChip(best.status === "verifying" ? (best.need_move ? `${best.name}: move your head slightly to confirm` : `${best.name}: verifying, keep looking at the camera`) : "Face not recognised", best.status === "verifying");
    if (now > holdUntil) hideProfile();                          // boxes/labels on the camera already show the state
  }
}
function newEntry(f) {
  announced.add(f.id); todayCount++; $("#todayCount").textContent = todayCount;
  $("#recent").insertAdjacentHTML("afterbegin", recentRow(f));
  beep();
}
const recentRow = f => `<li><span class="avatar lg"><img src="/api/students/${f.id}/photo" alt="" onerror="this.remove()">${esc(initials(f.name))}</span>
  <div class="grow"><b>${esc(f.name)}</b><span>${esc(f.roll)}</span></div><span class="badge ok">${esc(f.time || "")}</span></li>`;
async function loadToday() {
  try {
    const r = await api("/api/gate/today");
    todayCount = r.entries.length; $("#todayCount").textContent = todayCount;
    r.entries.forEach(e => announced.add(e.id));
    $("#recent").innerHTML = r.entries.slice(0, 30).map(e => recentRow({ ...e })).join("");
  } catch {}
}
function beep() {
  if (!audio) return;
  try {
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(audio.destination);
    o.start(); o.stop(audio.currentTime + 0.14);
  } catch {}
}

// ---------- profile card (floats on the camera view, next to the face) ----------
const BANNER = { marked: "✓ Attendance marked", already: "Already marked today" };
function hideProfile() { const el = $("#profile"); el.hidden = true; shownId = null; shownStatus = ""; }
function placeProfile(f) {
  const el = $("#profile"), st = $("#stage"), W = st.clientWidth, H = st.clientHeight;
  if (W < 600) return;                                           // small screens: card is docked at the bottom by CSS
  let [x, y, w] = f.box; if ($("#remote").hidden) x = 1 - x - w;  // local camera is mirrored; CCTV stream is not
  const cw = el.offsetWidth, ch = el.offsetHeight, gap = 14;
  let left = (x + w) * W + gap;
  if (left + cw > W - 8) left = x * W - cw - gap;                // not enough room on the right: put it on the left
  if (left < 8) left = Math.max(8, (W - cw) / 2);
  el.style.left = left + "px";
  el.style.top = Math.min(Math.max(8, y * H), Math.max(8, H - ch - 8)) + "px";
}
function renderProfile(f) {
  const el = $("#profile"), status = f.status;
  if (f.id === shownId && status === shownStatus && !el.hidden) { updateRing(f); placeProfile(f); return; }
  shownId = f.id; shownStatus = status; el.hidden = false;
  el.className = "profile " + status + " pop";
  const st = f.stats || {}, pct = st.percent ?? 0, low = pct < 75;
  el.innerHTML = `<span class="banner">${BANNER[status]}</span>
    <div class="photo"><img src="/api/students/${f.id}/photo" alt="" onerror="this.remove()"><span>${esc(initials(f.name))}</span></div>
    <h2>${esc(f.name)}</h2><p class="roll">Roll no. ${esc(f.roll)}</p>
    <div class="meta">
      ${f.time ? `<div><b>${esc(f.time)}</b><span>Entry time</span></div>` : ""}
      ${f.stats ? `<div class="ring" id="ring" style="--p:${pct};--c:${low ? "#ef4444" : "#22c55e"}"><em>${pct}%</em></div><div><b>${st.present}/${st.total}</b><span>Attendance</span></div>` : ""}
    </div>`;
  placeProfile(f);
}
function updateRing(f) {
  const r = $("#ring"); if (!r || !f.stats) return;
  r.style.setProperty("--p", f.stats.percent); r.firstChild.textContent = f.stats.percent + "%";
}
