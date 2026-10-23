"""Install and control the livestream as a systemd *user* service (no sudo needed).

    python3 -m livestream.service install [--config config.json] [--profile NAME]
    python3 -m livestream.service start | stop | restart | status | logs | uninstall

`install` enables the service at boot but does not start broadcasting; run
`start` for that. systemd restarts it automatically if it crashes.

The HTTPS settings page runs as a second service:

    python3 -m livestream.service install-control [--tls-cert FULLCHAIN --tls-key PRIVKEY] [--port 8443]
    python3 -m livestream.service control-status | control-logs | uninstall-control

The OBS takeover (MediaMTX + livestream.switcher) runs as two more services:

    python3 -m livestream.service install-switcher [--config config.json]
    python3 -m livestream.service switcher-status | switcher-logs | uninstall-switcher
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
MEDIAMTX_SERVICE_NAME = "music-livestream-mediamtx"
SWITCHER_SERVICE_NAME = "music-livestream-switcher"


def systemctl(*args: str, check: bool = True) -> int:
    return subprocess.run(["systemctl", "--user", *args], check=check).returncode


def render_unit(config: Path, profile: str) -> str:
    command = [sys.executable, "-u", "-m", "livestream.runner", "--config", str(config)]
    if profile:
        command += ["--profile", profile]
    return f"""[Unit]
Description=Continuous music livestream (FFmpeg)
Wants=network-online.target
# With the OBS takeover, the radio streams to the local MediaMTX.
After=network-online.target {MEDIAMTX_SERVICE_NAME}.service

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


def _unit_path(name: str) -> Path:
    return UNIT_PATH.with_name(f"{name}.service")


def install_switcher(config: Path) -> None:
    """MediaMTX (receives the radio and OBS) and the switcher (sends one of them to output_url)."""
    if not config.exists():
        sys.exit(f"Config not found: {config}")
    python = f'{sys.executable} -u -m livestream.switcher --config "{config}"'
    units = {
        MEDIAMTX_SERVICE_NAME: f"""[Unit]
Description=MediaMTX media server for the music livestream (radio + OBS ingest)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={REPO_ROOT}
# mediamtx.yml loops this clip on the live path when no source is relayed.
ExecStartPre={python} --prepare-offline-clip
# Paths in mediamtx.yml are relative to its folder.
ExecStart=/bin/sh -c 'cd mediamtx && exec ./mediamtx mediamtx.yml'
Restart=always
RestartSec=5
TimeoutStartSec=300

[Install]
WantedBy=default.target
""",
        SWITCHER_SERVICE_NAME: f"""[Unit]
Description=Music livestream source switcher (OBS takes over the radio)
Requires={MEDIAMTX_SERVICE_NAME}.service
After={MEDIAMTX_SERVICE_NAME}.service

[Service]
Type=simple
WorkingDirectory={REPO_ROOT}
ExecStart={python}
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=default.target
""",
    }
    for name, text in units.items():
        path = _unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    systemctl("daemon-reload")
    for name in units:
        systemctl("enable", name)
    print(f"Installed and enabled {MEDIAMTX_SERVICE_NAME} and {SWITCHER_SERVICE_NAME}.")
    print("Start them (the radio service too) with:\n"
          f"    systemctl --user start {MEDIAMTX_SERVICE_NAME} {SWITCHER_SERVICE_NAME} {SERVICE_NAME}")
    print("Open TCP port 1935 in the firewall for OBS (e.g. sudo ufw allow 1935/tcp).")


def uninstall_switcher() -> None:
    for name in (SWITCHER_SERVICE_NAME, MEDIAMTX_SERVICE_NAME):
        systemctl("disable", "--now", name, check=False)
        if _unit_path(name).exists():
            _unit_path(name).unlink()
    systemctl("daemon-reload")
    print(f"Removed {SWITCHER_SERVICE_NAME} and {MEDIAMTX_SERVICE_NAME}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the music livestream systemd user services.")
    parser.add_argument(
        "action",
        choices=[
            "install", "uninstall", "start", "stop", "restart", "status", "logs",
            "install-control", "uninstall-control", "control-status", "control-logs",
            "install-switcher", "uninstall-switcher", "switcher-status", "switcher-logs",
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
    elif args.action == "install-switcher":
        install_switcher(Path(args.config).resolve())
    elif args.action == "uninstall-switcher":
        uninstall_switcher()
    elif args.action in ("status", "control-status", "switcher-status"):
        names = {"control-status": [CONTROL_SERVICE_NAME], "switcher-status": [MEDIAMTX_SERVICE_NAME, SWITCHER_SERVICE_NAME]}
        systemctl("status", "--no-pager", *names.get(args.action, [SERVICE_NAME]), check=False)
    elif args.action in ("logs", "control-logs", "switcher-logs"):
        names = {"control-logs": [CONTROL_SERVICE_NAME], "switcher-logs": [MEDIAMTX_SERVICE_NAME, SWITCHER_SERVICE_NAME]}
        units = [part for name in names.get(args.action, [SERVICE_NAME]) for part in ("-u", name)]
        subprocess.run(["journalctl", "--user", *units, "-n", "100", "--no-pager"])
    else:
        systemctl(args.action, SERVICE_NAME)
        systemctl("status", "--no-pager", "--lines=5", SERVICE_NAME, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
