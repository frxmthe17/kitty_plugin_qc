"""Проверка локальных портов: свободен ли, кто занял, слушает ли кто-то."""
from __future__ import annotations

import re
import socket
import subprocess


def port_free(bind: str, port: int) -> bool:
    """Самая честная проверка — попробовать занять порт самим."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind, port))
        except OSError:
            return False
    return True


def _listen_ports(path: str) -> set[int]:
    ports: set[int] = set()
    try:
        with open(path) as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) > 3 and parts[3] == "0A":  # LISTEN
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    except OSError:
        pass
    return ports


def listening(port: int) -> bool:
    """Кто-то слушает порт (без подключения к нему — не шумим в логах postgres)."""
    return port in _listen_ports("/proc/net/tcp") or port in _listen_ports("/proc/net/tcp6")


def owner(port: int) -> str:
    """Кто держит порт: «postgres (pid 123)» или «другой пользователь / docker»."""
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "неизвестно"
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
    if m:
        return f"{m.group(1)} (pid {m.group(2)})"
    if out.strip():
        return "процесс другого пользователя (docker / root)"
    return "неизвестно"
