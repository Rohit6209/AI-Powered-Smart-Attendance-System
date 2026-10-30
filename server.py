"""Server for the Smart Attendance System.

  python server.py            ->  http://0.0.0.0:5000   (waitress, multi-threaded)
  python server.py --https    ->  https://0.0.0.0:5000  (self-signed or your cert.pem/key.pem)

Browsers only allow the camera on http://localhost or on HTTPS pages, so use --https
when students/teachers open the site from other devices on the network.
"""
import argparse
import os
import socket

from app import app, BASE


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description="Smart Attendance server")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    p.add_argument("--https", action="store_true", help="serve over HTTPS")
    a = p.parse_args()
    ip = lan_ip()

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
