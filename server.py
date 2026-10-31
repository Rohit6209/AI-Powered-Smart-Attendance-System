"""Server for the Smart Attendance System.

  python server.py            ->  http://0.0.0.0:5000   (waitress, multi-threaded)
  python server.py --https    ->  https://0.0.0.0:5000  (self-signed or your cert.pem/key.pem)

Browsers only allow the camera on http://localhost or on HTTPS pages, so use --https
when students/teachers open the site from other devices on the network.
"""
import argparse
import os
import socket

from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

from app import app, BASE

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "smart_attendance")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def connect_db():
    """Connect to MongoDB, ping it, and create collections + indexes. Returns db or None."""
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")          # raises error if not connected
        db = client[MONGO_DB_NAME]
        print(f"MongoDB connected  (database: {MONGO_DB_NAME})")
    except PyMongoError as e:
        print(f"MongoDB connection FAILED: {e}")
        return None

    # MongoDB me "table" = collection. Yaha banate hain (already ho to skip).
    existing = set(db.list_collection_names())
    for name in ("students", "attendance", "admins"):
        if name not in existing:
            db.create_collection(name)
            print(f"  collection created: {name}")

    # Indexes (duplicate rokne aur search fast karne ke liye)
    db.students.create_index([("roll_no", ASCENDING)], unique=True)
    db.attendance.create_index(
        [("roll_no", ASCENDING), ("date", ASCENDING)], unique=True)  # ek din me ek entry
    db.attendance.create_index([("date", ASCENDING)])
    db.admins.create_index([("username", ASCENDING)], unique=True)
    return db


def setup_health_route(db):
    """GET /health -> browser se check karo DB connected hai ya nahi."""
    def health():
        try:
            if db is None:
                raise RuntimeError("db not connected")
            db.client.admin.command("ping")
            return {"server": "ok", "db": "connected"}, 200
        except Exception as e:
            return {"server": "ok", "db": "not connected", "error": str(e)}, 500

    if "health" not in app.view_functions:
        app.add_url_rule("/health", "health", health)


def main():
    p = argparse.ArgumentParser(description="Smart Attendance server")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    p.add_argument("--https", action="store_true", help="serve over HTTPS")
    a = p.parse_args()
    ip = lan_ip()

    db = connect_db()
    app.config["MONGO_DB"] = db        # app.py me: current_app.config["MONGO_DB"].students ...
    setup_health_route(db)

    if a.https:
        cert, key = os.path.join(BASE, "cert.pem"), os.path.join(BASE, "key.pem")
        ctx = (cert, key) if os.path.exists(cert) and os.path.exists(key) else "adhoc"
        print(f"HTTPS server running (browser will warn about the self-signed certificate)")
        print(f"  Student page : https://{ip}:{a.port}/")
        print(f"  Admin page   : https://{ip}:{a.port}/admin")
        print(f"  Gate screen  : https://{ip}:{a.port}/gate")
        app.run(host=a.host, port=a.port, ssl_context=ctx, threaded=True)
    else:
        from waitress import serve
        print("Server running")
        print(f"  Student page : http://localhost:{a.port}/   (network: http://{ip}:{a.port}/)")
        print(f"  Admin page   : http://localhost:{a.port}/admin")
        print(f"  Gate screen  : http://localhost:{a.port}/gate")
        serve(app, host=a.host, port=a.port, threads=16)   # extra threads: each open CCTV preview uses one


if __name__ == "__main__":
    main()
