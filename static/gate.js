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
    loadToday(); renderProfile(null);
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
  if (!faces.length) { setChip("Waiting for someone...", true); if (now > holdUntil) renderProfile(null); return; }
  setChip(`${faces.length} face${faces.length > 1 ? "s" : ""} in view`, true);
  const area = f => f.box[2] * f.box[3];
  const best = [...faces].sort((a, b) => rank[b.status] - rank[a.status] || area(b) - area(a))[0];

  for (const f of faces) if (f.status === "marked" && !announced.has(f.id)) newEntry(f);

  if (best.status === "unknown") {
    if (now > holdUntil) renderProfile(best);
    return;
  }
  // do not let a "verifying" face replace a freshly marked profile while it is still on screen
  if (best.status === "verifying" && now < holdUntil && shownStatus === "marked") return;
  renderProfile(best); holdUntil = now + HOLD_MS;
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

// ---------- profile card ----------
const BANNER = { marked: "✓ Attendance marked", already: "Already marked today", verifying: "Verifying… move your head slightly",
                 unknown: "Face not recognised", idle: "Look at the camera" };
function renderProfile(f) {
  const el = $("#profile"), status = f ? f.status : "idle";
  if (f && f.id === shownId && status === shownStatus) { updateRing(f); return; }   // nothing changed: avoid flicker
  shownId = f ? f.id : null; shownStatus = status;
  el.className = "profile " + status + (f && f.id ? " pop" : "");
  if (!f) { el.innerHTML = `<span class="banner">${BANNER.idle}</span><div class="photo">?</div><h2>Welcome</h2><p class="roll">Stand in front of the camera to mark your attendance</p>`; return; }
  if (!f.id) { el.innerHTML = `<span class="banner">${BANNER.unknown}</span><div class="photo">?</div><h2>Unknown</h2><p class="roll">Please contact the administrator</p>`; return; }
  const st = f.stats || {}, pct = st.percent ?? 0, low = pct < 75;
  el.innerHTML = `<span class="banner">${BANNER[status]}</span>
    <div class="photo"><img src="/api/students/${f.id}/photo" alt="" onerror="this.remove()"><span>${esc(initials(f.name))}</span></div>
    <h2>${esc(f.name)}</h2><p class="roll">Roll no. ${esc(f.roll)}</p>
    <div class="meta">
      ${f.time ? `<div><b>${esc(f.time)}</b><span>Entry time</span></div>` : ""}
      ${f.stats ? `<div class="ring" id="ring" style="--p:${pct};--c:${low ? "#ef4444" : "#22c55e"}"><em>${pct}%</em></div><div><b>${st.present}/${st.total}</b><span>Attendance</span></div>` : ""}
    </div>`;
}
function updateRing(f) {
  const r = $("#ring"); if (!r || !f.stats) return;
  r.style.setProperty("--p", f.stats.percent); r.firstChild.textContent = f.stats.percent + "%";
}
