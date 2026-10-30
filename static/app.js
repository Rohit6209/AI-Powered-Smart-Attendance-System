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
  initScanner();
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
  if (tab !== "mark") stopScan();
  if (tab !== "reg") closeRegCamera();
  if (tab !== "cams") closeCamView();
  if (tab === "mark") scanner.openCamera().catch(camError);
  if (tab === "reg") openRegCamera();
  if (tab === "cams") loadCams();
  if (tab === "rep") loadReports();
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
function camError() { showResult("Allow camera access in your browser (HTTPS or localhost is required).", "bad"); }
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
  try { regStream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } } }); $("#vidReg").srcObject = regStream; }
  catch { $("#regMsg").textContent = "Allow camera access in your browser."; }
}
function closeRegCamera() { if (regStream) regStream.getTracks().forEach(t => t.stop()); regStream = null; }
function snap(video, w = 720) {
  const c = document.createElement("canvas"), k = Math.min(w, video.videoWidth);
  c.width = k; c.height = Math.round(k * video.videoHeight / video.videoWidth);
  c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.85);
}
$("#regBtn").onclick = async () => {
  const roll = $("#rRoll").value.trim(), name = $("#rName").value.trim(), msg = $("#regMsg");
  if (!roll || !name) { msg.textContent = "Roll number and name are required."; return; }
  $("#regBtn").disabled = true; msg.textContent = "Capturing... look at the camera and turn your head slightly";
  try {
    const frames = []; for (let i = 0; i < 5; i++) { frames.push(snap($("#vidReg"))); await sleep(300); }
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
  $("#logTable").innerHTML = "<tr><th>Date</th><th>Time</th><th>Subject</th><th>Roll</th><th>Name</th></tr>" +
    (log.map(r => `<tr><td>${r.date}</td><td>${r.time}</td><td>${esc(r.subject)}</td><td>${esc(r.roll)}</td><td>${esc(r.name)}</td></tr>`).join("")
      || "<tr><td colspan=5>No records for this filter.</td></tr>");
  const thr = +$("#thr").value;
  $("#sumTable").innerHTML = "<tr><th>Roll</th><th>Name</th><th>Present</th><th>Total classes</th><th>Percent</th></tr>" +
    sum.students.map(s => `<tr><td>${esc(s.roll)}</td><td>${esc(s.name)}</td><td>${s.present}</td><td>${s.total}</td>
      <td class="${s.total && s.percent < thr ? "low" : ""}">${s.percent}%</td></tr>`).join("");
  drawChart(sum.trend);
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

boot();
