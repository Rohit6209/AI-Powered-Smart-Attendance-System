import os, io, re, csv, time, math, base64, sqlite3, pickle, smtplib, threading, collections
from datetime import datetime, timedelta, timezone
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
MOVE_YAW = float(os.environ.get("MOVE_YAW", 0.025))      # head-turn threshold (fraction of eye distance) = person is "live"
MOVE_SHIFT = float(os.environ.get("MOVE_SHIFT", 0.12))  # body/face travel threshold (in face widths), e.g. walking in
MIN_OBS = 2            # a face must be seen this many times (within RECENT_SECONDS of each other) before attendance is marked
RECENT_SECONDS = 4.0
REG_WIDTH = int(os.environ.get("REG_WIDTH", 480))       # registration frames are shrunk to this width (faster)
TZ_OFFSET_MIN = int(os.environ.get("TZ_OFFSET_MIN", 330))   # minutes from UTC; 330 = India (IST). Render servers run in UTC.
TRACK_SECONDS = 10.0   # long window: a slow server (e.g. Render free plan) answers only 1 frame every 1-2 s
GATE_SUBJECT = os.environ.get("GATE_SUBJECT", "Gate Entry")   # subject name used for gate-camera attendance
SMTP_HOST = os.environ.get("SMTP_HOST")          # e.g. smtp.gmail.com
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")          # Gmail App Password


def now_local():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_OFFSET_MIN)


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
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS leaves(
        id INTEGER PRIMARY KEY, student_id INTEGER, from_date TEXT, to_date TEXT, reason TEXT,
        status TEXT DEFAULT 'pending', applied_at TEXT, decided_by TEXT, decided_at TEXT, remark TEXT);
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(students)")]
    if "password" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN password TEXT")
    if "photo" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN photo BLOB")
    if "method" not in [r[1] for r in con.execute("PRAGMA table_info(attendance)")]:
        con.execute("ALTER TABLE attendance ADD COLUMN method TEXT DEFAULT 'face'")
    if not con.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        con.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                    ("admin", generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")), "admin"))
    con.commit()
    con.close()


# ---------------- Settings (admin panel) ----------------
DEFAULT_SETTINGS = {"loc_enabled": "0", "loc_lat": "", "loc_lng": "", "loc_radius": "150",
                    "require_move": "0", "low_light_boost": "1", "tolerance": "0.5",
                    "self_mark": "1", "allow_loc_only": "1"}
_SETTINGS = {"t": 0.0, "v": None}


def get_settings():
    if _SETTINGS["v"] is not None and time.time() - _SETTINGS["t"] < 5:
        return _SETTINGS["v"]
    con = db()
    rows = con.execute("SELECT k,v FROM settings").fetchall()
    con.close()
    raw = dict(DEFAULT_SETTINGS)
    raw.update({r["k"]: r["v"] for r in rows})

    def num(k, default):
        try:
            return float(raw[k])
        except (TypeError, ValueError):
            return default
    s = {"loc_enabled": raw["loc_enabled"] == "1", "loc_lat": num("loc_lat", None), "loc_lng": num("loc_lng", None),
         "loc_radius": num("loc_radius", 150.0), "require_move": raw["require_move"] == "1",
         "low_light_boost": raw["low_light_boost"] == "1",
         "self_mark": raw["self_mark"] == "1", "allow_loc_only": raw["allow_loc_only"] == "1", "tolerance": min(0.65, max(0.35, num("tolerance", 0.5)))}
    _SETTINGS.update(t=time.time(), v=s)
    return s


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def location_lock_on(s=None):
    s = s or get_settings()
    return bool(s["loc_enabled"] and s["loc_lat"] is not None and s["loc_lng"] is not None)


def location_check(loc):
    """(ok, message). Browser scans must come from inside the allowed circle when the location lock is ON."""
    s = get_settings()
    if not location_lock_on(s):
        return True, ""
    try:
        lat, lng, acc = float(loc["lat"]), float(loc["lng"]), float(loc.get("acc") or 0)
    except (TypeError, KeyError, ValueError):
        return False, "Location lock is ON. Allow location access in your browser to mark attendance."
    dist = haversine_m(lat, lng, s["loc_lat"], s["loc_lng"])
    if dist - min(max(acc, 0), 100) > s["loc_radius"]:     # GPS error margin (max 100 m) is forgiven
        return False, f"You are about {int(dist)} m away from the allowed place (limit {int(s['loc_radius'])} m). Attendance can be marked only inside the campus."
    return True, ""


@app.get("/api/public-config")
def public_config():
    s = get_settings()
    return jsonify(loc_enabled=location_lock_on(s), require_move=s["require_move"], self_mark=s["self_mark"],
                   loc_only=bool(s["allow_loc_only"] and location_lock_on(s)))


@app.get("/api/settings")
def read_settings():
    if session.get("role") not in ("admin", "teacher"):
        return jsonify(error="Login required"), 401
    return jsonify(get_settings())


@app.post("/api/settings")
def save_settings():
    if session.get("role") != "admin":
        return jsonify(error="Only the admin can change settings"), 403
    d = request.json or {}
    try:
        lat = float(d["loc_lat"]) if d.get("loc_lat") not in (None, "") else None
        lng = float(d["loc_lng"]) if d.get("loc_lng") not in (None, "") else None
        radius, tol = float(d.get("loc_radius", 150)), float(d.get("tolerance", 0.5))
    except (TypeError, ValueError):
        return jsonify(error="Invalid number in settings"), 400
    if (lat is not None and not -90 <= lat <= 90) or (lng is not None and not -180 <= lng <= 180):
        return jsonify(error="Invalid map location"), 400
    if not 20 <= radius <= 5000:
        return jsonify(error="Radius must be between 20 and 5000 metres"), 400
    if not 0.35 <= tol <= 0.65:
        return jsonify(error="Invalid match strictness"), 400
    enabled = bool(d.get("loc_enabled"))
    if enabled and (lat is None or lng is None):
        return jsonify(error="Pick the allowed location on the map first"), 400
    vals = {"loc_enabled": "1" if enabled else "0", "loc_lat": "" if lat is None else repr(lat),
            "loc_lng": "" if lng is None else repr(lng), "loc_radius": repr(radius),
            "require_move": "1" if d.get("require_move") else "0",
            "low_light_boost": "1" if d.get("low_light_boost") else "0", "tolerance": repr(tol),
            "self_mark": "1" if d.get("self_mark") else "0", "allow_loc_only": "1" if d.get("allow_loc_only") else "0"}
    con = db()
    for k, v in vals.items():
        con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))
    con.commit(); con.close()
    _SETTINGS["v"] = None
    return jsonify(ok=True, settings=get_settings())


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


@app.post("/api/me/password")
@login_required("admin", "teacher")
def change_my_password():
    d = request.json or {}
    new = d.get("new", "")
    if len(new) < 6:
        return jsonify(error="New password must be at least 6 characters"), 400
    con = db()
    u = con.execute("SELECT password FROM users WHERE username=?", (session["user"],)).fetchone()
    if not u or not check_password_hash(u["password"], d.get("old", "")):
        con.close()
        return jsonify(error="Old password is incorrect"), 400
    con.execute("UPDATE users SET password=? WHERE username=?", (generate_password_hash(new), session["user"]))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.get("/api/teachers")
@login_required("admin")
def list_teachers():
    con = db()
    rows = con.execute("SELECT username FROM users WHERE role='teacher' ORDER BY username").fetchall()
    con.close()
    return jsonify([r["username"] for r in rows])


@app.delete("/api/teachers/<username>")
@login_required("admin")
def delete_teacher(username):
    con = db()
    con.execute("DELETE FROM users WHERE username=? AND role='teacher'", (username,))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.post("/api/teachers")
@login_required("admin")
def add_teacher():
    d = request.json or {}
    user, pw = (d.get("username") or "").strip(), d.get("password") or ""
    if not user or len(pw) < 6:
        return jsonify(error="Enter a username and a password of 6+ characters"), 400
    try:
        con = db()
        con.execute("INSERT INTO users(username,password,role) VALUES(?,?,'teacher')",
                    (user, generate_password_hash(pw)))
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


def enhance(rgb):
    """Brighten a dark / low-contrast frame (gamma on luminance + CLAHE). Makes face detection work in poor light."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    l, a, b = cv2.split(lab)
    m = max(float(l.mean()), 1.0)
    if m < 110:
        gamma = min(1.0, max(0.35, math.log(0.5) / math.log(m / 255.0)))
        lut = np.clip(255.0 * (np.arange(256) / 255.0) ** gamma, 0, 255).astype(np.uint8)
        l = cv2.LUT(l, lut)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)


def detect(img, boost=True):
    """HOG face detection that copes with dark frames: brighten first when the frame is clearly dark,
    and retry brightened when a dim frame gives no face. Returns (image_used, locations)."""
    enhanced = False
    mean = float(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).mean()) if boost else 255.0
    if boost and mean < 95:
        img, enhanced = enhance(img), True
    locs = face_recognition.face_locations(img, model="hog")
    if not locs and boost and not enhanced and mean < 130:
        img = enhance(img)
        locs = face_recognition.face_locations(img, model="hog")
    return img, locs


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


def do_mark(sid, subject, now, method="face"):
    date, t = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
    con = db()
    cur = con.execute("INSERT OR IGNORE INTO attendance(student_id,subject,date,time,method) VALUES(?,?,?,?,?)",
                      (sid, subject, date, t, method))
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


def confirmed(obs, require_move):
    """Decide if a recognised face may be marked. Default: the same student seen in 2+ frames within a few seconds
    (no head movement needed). With 'require head movement' ON, the old anti-photo check is used."""
    recent = [o for o in obs if obs[-1][0] - o[0] <= RECENT_SECONDS]
    if len(recent) < MIN_OBS:
        return False
    return moved(recent) if require_move else True


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
    cfg = get_settings()
    K = get_known()
    with RECOG_LOCK:
        img, locs = detect(img, cfg["low_light_boost"])
        if not locs:
            return []
        lms = face_recognition.face_landmarks(img, locs, model="small")
        encs = face_recognition.face_encodings(img, locs) if K["mat"] is not None else []

    H, W = img.shape[:2]
    pairs = []
    for i, e in enumerate(encs):
        d = np.linalg.norm(K["mat"] - e, axis=1)
        j = int(np.argmin(d))
        if d[j] <= cfg["tolerance"]:
            pairs.append((float(d[j]), i, j))
    cand, used = {}, set()
    for dist, i, j in sorted(pairs):          # one student can only match one face per frame
        if j not in used:
            used.add(j); cand[i] = (dist, j)

    now = now_local(); ts = time.time(); date = now.strftime("%Y-%m-%d")
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
                if confirmed(obs, cfg["require_move"]):
                    inserted = do_mark(sid, subject, now)
                    f.update(status="marked" if inserted else "already", time=marked_time(sid, subject, date))
                else:
                    f.update(status="verifying", need_move=cfg["require_move"])
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
    ok, msg = location_check(d.get("loc"))
    if not ok:
        return jsonify(error=msg, code="location"), 403
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
                       (GATE_SUBJECT, now_local().strftime("%Y-%m-%d"))).fetchall()
    con.close()
    return jsonify(subject=GATE_SUBJECT, entries=[dict(r) for r in rows])


# ---------------- Students ----------------
@app.post("/api/students")
@login_required("admin", "teacher")
def register_student():
    d = request.json
    encs, best, brights, seen_face = [], None, [], 0
    for f in (d.get("frames") or [])[:6]:
        img = decode(f)
        if img is None:
            continue
        h0, w0 = img.shape[:2]
        if w0 > REG_WIDTH:                                   # smaller frame = much faster detection
            img = cv2.resize(img, (REG_WIDTH, int(h0 * REG_WIDTH / w0)), interpolation=cv2.INTER_AREA)
        brights.append(float(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).mean()))
        with RECOG_LOCK:
            img, locs = detect(img, get_settings()["low_light_boost"])   # brightens dark frames automatically
            if not locs:
                continue
            loc = max(locs, key=lambda l: (l[2] - l[0]) * (l[1] - l[3]))
            encs.append(face_recognition.face_encodings(img, [loc])[0])
        seen_face += 1
        if best is None or (loc[2] - loc[0]) > (best[1][2] - best[1][0]):
            best = (img, loc)
        if len(encs) >= 3:                                   # 3 good samples are enough: stop early = faster
            break
    if len(encs) < 2:
        dark = brights and sum(brights) / len(brights) < 60
        return jsonify(error=("Too dark. Face a light source (window / lamp) and try again." if dark else
                              "Could not find a clear face. Look straight at the camera, keep your face fully in the frame and try again.")), 400
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
    con.execute("DELETE FROM leaves WHERE student_id=?", (sid,))
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
    q = """SELECT a.date,a.time,a.subject,a.method,s.roll,s.name FROM attendance a
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
        day = (now_local() - timedelta(days=k)).strftime("%Y-%m-%d")
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
    w.writerow(["Date", "Time", "Subject", "Roll", "Name", "Method"])
    for r in rows:
        w.writerow([r["date"], r["time"], r["subject"], r["roll"], r["name"], r["method"] or "face"])
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
    s = con.execute("SELECT roll,name,email,parent_email,(photo IS NOT NULL) has_photo FROM students WHERE id=?", (sid,)).fetchone()
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
    today_time = marked_time(sid, GATE_SUBJECT, now_local().strftime("%Y-%m-%d"))
    return jsonify(profile=dict(s), today=dict(marked=bool(today_time), time=today_time), present=present, total=total,
                   percent=round(present * 100 / total, 1) if total else 0, subjects=subjects)


def own_log(sid, subject=""):
    q = "SELECT date,time,subject,method FROM attendance WHERE student_id=?"
    args = [sid]
    if subject:
        q += " AND subject=?"; args.append(subject)
    con = db()
    rows = con.execute(q + " ORDER BY date DESC, time DESC", args).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/student/photo")
@student_only
def my_photo():
    con = db()
    r = con.execute("SELECT photo FROM students WHERE id=?", (session["sid"],)).fetchone()
    con.close()
    if not r or not r["photo"]:
        return Response(status=404)
    return Response(r["photo"], mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/student/profile")
@student_only
def update_profile():
    d = request.json or {}
    email, parent = (d.get("email") or "").strip(), (d.get("parent_email") or "").strip()
    for v in (email, parent):
        if v and (len(v) > 120 or not EMAIL_RE.match(v)):
            return jsonify(error="Enter a valid email address"), 400
    con = db()
    con.execute("UPDATE students SET email=?, parent_email=? WHERE id=?", (email, parent, session["sid"]))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.post("/api/student/mark")
@student_only
def student_mark():
    """Student marks their OWN daily attendance from the student app.
    mode 'face'     : live camera frames must match the student's own registered face.
    mode 'location' : fallback when the camera does not work; allowed only if the location lock is ON and the admin allows it.
    The location lock (if ON) is checked in both modes."""
    cfg, sid = get_settings(), session["sid"]
    if not cfg["self_mark"]:
        return jsonify(error="Marking from the student app is turned off by the admin"), 403
    d = request.json or {}
    ok, msg = location_check(d.get("loc"))
    if not ok:
        return jsonify(error=msg, code="location"), 403
    now = now_local()
    done = marked_time(sid, GATE_SUBJECT, now.strftime("%Y-%m-%d"))
    if done:
        return jsonify(status="already", time=done)
    if d.get("mode") == "location":
        if not (cfg["allow_loc_only"] and location_lock_on(cfg)):
            return jsonify(error="Location-only attendance is not enabled. Use the camera or ask your teacher."), 403
        do_mark(sid, GATE_SUBJECT, now, "location")
        return jsonify(status="marked", time=marked_time(sid, GATE_SUBJECT, now.strftime("%Y-%m-%d")), method="location")

    con = db()
    r = con.execute("SELECT encoding FROM students WHERE id=?", (sid,)).fetchone()
    con.close()
    if not r:
        return jsonify(error="Student not found"), 404
    mine = pickle.loads(r["encoding"])
    obs, matched, saw_face = [], 0, 0
    for i, f in enumerate((d.get("frames") or [])[:3]):
        img = decode(f)
        if img is None:
            continue
        h0, w0 = img.shape[:2]
        if w0 > REG_WIDTH:
            img = cv2.resize(img, (REG_WIDTH, int(h0 * REG_WIDTH / w0)), interpolation=cv2.INTER_AREA)
        with RECOG_LOCK:
            img, locs = detect(img, cfg["low_light_boost"])
            if not locs:
                continue
            loc = max(locs, key=lambda l: (l[2] - l[0]) * (l[1] - l[3]))
            enc = face_recognition.face_encodings(img, [loc])[0]
            lm = face_recognition.face_landmarks(img, [loc], model="small")[0]
        saw_face += 1
        if float(np.linalg.norm(mine - enc)) <= cfg["tolerance"]:
            t, rr, b, l = loc
            matched += 1
            obs.append((i * 0.5, (l + rr) / 2.0, (t + b) / 2.0, float(rr - l), yaw_ratio(lm)))
    if not saw_face:
        return jsonify(error="No face found. Face the light, keep your face inside the frame and try again.", code="noface"), 400
    if matched < 2:
        return jsonify(error="Your face did not match your profile. Try again facing the light.", code="nomatch"), 400
    if cfg["require_move"] and not moved(obs):
        return jsonify(error="Turn your head slightly while the photos are taken and try again.", code="move"), 400
    do_mark(sid, GATE_SUBJECT, now, "face")
    return jsonify(status="marked", time=marked_time(sid, GATE_SUBJECT, now.strftime("%Y-%m-%d")), method="face")


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


# ---------------- Day-wise attendance + leaves ----------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_date(x):
    if not isinstance(x, str) or not DATE_RE.match(x):
        return False
    try:
        datetime.strptime(x, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def each_day(start, end):
    cur, last = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    while cur <= last:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def daily_status(con, sid, start, end):
    """Per-day status for one student between start..end.
    present = marked at least once that day (gate or any class); leave = approved leave;
    absent = the institute was open (someone was marked) but this student wasn't and had no leave;
    pending = today, not marked yet. Days nobody attended (Sundays/holidays) are not counted."""
    today = now_local().strftime("%Y-%m-%d")
    work = {r[0] for r in con.execute("SELECT DISTINCT date FROM attendance WHERE date BETWEEN ? AND ?", (start, end))}
    mine = {r["date"]: r["t"] for r in con.execute(
        "SELECT date, MIN(time) t FROM attendance WHERE student_id=? AND date BETWEEN ? AND ? GROUP BY date", (sid, start, end))}
    on_leave = set()
    for l in con.execute("SELECT from_date,to_date FROM leaves WHERE student_id=? AND status='approved' AND to_date>=? AND from_date<=?",
                         (sid, start, end)):
        on_leave.update(each_day(max(l["from_date"], start), min(l["to_date"], end)))
    days, c = {}, {"present": 0, "absent": 0, "leave": 0, "working": 0}
    for ds in each_day(start, end):
        if ds in mine:
            days[ds] = {"status": "present", "time": mine[ds]}
        elif ds in on_leave:
            days[ds] = {"status": "leave"}
        elif ds > today or ds not in work:
            continue
        else:
            days[ds] = {"status": "pending" if ds == today else "absent"}
        st = days[ds]["status"]
        if ds in work and ds <= today:
            c["working"] += 1
            if st in ("present", "absent", "leave"):
                c[st] += 1
    counted = c["present"] + c["absent"]          # approved leave days are excused: they don't lower the percentage
    c["percent"] = round(c["present"] * 100 / counted, 1) if counted else 0
    return days, c


@app.get("/api/student/daily")
@student_only
def student_daily():
    ym = request.args.get("month") or now_local().strftime("%Y-%m")
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", ym):
        return jsonify(error="Invalid month"), 400
    y, m = int(ym[:4]), int(ym[5:7])
    start = f"{ym}-01"
    end = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
    con = db()
    days, counts = daily_status(con, session["sid"], start, end)
    first = con.execute("SELECT MIN(date) FROM attendance").fetchone()[0]
    overall = daily_status(con, session["sid"], first, now_local().strftime("%Y-%m-%d"))[1] if first else None
    con.close()
    return jsonify(month=ym, days=days, counts=counts, overall=overall)


@app.get("/api/student/leaves")
@student_only
def my_leaves():
    con = db()
    rows = con.execute("SELECT * FROM leaves WHERE student_id=? ORDER BY id DESC", (session["sid"],)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.post("/api/student/leaves")
@student_only
def apply_leave():
    d = request.json or {}
    fd = (d.get("from_date") or "").strip()
    td = (d.get("to_date") or fd).strip()
    reason = (d.get("reason") or "").strip()
    if not valid_date(fd) or not valid_date(td):
        return jsonify(error="Choose valid dates"), 400
    today = now_local().date()
    f_dt, t_dt = datetime.strptime(fd, "%Y-%m-%d").date(), datetime.strptime(td, "%Y-%m-%d").date()
    if t_dt < f_dt:
        return jsonify(error="'To' date cannot be before 'From' date"), 400
    if (t_dt - f_dt).days > 59:
        return jsonify(error="A leave can be at most 60 days"), 400
    if f_dt < today - timedelta(days=7):
        return jsonify(error="Leave can be applied for the last 7 days at most"), 400
    if len(reason) < 3 or len(reason) > 500:
        return jsonify(error="Write the reason (3 to 500 characters)"), 400
    con = db()
    if con.execute("SELECT 1 FROM leaves WHERE student_id=? AND status IN ('pending','approved') AND from_date<=? AND to_date>=?",
                   (session["sid"], td, fd)).fetchone():
        con.close()
        return jsonify(error="You already have a pending/approved leave on these dates"), 400
    con.execute("INSERT INTO leaves(student_id,from_date,to_date,reason,status,applied_at) VALUES(?,?,?,?,'pending',?)",
                (session["sid"], fd, td, reason, now_local().strftime("%Y-%m-%d %H:%M")))
    con.commit(); con.close()
    return jsonify(ok=True)


@app.delete("/api/student/leaves/<int:lid>")
@student_only
def cancel_leave(lid):
    con = db()
    cur = con.execute("DELETE FROM leaves WHERE id=? AND student_id=? AND status='pending'", (lid, session["sid"]))
    con.commit(); con.close()
    if not cur.rowcount:
        return jsonify(error="Only a pending leave can be cancelled"), 400
    return jsonify(ok=True)


@app.get("/api/leaves")
@login_required("admin", "teacher")
def list_leaves():
    status = request.args.get("status", "")
    q = "SELECT l.*, s.name, s.roll FROM leaves l JOIN students s ON s.id=l.student_id"
    args = []
    if status in ("pending", "approved", "rejected"):
        q += " WHERE l.status=?"; args.append(status)
    con = db()
    rows = con.execute(q + " ORDER BY (l.status='pending') DESC, l.id DESC LIMIT 300", args).fetchall()
    pending = con.execute("SELECT COUNT(*) FROM leaves WHERE status='pending'").fetchone()[0]
    con.close()
    return jsonify(leaves=[dict(r) for r in rows], pending=pending)


@app.post("/api/leaves/<int:lid>/decide")
@login_required("admin", "teacher")
def decide_leave(lid):
    d = request.json or {}
    status = d.get("status")
    if status not in ("approved", "rejected"):
        return jsonify(error="Status must be approved or rejected"), 400
    con = db()
    cur = con.execute("UPDATE leaves SET status=?, remark=?, decided_by=?, decided_at=? WHERE id=?",
                      (status, (d.get("remark") or "").strip()[:200], session["user"],
                       now_local().strftime("%Y-%m-%d %H:%M"), lid))
    con.commit(); con.close()
    if not cur.rowcount:
        return jsonify(error="Leave not found"), 404
    return jsonify(ok=True)


@app.get("/api/daily")
@login_required("admin", "teacher")
def daily_report():
    date = request.args.get("date") or now_local().strftime("%Y-%m-%d")
    if not valid_date(date):
        return jsonify(error="Invalid date"), 400
    con = db()
    students = con.execute("SELECT id,roll,name FROM students ORDER BY roll").fetchall()
    first = {r["student_id"]: r["t"] for r in con.execute(
        "SELECT student_id, MIN(time) t FROM attendance WHERE date=? GROUP BY student_id", (date,))}
    leave = {r["student_id"]: r["reason"] for r in con.execute(
        "SELECT student_id, reason FROM leaves WHERE status='approved' AND from_date<=? AND to_date>=?", (date, date))}
    con.close()
    working = bool(first)
    rows, c = [], {"present": 0, "absent": 0, "leave": 0}
    for s in students:
        if s["id"] in first:
            st, info = "present", first[s["id"]]
        elif s["id"] in leave:
            st, info = "leave", leave[s["id"]]
        elif working:
            st, info = "absent", ""
        else:
            st, info = "-", ""
        if st in c:
            c[st] += 1
        rows.append({"roll": s["roll"], "name": s["name"], "status": st, "info": info})
    return jsonify(date=date, working=working, counts=c, rows=rows)


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
