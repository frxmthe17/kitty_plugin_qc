"""Copy a password to the clipboard and clear it after a while (only if it still holds that password)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time


class ClipboardError(Exception):
    pass


def _tools() -> tuple[list[str], list[str], list[str]] | None:
    """(copy, paste, clear) commands for this session."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy", "--type", "text/plain"], ["wl-paste", "--no-newline"], ["wl-copy", "--clear"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"],
                ["xclip", "-selection", "clipboard", "-i", "/dev/null"])
    return None


def copy(secret: str, clear_after: int = 30) -> None:
    from qc import vault

    tools = _tools()
    if not tools:
        raise ClipboardError("no clipboard tool: install wl-clipboard (Wayland) or xclip (X11)")
    try:
        # the password goes through stdin, never through the command line
        subprocess.run(tools[0], input=secret.encode(), check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise ClipboardError(f"could not copy: {e}") from None
    if clear_after > 0:
        subprocess.Popen([sys.executable, "-m", "qc.clipboard", vault.fingerprint(secret), str(clear_after)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def clear_later(fp: str, seconds: int) -> None:
    from qc import vault

    time.sleep(seconds)
    tools = _tools()
    if not tools:
        return
    try:
        now = subprocess.run(tools[1], capture_output=True, timeout=5).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return
    if vault.fingerprint(now) == fp:  # still our password — something else copied since then is left alone
        subprocess.run(tools[2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)


if __name__ == "__main__":
    clear_later(sys.argv[1], int(sys.argv[2]))
