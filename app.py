import os, io, re, csv, time, math, base64, sqlite3, pickle, smtplib, threading, collections
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")  # stable RTSP/CCTV streams
import cv2
import numpy as np
import face_recognition
from flask import Flask, request, jsonify, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(BASE, ".env")):
    """Load KEY=VALUE lines from .env (real environment variables win)."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_secret_key():
    """SECRET_KEY from env, else a random key generated once and saved in secret.key."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(BASE, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(os.urandom(32).hex())
    return open(path).read().strip()


load_env()
app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = get_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DB = os.environ.get("DATABASE", os.path.join(BASE, "attendance.db"))
TOLERANCE = 0.5        # lower = stricter matching
SCAN_WIDTH = int(os.environ.get("SCAN_WIDTH", 480))     # browser frames are shrunk to this width before detection (smaller = faster)
CCTV_WIDTH = int(os.environ.get("CCTV_WIDTH", 960))     # CCTV/IP camera frames: larger so small faces far away still get detected
MOVE_YAW = float(os.environ.get("MOVE_YAW", 0.04))      # head-turn threshold (fraction of eye distance) = person is "live"
MOVE_SHIFT = float(os.environ.get("MOVE_SHIFT", 0.20))  # body/face travel threshold (in face widths), e.g. walking in
MIN_OBS = 2            # a face must be seen this many times inside TRACK_SECONDS before attendance is marked
TRACK_SECONDS = 10.0   # long window: a slow server (e.g. Render free plan) answers only 1 frame every 1-2 s
GATE_SUBJECT = os.environ.get("GATE_SUBJECT", "Gate Entry")   # subject name used for gate-camera attendance
SMTP_HOST = os.environ.get("SMTP_HOST")          # e.g. smtp.gmail.com
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")          # Gmail App Password


# ---------------- Database ----------------
def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, role TEXT);
    CREATE TABLE IF NOT EXISTS students(
        id INTEGER PRIMARY KEY, roll TEXT UNIQUE, name TEXT, email TEXT,
        parent_email TEXT, encoding BLOB, password TEXT);
    CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY, student_id INTEGER, subject TEXT,
        date TEXT, time TEXT, UNIQUE(student_id, subject, date));
    CREATE TABLE IF NOT EXISTS cameras(
        id INTEGER PRIMARY KEY, name TEXT, url TEXT, mode TEXT DEFAULT 'class',
        subject TEXT DEFAULT '', active INTEGER DEFAULT 0);
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(students)")]
    if "password" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN password TEXT")
    if "photo" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN photo BLOB")
    if not con.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        con.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                    ("admin", generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")), "admin"))
    con.commit()
    con.close()


# ---------------- Auth ----------------
def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            if "user" not in session:
                return jsonify(error="Login required"), 401
            if roles and session["role"] not in roles:
                return jsonify(error="Permission denied"), 403
            return fn(*a, **kw)
        return wrap
    return deco


@app.post("/api/login")
def login():
    d = request.json
    con = db()
    u = con.execute("SELECT * FROM users WHERE username=?", (d.get("username"),)).fetchone()
    con.close()
    if not u or not check_password_hash(u["password"], d.get("password", "")):
        return jsonify(error="Wrong username or password"), 401
    session["user"], session["role"] = u["username"], u["role"]
    return jsonify(user=u["username"], role=u["role"])


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    if session.get("role") not in ("admin", "teacher"):
        return jsonify(error="Not logged in"), 401
    return jsonify(user=session["user"], role=session["role"])


@app.post("/api/teachers")
@login_required("admin")
def add_teacher():
    d = request.json
    try:
        con = db()
        con.execute("INSERT INTO users(username,password,role) VALUES(?,?,'teacher')",
                    (d["username"], generate_password_hash(d["password"])))
        con.commit(); con.close()
    except sqlite3.IntegrityError:
        return jsonify(error="Username already exists"), 400
    return jsonify(ok=True)


# ---------------- Face helpers ----------------
def decode(b64):
    raw = base64.b64decode(b64.split(",")[-1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    return None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def biggest_face(img):
    locs = face_recognition.face_locations(img, model="hog")
    if not locs:
        return None
    return max(locs, key=lambda l: (l[2] - l[0]) * (l[1] - l[3]))


def crop_photo(rgb, loc, size=200):
    """Square JPEG thumbnail of a face (used as the profile picture)."""
    t, r, b, l = loc
    h, w = rgb.shape[:2]
    m = int((r - l) * 0.35)
    crop = rgb[max(0, t - m):min(h, b + m), max(0, l - m):min(w, r + m)]
    if crop.size == 0:
        return None
    crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


# ---------------- In-memory caches (speed) ----------------
STATE_LOCK = threading.RLock()
RECOG_LOCK = threading.Lock()          # dlib is run one frame at a time
_known = {"ok": False, "ids": [], "rolls": [], "names": [], "mat": None, "photo": set()}
TRACKS = {}                            # camera key -> {student_id: deque of observations}
DONE = {}                              # (student_id, subject, date) -> time already marked
JUST = {}                              # (student_id, subject) -> time.time() of the mark (keeps "marked" on screen a few secs)
STATS = {}                             # student_id -> (time.time(), stats dict)


def invalidate_known():
    with STATE_LOCK:
        _known["ok"] = False
        DONE.clear(); STATS.clear()


def get_known():
    with STATE_LOCK:
        if not _known["ok"]:
            con = db()
            rows = con.execute("SELECT id,roll,name,encoding,(photo IS NOT NULL) hp FROM students").fetchall()
            con.close()
            _known.update(ids=[r["id"] for r in rows], rolls=[r["roll"] for r in rows],
                          names=[r["name"] for r in rows],
                          mat=np.array([pickle.loads(r["encoding"]) for r in rows]) if rows else None,
                          photo={r["id"] for r in rows if r["hp"]}, ok=True)
        return _known


def marked_time(sid, subject, date):
    k = (sid, subject, date)
    with STATE_LOCK:
        if k in DONE:
            return DONE[k]
    con = db()
    r = con.execute("SELECT time FROM attendance WHERE student_id=? AND subject=? AND date=?", k).fetchone()
    con.close()
    if r:
        with STATE_LOCK:
            DONE[k] = r["time"]
        return r["time"]
    return None


def do_mark(sid, subject, now):
    date, t = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
    con = db()
    cur = con.execute("INSERT OR IGNORE INTO attendance(student_id,subject,date,time) VALUES(?,?,?,?)",
                      (sid, subject, date, t))
    con.commit(); con.close()
    with STATE_LOCK:
        STATS.pop(sid, None)
        if cur.rowcount:
            DONE[(sid, subject, date)] = t
            JUST[(sid, subject)] = time.time()
    return cur.rowcount > 0


def student_stats(sid):
    """Overall attendance numbers shown on the gate profile card (cached for a few seconds)."""
    with STATE_LOCK:
        c = STATS.get(sid)
        if c and time.time() - c[0] < 15:
            return c[1]
    con = db()
    total = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT date,subject FROM attendance)").fetchone()[0]
    present = con.execute("SELECT COUNT(*) FROM attendance WHERE student_id=?", (sid,)).fetchone()[0]
    con.close()
    out = {"present": present, "total": total, "percent": round(present * 100 / total, 1) if total else 0}
    with STATE_LOCK:
        STATS[sid] = (time.time(), out)
    return out


# ---------------- Head-movement liveness ----------------
def yaw_ratio(lm):
    """Nose position between the eyes (0 = centred). Changes when the head turns, but NOT when a flat photo
    is simply moved around, so it is a better liveness signal than raw face position."""
    le, re_ = np.mean(lm["left_eye"], axis=0), np.mean(lm["right_eye"], axis=0)
    d = float(np.linalg.norm(le - re_)) or 1.0
    return float((np.array(lm["nose_tip"][0])[0] - (le[0] + re_[0]) / 2.0) / d)


def track(key, sid, obs, now):
    with STATE_LOCK:
        q = TRACKS.setdefault(key, {}).setdefault(sid, collections.deque(maxlen=8))
        q.append(obs)
        while q and now - q[0][0] > TRACK_SECONDS:
            q.popleft()
        return list(q)


def moved(obs):
    """obs = [(time, cx, cy, face_width, yaw), ...]. True if the person turned/moved their head or walked in."""
    if len(obs) < MIN_OBS:
        return False
    fw = float(np.mean([o[3] for o in obs])) or 1.0
    yaw = [o[4] for o in obs]
    shift = math.hypot(max(o[1] for o in obs) - min(o[1] for o in obs),
                       max(o[2] for o in obs) - min(o[2] for o in obs)) / fw
    return (max(yaw) - min(yaw)) >= MOVE_YAW or shift >= MOVE_SHIFT


# ---------------- Multi-face recognition ----------------
def analyze(rgb, key, subject, width=SCAN_WIDTH):
    """Find EVERY face in the frame, recognise each one, verify head movement and mark attendance.
    Returns a list of faces: {box:[x,y,w,h] (0-1), status, id, name, roll, confidence, time, stats}.
    status: marked | already | verifying (recognised, waiting for head movement) | unknown"""
    h0, w0 = rgb.shape[:2]
    sc = min(1.0, width / float(w0))
    img = cv2.resize(rgb, (int(w0 * sc), int(h0 * sc)), interpolation=cv2.INTER_AREA) if sc < 1.0 else rgb
    H, W = img.shape[:2]
    K = get_known()
    with RECOG_LOCK:
        locs = face_recognition.face_locations(img, model="hog")
        if not locs:
            return []
        lms = face_recognition.face_landmarks(img, locs, model="small")
        encs = face_recognition.face_encodings(img, locs) if K["mat"] is not None else []

    pairs = []
    for i, e in enumerate(encs):
        d = np.linalg.norm(K["mat"] - e, axis=1)
        j = int(np.argmin(d))
        if d[j] <= TOLERANCE:
            pairs.append((float(d[j]), i, j))
    cand, used = {}, set()
    for dist, i, j in sorted(pairs):          # one student can only match one face per frame
        if j not in used:
            used.add(j); cand[i] = (dist, j)

    now = datetime.now(); ts = time.time(); date = now.strftime("%Y-%m-%d")
    faces = []
    for i, (t, r, b, l) in enumerate(locs):
        fw = float(r - l)
        f = {"box": [l / W, t / H, fw / W, (b - t) / H], "status": "unknown"}
        if i in cand:
            dist, j = cand[i]
            sid = K["ids"][j]
            f.update(id=sid, name=K["names"][j], roll=K["rolls"][j], confidence=round((1 - dist) * 100, 1))
            done = marked_time(sid, subject, date)
            if done:
                f.update(status="already", time=done)
                if ts - JUST.get((sid, subject), 0) < 8:     # keep showing "marked" for a few seconds
                    f["status"] = "marked"
            else:
                obs = track(key, sid, (ts, (l + r) / 2.0, (t + b) / 2.0, fw, yaw_ratio(lms[i])), ts)
                if moved(obs):
                    inserted = do_mark(sid, subject, now)
                    f.update(status="marked" if inserted else "already", time=marked_time(sid, subject, date))
                else:
                    f["status"] = "verifying"
            if f["status"] in ("marked", "already"):
                f["stats"] = student_stats(sid)
                if sid not in K["photo"] and fw >= 90:      # first clear sighting becomes the profile picture
                    photo = crop_photo(img, (t, r, b, l))
                    if photo:
                        con = db()
                        con.execute("UPDATE students SET photo=? WHERE id=?", (photo, sid))
                        con.commit(); con.close()
                        K["photo"].add(sid)
        faces.append(f)
    return faces


@app.post("/api/scan")
@login_required("admin", "teacher")
def scan():
    """One frame from the browser camera. Multi-face. mode='class' (needs subject) or 'gate'."""
    d = request.json or {}
    mode = d.get("mode", "class")
    subject = GATE_SUBJECT if mode == "gate" else (d.get("subject") or "").strip()
    if not subject:
        return jsonify(error="Enter a subject first"), 400
    img = decode(d.get("frame", ""))
    if img is None:
        return jsonify(faces=[])
    faces = analyze(img, f"web:{session['user']}:{subject}", subject, SCAN_WIDTH)
    return jsonify(faces=faces)


@app.get("/api/gate/today")
@login_required("admin", "teacher")
def gate_today():
    con = db()
    rows = con.execute("""SELECT s.id,s.name,s.roll,a.time FROM attendance a JOIN students s ON s.id=a.student_id
                          WHERE a.subject=? AND a.date=? ORDER BY a.time DESC""",
                       (GATE_SUBJECT, datetime.now().strftime("%Y-%m-%d"))).fetchall()
    con.close()
    return jsonify(subject=GATE_SUBJECT, entries=[dict(r) for r in rows])


# ---------------- Students ----------------
@app.post("/api/students")
@login_required("admin", "teacher")
def register_student():
    d = request.json
    encs, best = [], None
    for f in d.get("frames", []):
        img = decode(f)
        if img is None:
            continue
        loc = biggest_face(img)
        if loc:
            encs.append(face_recognition.face_encodings(img, [loc])[0])
            if best is None or (loc[2] - loc[0]) > (best[1][2] - best[1][0]):
                best = (img, loc)
    if len(encs) < 3:
        return jsonify(error="Could not capture a clear face. Look at the camera in good light and try again."), 400
    mean_enc = np.mean(encs, axis=0)
    try:
        con = db()
        pw = (d.get("password") or d["roll"]).strip()   # if empty, the roll number is the password
        con.execute("INSERT INTO students(roll,name,email,parent_email,encoding,password,photo) VALUES(?,?,?,?,?,?,?)",
                    (d["roll"], d["name"], d.get("email", ""), d.get("parent_email", ""),
                     pickle.dumps(mean_enc), generate_password_hash(pw), crop_photo(*best)))
        con.commit(); con.close()
    except sqlite3.IntegrityError:
        return jsonify(error="A student with this roll number is already registered"), 400
    invalidate_known()
    return jsonify(ok=True, faces_used=len(encs))


@app.get("/api/students")
@login_required("admin", "teacher")
def list_students():
    con = db()
    rows = con.execute("SELECT id,roll,name,email,parent_email,(photo IS NOT NULL) has_photo FROM students ORDER BY roll").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/students/<int:sid>/photo")
@login_required("admin", "teacher")
def student_photo(sid):
    con = db()
    r = con.execute("SELECT photo FROM students WHERE id=?", (sid,)).fetchone()
    con.close()
    if not r or not r["photo"]:
        return Response(status=404)
    return Response(r["photo"], mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.delete("/api/students/<int:sid>")
@login_required("admin")
def delete_student(sid):
    con = db()
    con.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
    con.execute("DELETE FROM students WHERE id=?", (sid,))
    con.commit(); con.close()
    invalidate_known()
    return jsonify(ok=True)


# ---------------- CCTV / IP cameras (server side, RTSP) ----------------
STATUS_BGR = {"marked": (94, 197, 34), "already": (250, 165, 56), "verifying": (36, 191, 251), "unknown": (68, 68, 239)}
WORKERS = {}


def mask_url(u):
    return re.sub(r"://([^:/@]+):[^@/]*@", r"://\1:***@", u or "")


def cam_source(url):
    url = (url or "").strip()
    return int(url) if url.isdigit() else url


def draw_faces(bgr, faces):
    H, W = bgr.shape[:2]
    for f in faces:
        x, y, w, h = f["box"]
        x0, y0, x1, y1 = int(x * W), int(y * H), int((x + w) * W), int((y + h) * H)
        col = STATUS_BGR.get(f["status"], STATUS_BGR["unknown"])
        cv2.rectangle(bgr, (x0, y0), (x1, y1), col, 2)
        label = f.get("name") or "Unknown"
        if f["status"] == "verifying":
            label += " (verifying)"
        elif f["status"] == "marked":
            label += " - marked"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(bgr, (x0, max(0, y0 - th - 10)), (x0 + tw + 8, y0), col, -1)
        cv2.putText(bgr, label, (x0 + 4, y0 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1, cv2.LINE_AA)
    return bgr


class CamWorker:
    """Reads one IP/CCTV camera in the background, finds all faces, marks attendance."""

    def __init__(self, cam):
        self.id, self.name = cam["id"], cam["name"]
        self.src, self.mode, self.subject = cam_source(cam["url"]), cam["mode"], cam["subject"]
        self.key = f"cam:{self.id}"
        self.stop_evt = threading.Event()
        self.frame, self.seq, self.faces, self.error = None, 0, [], "Connecting..."
        self.lock = threading.Lock()

    @property
    def att_subject(self):
        return GATE_SUBJECT if self.mode == "gate" else self.subject

    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._processor, daemon=True).start()

    def stop(self):
        self.stop_evt.set()

    def _reader(self):
        while not self.stop_evt.is_set():
            cap = cv2.VideoCapture(self.src)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                self.error = "Cannot open camera. Check the URL / network."
                cap.release(); self.stop_evt.wait(3); continue
            self.error = ""
            while not self.stop_evt.is_set():
                ok, frame = cap.read()
                if not ok:
                    self.error = "Stream lost, reconnecting..."
                    break
                with self.lock:
                    self.frame, self.seq = frame, self.seq + 1
            cap.release(); self.stop_evt.wait(2)

    def _processor(self):
        last = -1
        while not self.stop_evt.is_set():
            with self.lock:
                frame, seq = self.frame, self.seq
            if frame is None or seq == last:
                self.stop_evt.wait(0.05); continue
            last = seq
            try:
                faces = analyze(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), self.key, self.att_subject, CCTV_WIDTH)
                with self.lock:
                    self.faces = faces
            except Exception as e:
                self.error = f"Recognition error: {e}"
                self.stop_evt.wait(1)
            self.stop_evt.wait(0.15)

    def snapshot(self):
        with self.lock:
            return (None if self.frame is None else self.frame.copy()), list(self.faces)


def start_worker(cam):
    stop_worker(cam["id"])
    w = CamWorker(cam)
    WORKERS[cam["id"]] = w
    w.start()


def stop_worker(cid):
    w = WORKERS.pop(cid, None)
    if w:
        w.stop()


def cam_row(cid):
    con = db()
    r = con.execute("SELECT * FROM cameras WHERE id=?", (cid,)).fetchone()
    con.close()
    return r


@app.get("/api/cameras")
@login_required("admin", "teacher")
def list_cameras():
    con = db()
    rows = con.execute("SELECT * FROM cameras ORDER BY id").fetchall()
    con.close()
    out = []
    for r in rows:
        w = WORKERS.get(r["id"])
        out.append({"id": r["id"], "name": r["name"], "url": mask_url(r["url"]), "mode": r["mode"],
                    "subject": r["subject"], "running": bool(w), "error": w.error if w else ""})
    return jsonify(out)


@app.post("/api/cameras")
@login_required("admin")
def add_camera():
    d = request.json or {}
    name, url, mode = (d.get("name") or "").strip(), (d.get("url") or "").strip(), d.get("mode", "class")
    if not name or not re.match(r"^(\d+|(rtsp|rtmp|http|https)://\S+)$", url):
        return jsonify(error="Enter a name and a camera URL (rtsp://..., http://... or 0 for the server's webcam)"), 400
    if mode not in ("class", "gate"):
        return jsonify(error="Mode must be class or gate"), 400
    con = db()
    con.execute("INSERT INTO cameras(name,url,mode,subject) VALUES(?,?,?,?)",
                (name, url, mode, (d.get("subject") or "").strip()))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.post("/api/cameras/<int:cid>/start")
@login_required("admin", "teacher")
def start_camera(cid):
    r = cam_row(cid)
    if not r:
        return jsonify(error="Camera not found"), 404
    subject = (request.json or {}).get("subject", "").strip() or r["subject"]
    if r["mode"] == "class" and not subject:
        return jsonify(error="Enter the subject for this camera first"), 400
    con = db()
    con.execute("UPDATE cameras SET active=1, subject=? WHERE id=?", (subject, cid))
    con.commit(); con.close()
    start_worker(dict(cam_row(cid)))
    return jsonify(ok=True)


@app.post("/api/cameras/<int:cid>/stop")
@login_required("admin", "teacher")
def stop_camera(cid):
    stop_worker(cid)
    con = db()
    con.execute("UPDATE cameras SET active=0 WHERE id=?", (cid,))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.delete("/api/cameras/<int:cid>")
@login_required("admin")
def delete_camera(cid):
    stop_worker(cid)
    con = db()
    con.execute("DELETE FROM cameras WHERE id=?", (cid,))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.get("/api/cameras/<int:cid>/state")
@login_required("admin", "teacher")
def camera_state(cid):
    w = WORKERS.get(cid)
    if not w:
        return jsonify(running=False, error="Camera is stopped", faces=[])
    return jsonify(running=True, error=w.error, faces=w.snapshot()[1], mode=w.mode)


@app.get("/api/cameras/<int:cid>/stream")
@login_required("admin", "teacher")
def camera_stream(cid):
    w = WORKERS.get(cid)
    if not w:
        return Response("Camera is not running", status=404)

    def gen():
        while not w.stop_evt.is_set():
            frame, faces = w.snapshot()
            if frame is None:
                time.sleep(0.2); continue
            if frame.shape[1] > 960:
                s = 960.0 / frame.shape[1]
                frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", draw_faces(frame, faces), [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            time.sleep(0.08)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


def resume_cameras():
    con = db()
    rows = con.execute("SELECT * FROM cameras WHERE active=1").fetchall()
    con.close()
    for r in rows:
        start_worker(dict(r))


# ---------------- Reports ----------------
def fetch_log(date="", subject=""):
    q = """SELECT a.date,a.time,a.subject,s.roll,s.name FROM attendance a
           JOIN students s ON s.id=a.student_id WHERE 1=1"""
    args = []
    if date:
        q += " AND a.date=?"; args.append(date)
    if subject:
        q += " AND a.subject=?"; args.append(subject)
    con = db()
    rows = con.execute(q + " ORDER BY a.date DESC, a.time DESC", args).fetchall()
    con.close()
    return [dict(r) for r in rows]


def summary_data():
    con = db()
    total = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT date,subject FROM attendance)").fetchone()[0]
    rows = con.execute("""SELECT s.id,s.roll,s.name,s.email,s.parent_email,COUNT(a.id) present
        FROM students s LEFT JOIN attendance a ON a.student_id=s.id GROUP BY s.id ORDER BY s.roll""").fetchall()
    trend = []
    for k in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d")
        c = con.execute("SELECT COUNT(*) FROM attendance WHERE date=?", (day,)).fetchone()[0]
        trend.append({"date": day[5:], "count": c})
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["total"] = total
        d["percent"] = round(r["present"] * 100 / total, 1) if total else 0
        out.append(d)
    return out, trend


@app.get("/api/attendance")
@login_required("admin", "teacher")
def attendance():
    return jsonify(fetch_log(request.args.get("date", ""), request.args.get("subject", "")))


@app.get("/api/summary")
@login_required("admin", "teacher")
def summary():
    students, trend = summary_data()
    return jsonify(students=students, trend=trend)


@app.get("/api/export")
@login_required("admin", "teacher")
def export_csv():
    rows = fetch_log(request.args.get("date", ""), request.args.get("subject", ""))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "Subject", "Roll", "Name"])
    for r in rows:
        w.writerow([r["date"], r["time"], r["subject"], r["roll"], r["name"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance.csv"})


# ---------------- Low attendance alerts ----------------
def send_mail(to, subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_USER, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


@app.post("/api/alerts")
@login_required("admin", "teacher")
def alerts():
    limit = float(request.json.get("threshold", 75))
    students, _ = summary_data()
    low = [s for s in students if s["total"] and s["percent"] < limit]
    sent, errors = 0, []
    for s in low:
        body = (f"Dear {s['name']} ({s['roll']}),\n\nYour attendance is {s['percent']}% "
                f"({s['present']}/{s['total']} classes), below the required {limit:.0f}%.\n"
                "Please attend classes regularly.\n\n- College Attendance System")
        for to in filter(None, [s["email"], s["parent_email"]]):
            if not SMTP_HOST:
                continue
            try:
                send_mail(to, "Low Attendance Alert", body); sent += 1
            except Exception as e:
                errors.append(f"{to}: {e}")
    return jsonify(low=[f"{s['name']} ({s['percent']}%)" for s in low], emails_sent=sent,
                   smtp_configured=bool(SMTP_HOST), errors=errors)


@app.post("/api/students/<int:sid>/password")
@login_required("admin", "teacher")
def reset_student_password(sid):
    new = (request.json.get("password") or "").strip()
    if len(new) < 4:
        return jsonify(error="Password must be at least 4 characters"), 400
    con = db()
    con.execute("UPDATE students SET password=? WHERE id=?", (generate_password_hash(new), sid))
    con.commit(); con.close()
    return jsonify(ok=True)


# ---------------- Student portal APIs (own data only) ----------------
def student_only(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if session.get("role") != "student":
            return jsonify(error="Student login required"), 401
        return fn(*a, **kw)
    return wrap


@app.post("/api/student/login")
def student_login():
    d = request.json
    con = db()
    s = con.execute("SELECT * FROM students WHERE roll=?", ((d.get("roll") or "").strip(),)).fetchone()
    con.close()
    if not s or not s["password"] or not check_password_hash(s["password"], d.get("password", "")):
        return jsonify(error="Wrong roll number or password"), 401
    session.clear()
    session["user"], session["role"], session["sid"] = s["roll"], "student", s["id"]
    return jsonify(ok=True)


@app.get("/api/student/me")
@student_only
def student_me():
    sid = session["sid"]
    con = db()
    s = con.execute("SELECT roll,name,email,parent_email FROM students WHERE id=?", (sid,)).fetchone()
    if not s:
        con.close(); session.clear()
        return jsonify(error="Student not found"), 401
    total = con.execute("SELECT COUNT(*) FROM (SELECT DISTINCT date,subject FROM attendance)").fetchone()[0]
    present = con.execute("SELECT COUNT(*) FROM attendance WHERE student_id=?", (sid,)).fetchone()[0]
    tot_sub = {r["subject"]: r["c"] for r in con.execute(
        "SELECT subject, COUNT(DISTINCT date) c FROM attendance GROUP BY subject")}
    pres_sub = {r["subject"]: r["c"] for r in con.execute(
        "SELECT subject, COUNT(*) c FROM attendance WHERE student_id=? GROUP BY subject", (sid,))}
    con.close()
    subjects = [{"subject": k, "present": pres_sub.get(k, 0), "total": v,
                 "percent": round(pres_sub.get(k, 0) * 100 / v, 1)} for k, v in sorted(tot_sub.items())]
    return jsonify(profile=dict(s), present=present, total=total,
                   percent=round(present * 100 / total, 1) if total else 0, subjects=subjects)


def own_log(sid, subject=""):
    q = "SELECT date,time,subject FROM attendance WHERE student_id=?"
    args = [sid]
    if subject:
        q += " AND subject=?"; args.append(subject)
    con = db()
    rows = con.execute(q + " ORDER BY date DESC, time DESC", args).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/student/attendance")
@student_only
def student_attendance():
    return jsonify(own_log(session["sid"], request.args.get("subject", "")))


@app.get("/api/student/export")
@student_only
def student_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "Subject"])
    for r in own_log(session["sid"], request.args.get("subject", "")):
        w.writerow([r["date"], r["time"], r["subject"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=my_attendance.csv"})


@app.post("/api/student/password")
@student_only
def student_password():
    d = request.json
    new = d.get("new", "")
    if len(new) < 6:
        return jsonify(error="New password must be at least 6 characters"), 400
    con = db()
    s = con.execute("SELECT password FROM students WHERE id=?", (session["sid"],)).fetchone()
    if not s or not check_password_hash(s["password"], d.get("old", "")):
        con.close()
        return jsonify(error="Old password is incorrect"), 400
    con.execute("UPDATE students SET password=? WHERE id=?", (generate_password_hash(new), session["sid"]))
    con.commit(); con.close()
    return jsonify(ok=True)


# ---------------- Pages ----------------
@app.get("/")
def student_page():
    return app.send_static_file("student.html")


@app.get("/admin")
def admin_page():
    return app.send_static_file("admin.html")


@app.get("/gate")
def gate_page():
    return app.send_static_file("gate.html")


init_db()  # runs for every server (waitress, gunicorn, flask)
resume_cameras()  # restart CCTV cameras that were running before the server stopped

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, host="127.0.0.1", port=5000)  # development only; use server.py otherwise
