// Shared helpers for the admin and gate pages
const $ = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = n => String(n || "?").trim().split(/\s+/).slice(0, 2).map(x => x[0]).join("").toUpperCase();

async function api(url, opts = {}) {
  const r = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || d.message || "Something went wrong");
  return d;
}
const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body) });

const STATUS = {
  marked:    { color: "#22c55e", label: "Marked" },
  already:   { color: "#38bdf8", label: "Already marked" },
  verifying: { color: "#fbbf24", label: "Move your head slightly" },
  unknown:   { color: "#ef4444", label: "Not recognised" },
};

/** Draw a box + label for every face. Canvas sits on top of a mirrored <video>, so x is flipped. */
function drawBoxes(canvas, faces, mirror = true) {
  const w = canvas.clientWidth, h = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.font = "600 13px system-ui,sans-serif"; g.textBaseline = "middle";
  for (const f of faces || []) {
    let [x, y, bw, bh] = f.box; if (mirror) x = 1 - x - bw;
    const px = x * w, py = y * h, pw = bw * w, ph = bh * h, s = STATUS[f.status] || STATUS.unknown, k = Math.min(pw, ph) * 0.28;
    g.strokeStyle = s.color; g.lineWidth = 3; g.lineCap = "round"; g.beginPath();
    [[px, py, 1, 1], [px + pw, py, -1, 1], [px, py + ph, 1, -1], [px + pw, py + ph, -1, -1]].forEach(([cx, cy, dx, dy]) => {
      g.moveTo(cx + dx * k, cy); g.lineTo(cx, cy); g.lineTo(cx, cy + dy * k);
    });
    g.stroke();
    const text = (f.name || "Unknown") + (f.status === "verifying" ? "  ·  move your head" : f.status === "marked" ? "  ✓" : "");
    const tw = g.measureText(text).width + 16, ty = py - 24 < 0 ? py + ph + 4 : py - 24;
    g.fillStyle = s.color; g.beginPath(); g.roundRect(px, ty, tw, 22, 11); g.fill();
    g.fillStyle = "#06101c"; g.fillText(text, px + 8, ty + 11.5);
  }
}

/** Browser-camera scanner: sends one frame at a time, keeps scanning continuously. */
class FaceScanner {
  constructor(o) {
    Object.assign(this, { gap: 100, width: 720, deviceId: "", payload: () => ({}), onFaces: null, onError: null }, o);
    this.on = false; this.stream = null; this.cv = document.createElement("canvas");
  }
  async openCamera() {
    this.closeCamera();
    const video = { width: { ideal: 1280 }, height: { ideal: 720 } };
    if (this.deviceId) video.deviceId = { exact: this.deviceId }; else video.facingMode = "user";
    this.stream = await navigator.mediaDevices.getUserMedia({ video });
    this.video.srcObject = this.stream;
    if (this.video.readyState < 1) await new Promise(r => this.video.addEventListener("loadedmetadata", r, { once: true }));
    this.video.play().catch(() => {});
    if (this.stage && this.video.videoWidth) this.stage.style.aspectRatio = `${this.video.videoWidth}/${this.video.videoHeight}`;
  }
  closeCamera() {
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    this.stream = null; if (this.video) this.video.srcObject = null;
  }
  grab() {
    const v = this.video; if (!v.videoWidth) return null;
    const w = Math.min(this.width, v.videoWidth), h = Math.round(w * v.videoHeight / v.videoWidth);
    this.cv.width = w; this.cv.height = h; this.cv.getContext("2d").drawImage(v, 0, 0, w, h);
    return this.cv.toDataURL("image/jpeg", 0.7);
  }
  async start() {
    if (this.on) return;
    if (!this.stream) await this.openCamera();
    this.on = true; this.loop();
  }
  stop() {
    this.on = false;
    if (this.canvas) drawBoxes(this.canvas, []);
  }
  async loop() {
    while (this.on) {
      const t0 = performance.now(); let failed = false;
      try {
        const frame = this.grab();
        if (frame) {
          const r = await post("/api/scan", { ...this.payload(), frame });
          if (!this.on) break;
          drawBoxes(this.canvas, r.faces);
          if (this.onFaces) this.onFaces(r.faces);
        }
      } catch (e) { failed = true; if (this.onError) this.onError(e); }
      await sleep(failed ? 1500 : Math.max(30, this.gap - (performance.now() - t0)));
    }
  }
}

async function listVideoDevices() {
  try { return (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput"); } catch { return []; }
}
