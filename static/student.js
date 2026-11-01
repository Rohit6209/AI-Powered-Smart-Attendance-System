const $ = s => document.querySelector(s);
const LIMIT = 75;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(url, opts = {}) {
  const r = await fetch(url, { credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || "Something went wrong");
  return d;
}
const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body) });

async function boot() {
  try { render(await api("/api/student/me")); }
  catch { $("#loginView").hidden = false; $("#appView").hidden = true; }
}

function render(d) {
  $("#loginView").hidden = true; $("#appView").hidden = false;
  const p = d.profile;
  $("#who").textContent = `${p.name} (${p.roll})`;
  $("#profile").innerHTML = `<dt>Name</dt><dd>${esc(p.name)}</dd><dt>Roll number</dt><dd>${esc(p.roll)}</dd>
    <dt>Email</dt><dd>${esc(p.email) || "-"}</dd><dt>Parent email</dt><dd>${esc(p.parent_email) || "-"}</dd>`;

  const pct = $("#pct");
  if (!d.total) {
    pct.textContent = "-"; pct.className = "stat";
    $("#pctMsg").textContent = "No classes have been held yet.";
  } else {
    const low = d.percent < LIMIT;
    pct.textContent = d.percent + "%"; pct.className = "stat " + (low ? "low" : "ok");
    const need = Math.max(0, Math.ceil(3 * d.total - 4 * d.present)); // classes to reach 75%
    $("#pctMsg").textContent = low
      ? `Below ${LIMIT}%. Attend the next ${need} class(es) in a row to reach ${LIMIT}%.`
      : `You are above the ${LIMIT}% requirement.`;
  }
  $("#counts").textContent = `Present in ${d.present} of ${d.total} classes`;

  $("#subTable").innerHTML = "<tr><th>Subject</th><th>Present</th><th>Total</th><th>Percent</th><th></th></tr>" +
    (d.subjects.map(s => `<tr><td>${esc(s.subject)}</td><td>${s.present}</td><td>${s.total}</td>
      <td class="${s.percent < LIMIT ? "low" : ""}">${s.percent}%</td>
      <td><div class="bar"><i class="${s.percent < LIMIT ? "low" : ""}" style="width:${s.percent}%"></i></div></td></tr>`).join("")
      || "<tr><td colspan=5>No attendance data yet.</td></tr>");

  const sel = $("#subSel"), cur = sel.value;
  sel.innerHTML = '<option value="">All subjects</option>' +
    d.subjects.map(s => `<option>${esc(s.subject)}</option>`).join("");
  sel.value = cur;
  loadLog(); loadDaily(); loadLeaves();
}

// ---------- Day-wise calendar ----------
let month = new Date().toLocaleDateString("en-CA").slice(0, 7);   // YYYY-MM
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
  $("#dOverall").textContent = o && (o.present + o.absent) ? `Overall: present ${o.present} day(s), absent ${o.absent}, on leave ${o.leave} (leave days are not counted against you) — ${o.percent}% day-wise.` : "";
  let h = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => `<div class="dow">${d}</div>`).join("") + '<div></div>'.repeat(first.getDay());
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

async function loadLog() {
  const rows = await api("/api/student/attendance?subject=" + encodeURIComponent($("#subSel").value));
  $("#logTable").innerHTML = "<tr><th>Date</th><th>Time</th><th>Subject</th></tr>" +
    (rows.map(r => `<tr><td>${r.date}</td><td>${r.time}</td><td>${esc(r.subject)}</td></tr>`).join("")
      || "<tr><td colspan=3>No attendance records yet.</td></tr>");
}

$("#loginBtn").onclick = async () => {
  try {
    await post("/api/student/login", { roll: $("#lr").value, password: $("#lp").value });
    boot();
  } catch (e) { $("#loginErr").textContent = e.message; }
};
$("#lp").onkeydown = e => { if (e.key === "Enter") $("#loginBtn").click(); };
$("#logoutBtn").onclick = async () => { await post("/api/logout", {}); location.reload(); };
$("#subSel").onchange = loadLog;
$("#csvBtn").onclick = () => location.href = "/api/student/export?subject=" + encodeURIComponent($("#subSel").value);
$("#pwBtn").onclick = async () => {
  try {
    await post("/api/student/password", { old: $("#oldPw").value, new: $("#newPw").value });
    $("#pwMsg").textContent = "Password updated.";
    $("#oldPw").value = $("#newPw").value = "";
  } catch (e) { $("#pwMsg").textContent = e.message; }
};

boot();
