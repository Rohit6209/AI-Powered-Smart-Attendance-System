# Smart Attendance System (Multi-face, Head-movement Liveness, CCTV + Gate screen)

Frontend: HTML, CSS, JavaScript | Server/Backend: Python (Flask + Waitress) | Database: SQLite | AI: OpenCV + face_recognition (dlib)

## Project structure
```
app.py            Flask app: API, auth, face recognition, reports, alerts
server.py         Server launcher (Waitress HTTP, or HTTPS for camera access on other devices)
static/
  student.html/js   Student portal (own attendance only)
  admin.html/app.js Admin / teacher portal (live multi-face scan, CCTV & gate, students, reports)
  gate.html/js      Gate screen: camera + live profile card of whoever walks in
  common.js         Shared scanner / face-box drawing
  style.css
.env.example      Configuration template
Dockerfile        Container build
```

## Setup
1. Install Python 3.9 - 3.11.
2. Install dlib. Windows: install Visual Studio C++ Build Tools, then `pip install cmake dlib`. Easier: with Anaconda run `conda install -c conda-forge dlib`.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set `ADMIN_PASSWORD` (default is `admin123`).

## Run the server
| Command | Use |
|---|---|
| `python server.py` | Production-style server (Waitress) at http://localhost:5000 |
| `python server.py --https` | HTTPS server, needed when other devices open the site and use the camera |
| `python app.py` | Development mode with auto-reload |

Pages: Student `http://localhost:5000/` | Admin and teacher `http://localhost:5000/admin`

Browsers allow the camera only on `localhost` or HTTPS. For a LAN demo run `python server.py --https` and open `https://<server-ip>:5000` (accept the certificate warning). To use your own certificate put `cert.pem` and `key.pem` next to `server.py`.

Default admin login: `admin` / the `ADMIN_PASSWORD` you set.

## Docker (optional)
```
docker build -t smart-attendance .
docker run -p 5000:5000 -v attendance-data:/data --env-file .env smart-attendance
```
Open http://localhost:5000. The database is kept in the `attendance-data` volume. For other devices put an HTTPS reverse proxy (Nginx or Caddy) in front.

## How to use
1. **Students tab**: roll number, name, emails, optional login password, then "Capture face and register" (a profile photo is saved automatically).
2. **Live scan tab**: enter the subject, press *Start scanning*. All faces in view are scanned together. Each person only has to move their head a little (or walk in) - no blinking, so it is fast (about 1 second).
3. **CCTV & Gate tab**:
   - *Open gate screen* (`/gate`): put a screen + camera at the entrance. Anyone who walks in is marked present (subject `Gate Entry`) and their photo, name, roll, entry time and attendance % appear on the screen. Sign in at `/admin` once on that device first.
   - *Add CCTV / IP camera*: paste the RTSP URL (e.g. `rtsp://user:pass@192.168.1.20:554/stream1`, or `0` for a webcam on the server). Mode **Classroom** marks attendance for the given subject, mode **Gate** marks daily entry. The server reads the stream in the background (keeps running after you close the browser; restarts with the server), and *Preview* shows the live feed with boxes. A gate-mode camera can also be shown on a gate screen: `/gate?cam=<id>`.
4. **Reports tab**: attendance log, percentages, 7-day chart, CSV export, low-attendance email alerts.
5. **Student page**: students sign in with their roll number to see only their own attendance.

## Email alerts
Set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` in `.env` (Gmail needs an App Password). Without them the alert button only lists the students below the threshold.

## Settings in app.py
- `TOLERANCE` (0.5): lower is stricter face matching
- `MOVE_YAW` (0.05) / `MOVE_SHIFT` (0.30): how much head turn / walking movement proves a live person (also settable in `.env`)
- `SCAN_WIDTH` (480) / `CCTV_WIDTH` (960): frame width used for detection. Smaller = faster, larger = detects farther faces

### Tips for CCTV accuracy
Use the camera's 1080p sub-stream, mount it at face height, within about 4-5 m, with the face at least ~80 px wide. Wide-angle ceiling cameras will miss faces.

### About the movement check
The movement check replaces the blink test. It looks at head-turn (which a flat photo that is merely waved around does not produce) and walking movement, and it is much faster than waiting for a blink. It stops a still photo/screenshot, but a *video* replay of a person can still fool any camera-only check. For high-security doors add an IR/depth camera.

## Security notes
- Passwords are stored hashed; a random session secret is generated into `secret.key` (keep it private).
- Change the default admin password before any demo or deployment.

## Limitations and future scope
Future: mobile app, mask-aware recognition, cloud deployment, ERP integration.
