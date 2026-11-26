"""qc setup / qc uninstall: systemd services, Include in ~/.ssh/config, bash and kitty hotkeys."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from qc import paths, settings, sshgen
from qc.textblock import put_block, remove_block, text_outside_block

KITTY_KEY = "ctrl+shift+m"
BASH_KEY = r"\C-g"


def qc_command() -> list[str]:
    """How to run qc from services and kitty: the script path (the pipx symlink survives `pipx upgrade`)."""
    exe = shutil.which("qc")
    if exe:
        return [exe]
    argv0 = Path(sys.argv[0])
    if argv0.name == "qc" and argv0.exists():
        return [str(argv0.absolute())]
    return [sys.executable, "-m", "qc"]


def _units_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or paths.HOME / ".config") / "systemd" / "user"


def _unit_files(schedule: str) -> dict[str, str]:
    qc = " ".join(shlex.quote(p) for p in qc_command())
    calendar = {"hourly": "hourly"}.get(schedule, "daily")
    return {
        "qcd.service": f"""\
[Unit]
Description=qc: SSH tunnels daemon
After=network-online.target

[Service]
ExecStart={qc} daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""",
        "qc-sync.service": f"""\
[Unit]
Description=qc: sync ~/qc with git
After=network-online.target

[Service]
Type=oneshot
ExecStart={qc} sync --notify
""",
        "qc-sync.timer": f"""\
[Unit]
Description=qc: scheduled sync of ~/qc

[Timer]
OnCalendar={calendar}
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
""",
    }


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def install_units() -> list[str]:
    out = []
    schedule = settings.get("sync.schedule") or "daily"
    d = _units_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in _unit_files(schedule).items():
        (d / name).write_text(text)
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", "qcd.service")
    out.append("qcd service (tunnels): " + ("started" if r.returncode == 0 else r.stderr.strip()))
    _systemctl("restart", "qcd.service")  # pick up the new program version
    out += apply_schedule(schedule)
    return out


def apply_schedule(schedule: str) -> list[str]:
    if schedule not in settings.SCHEDULES:
        raise ValueError(f"schedule: {', '.join(settings.SCHEDULES)}")
    settings.set_value("sync.schedule", schedule)
    d = _units_dir()
    if not (d / "qc-sync.timer").exists():
        return ["sync timer is not installed yet: run qc setup"]
    (d / "qc-sync.timer").write_text(_unit_files(schedule)["qc-sync.timer"])
    _systemctl("daemon-reload")
    if schedule == "off":
        _systemctl("disable", "--now", "qc-sync.timer")
        return ["scheduled sync is off"]
    r = _systemctl("enable", "--now", "qc-sync.timer")
    _systemctl("restart", "qc-sync.timer")
    word = {"daily": "daily", "hourly": "hourly"}[schedule]
    return ["sync " + word + ("" if r.returncode == 0 else f": {r.stderr.strip()}")]


def bashrc() -> Path:
    return paths.HOME / ".bashrc"


def install_bash() -> str:
    put_block(bashrc(), '[[ $- == *i* ]] && command -v qc >/dev/null && eval "$(qc shell-init bash)"')
    return f"bash: menu on Ctrl+G (in new tabs; in the current one run source ~/.bashrc)"


def shell_init_bash() -> str:
    """Code to eval in .bashrc. The menu writes the choice to a file, and the function inserts it into the prompt line."""
    return r'''__qc_menu() {
  local out
  out=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/qc-menu.XXXXXX") || return
  qc menu --out "$out"
  if [[ -s $out ]]; then
    local text
    text=$(<"$out")
    READLINE_LINE="${READLINE_LINE:0:READLINE_POINT}${text}${READLINE_LINE:READLINE_POINT}"
    READLINE_POINT=$((READLINE_POINT + ${#text}))
  fi
  rm -f "$out"
}
bind -x '"''' + BASH_KEY + r'''": __qc_menu'
'''


def kitty_conf() -> Path:
    return Path(os.environ.get("KITTY_CONFIG_DIRECTORY") or paths.HOME / ".config" / "kitty") / "kitty.conf"


def kitty_key_taken() -> str | None:
    """Whether the key is already mapped in kitty.conf outside our block."""
    for line in text_outside_block(kitty_conf()).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "map" and parts[1].lower() == KITTY_KEY:
            return line.strip()
    return None


def menu_size() -> tuple[int, int]:
    """Menu window size, columns × lines (settings menu.size)."""
    try:
        c, r = str(settings.get("menu.size") or "").lower().replace("\u0445", "x").split("x")
        return max(60, int(c)), max(15, int(r))
    except ValueError:
        return 110, 32


def popup_opts() -> list[str]:
    """Options of the floating kitty panel: centered, above all windows, with exclusive keyboard focus."""
    cols, lines = menu_size()
    return [f"edge=center-sized", f"columns={cols}", f"lines={lines}", "layer=overlay",
            "focus-policy=exclusive"]


def popup_available() -> bool:
    """Whether the menu can open as a separate centered window: kitty is installed and there is a graphical session."""
    return bool(shutil.which("kitten") and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")))


def install_kitty() -> str:
    taken = kitty_key_taken()
    if taken:
        return f"kitty: {KITTY_KEY} is already mapped in kitty.conf ({taken}); qc block not added"
    qc = " ".join(shlex.quote(p) for p in qc_command())
    # The menu opens as a centered window (a panel of the same kitty) or over the current window (menu.window = full/inline).
    # kitty fills in @active-kitty-window-id itself: the command is inserted there, and the ssh tab opens next to it.
    # --allow-remote-control lets the menu open an ssh tab and insert text.
    if (settings.get("menu.window") or "popup") == "popup":
        how = "--type=os-panel " + " ".join(f"--os-panel {o}" for o in popup_opts())
    else:
        how = "--type=overlay"
    put_block(kitty_conf(), f"map {KITTY_KEY} launch {how} --allow-remote-control "
                            f"{qc} menu --kitty @active-kitty-window-id --in-popup")
    return f"kitty: menu on Ctrl+Shift+M (takes effect after restarting kitty or Ctrl+Shift+F5)"


def setup(remote: str | None) -> list[str]:
    from qc import sync

    out = []
    if remote:
        out.append("data: " + sync.attach(remote))
    elif not sync.is_repo():
        sync.init_local()
        out.append(f"data: created {paths.data_dir()} (set the git URL later in the 'Settings' menu)")
    else:
        out.append(f"data: {paths.data_dir()} already exists")
    from qc import config

    sshgen.write(config.load())
    sshgen.install_include()
    out.append(f"ssh: Include in {sshgen.user_ssh_config()}, so `ssh <name>` works without the menu too")
    out.append(install_bash())
    out.append(install_kitty())
    out += install_units()
    return out


def uninstall() -> list[str]:
    out = []
    for name in ("qc-sync.timer", "qcd.service"):
        _systemctl("disable", "--now", name)
    for name in ("qcd.service", "qc-sync.service", "qc-sync.timer"):
        (_units_dir() / name).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    out.append("qcd and qc-sync services removed (tunnels stopped)")
    for path in (sshgen.user_ssh_config(), bashrc(), kitty_conf()):
        if remove_block(path):
            out.append(f"removed the qc block from {path}")
    out.append(f"data {paths.data_dir()} and settings {paths.config_dir()} left untouched")
    return out


def kitty_window_ok(window: str | None) -> bool:
    return bool(window and re.fullmatch(r"\d+", window))
