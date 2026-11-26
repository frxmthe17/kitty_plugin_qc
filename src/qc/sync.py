"""Репозиторий данных ~/qc: начальная структура, подключение адреса гита, синхронизация и её статус."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager

from qc import paths, settings

SKELETON_MSG = "qc: начальная структура"
SUBDIRS = ("ssh", "tunnels", "commands", "secure")

DATA_README = """\
# Данные qc

Синхронизируется между компьютерами через git (`qc sync`, раз в день — само).
Правь файлы руками или через меню `qc`. Один файл = одна группа (проект).

- `ssh/*.toml` — серверы. Из них собирается `.generated/ssh_config` (подключён в `~/.ssh/config`),
  поэтому `ssh <имя>`, `scp`, `rsync` работают и без меню.
- `tunnels/*.toml` — туннели, их держит демон `qcd` в фоне.
- `commands/*.toml` — быстрые команды. `{{имя}}` — спросить значение при выборе.
- `secure/` — пароли, каждый в своём зашифрованном файле.

```toml
# ssh/пример.toml
group = "Пример"

[hosts.bastion]
host = "10.0.0.1"
user = "admin"

[hosts.db]
host = "192.168.1.10"
user = "pam"
jump = "bastion"          # = ssh -J
# port = 2222
# identity = "~/.ssh/id_ed25519"
# password = "db"         # имя записи в хранилище, не сам пароль
# options = { Compression = "yes" }

# tunnels/пример.toml
[tunnels.db-pg]
via = "db"                # сервер из ssh/
local = 15432             # порт на этом компьютере
remote = "127.0.0.1:5432" # куда на той стороне
autostart = false         # поднимать при входе в систему

# commands/пример.toml
[commands.pg-activity]
title = "Активные запросы"
run = "psql -h 127.0.0.1 -p {{port}} -U {{user}} -c 'select pid, state, query from pg_stat_activity'"
tags = ["pg"]
```
"""

GITIGNORE = ".generated/\n*.tmp\n"


class SyncError(Exception):
    def __init__(self, message: str, conflict: list[str] | None = None):
        super().__init__(message)
        self.conflict = conflict or []


def _env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # по таймеру спросить пароль некому — пусть падает с ошибкой
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    env["LC_ALL"] = "C"
    return env


def git(*args: str, check: bool = True, timeout: int = 60, cwd=None) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", *args], cwd=cwd or paths.data_dir(), env=_env(),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {args[0]}: нет ответа {timeout} с — нет сети?") from None
    if check and r.returncode != 0:
        lines = [ln.strip() for ln in (r.stderr or r.stdout).splitlines() if ln.strip()]
        main = next((ln for ln in lines if ln.lower().startswith(("fatal:", "error:", "remote: error"))),
                    lines[0] if lines else f"код {r.returncode}")
        raise SyncError(f"git {args[0]}: {main}")
    return r


def _identity_args() -> list[str]:
    r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
    if r.stdout.strip():
        return []
    return ["-c", "user.name=qc", "-c", f"user.email=qc@{socket.gethostname()}"]


def is_repo() -> bool:
    return (paths.data_dir() / ".git").exists()


def remote_url() -> str:
    if not is_repo():
        return ""
    return git("remote", "get-url", "origin", check=False).stdout.strip()


def branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def commit_local(message: str) -> bool:
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    git(*_identity_args(), "commit", "-q", "-m", f"{message} ({socket.gethostname()}, {stamp})")
    return True


def _write_skeleton() -> None:
    base = paths.data_dir()
    base.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (base / sub).mkdir(exist_ok=True)
        keep = base / sub / ".gitkeep"
        if not any(p for p in (base / sub).iterdir() if p.name != ".gitkeep"):
            keep.touch()
    for name, text in (("README.md", DATA_README), (".gitignore", GITIGNORE)):
        if not (base / name).exists():
            (base / name).write_text(text)
    os.chmod(base / "secure", 0o700)


def init_local() -> None:
    """~/qc без гита (новый компьютер, адрес ещё не задан) — заводим локальный репозиторий."""
    _write_skeleton()
    if not is_repo():
        git("init", "-q", "-b", "main")
    if git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
        git("add", "-A")
        git(*_identity_args(), "commit", "-q", "-m", SKELETON_MSG)


def _pristine() -> bool:
    """В локальном репо только начальная структура — его можно смело заменить данными с сервера."""
    log = git("log", "--format=%s", check=False).stdout.strip().splitlines()
    clean = not git("status", "--porcelain").stdout.strip()
    skeleton = {"README.md", ".gitignore", *(f"{d}/.gitkeep" for d in SUBDIRS)}
    files = set(git("ls-files", check=False).stdout.split())
    return clean and log == [SKELETON_MSG] and files <= skeleton


def attach(url: str) -> str:
    """Подключить репозиторий данных (меню «Настройки» или `qc setup <адрес>`). Возвращает, что сделано."""
    url = url.strip()
    if not url:
        raise SyncError("адрес пустой")
    base = paths.data_dir()
    if not base.exists() or not any(base.iterdir()):
        base.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", url, str(base), cwd=base.parent, timeout=120)
        if git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
            init_local()  # репозиторий на сервере пустой — кладём структуру
            git("push", "-q", "-u", "origin", branch())
            done = "репозиторий пустой — положил начальную структуру и отправил"
        else:
            _write_skeleton()
            done = "данные скачаны"
    else:
        init_local()
        old = remote_url()
        git("remote", "set-url" if old else "add", "origin", url)
        try:
            git("fetch", "-q", "origin", timeout=120)
        except SyncError:
            # адрес не рабочий — вернуть как было, чтобы не сломать синхронизацию
            git("remote", "set-url", "origin", old) if old else git("remote", "remove", "origin")
            raise
        heads = [ln.split("refs/heads/", 1)[1] for ln in
                 git("ls-remote", "--heads", "origin").stdout.splitlines() if "refs/heads/" in ln]
        if not heads:
            commit_local("qc: правки до подключения репозитория")
            git("push", "-q", "-u", "origin", branch())
            done = "репозиторий пустой — отправил туда данные с этого компьютера"
        else:
            rb = "main" if "main" in heads else heads[0]
            if _pristine():
                git("checkout", "-q", "-B", rb, f"origin/{rb}")
                done = "данные скачаны"
            else:
                commit_local("qc: правки до подключения репозитория")
                r = git(*_identity_args(), "merge", "-q", "--allow-unrelated-histories", "--no-edit",
                        f"origin/{rb}", check=False)
                if r.returncode != 0:
                    files = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                    git("merge", "--abort", check=False)
                    raise SyncError("локальные данные конфликтуют с репозиторием: " + ", ".join(files), files)
                if branch() != rb:
                    git("branch", "-q", "-M", rb)
                git("push", "-q", "-u", "origin", rb)
                done = "локальные данные объединены с репозиторием и отправлены"
            git("branch", "-q", "-u", f"origin/{rb}", check=False)
            _write_skeleton()
    settings.set_value("sync.remote", url)
    _after_change()
    _record({"ok": True, "message": f"подключён {url}: {done}"})
    return done


def set_remote_only(url: str) -> None:
    settings.set_value("sync.remote", url.strip())


def _after_change() -> None:
    """После скачивания: пересобрать ssh_config (демон и сам заметит изменения файлов)."""
    from qc import config, sshgen

    try:
        sshgen.write(config.load())
    except OSError:
        pass


# ---------- синхронизация ----------

def _state_file():
    return paths.state_dir() / "sync.json"


def last_result() -> dict | None:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return None


def _record(result: dict) -> None:
    result["time"] = time.time()
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(result, ensure_ascii=False))
    with open(paths.state_dir() / "sync.log", "a") as f:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        f.write(f"{stamp} {'✓' if result['ok'] else '✗'} {result['message']}\n")


@contextmanager
def _lock():
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    with open(paths.state_dir() / "sync.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("синхронизация уже идёт") from None
        yield


def _counts(br: str) -> tuple[int, int]:
    """(отстаём, опережаем) относительно origin/<ветка> по последнему fetch."""
    r = git("rev-list", "--left-right", "--count", f"origin/{br}...HEAD", check=False)
    if r.returncode != 0:
        return 0, int(git("rev-list", "--count", "HEAD", check=False).stdout.strip() or 0)
    behind, ahead = r.stdout.split()
    return int(behind), int(ahead)


def _sync() -> dict:
    if not is_repo():
        raise SyncError("~/qc ещё не репозиторий — добавь адрес гита в Настройках")
    if not remote_url():
        raise SyncError("адрес репозитория данных не задан — Настройки → адрес гита")
    gitdir = paths.data_dir() / ".git"
    if (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists() or (gitdir / "MERGE_HEAD").exists():
        raise SyncError("в ~/qc незаконченное слияние — реши руками (git status)")
    commit_local("qc sync: правки")
    br = branch()
    git("fetch", "-q", "origin", timeout=60)
    pulled = 0
    has_remote = git("rev-parse", "--verify", "--quiet", f"origin/{br}", check=False).returncode == 0
    if has_remote:
        behind, _ = _counts(br)
        if behind:
            r = git("rebase", "-q", f"origin/{br}", check=False)
            if r.returncode != 0:
                files = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                git("rebase", "--abort", check=False)
                raise SyncError("конфликт в " + (", ".join(files) or "файлах") + " — реши руками в ~/qc", files)
            pulled = behind
    _, ahead = _counts(br) if has_remote else (0, int(git("rev-list", "--count", "HEAD").stdout))
    if ahead:
        git("push", "-q", "-u", "origin", br, timeout=60)
    parts = []
    if pulled:
        parts.append(f"скачано {pulled}")
    if ahead:
        parts.append(f"отправлено {ahead}")
    if pulled:
        _after_change()
    return {"ok": True, "message": ", ".join(parts) or "всё уже совпадало", "pulled": pulled, "pushed": ahead}


def sync(notify: bool = False) -> dict:
    try:
        with _lock():
            try:
                result = _sync()
            except SyncError as e:
                result = {"ok": False, "message": str(e), "conflict": e.conflict}
            _record(result)
    except SyncError as e:  # уже идёт
        return {"ok": False, "message": str(e), "busy": True}
    if notify and not result["ok"]:
        subprocess.run(["notify-send", "-a", "qc", "-i", "dialog-warning", "qc: синхронизация не прошла",
                        result["message"]], capture_output=True)
    return result


def log_tail(n: int = 20) -> list[str]:
    try:
        return (paths.state_dir() / "sync.log").read_text().splitlines()[-n:]
    except OSError:
        return []


def fetch() -> bool:
    """Тихий fetch для статуса в меню (узнать, есть ли новое на сервере)."""
    if not (is_repo() and remote_url()):
        return False
    try:
        git("fetch", "-q", "origin", timeout=20)
        return True
    except SyncError:
        return False


def next_run() -> dt.datetime | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "qc-sync.timer", "-p", "NextElapseUSecRealtime", "--value"],
            capture_output=True, text=True, timeout=3).stdout.split()
        return dt.datetime.strptime(f"{out[1]} {out[2]}", "%Y-%m-%d %H:%M:%S")
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return None


def status() -> dict:
    st: dict = {"repo": is_repo(), "remote": "", "dirty": 0, "ahead": 0, "behind": 0,
                "last": last_result(), "next": next_run(), "schedule": settings.get("sync.schedule"),
                "merging": False}
    if not st["repo"]:
        return st
    st["remote"] = remote_url()
    gitdir = paths.data_dir() / ".git"
    st["merging"] = any((gitdir / n).exists() for n in ("rebase-merge", "rebase-apply", "MERGE_HEAD"))
    try:
        st["dirty"] = len(git("status", "--porcelain").stdout.splitlines())
        br = branch()
        if git("rev-parse", "--verify", "--quiet", f"origin/{br}", check=False).returncode == 0:
            st["behind"], st["ahead"] = _counts(br)
        elif st["remote"]:
            st["ahead"] = int(git("rev-list", "--count", "HEAD", check=False).stdout.strip() or 0)
    except SyncError:
        pass
    return st


def _when(t: dt.datetime) -> str:
    today = dt.date.today()
    if t.date() == today:
        day = "сегодня"
    elif t.date() == today - dt.timedelta(days=1):
        day = "вчера"
    elif t.date() == today + dt.timedelta(days=1):
        day = "завтра"
    else:
        day = t.strftime("%d.%m")
    return f"{day} {t:%H:%M}"


def status_line(st: dict) -> tuple[str, str]:
    """(уровень, текст) для строки внизу меню и `qc status`. Уровень: ok | warn | error | off."""
    if not st["repo"] or not st["remote"]:
        return "off", "○ синхронизация не настроена — Настройки → адрес репозитория данных"
    last = st["last"]
    if st["merging"]:
        return "error", "⚠ в ~/qc незаконченное слияние — реши руками (git status)"
    extra = []
    if st["ahead"]:
        extra.append(f"↑{st['ahead']} не отправлено")
    if st["behind"]:
        extra.append(f"↓{st['behind']} есть на сервере")
    if st["dirty"]:
        extra.append(f"правок не в git: {st['dirty']}")
    if st["schedule"] == "off":
        extra.append("авто выключено")
    elif st["next"]:
        extra.append(f"следующая {_when(st['next'])}")
    if last and not last.get("ok"):
        when = _when(dt.datetime.fromtimestamp(last["time"]))
        if last.get("conflict"):
            return "error", f"⚠ {when}: {last['message']}"
        return "error", f"✗ синхр. {when}: {last['message']}" + (" · " + " · ".join(extra) if extra else "")
    head = f"✓ синхр. {_when(dt.datetime.fromtimestamp(last['time']))}" if last else "○ синхронизаций ещё не было"
    level = "warn" if (st["ahead"] or st["behind"] or st["dirty"] or not last) else "ok"
    return level, " · ".join([head, *extra])
