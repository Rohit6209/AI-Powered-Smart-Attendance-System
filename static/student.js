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
  loadLog();
}

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
