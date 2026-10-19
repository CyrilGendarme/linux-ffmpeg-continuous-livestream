#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CERT_DIR = ROOT / ".certs"
CERT_DIR.mkdir(exist_ok=True)
CERT_FILE = CERT_DIR / "dev.crt"
KEY_FILE = CERT_DIR / "dev.key"

if not CERT_FILE.exists() or not KEY_FILE.exists():
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(KEY_FILE),
            "-out",
            str(CERT_FILE),
            "-days",
            "365",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
    )

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "streamcontrol.settings")

from django.core.management import execute_from_command_line

if len(sys.argv) == 1:
    sys.argv = [
        "manage.py",
        "runserver_plus",
        "0.0.0.0:8443",
        "--cert-file",
        str(CERT_FILE),
        "--key-file",
        str(KEY_FILE),
    ]

execute_from_command_line(sys.argv)
