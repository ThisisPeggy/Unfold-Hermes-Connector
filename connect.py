#!/usr/bin/env python3
"""Save the Browser pairing token in the Hermes environment."""

import argparse
import getpass
import os
import tempfile
from pathlib import Path


def _hermes_home(platform=None):
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    if (platform or os.name) == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "hermes"
    return Path.home() / ".hermes"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        help="Deprecated: omit this option and paste the token into the hidden prompt.",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = (args.token or getpass.getpass("Paste Unfold pairing token: ")).strip()
    if len(token) < 32 or any(ch.isspace() for ch in token):
        raise SystemExit("Invalid Unfold pairing token.")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Invalid connector port.")
    _write_env({
        "HERMES_BROWSER_CONNECTOR_TOKEN": token,
        "HERMES_BROWSER_CONNECTOR_PORT": str(args.port),
        "HERMES_BROWSER_CONNECTOR_ALLOW_ALL_USERS": "true",
    })
    print("Unfold is paired. Restart the Hermes gateway:")
    print("  hermes gateway restart")


def _write_env(values):
    home = _hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        output.append(f"{key}={pending.pop(key)}" if key in pending else line)
    output.extend(f"{key}={value}" for key, value in pending.items())
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
