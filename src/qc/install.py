"""qc setup / qc uninstall: службы systemd, Include в ~/.ssh/config, горячие клавиши bash и kitty."""
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
    """Как запускать qc из служб и kitty: путь к скрипту (симлинк pipx — переживёт `pipx upgrade`)."""
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
Description=qc — демон SSH-туннелей
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
Description=qc — синхронизация ~/qc с гитом
After=network-online.target

[Service]
Type=oneshot
ExecStart={qc} sync --notify
""",
        "qc-sync.timer": f"""\
[Unit]
Description=qc — синхронизация ~/qc по расписанию

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
    out.append("служба qcd (туннели): " + ("запущена" if r.returncode == 0 else r.stderr.strip()))
    _systemctl("restart", "qcd.service")  # подхватить новую версию программы
    out += apply_schedule(schedule)
    return out


def apply_schedule(schedule: str) -> list[str]:
    if schedule not in settings.SCHEDULES:
        raise ValueError(f"расписание: {', '.join(settings.SCHEDULES)}")
    settings.set_value("sync.schedule", schedule)
    d = _units_dir()
    if not (d / "qc-sync.timer").exists():
        return ["таймер синхронизации ещё не установлен — qc setup"]
    (d / "qc-sync.timer").write_text(_unit_files(schedule)["qc-sync.timer"])
    _systemctl("daemon-reload")
    if schedule == "off":
        _systemctl("disable", "--now", "qc-sync.timer")
        return ["синхронизация по расписанию выключена"]
    r = _systemctl("enable", "--now", "qc-sync.timer")
    _systemctl("restart", "qc-sync.timer")
    word = {"daily": "раз в день", "hourly": "раз в час"}[schedule]
    return ["синхронизация " + word + ("" if r.returncode == 0 else f": {r.stderr.strip()}")]


def bashrc() -> Path:
    return paths.HOME / ".bashrc"


def install_bash() -> str:
    put_block(bashrc(), '[[ $- == *i* ]] && command -v qc >/dev/null && eval "$(qc shell-init bash)"')
    return f"bash: меню по Ctrl+G (в новых вкладках; в текущей — source ~/.bashrc)"


def shell_init_bash() -> str:
    """Код для eval в .bashrc. Меню пишет выбор в файл, а функция вставляет его в строку ввода."""
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
    """Занята ли клавиша в kitty.conf вне нашего блока."""
    for line in text_outside_block(kitty_conf()).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "map" and parts[1].lower() == KITTY_KEY:
            return line.strip()
    return None


def install_kitty() -> str:
    taken = kitty_key_taken()
    if taken:
        return f"kitty: {KITTY_KEY} уже занят в kitty.conf ({taken}) — блок qc не добавлен"
    qc = " ".join(shlex.quote(p) for p in qc_command())
    # Меню — поверх текущего окна; @active-kitty-window-id kitty подставит сам: туда вставляется команда.
    # --allow-remote-control даёт меню право открыть вкладку с ssh и вставить текст.
    put_block(kitty_conf(), f"map {KITTY_KEY} launch --type=overlay --allow-remote-control "
                            f"{qc} menu --kitty @active-kitty-window-id")
    return f"kitty: меню по Ctrl+Shift+M (подхватится после перезапуска kitty или Ctrl+Shift+F5)"


def setup(remote: str | None) -> list[str]:
    from qc import sync

    out = []
    if remote:
        out.append("данные: " + sync.attach(remote))
    elif not sync.is_repo():
        sync.init_local()
        out.append(f"данные: заведена {paths.data_dir()} (адрес гита — потом в меню «Настройки»)")
    else:
        out.append(f"данные: {paths.data_dir()} на месте")
    from qc import config

    sshgen.write(config.load())
    sshgen.install_include()
    out.append(f"ssh: Include в {sshgen.user_ssh_config()} — `ssh <имя>` работает и без меню")
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
    out.append("службы qcd и qc-sync убраны (туннели остановлены)")
    for path in (sshgen.user_ssh_config(), bashrc(), kitty_conf()):
        if remove_block(path):
            out.append(f"убран блок qc из {path}")
    out.append(f"данные {paths.data_dir()} и настройки {paths.config_dir()} не тронуты")
    return out


def kitty_window_ok(window: str | None) -> bool:
    return bool(window and re.fullmatch(r"\d+", window))
