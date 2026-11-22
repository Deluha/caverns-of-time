"""The local PostgreSQL test instance: a throwaway Docker container on localhost.

`start` runs the container (pulling the image when needed) and waits until it accepts
connections; `stop` removes it; `dsn` prints the connection string; `status` says
whether it is running. The image, port and password are fixed here and nowhere else.
The password is a throwaway for a database that holds no data. Exit 0 = done,
1 = the instance could not be started or is not running. Stdlib only.
Details: tests/README.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence

POSTGRES_MAJOR = 18
IMAGE = f"postgres:{POSTGRES_MAJOR}"
CONTAINER = "caverns-test-pg"
HOST = "127.0.0.1"
PORT = 55432
USER = "postgres"
PASSWORD = "caverns"
DATABASE = "postgres"
DEFAULT_DSN = f"postgresql://{USER}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}"
START_TIMEOUT_SECONDS = 90


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def is_running() -> bool:
    """Whether the container exists and is running."""
    try:
        out = _docker("inspect", "--format", "{{.State.Running}}", CONTAINER)
    except FileNotFoundError:
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def is_ready() -> bool:
    """Whether the server inside the container accepts TCP connections.

    `-h` matters: without it pg_isready tests the unix socket, which also answers
    during the image's first-run bootstrap server, before the real one listens.
    """
    out = _docker("exec", CONTAINER, "pg_isready", "-h", HOST, "-U", USER, "-d", DATABASE)
    return out.returncode == 0


def start() -> int:
    """Run the container if it is not running, then wait until it is ready."""
    try:
        if not is_running():
            _docker("rm", "-f", CONTAINER)  # a stopped leftover blocks the name
            run = _docker(
                "run", "-d", "--name", CONTAINER,
                "-e", f"POSTGRES_PASSWORD={PASSWORD}",
                "-p", f"{HOST}:{PORT}:5432",
                IMAGE,
            )  # fmt: skip
            if run.returncode != 0:
                print(f"❌ test-db: docker run failed: {run.stderr.strip()}", file=sys.stderr)
                return 1
    except FileNotFoundError:
        print("❌ test-db: docker is not installed", file=sys.stderr)
        return 1
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_ready():
            print(f"✅ test-db: {IMAGE} is ready on {HOST}:{PORT}")
            return 0
        time.sleep(1)
    print(f"❌ test-db: {IMAGE} did not become ready in {START_TIMEOUT_SECONDS}s", file=sys.stderr)
    return 1


def stop() -> int:
    """Remove the container; a missing one is not an error."""
    try:
        _docker("rm", "-f", CONTAINER)
    except FileNotFoundError:
        print("❌ test-db: docker is not installed", file=sys.stderr)
        return 1
    print("✅ test-db: stopped")
    return 0


def status() -> int:
    running = is_running() and is_ready()
    print(f"{'✅' if running else '❌'} test-db: {'running' if running else 'not running'}")
    return 0 if running else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The local PostgreSQL 18 test instance (Docker).")
    parser.add_argument("command", choices=("start", "stop", "status", "dsn"))
    args = parser.parse_args(argv)
    if args.command == "dsn":
        print(DEFAULT_DSN)
        return 0
    return {"start": start, "stop": stop, "status": status}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
