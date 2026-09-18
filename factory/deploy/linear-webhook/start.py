"""Prepare the dedicated volume, then execute the receiver without root access."""

from __future__ import annotations

import os
import sys


UID = GID = 10001


def start(arguments: list[str], *, volume: str = "/data") -> None:
    if not arguments:
        raise ValueError("a receiver command is required")
    if os.geteuid() == 0:
        # Open the mount itself without following symlinks. Existing database
        # ownership is deliberately not rewritten; an incompatible inbox fails.
        fd = os.open(volume, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchown(fd, UID, GID)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    if os.geteuid() != UID or os.getegid() != GID:
        raise ValueError("receiver must run as UID/GID 10001")
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    try:
        start(sys.argv[1:])
    except (OSError, ValueError):
        print("receiver startup refused; check command and dedicated volume permissions", file=sys.stderr)
        raise SystemExit(1)
