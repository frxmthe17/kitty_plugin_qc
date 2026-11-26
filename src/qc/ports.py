"""Local port checks: is it free, who holds it, is anyone listening."""
from __future__ import annotations

import re
import socket
import subprocess


def norm_bind(bind: str | None) -> str:
    """Local address of a tunnel as ssh binds it: '' / '*' → 0.0.0.0, localhost → 127.0.0.1, [::1] → ::1."""
    b = (bind or "127.0.0.1").strip().strip("[]")
    return {"localhost": "127.0.0.1", "*": "0.0.0.0", "": "0.0.0.0"}.get(b.lower(), b)


def is_wildcard(bind: str | None) -> bool:
    """0.0.0.0 / :: — every address of this computer (open to the network)."""
    return norm_bind(bind) in ("0.0.0.0", "::")


def overlap(a: str | None, b: str | None) -> bool:
    """Whether two tunnels on the same port get in each other's way: 127.0.0.2 and 127.0.0.3 do not,
    0.0.0.0 clashes with any address."""
    a, b = norm_bind(a), norm_bind(b)
    return a == b or is_wildcard(a) or is_wildcard(b)


def endpoint(bind: str | None, port: int) -> str:
    b = norm_bind(bind)
    return f"[{b}]:{port}" if ":" in b else f"{b}:{port}"


def short(bind: str | None, port: int) -> str:
    """For lists: just the port for the usual 127.0.0.1, otherwise address:port."""
    return str(port) if norm_bind(bind) == "127.0.0.1" else endpoint(bind, port)


def port_free(bind: str, port: int) -> bool:
    """The most reliable check: try to bind the address and port ourselves."""
    b = norm_bind(bind)
    family = socket.AF_INET6 if ":" in b else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((b, port))
        except OSError:
            return False
    return True


def _addr(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # IPv6 in /proc: four 32-bit words, each little-endian
    return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))


def _listeners(path: str) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    try:
        with open(path) as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) > 3 and parts[3] == "0A":  # LISTEN
                    addr, port = parts[1].rsplit(":", 1)
                    out.add((_addr(addr), int(port, 16)))
    except (OSError, ValueError):
        pass
    return out


def listening(port: int, bind: str = "127.0.0.1") -> bool:
    """Whether someone listens on exactly this address and port
    (without connecting to it, so we do not spam the postgres logs)."""
    b = norm_bind(bind)
    return (b, port) in _listeners("/proc/net/tcp") | _listeners("/proc/net/tcp6")


def owner(port: int, bind: str | None = None) -> str:
    """Who holds the port (on this address): 'postgres (pid 123)' or 'another user / docker'."""
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if bind is not None:
        def local(ln: str) -> str:
            cols = ln.split()
            return cols[3].rsplit(":", 1)[0].strip("[]").split("%")[0] if len(cols) > 3 else ""
        lines = [ln for ln in lines if overlap(local(ln), bind)] or lines
    for ln in lines:
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', ln)
        if m:
            return f"{m.group(1)} (pid {m.group(2)})"
    if lines:
        return "a process of another user (docker / root)"
    return "unknown"
