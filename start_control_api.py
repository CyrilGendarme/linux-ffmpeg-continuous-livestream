#!/usr/bin/env python3
"""Serve the control API and the stream settings page over HTTPS.

Production (default): gunicorn with TLS, DEBUG off, a strong INSTANCE_API_KEY required.
Development (DJANGO_DEBUG=1): Django runserver_plus on 127.0.0.1 only (its debugger
can execute code, so it must never be reachable from the network).

Environment:
  INSTANCE_API_KEY      secret required to view/save settings (>= 16 characters in production)
  CONTROL_HOST          bind address (default 0.0.0.0; development: 127.0.0.1)
  CONTROL_PORT          HTTPS port (default 8443)
  CONTROL_TLS_CERT      certificate chain, e.g. /etc/letsencrypt/live/<domain>/fullchain.pem
  CONTROL_TLS_KEY       private key,      e.g. /etc/letsencrypt/live/<domain>/privkey.pem
                        (without both, a self-signed certificate is generated in .certs/)
  CONTROL_ALLOWED_HOSTS comma-separated host names accepted (default *)
  DJANGO_SECRET_KEY     Django signing key (default: generated once into .certs/django_secret_key)
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CERT_DIR = ROOT / ".certs"
MIN_SECRET_LENGTH = 16


def self_signed_certificate() -> tuple:
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    cert, key = CERT_DIR / "selfsigned.crt", CERT_DIR / "selfsigned.key"
    if not cert.exists() or not key.exists():
        hostname = socket.getfqdn()
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                "-keyout", str(key), "-out", str(cert), "-days", "825",
                "-subj", f"/CN={hostname}",
                "-addext", f"subjectAltName=DNS:{hostname},DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(key, 0o600)
    return str(cert), str(key)


def django_secret_key() -> str:
    path = CERT_DIR / "django_secret_key"
    if not path.exists():
        CERT_DIR.mkdir(mode=0o700, exist_ok=True)
        path.write_text(secrets.token_urlsafe(50), encoding="utf-8")
        os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8").strip()


def main() -> None:
    os.chdir(ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "streamcontrol.settings")
    development = os.environ.get("DJANGO_DEBUG", "0") == "1"
    port = os.environ.get("CONTROL_PORT", "8443")

    cert, key = os.environ.get("CONTROL_TLS_CERT"), os.environ.get("CONTROL_TLS_KEY")
    if not (cert and key):
        cert, key = self_signed_certificate()
        print(f"[control] Using a self-signed certificate ({cert}); browsers will warn. "
              "Set CONTROL_TLS_CERT/CONTROL_TLS_KEY for a real one.")

    if development:
        from django.core.management import execute_from_command_line

        host = os.environ.get("CONTROL_HOST", "127.0.0.1")
        arguments = sys.argv[1:] or ["runserver_plus", f"{host}:{port}", "--cert-file", cert, "--key-file", key]
        execute_from_command_line(["manage.py", *arguments])
        return

    secret = os.environ.get("INSTANCE_API_KEY", "")
    if len(secret) < MIN_SECRET_LENGTH or secret == "test-secret-key":
        sys.exit(
            f"INSTANCE_API_KEY must be set to a secret of at least {MIN_SECRET_LENGTH} characters.\n"
            "Tip: python3 -m livestream.service install-control generates one."
        )
    os.environ["DJANGO_DEBUG"] = "0"
    os.environ.setdefault("DJANGO_SECRET_KEY", django_secret_key())

    gunicorn = shutil.which("gunicorn")
    command = [gunicorn] if gunicorn else [sys.executable, "-m", "gunicorn"]
    host = os.environ.get("CONTROL_HOST", "0.0.0.0")
    print(f"[control] Serving https://{host}:{port}/config/")
    os.execvp(command[0], [
        *command,
        "streamcontrol.wsgi:application",
        "--bind", f"{host}:{port}",
        "--certfile", cert,
        "--keyfile", key,
        # One process keeps the brute-force counters and config writes consistent.
        "--workers", "1",
        "--threads", "4",
        "--timeout", "60",
        "--access-logfile", "-",
    ])


if __name__ == "__main__":
    main()
