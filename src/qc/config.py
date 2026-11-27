"""Reading data from ~/qc (TOML) and checking it: port conflicts, broken references, format errors."""
from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from qc import paths

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# {{port}} is a placeholder in commands (any alphabet); {{.Names}} (docker) and {{json .}} are not placeholders
PLACEHOLDER_RE = re.compile(r"\{\{([^\W\d][\w-]*)\}\}")


@dataclass
class Host:
    name: str
    host: str
    user: str | None = None
    port: int | None = None
    jump: str | None = None
    identity: str | None = None
    password: str | None = None  # entry name in the password vault, not the password itself
    options: dict[str, str] = field(default_factory=dict)
    description: str = ""
    group: str = ""  # menu folder: "prod/eu"
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

    @property
    def keep_open(self) -> bool:
        """No ports: the tunnel just keeps an ssh connection to `via` open; ssh, scp and jumps through it reuse it."""
        return not self.local

    def spec_key(self) -> tuple:
        """What affects the running ssh: when it changes, the tunnel is restarted."""
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
    issues: list[Issue] = field(default_factory=list)  # file read errors


def _files(base: Path, sub: str) -> list[Path]:
    """All *.toml files of a section, including subfolders (ssh/prod/eu.toml)."""
    d = base / sub
    return sorted(f for f in d.rglob("*.toml") if not any(x.startswith(".") for x in f.relative_to(d).parts)) \
        if d.is_dir() else []


def norm_dir(path: str) -> str:
    """'/prod//eu/' → 'prod/eu'."""
    return "/".join(x.strip() for x in str(path).split("/") if x.strip())


def group_path(base: Path, sub: str, path: Path, doc: dict) -> str:
    """Menu folder of an entry: the file's subfolders + group (or the file name). group = "prod/eu" means nested folders."""
    rel = path.relative_to(base / sub)
    parent = "/".join(rel.parts[:-1])
    own = doc.get("group") or rel.stem
    return norm_dir(f"{parent}/{own}")


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
            data.issues.append(Issue("error", f"{path.relative_to(base)}: cannot be read: {e}"))
            return None

    def add(kind: str, store: dict, name: str, obj, rel: str) -> None:
        if not NAME_RE.match(name):
            data.issues.append(Issue("error", f"{rel}: name '{name}': only Latin letters, digits, . _ -"))
            return
        if name in store:
            data.issues.append(Issue("error", f"{rel}: {kind} '{name}' already exists in {store[name].file}"))
            return
        store[name] = obj

    for path in _files(base, "ssh"):
        doc = read(path)
        if doc is None:
            continue
        rel, group = str(path.relative_to(base)), group_path(base, "ssh", path, doc)
        for name, h in (doc.get("hosts") or {}).items():
            if not isinstance(h, dict) or not h.get("host"):
                data.issues.append(Issue("error", f"{rel}: server '{name}' has no host field"))
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
                data.issues.append(Issue("error", f"{rel}: server '{name}': {e}"))
                continue
            add("server", data.hosts, name, obj, rel)

    for path in _files(base, "tunnels"):
        doc = read(path)
        if doc is None:
            continue
        rel, group = str(path.relative_to(base)), group_path(base, "tunnels", path, doc)
        for name, t in (doc.get("tunnels") or {}).items():
            if not isinstance(t, dict):
                continue
            local, raw_remote = t.get("local"), t.get("remote")
            if t.get("via") and local is None and raw_remote is None:
                local, remote = 0, ""  # only via: keep the connection open (an ssh master), no ports
            else:
                remote = _parse_remote(raw_remote)
                if not t.get("via") or not isinstance(local, int) or remote is None:
                    data.issues.append(Issue(
                        "error", f"{rel}: tunnel '{name}' needs via, local (number) and remote (\"host:port\") "
                                 "— or only via, to keep the connection open"))
                    continue
            obj = Tunnel(
                name=name, via=t["via"], local=local, remote=remote,
                bind=t.get("bind", "127.0.0.1"), autostart=bool(t.get("autostart", False)),
                description=t.get("description", ""), group=group, file=rel,
            )
            add("tunnel", data.tunnels, name, obj, rel)

    # one file = one command: commands/<folder…>/<name>.toml, the menu folder is the real folder
    for path in _files(base, "commands"):
        doc = read(path)
        if doc is None:
            continue
        rel = str(path.relative_to(base))
        if isinstance(doc.get("commands"), dict):
            data.issues.append(Issue("error", f"{rel}: old format — one file holds one command now: "
                                              f"move each [commands.<name>] to commands/<folder>/<name>.toml"))
            continue
        c = doc
        if not isinstance(c.get("run"), str) or not c["run"].strip():
            data.issues.append(Issue("error", f"{rel}: no run field"))
            continue
        obj = Command(
            name=path.stem, run=c["run"].strip(), title=c.get("title", ""),
            tags=list(c.get("tags") or []), description=c.get("description", ""),
            group=norm_dir("/".join(path.relative_to(base / "commands").parts[:-1])), file=rel,
        )
        add("command", data.commands, path.stem, obj, rel)

    return data


def unprivileged_port_start() -> int:
    try:
        return int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text())
    except (OSError, ValueError):
        return 1024


def check(data: Data) -> list[Issue]:
    """Offline check: references, jump host loops, ports."""
    issues = list(data.issues)

    for h in data.hosts.values():
        if h.jump and h.jump not in data.hosts:
            issues.append(Issue("error", f"server '{h.name}': jump '{h.jump}': no such server"))
    for h in data.hosts.values():
        seen, cur = {h.name}, h
        while cur.jump and cur.jump in data.hosts:
            if cur.jump in seen:
                issues.append(Issue("error", f"server '{h.name}': jump host loop ({' → '.join(seen)})"))
                break
            seen.add(cur.jump)
            cur = data.hosts[cur.jump]

    from qc import vault

    if vault.initialized():
        have = set(vault.names())
        for h in data.hosts.values():
            if h.password and h.password not in have:
                issues.append(Issue("warning", f"server '{h.name}': password '{h.password}' is not in the vault "
                                               f"— add it in the Passwords tab or with qc pass add {h.password}"))

    from qc import ports

    low = unprivileged_port_start()
    by_port: dict[int, list[Tunnel]] = {}
    for t in data.tunnels.values():
        try:
            ipaddress.ip_address(ports.norm_bind(t.bind))
        except ValueError:
            issues.append(Issue("error", f"tunnel '{t.name}': bind '{t.bind}' is not an IP address "
                                         "(127.0.0.1, 127.0.0.2, 0.0.0.0…)"))
        if t.via not in data.hosts:
            issues.append(Issue("error", f"tunnel '{t.name}': via '{t.via}': no such server"))
        if t.keep_open:
            continue
        if not (0 < t.local < 65536):
            issues.append(Issue("error", f"tunnel '{t.name}': port {t.local} out of range"))
        elif t.local < low:
            issues.append(Issue(
                "error", f"tunnel '{t.name}': port {t.local} < {low} cannot be opened without root, pick a higher port"))
        by_port.setdefault(t.local, []).append(t)
    for port, ts in sorted(by_port.items()):
        # the same port is fine on different addresses (127.0.0.2:443 and 127.0.0.3:443), 0.0.0.0 takes them all
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                if ports.overlap(a.bind, b.bind):
                    where = ports.endpoint(a.bind if not ports.is_wildcard(b.bind) else b.bind, port)
                    issues.append(Issue("warning", f"{where} is used by tunnels '{a.name}' and '{b.name}': "
                                                   "they cannot run at the same time"))
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
    raise RuntimeError("no free ports")
