const $ = s => document.querySelector(s);
const LIMIT = 75;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const initials = n => String(n || "?").trim().split(/\s+/).slice(0, 2).map(x => x[0]).join("").toUpperCase();

async function api(url, opts = {}) {
  const r = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(d.error || "Something went wrong"); e.code = d.code; throw e; }
  return d;
}
const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body) });
const avatarHtml = name => `<img src="/api/student/photo" alt="" onerror="this.remove()">${esc(initials(name))}`;

let ME = null, CFG = { loc_enabled: false, self_mark: true, loc_only: false };

async function boot() {
  try { ME = await api("/api/student/me"); CFG = await api("/api/public-config").catch(() => CFG); render(ME); }
  catch { $("#loginView").hidden = false; $("#appView").hidden = true; }
}

// ---------- navigation ----------
document.querySelectorAll("#stuNav .tab").forEach(t => t.onclick = () => {
  const p = t.dataset.p;
  document.querySelectorAll("#stuNav .tab").forEach(x => x.classList.toggle("active", x === t));
  document.querySelectorAll(".spane").forEach(x => x.hidden = x.id !== p);
  if (p !== "home") closeMark();
  window.scrollTo({ top: 0 });
});

function render(d) {
  $("#loginView").hidden = true; $("#appView").hidden = false;
  const p = d.profile, h = new Date().getHours();
  $("#who").textContent = p.name;
  for (const id of ["#hdrAv", "#heroAv", "#profAv"]) $(id).innerHTML = avatarHtml(p.name);
  $("#greet").textContent = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  $("#heroName").textContent = p.name; $("#heroRoll").textContent = "Roll no. " + p.roll;
  $("#pName").textContent = p.name; $("#pRoll").textContent = "Roll no. " + p.roll;
  $("#iName").textContent = p.name; $("#iRoll").textContent = p.roll;
  $("#pFace").hidden = !p.has_photo;
  $("#eMail").value = p.email || ""; $("#eParent").value = p.parent_email || "";
  setToday(d.today);

  const pct = $("#pct");
  if (!d.total) {
    pct.textContent = "-"; pct.className = "stat";
    $("#pctMsg").textContent = "No classes have been held yet.";
  } else {
    const low = d.percent < LIMIT;
    pct.textContent = d.percent + "%"; pct.className = "stat " + (low ? "low" : "ok");
    const need = Math.max(0, Math.ceil(3 * d.total - 4 * d.present));
    $("#pctMsg").textContent = low ? `Below ${LIMIT}%. Attend the next ${need} class(es) in a row to reach ${LIMIT}%.` : `You are above the ${LIMIT}% requirement.`;
  }
  $("#counts").textContent = `Present in ${d.present} of ${d.total} classes`;
  $("#hPct").textContent = d.total ? d.percent + "%" : "-"; $("#hCnt").textContent = `${d.present}/${d.total}`;

  $("#subTable").innerHTML = "<tr><th>Subject</th><th>Present</th><th>Total</th><th>Percent</th><th></th></tr>" +
    (d.subjects.map(s => `<tr><td>${esc(s.subject)}</td><td>${s.present}</td><td>${s.total}</td>
      <td class="${s.percent < LIMIT ? "low" : ""}">${s.percent}%</td>
      <td><div class="bar"><i class="${s.percent < LIMIT ? "low" : ""}" style="width:${s.percent}%"></i></div></td></tr>`).join("")
      || "<tr><td colspan=5>No attendance data yet.</td></tr>");
  const sel = $("#subSel"), cur = sel.value;
  sel.innerHTML = '<option value="">All subjects</option>' + d.subjects.map(s => `<option>${esc(s.subject)}</option>`).join("");
  sel.value = cur;
  loadLog(); loadDaily(); loadLeaves(); setupMark();
}

function setToday(t) {
  const b = $("#todayBadge");
  b.className = "badge " + (t && t.marked ? "ok" : "warn");
  b.textContent = t && t.marked ? `Marked ${t.time.slice(0, 5)}` : "Not marked today";
  $("#markBtn").hidden = !!(t && t.marked) || !CFG.self_mark;
  if (t && t.marked) { markMsg("You are marked present today.", "marked"); $("#markSub").textContent = "Attendance for today is done."; }
}

async function loadLog() {
  const rows = await api("/api/student/attendance?subject=" + encodeURIComponent($("#subSel").value));
  $("#logTable").innerHTML = "<tr><th>Date</th><th>Time</th><th>Subject</th><th>Method</th></tr>" +
    (rows.map(r => `<tr><td>${r.date}</td><td>${r.time}</td><td>${esc(r.subject)}</td><td>${r.method === "location" ? "Location" : "Face"}</td></tr>`).join("")
      || "<tr><td colspan=4>No attendance records yet.</td></tr>");
}

// ---------- Day-wise calendar ----------
let month = new Date().toLocaleDateString("en-CA").slice(0, 7);
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
function shiftMonth(n) {
  const [y, m] = month.split("-").map(Number), d = new Date(y, m - 1 + n, 1);
  month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; loadDaily();
}
async function loadDaily() {
  let r; try { r = await api("/api/student/daily?month=" + month); } catch { return; }
  const [y, m] = month.split("-").map(Number), first = new Date(y, m - 1, 1), n = new Date(y, m, 0).getDate();
  $("#mTitle").textContent = `${MONTHS[m - 1]} ${y}`;
  $("#dP").textContent = r.counts.present; $("#dA").textContent = r.counts.absent; $("#dL").textContent = r.counts.leave;
  $("#dPct").textContent = r.counts.present + r.counts.absent ? r.counts.percent + "%" : "-";
  const o = r.overall;
  $("#hDay").textContent = o && (o.present + o.absent) ? o.percent + "%" : "-";
  $("#dOverall").textContent = o && (o.present + o.absent) ? `Overall: present ${o.present} day(s), absent ${o.absent}, on leave ${o.leave} (leave days are not counted against you) — ${o.percent}% day-wise.` : "";
  let h = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => `<div class="dow">${d}</div>`).join("") + "<div></div>".repeat(first.getDay());
  for (let d = 1; d <= n; d++) {
    const ds = `${month}-${String(d).padStart(2, "0")}`, x = r.days[ds];
    h += `<div class="day ${x ? x.status : ""}" title="${x ? x.status + (x.time ? " · " + x.time : "") : ""}">${d}</div>`;
  }
  $("#cal").innerHTML = h;
}
$("#mPrev").onclick = () => shiftMonth(-1);
$("#mNext").onclick = () => shiftMonth(1);

// ---------- Leaves ----------
async function loadLeaves() {
  let rows; try { rows = await api("/api/student/leaves"); } catch { return; }
  const cls = { pending: "warn", approved: "ok", rejected: "bad" };
  $("#leaveTable").innerHTML = "<tr><th>Dates</th><th>Reason</th><th>Status</th><th>Teacher remark</th><th></th></tr>" +
    (rows.map(l => `<tr><td>${esc(l.from_date)}${l.to_date !== l.from_date ? " to " + esc(l.to_date) : ""}</td><td style="white-space:normal;min-width:140px">${esc(l.reason)}</td>
      <td><span class="badge ${cls[l.status]}">${esc(l.status)}</span></td><td style="white-space:normal">${esc(l.remark || "")}</td>
      <td>${l.status === "pending" ? `<button class="danger" onclick="cancelLeave(${l.id})">Cancel</button>` : ""}</td></tr>`).join("")
      || "<tr><td colspan=5>No leave requests yet.</td></tr>");
}
$("#lBtn").onclick = async () => {
  const from = $("#lFrom").value, to = $("#lTo").value || from;
  try {
    await post("/api/student/leaves", { from_date: from, to_date: to, reason: $("#lReason").value });
    $("#lMsg").textContent = "Leave request sent. Your teacher will approve or reject it.";
    $("#lReason").value = ""; loadLeaves();
  } catch (e) { $("#lMsg").textContent = e.message; }
};
window.cancelLeave = async id => {
  if (!confirm("Cancel this leave request?")) return;
  try { await api("/api/student/leaves/" + id, { method: "DELETE" }); } catch (e) { alert(e.message); }
  loadLeaves();
};

// ---------- Mark my attendance (face + location lock, location-only fallback) ----------
let mStream = null, mLoc = null, faceFails = 0, camBroken = false;
function markMsg(t, cls = "") { const b = $("#markMsg"); b.className = "result " + cls; b.textContent = t; b.hidden = !t; }
function setupMark() {
  $("#markCard").hidden = !CFG.self_mark;
  if (!CFG.self_mark) return;
  if (CFG.loc_enabled) $("#markSub").textContent = "Location lock is ON: you must be at the campus. Then your face is checked against your registered photo.";
  if (!(ME && ME.today && ME.today.marked)) markMsg("");
}
function getLoc() {
  return new Promise((res, rej) => {
    if (!navigator.geolocation) return rej(new Error("This browser has no location support."));
    navigator.geolocation.getCurrentPosition(
      p => res({ lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy }),
      e => rej(new Error(e.code === 1 ? "Location permission denied. Allow location for this site in your browser settings, then try again." : "Could not get your location. Turn on GPS and try again.")),
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
  });
}
function camMsg(e) {
  const n = e && e.name;
  if (n === "NotAllowedError") return "Camera permission is blocked for this site.";
  if (n === "NotReadableError") return "Camera is being used by another app.";
  if (n === "NotFoundError") return "No camera found on this device.";
  return "Camera could not start (HTTPS is required).";
}
function showFallback(why) {
  if (!CFG.loc_only) return;
  $("#locBox").hidden = false; $("#locWhy").textContent = why;
}
function closeMark() {
  if (mStream) mStream.getTracks().forEach(t => t.stop());
  mStream = null; $("#mStage").hidden = true; $("#snapBtn").hidden = true; $("#markCancel").hidden = true;
  if (!(ME && ME.today && ME.today.marked)) $("#markBtn").hidden = !CFG.self_mark;
}
$("#markCancel").onclick = () => { closeMark(); markMsg(""); };

$("#markBtn").onclick = async () => {
  markMsg(""); $("#locBox").hidden = true; $("#markBtn").disabled = true;
  try {
    CFG = await api("/api/public-config").catch(() => CFG);
    if (!CFG.self_mark) { markMsg("Marking from the app is turned off by the admin.", "bad"); return; }
    if (CFG.loc_enabled) { markMsg("Checking your location..."); mLoc = await getLoc(); markMsg(""); } else mLoc = null;
  } catch (e) { markMsg(e.message, "bad"); $("#markBtn").disabled = false; return; }
  try {
    mStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } } });
    const v = $("#mVid"); v.srcObject = mStream;
    if (v.readyState < 1) await new Promise(r => v.addEventListener("loadedmetadata", r, { once: true }));
    await v.play().catch(() => {});
    if (v.videoWidth) $("#mStage").style.aspectRatio = `${v.videoWidth}/${v.videoHeight}`;
    $("#mStage").hidden = false; $("#snapBtn").hidden = false; $("#markCancel").hidden = false; $("#markBtn").hidden = true;
    $("#mChipTxt").textContent = "Look at the camera, then tap Capture";
  } catch (e) {
    camBroken = true; markMsg(camMsg(e), "bad");
    showFallback(camMsg(e) + " You can mark with your location instead.");
  }
  $("#markBtn").disabled = false;
};

function snap(video, w = 560) {
  const c = document.createElement("canvas"), k = Math.min(w, video.videoWidth);
  c.width = k; c.height = Math.round(k * video.videoHeight / video.videoWidth);
  c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.8);
}
$("#snapBtn").onclick = async () => {
  $("#snapBtn").disabled = true; markMsg("Verifying your face...");
  try {
    if (CFG.loc_enabled) mLoc = await getLoc().catch(() => mLoc);      // refresh position
    const frames = []; for (let i = 0; i < 3; i++) { frames.push(snap($("#mVid"))); await sleep(450); }
    done(await post("/api/student/mark", { mode: "face", frames, loc: mLoc }));
  } catch (e) {
    markMsg(e.message, "bad");
    if (e.code !== "location") { faceFails++; if (faceFails >= 2) showFallback("Face check keeps failing. You can mark with your location instead."); }
  }
  $("#snapBtn").disabled = false;
};
$("#locBtn").onclick = async () => {
  if (!confirm("Mark today's attendance using only your location (no face check)?")) return;
  $("#locBtn").disabled = true; markMsg("Checking your location...");
  try {
    mLoc = await getLoc();
    done(await post("/api/student/mark", { mode: "location", loc: mLoc }));
  } catch (e) { markMsg(e.message, "bad"); }
  $("#locBtn").disabled = false;
};
async function done(r) {
  closeMark(); $("#locBox").hidden = true;
  ME = await api("/api/student/me").catch(() => ME); if (ME) render(ME);
  markMsg(r.status === "already" ? `Already marked today at ${r.time}.` : `Attendance marked at ${r.time}${r.method === "location" ? " (by location)" : ""}.`, "marked");
}

// ---------- Profile / settings ----------
$("#saveProf").onclick = async () => {
  try { await post("/api/student/profile", { email: $("#eMail").value, parent_email: $("#eParent").value }); $("#profMsg").textContent = "Details saved."; }
  catch (e) { $("#profMsg").textContent = e.message; }
};
$("#pwBtn").onclick = async () => {
  if ($("#newPw").value !== $("#newPw2").value) { $("#pwMsg").textContent = "New passwords do not match."; return; }
  try {
    await post("/api/student/password", { old: $("#oldPw").value, new: $("#newPw").value });
    $("#pwMsg").textContent = "Password updated.";
    $("#oldPw").value = $("#newPw").value = $("#newPw2").value = "";
  } catch (e) { $("#pwMsg").textContent = e.message; }
};
$("#logoutBtn").onclick = async () => { await post("/api/logout", {}); location.reload(); };

$("#loginBtn").onclick = async () => {
  try { await post("/api/student/login", { roll: $("#lr").value, password: $("#lp").value }); boot(); }
  catch (e) { $("#loginErr").textContent = e.message; }
};
$("#lp").onkeydown = e => { if (e.key === "Enter") $("#loginBtn").click(); };
$("#subSel").onchange = loadLog;
$("#csvBtn").onclick = () => location.href = "/api/student/export?subject=" + encodeURIComponent($("#subSel").value);

boot();
