let scanner = null, regStream = null, camTimer = null, viewCam = null;
const sessionSeen = new Set();

// ---------- Auth ----------
async function boot() {
  try { showApp(await api("/api/me")); } catch { $("#loginView").hidden = false; }
}
function showApp(u) {
  $("#loginView").hidden = true; $("#appView").hidden = false;
  $("#who").textContent = `${u.user} (${u.role})`;
  $("#cName").closest(".panel").querySelector("#camAdd").hidden = u.role !== "admin";
  $("#settingsTab").hidden = u.role !== "admin";
  initScanner();
  loadLeaveBadge(); setInterval(loadLeaveBadge, 60000);
}
$("#loginBtn").onclick = async () => {
  try { showApp(await post("/api/login", { username: $("#lu").value, password: $("#lp").value })); }
  catch (e) { $("#loginErr").textContent = e.message; }
};
$("#lp").onkeydown = e => { if (e.key === "Enter") $("#loginBtn").click(); };
$("#logoutBtn").onclick = async () => { await post("/api/logout", {}); location.reload(); };

// ---------- Tabs ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = async () => {
  const tab = t.dataset.tab;
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  document.querySelectorAll(".pane").forEach(p => p.hidden = p.id !== tab);
  if (tab !== "mark") { stopScan(); scanner.closeCamera(); }   // phones allow only ONE camera stream at a time
  if (tab !== "reg") closeRegCamera();
  if (tab !== "cams") closeCamView();
  if (tab === "mark" || tab === "reg") await sleep(250);        // let the camera hardware be released
  if (tab === "mark") scanner.openCamera().then(() => setLive(false, "Camera ready")).catch(camError);
  if (tab === "reg") openRegCamera();
  if (tab === "cams") loadCams();
  if (tab === "rep") loadReports();
  if (tab === "leaves") loadLeaves();
  if (tab === "settings") loadSettings();
});

// ---------- Live multi-face scan ----------
function initScanner() {
  scanner = new FaceScanner({
    video: $("#vidMark"), canvas: $("#ovMark"), stage: $("#stageMark"), width: 720,
    payload: () => ({ mode: "class", subject: $("#subject").value.trim() }),
    onFaces: handleFaces, onError: e => showResult(e.message, "bad"),
  });
  scanner.openCamera().then(fillCamSelect).catch(camError);
}
function camMsg(e) {
  const n = e && e.name;
  if (n === "NotAllowedError") return "Camera permission blocked. Tap the lock icon in the address bar > Permissions > Camera > Allow, then reload.";
  if (n === "NotReadableError") return "Camera is being used by another app/tab. Close it and try again.";
  if (n === "NotFoundError") return "No camera found on this device.";
  return "Camera error: " + ((e && e.message) || "unknown") + " (HTTPS or localhost is required).";
}
function camError(e) { showResult(camMsg(e), "bad"); setLive(false, "Camera off"); }
async function fillCamSelect() {
  const devs = await listVideoDevices(), sel = $("#camSel");
  sel.innerHTML = devs.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
  sel.hidden = devs.length < 2;
}
$("#camSel").onchange = async e => {
  scanner.deviceId = e.target.value;
  const was = scanner.on; scanner.stop();
  await scanner.openCamera().catch(camError);
  if (was) scanner.start();
};
function showResult(msg, cls = "") { const b = $("#result"); b.className = "result " + cls; b.textContent = msg; }
function setLive(on, txt) { $("#liveChip").classList.toggle("live", on); $("#liveTxt").textContent = txt; }

async function startScan() {
  if (!$("#subject").value.trim()) { showResult("Enter a subject first.", "bad"); return; }
  try { await scanner.start(); } catch { camError(); return; }
  $("#scanBtn").textContent = "Stop scanning"; setLive(true, "Scanning"); showResult("Scanning — everyone in view is checked together.");
}
function stopScan() {
  if (!scanner) return;
  scanner.stop(); $("#scanBtn").textContent = "Start scanning"; setLive(false, "Paused");
}
$("#scanBtn").onclick = () => scanner && scanner.on ? stopScan() : startScan();

function handleFaces(faces) {
  const marked = faces.filter(f => f.status === "marked" || f.status === "already");
  $("#stFaces").textContent = faces.length;
  $("#stWait").textContent = faces.filter(f => f.status === "verifying").length;
  $("#stMarked").textContent = sessionSeen.size;
  for (const f of faces) {
    if (f.status === "marked" && !sessionSeen.has(f.id)) {
      sessionSeen.add(f.id); $("#stMarked").textContent = sessionSeen.size;
      $("#sessionList").insertAdjacentHTML("afterbegin", `<li>${avatar(f)}<div class="grow"><b>${esc(f.name)}</b><span>${esc(f.roll)} · ${esc(f.time || "")}</span></div><span class="badge ok">Present</span></li>`);
      showResult(`${f.name} (${f.roll}) — marked present`, "marked");
    }
  }
  if (!faces.length) setLive(true, "No face in view");
  else setLive(true, `${faces.length} face${faces.length > 1 ? "s" : ""} · ${marked.length} confirmed`);
  if (faces.some(f => f.status === "unknown") && !faces.some(f => f.status === "marked"))
    showResult("A face was not recognised. Register this student first.", "bad");
}
const avatar = f => f.id
  ? `<span class="avatar"><img src="/api/students/${f.id}/photo" alt="" width="38" height="38" style="width:100%;height:100%;object-fit:cover" onerror="this.remove()">${esc(initials(f.name))}</span>`
  : `<span class="avatar">?</span>`;

// ---------- CCTV / gate cameras ----------
async function loadCams() {
  const rows = await api("/api/cameras").catch(() => []);
  $("#camList").innerHTML = rows.map(c => `<li>
      <span class="dot ${c.running ? (c.error ? "err" : "on") : ""}"></span>
      <div class="grow"><b>${esc(c.name)} <span class="badge ${c.mode === "gate" ? "warn" : ""}">${c.mode === "gate" ? "Gate" : "Class: " + esc(c.subject || "-")}</span></b>
        <span>${esc(c.url)}${c.error ? " · " + esc(c.error) : ""}</span></div>
      ${c.running ? `<button class="ghost" onclick="viewCamera(${c.id}, '${esc(c.name).replace(/'/g, "")}')">Preview</button>
                     <button class="ghost" onclick="camAction(${c.id},'stop')">Stop</button>`
                  : `<button onclick="camStart(${c.id}, '${c.mode}', '${esc(c.subject).replace(/'/g, "")}')">Start</button>`}
      ${c.mode === "gate" ? `<a class="btn ghost" href="/gate?cam=${c.id}" target="_blank" rel="noopener">Gate screen</a>` : ""}
      <button class="danger" onclick="camDelete(${c.id})">Delete</button></li>`).join("")
    || '<li class="muted">No cameras yet. Add an RTSP/IP camera above, or use "Open gate screen" with a webcam.</li>';
}
$("#cMode").onchange = () => $("#cSub").hidden = $("#cMode").value === "gate";
$("#camAdd").onclick = async () => {
  try {
    await post("/api/cameras", { name: $("#cName").value, url: $("#cUrl").value, mode: $("#cMode").value, subject: $("#cSub").value });
    $("#camMsg").textContent = "Camera added."; ["#cName", "#cUrl", "#cSub"].forEach(s => $(s).value = ""); loadCams();
  } catch (e) { $("#camMsg").textContent = e.message; }
};
window.camStart = async (id, mode, subject) => {
  if (mode === "class") { subject = prompt("Subject for this camera:", subject || ""); if (!subject) return; }
  try { await post(`/api/cameras/${id}/start`, { subject }); } catch (e) { alert(e.message); }
  loadCams();
};
window.camAction = async (id, a) => { await post(`/api/cameras/${id}/${a}`, {}); closeCamView(); loadCams(); };
window.camDelete = async id => { if (confirm("Delete this camera?")) { await api("/api/cameras/" + id, { method: "DELETE" }); closeCamView(); loadCams(); } };
window.viewCamera = (id, name) => {
  closeCamView(); viewCam = id;
  $("#camViewBox").hidden = false; $("#camViewTitle").textContent = name;
  $("#camFeed").src = `/api/cameras/${id}/stream?t=${Date.now()}`;
  const poll = async () => {
    try {
      const s = await api(`/api/cameras/${id}/state`);
      $("#camFaces").innerHTML = s.faces.map(f => `<li>${avatar(f)}<div class="grow"><b>${esc(f.name || "Unknown")}</b><span>${esc(f.roll || "")}</span></div>
        <span class="badge ${f.status === "marked" ? "ok" : f.status === "verifying" ? "warn" : f.status === "unknown" ? "bad" : ""}">${STATUS[f.status].label}</span></li>`).join("")
        || `<li class="muted">${esc(s.error || "No faces in view")}</li>`;
    } catch {}
  };
  poll(); camTimer = setInterval(poll, 1000);
};
function closeCamView() {
  clearInterval(camTimer); camTimer = null; viewCam = null;
  $("#camFeed").removeAttribute("src"); $("#camViewBox").hidden = true;
}
$("#camViewClose").onclick = closeCamView;

// ---------- Register ----------
async function openRegCamera() {
  const chip = $("#regChip"), v = $("#vidReg");
  chip.textContent = "Starting camera..."; closeRegCamera();
  try {
    regStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } } });
    v.srcObject = regStream;
    if (v.readyState < 1) await new Promise(r => v.addEventListener("loadedmetadata", r, { once: true }));
    await v.play().catch(() => {});
    if (v.videoWidth) v.parentElement.style.aspectRatio = `${v.videoWidth}/${v.videoHeight}`;
    chip.textContent = "Camera ready";
    clearInterval(lightTimer); lightTimer = setInterval(() => checkLight(v, chip), 700);
  } catch (e) { chip.textContent = camMsg(e) + " (tap to retry)"; }
}
let lightTimer = null;
const lightCv = document.createElement("canvas");
function checkLight(v, chip) {                       // live brightness hint while registering
  if (!v.videoWidth || !regStream) return;
  lightCv.width = 32; lightCv.height = 24;
  const g = lightCv.getContext("2d", { willReadFrequently: true }); g.drawImage(v, 0, 0, 32, 24);
  const px = g.getImageData(0, 0, 32, 24).data; let sum = 0;
  for (let i = 0; i < px.length; i += 4) sum += px[i] * 0.299 + px[i + 1] * 0.587 + px[i + 2] * 0.114;
  const m = sum / (px.length / 4);
  chip.textContent = m < 55 ? "Too dark - face a light" : m < 85 ? "Low light - face a light if possible" : "Camera ready";
}
$("#regChip").onclick = openRegCamera;
function closeRegCamera() { clearInterval(lightTimer); if (regStream) regStream.getTracks().forEach(t => t.stop()); regStream = null; }
function snap(video, w = 560) {
  const c = document.createElement("canvas"), k = Math.min(w, video.videoWidth);
  c.width = k; c.height = Math.round(k * video.videoHeight / video.videoWidth);
  c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.8);
}
$("#regBtn").onclick = async () => {
  const roll = $("#rRoll").value.trim(), name = $("#rName").value.trim(), msg = $("#regMsg");
  if (!roll || !name) { msg.textContent = "Roll number and name are required."; return; }
  $("#regBtn").disabled = true; msg.textContent = "Capturing... look straight at the camera";
  try {
    const frames = []; for (let i = 0; i < 4; i++) { frames.push(snap($("#vidReg"))); await sleep(120); }
    msg.textContent = "Checking face...";
    await post("/api/students", { roll, name, email: $("#rEmail").value, parent_email: $("#rParent").value, password: $("#rPass").value, frames });
    msg.textContent = `${name} registered.`;
    ["#rRoll", "#rName", "#rEmail", "#rParent", "#rPass"].forEach(s => $(s).value = "");
    loadStudents();
  } catch (e) { msg.textContent = e.message; }
  $("#regBtn").disabled = false;
};
async function loadStudents() {
  const rows = await api("/api/students");
  $("#stuTable").innerHTML = "<tr><th>Student</th><th>Roll</th><th>Email</th><th>Parent email</th><th></th></tr>" +
    (rows.map(s => `<tr><td><span class="avatar">${s.has_photo ? `<img src="/api/students/${s.id}/photo" alt="" style="width:100%;height:100%;object-fit:cover">` : esc(initials(s.name))}</span>${esc(s.name)}</td>
      <td>${esc(s.roll)}</td><td>${esc(s.email)}</td><td>${esc(s.parent_email)}</td>
      <td><button class="ghost" onclick="resetPw(${s.id})">Reset password</button> <button class="danger" onclick="delStu(${s.id})">Delete</button></td></tr>`).join("")
      || "<tr><td colspan=5>No students yet. Register the first one above.</td></tr>");
}
window.delStu = async id => {
  if (!confirm("Delete this student and their attendance?")) return;
  await api("/api/students/" + id, { method: "DELETE" }); loadStudents();
};
window.resetPw = async id => {
  const p = prompt("New password for this student:");
  if (!p) return;
  try { await post(`/api/students/${id}/password`, { password: p }); alert("Password updated."); }
  catch (e) { alert(e.message); }
};
const origTab = document.querySelector('[data-tab="reg"]');
origTab.addEventListener("click", loadStudents);

// ---------- Reports ----------
const qs = () => `date=${$("#fDate").value}&subject=${encodeURIComponent($("#fSub").value)}`;
async function loadReports() {
  const [log, sum] = await Promise.all([api("/api/attendance?" + qs()), api("/api/summary")]);
  $("#logTable").innerHTML = "<tr><th>Date</th><th>Time</th><th>Subject</th><th>Roll</th><th>Name</th><th>Method</th></tr>" +
    (log.map(r => `<tr><td>${r.date}</td><td>${r.time}</td><td>${esc(r.subject)}</td><td>${esc(r.roll)}</td><td>${esc(r.name)}</td><td>${r.method === "location" ? "Location" : "Face"}</td></tr>`).join("")
      || "<tr><td colspan=6>No records for this filter.</td></tr>");
  const thr = +$("#thr").value;
  $("#sumTable").innerHTML = "<tr><th>Roll</th><th>Name</th><th>Present</th><th>Total classes</th><th>Percent</th></tr>" +
    sum.students.map(s => `<tr><td>${esc(s.roll)}</td><td>${esc(s.name)}</td><td>${s.present}</td><td>${s.total}</td>
      <td class="${s.total && s.percent < thr ? "low" : ""}">${s.percent}%</td></tr>`).join("");
  drawChart(sum.trend);
  loadDay();
}
function drawChart(data) {
  const c = $("#chart"), g = c.getContext("2d"), css = getComputedStyle(document.body);
  g.clearRect(0, 0, c.width, c.height);
  const max = Math.max(1, ...data.map(d => d.count)), bw = c.width / data.length;
  g.font = "13px system-ui,sans-serif"; g.textAlign = "center";
  data.forEach((d, i) => {
    const h = (d.count / max) * (c.height - 50), x = i * bw + bw * 0.2;
    g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--brand");
    g.beginPath(); g.roundRect(x, c.height - 30 - h, bw * 0.6, Math.max(h, 2), 6); g.fill();
    g.fillStyle = css.color;
    g.fillText(d.count, x + bw * 0.3, c.height - 36 - h);
    g.fillText(d.date, x + bw * 0.3, c.height - 10);
  });
}
async function loadDay() {
  const date = $("#dDate").value || new Date().toLocaleDateString("en-CA");
  $("#dDate").value = date;
  try {
    const r = await api("/api/daily?date=" + date);
    $("#dayCounts").textContent = r.working ? `Present ${r.counts.present} · Absent ${r.counts.absent} · On leave ${r.counts.leave}` : "No attendance was recorded on this day (holiday?).";
    const cls = { present: "ok", absent: "bad", leave: "warn" }, lbl = { present: "Present", absent: "Absent", leave: "On leave" };
    $("#dayTable").innerHTML = "<tr><th>Roll</th><th>Name</th><th>Status</th><th>Details</th></tr>" +
      (r.rows.map(x => `<tr><td>${esc(x.roll)}</td><td>${esc(x.name)}</td><td><span class="badge ${cls[x.status] || ""}">${lbl[x.status] || "-"}</span></td><td>${esc(x.info)}</td></tr>`).join("")
        || "<tr><td colspan=4>No students yet.</td></tr>");
  } catch (e) { $("#dayCounts").textContent = e.message; }
}
$("#dayBtn").onclick = loadDay;
$("#loadBtn").onclick = loadReports;
$("#csvBtn").onclick = () => location.href = "/api/export?" + qs();
$("#alertBtn").onclick = async () => {
  try {
    const r = await post("/api/alerts", { threshold: $("#thr").value });
    $("#alertMsg").textContent = r.low.length
      ? `Low attendance: ${r.low.join(", ")}. ` + (r.smtp_configured ? `${r.emails_sent} email(s) sent.` : "SMTP is not configured, so no emails were sent (see README).")
      : "All students are above the threshold.";
  } catch (e) { $("#alertMsg").textContent = e.message; }
};

// ---------- Leaves (teacher / admin approval) ----------
async function loadLeaveBadge() {
  try {
    const r = await api("/api/leaves?status=pending"), b = $("#leaveBadge");
    b.textContent = r.pending; b.hidden = !r.pending;
  } catch {}
}
async function loadLeaves() {
  const r = await api("/api/leaves?status=" + $("#leaveFilter").value).catch(() => ({ leaves: [], pending: 0 }));
  const cls = { pending: "warn", approved: "ok", rejected: "bad" };
  $("#leaveList").innerHTML = r.leaves.map(l => {
    const days = Math.round((new Date(l.to_date) - new Date(l.from_date)) / 864e5) + 1;
    return `<li><div class="grow" style="white-space:normal"><b>${esc(l.name)} <span class="muted" style="display:inline">(${esc(l.roll)})</span>
        <span class="badge ${cls[l.status]}">${esc(l.status)}</span></b>
      <span style="white-space:normal">${esc(l.from_date)}${l.to_date !== l.from_date ? " to " + esc(l.to_date) : ""} · ${days} day${days > 1 ? "s" : ""} · applied ${esc(l.applied_at)}</span>
      <span style="white-space:normal">Reason: ${esc(l.reason)}</span>
      ${l.decided_by ? `<span style="white-space:normal">By ${esc(l.decided_by)}${l.remark ? " — " + esc(l.remark) : ""}</span>` : ""}</div>
      <div class="row" style="margin:0">
        ${l.status !== "approved" ? `<button onclick="decideLeave(${l.id},'approved')">Approve</button>` : ""}
        ${l.status !== "rejected" ? `<button class="danger" onclick="decideLeave(${l.id},'rejected')">Reject</button>` : ""}
      </div></li>`;
  }).join("") || '<li class="muted">No leave requests here.</li>';
  const b = $("#leaveBadge"); if ($("#leaveFilter").value === "pending") { b.textContent = r.pending; b.hidden = !r.pending; }
}
$("#leaveFilter").onchange = loadLeaves;
window.decideLeave = async (id, status) => {
  let remark = "";
  if (status === "rejected") { remark = prompt("Reason for rejecting (optional):", "") ; if (remark === null) return; }
  try { await post(`/api/leaves/${id}/decide`, { status, remark }); } catch (e) { alert(e.message); }
  loadLeaves(); loadLeaveBadge();
};

// ---------- Settings (admin) ----------
let map = null, pin = null, circle = null, picked = null;
function drawCircle() {
  if (!map || !picked) return;
  const r = Math.max(20, +$("#locRadius").value || 150);
  if (!circle) circle = L.circle([picked.lat, picked.lng], { radius: r, color: "#0369a1", fillOpacity: 0.15 }).addTo(map);
  else circle.setLatLng([picked.lat, picked.lng]).setRadius(r);
}
function setPoint(lat, lng, pan = true) {
  picked = { lat, lng };
  if (!pin) {
    pin = L.marker([lat, lng], { draggable: true }).addTo(map);
    pin.on("dragend", () => { const p = pin.getLatLng(); setPoint(p.lat, p.lng, false); });
  } else pin.setLatLng([lat, lng]);
  drawCircle();
  if (pan) map.setView([lat, lng], Math.max(map.getZoom(), 17));
  $("#locTxt").textContent = `Picked: ${lat.toFixed(6)}, ${lng.toFixed(6)}`;
}
async function loadSettings() {
  let s; try { s = await api("/api/settings"); } catch (e) { $("#setMsg").textContent = e.message; return; }
  $("#locOn").checked = s.loc_enabled; $("#locRadius").value = Math.round(s.loc_radius);
  $("#lowLight").checked = s.low_light_boost; $("#reqMove").checked = s.require_move;
  $("#selfMark").checked = s.self_mark; $("#locOnly").checked = s.allow_loc_only;
  $("#tol").value = String(s.tolerance <= 0.47 ? 0.45 : s.tolerance >= 0.53 ? 0.56 : 0.5);
  if (typeof L === "undefined") { $("#map").innerHTML = '<p class="muted" style="padding:1rem">Map could not load (check internet).</p>'; }
  else {
    if (!map) {
      map = L.map("map").setView([22.97, 78.66], 5);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
      map.on("click", e => setPoint(e.latlng.lat, e.latlng.lng, false));
    }
    if (s.loc_lat != null && s.loc_lng != null) setPoint(s.loc_lat, s.loc_lng);
    setTimeout(() => map.invalidateSize(), 100);
  }
  loadTeachers();
}
$("#locRadius").oninput = drawCircle;
$("#mapMe").onclick = () => {
  if (!map) return;
  $("#locTxt").textContent = "Getting your location...";
  navigator.geolocation.getCurrentPosition(p => setPoint(p.coords.latitude, p.coords.longitude),
    () => { $("#locTxt").textContent = "Could not get your location. Allow location access."; }, { enableHighAccuracy: true, timeout: 20000 });
};
$("#mapSearch").onclick = async () => {
  const q = $("#mapQ").value.trim(); if (!q || !map) return;
  try {
    const r = await (await fetch("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + encodeURIComponent(q))).json();
    if (!r.length) { $("#locTxt").textContent = "Place not found"; return; }
    setPoint(+r[0].lat, +r[0].lon);
  } catch { $("#locTxt").textContent = "Search failed (check internet)"; }
};
$("#saveSettings").onclick = async () => {
  try {
    await post("/api/settings", { loc_enabled: $("#locOn").checked, loc_lat: picked && picked.lat, loc_lng: picked && picked.lng,
      loc_radius: $("#locRadius").value, low_light_boost: $("#lowLight").checked, require_move: $("#reqMove").checked, tolerance: $("#tol").value,
      self_mark: $("#selfMark").checked, allow_loc_only: $("#locOnly").checked });
    $("#setMsg").textContent = "Settings saved.";
  } catch (e) { $("#setMsg").textContent = e.message; }
};
async function loadTeachers() {
  const t = await api("/api/teachers").catch(() => []);
  $("#teacherList").innerHTML = t.map(u => `<li><div class="grow"><b>${esc(u)}</b></div><button class="danger" onclick="delTeacher('${esc(u)}')">Remove</button></li>`).join("")
    || '<li class="muted">No teachers yet.</li>';
}
$("#tAdd").onclick = async () => {
  try {
    await post("/api/teachers", { username: $("#tUser").value.trim(), password: $("#tPass").value });
    $("#tMsg").textContent = "Teacher added."; $("#tUser").value = $("#tPass").value = ""; loadTeachers();
  } catch (e) { $("#tMsg").textContent = e.message; }
};
window.delTeacher = async u => { if (confirm(`Remove teacher "${u}"?`)) { await api("/api/teachers/" + encodeURIComponent(u), { method: "DELETE" }); loadTeachers(); } };
$("#myPwBtn").onclick = async () => {
  try {
    await post("/api/me/password", { old: $("#myOld").value, new: $("#myNew").value });
    $("#myPwMsg").textContent = "Password updated."; $("#myOld").value = $("#myNew").value = "";
  } catch (e) { $("#myPwMsg").textContent = e.message; }
};

boot();
