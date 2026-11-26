"""Чтение данных из ~/qc (TOML) и проверка: конфликты портов, битые ссылки, ошибки формата."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from qc import paths

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# {{port}} — подстановка в быстрых командах; {{.Names}} (docker) и {{json .}} подстановкой не считаются
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-zА-Яа-яЁё_][\w-]*)\}\}")


@dataclass
class Host:
    name: str
    host: str
    user: str | None = None
    port: int | None = None
    jump: str | None = None
    identity: str | None = None
    password: str | None = None  # имя записи в хранилище, не сам пароль
    options: dict[str, str] = field(default_factory=dict)
    description: str = ""
    group: str = ""
    file: str = ""

    @property
    def address(self) -> str:
        s = f"{self.user}@{self.host}" if self.user else self.host
        return f"{s}:{self.port}" if self.port and self.port != 22 else s


@dataclass
class Tunnel:
    name: str
    via: str
    local: int
    remote: str  # "host:port"
    bind: str = "127.0.0.1"
    autostart: bool = False
    description: str = ""
    group: str = ""
    file: str = ""

    def spec_key(self) -> tuple:
        """Что влияет на запущенный ssh — при изменении туннель перезапускается."""
        return (self.via, self.local, self.remote, self.bind)


@dataclass
class Command:
    name: str
    run: str
    title: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    group: str = ""
    file: str = ""

    @property
    def placeholders(self) -> list[str]:
        seen: list[str] = []
        for m in PLACEHOLDER_RE.finditer(self.run):
            if m.group(1) not in seen:
                seen.append(m.group(1))
        return seen

    def render(self, values: dict[str, str]) -> str:
        return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), self.run)


@dataclass
class Issue:
    level: str  # error | warning
    text: str

    def __str__(self) -> str:
        mark = "✗" if self.level == "error" else "⚠"
        return f"{mark} {self.text}"


@dataclass
class Data:
    hosts: dict[str, Host] = field(default_factory=dict)
    tunnels: dict[str, Tunnel] = field(default_factory=dict)
    commands: dict[str, Command] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)  # ошибки чтения файлов


def _files(base: Path, sub: str) -> list[Path]:
    d = base / sub
    return sorted(d.glob("*.toml")) if d.is_dir() else []


def _parse_remote(value) -> str | None:
    if isinstance(value, int):
        return f"127.0.0.1:{value}"
    if isinstance(value, str):
        host, sep, port = value.rpartition(":")
        if sep and host and port.isdigit():
            return value
    return None


def load(base: Path | None = None) -> Data:
    base = base or paths.data_dir()
    data = Data()

    def read(path: Path) -> dict | None:
        try:
            with path.open("rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            data.issues.append(Issue("error", f"{path.relative_to(base)}: не читается — {e}"))
            return None

    def add(kind: str, store: dict, name: str, obj, rel: str) -> None:
        if not NAME_RE.match(name):
            data.issues.append(Issue("error", f"{rel}: имя «{name}» — только латиница, цифры, . _ -"))
            return
        if name in store:
            data.issues.append(Issue("error", f"{rel}: {kind} «{name}» уже есть в {store[name].file}"))
            return
        store[name] = obj

    for path in _files(base, "ssh"):
        doc = read(path)
        if doc is None:
            continue
        rel, group = str(path.relative_to(base)), doc.get("group", path.stem)
        for name, h in (doc.get("hosts") or {}).items():
            if not isinstance(h, dict) or not h.get("host"):
                data.issues.append(Issue("error", f"{rel}: у сервера «{name}» нет поля host"))
                continue
            try:
                obj = Host(
                    name=name, host=str(h["host"]), user=h.get("user"),
                    port=int(h["port"]) if h.get("port") else None, jump=h.get("jump"),
                    identity=h.get("identity"), password=h.get("password"),
                    options={str(k): str(v) for k, v in (h.get("options") or {}).items()},
                    description=h.get("description", ""), group=group, file=rel,
                )
            except (TypeError, ValueError) as e:
                data.issues.append(Issue("error", f"{rel}: сервер «{name}» — {e}"))
                continue
            add("сервер", data.hosts, name, obj, rel)

    for path in _files(base, "tunnels"):
        doc = read(path)
        if doc is None:
            continue
        rel, group = str(path.relative_to(base)), doc.get("group", path.stem)
        for name, t in (doc.get("tunnels") or {}).items():
            if not isinstance(t, dict):
                continue
            remote = _parse_remote(t.get("remote"))
            local = t.get("local")
            if not t.get("via") or not isinstance(local, int) or remote is None:
                data.issues.append(Issue(
                    "error", f"{rel}: туннель «{name}» — нужны via, local (число) и remote (\"хост:порт\")"))
                continue
            obj = Tunnel(
                name=name, via=t["via"], local=local, remote=remote,
                bind=t.get("bind", "127.0.0.1"), autostart=bool(t.get("autostart", False)),
                description=t.get("description", ""), group=group, file=rel,
            )
            add("туннель", data.tunnels, name, obj, rel)

    for path in _files(base, "commands"):
        doc = read(path)
        if doc is None:
            continue
        rel, group = str(path.relative_to(base)), doc.get("group", path.stem)
        for name, c in (doc.get("commands") or {}).items():
            if not isinstance(c, dict) or not c.get("run"):
                data.issues.append(Issue("error", f"{rel}: у команды «{name}» нет поля run"))
                continue
            obj = Command(
                name=name, run=c["run"].strip(), title=c.get("title", ""),
                tags=list(c.get("tags") or []), description=c.get("description", ""),
                group=group, file=rel,
            )
            add("команда", data.commands, name, obj, rel)

    return data


def unprivileged_port_start() -> int:
    try:
        return int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text())
    except (OSError, ValueError):
        return 1024


def check(data: Data) -> list[Issue]:
    """Проверка без сети: ссылки, циклы прыжков, порты."""
    issues = list(data.issues)

    for h in data.hosts.values():
        if h.jump and h.jump not in data.hosts:
            issues.append(Issue("error", f"сервер «{h.name}»: jump «{h.jump}» — нет такого сервера"))
    for h in data.hosts.values():
        seen, cur = {h.name}, h
        while cur.jump and cur.jump in data.hosts:
            if cur.jump in seen:
                issues.append(Issue("error", f"сервер «{h.name}»: прыжки по кругу ({' → '.join(seen)})"))
                break
            seen.add(cur.jump)
            cur = data.hosts[cur.jump]

    low = unprivileged_port_start()
    by_port: dict[int, list[Tunnel]] = {}
    for t in data.tunnels.values():
        if t.via not in data.hosts:
            issues.append(Issue("error", f"туннель «{t.name}»: via «{t.via}» — нет такого сервера"))
        if not (0 < t.local < 65536):
            issues.append(Issue("error", f"туннель «{t.name}»: порт {t.local} вне диапазона"))
        elif t.local < low:
            issues.append(Issue(
                "error", f"туннель «{t.name}»: порт {t.local} < {low} — без root не открыть, возьми порт повыше"))
        by_port.setdefault(t.local, []).append(t)
    for port, ts in sorted(by_port.items()):
        if len(ts) > 1:
            names = ", ".join(f"«{t.name}»" for t in ts)
            issues.append(Issue("warning", f"порт {port} у туннелей {names} — одновременно не запустить"))
    return issues


def tunnels_on_port(data: Data, port: int) -> list[Tunnel]:
    return [t for t in data.tunnels.values() if t.local == port]


def suggest_free_port(data: Data, start: int) -> int:
    from qc.ports import port_free

    used = {t.local for t in data.tunnels.values()}
    p = max(start, unprivileged_port_start())
    while p < 65535:
        if p not in used and port_free("127.0.0.1", p):
            return p
        p += 1
    raise RuntimeError("нет свободных портов")
