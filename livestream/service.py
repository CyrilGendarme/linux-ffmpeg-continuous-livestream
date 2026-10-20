"""Install and control the livestream as a systemd *user* service (no sudo needed).

    python3 -m livestream.service install [--config config.json] [--profile NAME]
    python3 -m livestream.service start | stop | restart | status | logs | uninstall

`install` enables the service at boot but does not start broadcasting; run
`start` for that. systemd restarts it automatically if it crashes.

The HTTPS settings page runs as a second service:

    python3 -m livestream.service install-control [--tls-cert FULLCHAIN --tls-key PRIVKEY] [--port 8443]
    python3 -m livestream.service control-status | control-logs | uninstall-control
"""

import argparse
import getpass
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from .constants import REPO_ROOT

SERVICE_NAME = "music-livestream"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
CONTROL_SERVICE_NAME = "music-livestream-control"
CONTROL_UNIT_PATH = UNIT_PATH.with_name(f"{CONTROL_SERVICE_NAME}.service")
CONTROL_ENV_PATH = Path.home() / ".config" / "music-livestream" / "control.env"


def systemctl(*args: str, check: bool = True) -> int:
    return subprocess.run(["systemctl", "--user", *args], check=check).returncode


def render_unit(config: Path, profile: str) -> str:
    command = [sys.executable, "-u", "-m", "livestream.runner", "--config", str(config)]
    if profile:
        command += ["--profile", profile]
    return f"""[Unit]
Description=Continuous music livestream (FFmpeg)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={REPO_ROOT}
ExecStart={" ".join(f'"{part}"' if " " in part else part for part in command)}
Restart=always
RestartSec=5
# Give FFmpeg time to close the RTMP connection cleanly.
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""


def install(config: Path, profile: str) -> None:
    if not config.exists():
        sys.exit(f"Config not found: {config}")
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(render_unit(config.resolve(), profile), encoding="utf-8")
    systemctl("daemon-reload")
    systemctl("enable", SERVICE_NAME)
    print(f"Installed and enabled {UNIT_PATH}")
    print("Start broadcasting with:  python3 -m livestream.service start")

    linger = subprocess.run(
        ["loginctl", "show-user", getpass.getuser(), "--property=Linger"], capture_output=True, text=True
    ).stdout.strip()
    if linger != "Linger=yes":
        print(
            "\nNOTE: user services stop when you log out and don't start at boot until you run once:\n"
            f"    sudo loginctl enable-linger {getpass.getuser()}"
        )


def uninstall() -> None:
    systemctl("disable", "--now", SERVICE_NAME, check=False)
    if UNIT_PATH.exists():
        UNIT_PATH.unlink()
    systemctl("daemon-reload")
    print(f"Removed {SERVICE_NAME}")


def _read_env(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def install_control(port: int, tls_cert: str, tls_key: str) -> None:
    """Install the HTTPS settings page as a user service, with a generated secret."""
    for path in (tls_cert, tls_key):
        if path and not os.access(path, os.R_OK):
            sys.exit(f"Cannot read {path} as {getpass.getuser()}. Give this user read access (e.g. a group on /etc/letsencrypt).")
    env = _read_env(CONTROL_ENV_PATH)
    created = "INSTANCE_API_KEY" not in env
    env.setdefault("INSTANCE_API_KEY", secrets.token_urlsafe(24))
    env["CONTROL_PORT"] = str(port)
    env["STREAM_CONFIG_PATH"] = str(REPO_ROOT / "config.json")
    if tls_cert and tls_key:
        env["CONTROL_TLS_CERT"], env["CONTROL_TLS_KEY"] = tls_cert, tls_key

    CONTROL_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_ENV_PATH.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
    os.chmod(CONTROL_ENV_PATH, 0o600)

    CONTROL_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_UNIT_PATH.write_text(
        f"""[Unit]
Description=Music livestream settings page (HTTPS)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={REPO_ROOT}
EnvironmentFile={CONTROL_ENV_PATH}
ExecStart={sys.executable} {REPO_ROOT / "start_control_api.py"}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""",
        encoding="utf-8",
    )
    systemctl("daemon-reload")
    systemctl("enable", "--now", CONTROL_SERVICE_NAME)
    systemctl("restart", CONTROL_SERVICE_NAME)
    print(f"Settings page: https://<this-server>:{port}/config/")
    if created:
        print(f"\nAdmin secret (shown once, stored in {CONTROL_ENV_PATH}):\n\n    {env['INSTANCE_API_KEY']}\n")
    else:
        print(f"The existing admin secret in {CONTROL_ENV_PATH} was kept.")
    if not (tls_cert and tls_key):
        print("Using a self-signed certificate: pass --tls-cert/--tls-key (e.g. Let's Encrypt) to avoid browser warnings.")
    print(f"Open TCP port {port} in the firewall (e.g. sudo ufw allow {port}/tcp).")


def uninstall_control() -> None:
    systemctl("disable", "--now", CONTROL_SERVICE_NAME, check=False)
    if CONTROL_UNIT_PATH.exists():
        CONTROL_UNIT_PATH.unlink()
    systemctl("daemon-reload")
    print(f"Removed {CONTROL_SERVICE_NAME} (the secret in {CONTROL_ENV_PATH} was kept)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the music livestream systemd user services.")
    parser.add_argument(
        "action",
        choices=[
            "install", "uninstall", "start", "stop", "restart", "status", "logs",
            "install-control", "uninstall-control", "control-status", "control-logs",
        ],
    )
    parser.add_argument("--config", default=str(REPO_ROOT / "config.json"), help="Config file used by the service")
    parser.add_argument("--profile", default="", help="Config profile used by the service (e.g. mixcloud)")
    parser.add_argument("--port", type=int, default=8443, help="HTTPS port of the settings page")
    parser.add_argument("--tls-cert", default="", help="TLS certificate chain (e.g. Let's Encrypt fullchain.pem)")
    parser.add_argument("--tls-key", default="", help="TLS private key (e.g. Let's Encrypt privkey.pem)")
    args = parser.parse_args()

    if shutil.which("systemctl") is None:
        sys.exit("systemctl not found: this needs a systemd-based Linux.")

    if args.action == "install":
        install(Path(args.config), args.profile)
    elif args.action == "uninstall":
        uninstall()
    elif args.action == "install-control":
        install_control(args.port, args.tls_cert, args.tls_key)
    elif args.action == "uninstall-control":
        uninstall_control()
    elif args.action in ("status", "control-status"):
        name = CONTROL_SERVICE_NAME if args.action == "control-status" else SERVICE_NAME
        systemctl("status", "--no-pager", name, check=False)
    elif args.action in ("logs", "control-logs"):
        name = CONTROL_SERVICE_NAME if args.action == "control-logs" else SERVICE_NAME
        subprocess.run(["journalctl", "--user", "-u", name, "-n", "100", "--no-pager"])
    else:
        systemctl(args.action, SERVICE_NAME)
        systemctl("status", "--no-pager", "--lines=5", SERVICE_NAME, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
