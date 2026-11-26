"""Разговор с демоном qcd через unix-сокет."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time

from qc import paths


class DaemonDown(Exception):
    pass


def request(cmd: str, timeout: float = 5, **kw) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(paths.socket_path()))
    except OSError as e:
        s.close()
        raise DaemonDown(str(e)) from None
    try:
        s.sendall((json.dumps({"cmd": cmd, **kw}, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        return {"ok": False, "error": "пустой ответ демона"}
    return json.loads(buf)


def alive() -> bool:
    try:
        return request("ping", timeout=1).get("ok", False)
    except (DaemonDown, OSError, ValueError):
        return False


def ensure() -> None:
    """Демон не отвечает — пробуем запустить службу systemd (если qc setup её ставил)."""
    if alive():
        return
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "start", "qcd.service"],
                       capture_output=True, timeout=10)
        for _ in range(30):
            if alive():
                return
            time.sleep(0.1)
    raise DaemonDown("демон qcd не запущен — `qc setup` поставит службу, или запусти руками: qc daemon")
