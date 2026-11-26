"""Editing ~/qc data in an editor (vim): find an entry's file and line, template for a new entry, check after exit."""
from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from qc import config, paths, settings, sshgen

# menu section → (folder in ~/qc, TOML table, template for a new entry)
SECTIONS = {
    "ssh": ("ssh", "hosts", '''
[hosts.{name}]
host = ""                 # address
user = ""
# jump = ""               # jump host (= ssh -J)
# port = 22
# password = ""           # entry name in the password vault, not the password itself
description = ""
'''),
    "tun": ("tunnels", "tunnels", '''
[tunnels.{name}]
via = ""                  # server from ssh/
local = 15432             # port on this computer
# bind = "127.0.0.2"      # local address, 127.0.0.1 by default: the same port can run on 127.0.0.2,
                          # 127.0.0.3… at once; 0.0.0.0 opens it to the whole network
remote = "127.0.0.1:5432" # where to connect on the remote side
autostart = false
description = ""
'''),
    "cmd": ("commands", "commands", '''
[commands.{name}]
title = ""
run = ""                  # {{{{name}}}} asks for a value when chosen
tags = []
'''),
}
NEW_NAME = {"ssh": "new-server", "tun": "new-tunnel", "cmd": "new-command"}
TOML_LINE_RE = re.compile(r"at line (\d+)")


def editor() -> list[str]:
    """Editor: settings edit.editor → $VISUAL → $EDITOR → vim."""
    cmd = settings.get("edit.editor") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"
    return shlex.split(cmd)


def is_vim(cmd: list[str]) -> bool:
    return Path(cmd[0]).name in ("vim", "nvim", "vi", "gvim", "vimx")


def editor_cmd(files: list[Path], line: int | None = None) -> list[str]:
    cmd = editor()
    if not shutil.which(cmd[0]):
        raise EditError(f"editor '{cmd[0]}' not found: install vim or set qc config edit.editor <command>")
    args = [str(f) for f in files]
    if len(files) > 1 and is_vim(cmd):
        return [*cmd, "-p", *args]  # several files open as tabs
    return [*cmd, *([f"+{line}"] if line else []), *args]


class EditError(Exception):
    pass


def entry_line(path: Path, table: str, name: str) -> int | None:
    """Line number of '[hosts.name]' in the file."""
    pat = re.compile(rf'^\s*\[\s*{table}\s*\.\s*(?:"{re.escape(name)}"|\'{re.escape(name)}\'|{re.escape(name)})\s*\]')
    try:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pat.match(line):
                return i
    except OSError:
        pass
    return None


def items(data: config.Data, tab: str) -> dict:
    return {"ssh": data.hosts, "tun": data.tunnels, "cmd": data.commands}[tab]


def locate(data: config.Data, tab: str, name: str) -> tuple[Path, int | None]:
    obj = items(data, tab).get(name)
    if obj is None:
        raise EditError(f"no '{name}'")
    path = paths.data_dir() / obj.file
    return path, entry_line(path, SECTIONS[tab][1], name)


def dir_files(data: config.Data, tab: str, d: str) -> list[Path]:
    """Files a menu folder is built from (including nested folders); files directly in it come first."""
    d = config.norm_dir(d)
    own, deeper = [], []
    for x in items(data, tab).values():
        p = paths.data_dir() / x.file
        if x.group == d and p not in own:
            own.append(p)
        elif (not d or x.group.startswith(d + "/")) and p not in deeper:
            deeper.append(p)
    return own + [p for p in deeper if p not in own]


def find_any(data: config.Data, what: str) -> tuple[str, list[Path], int | None]:
    """For `qc edit`: an entry name (in any section) or a folder/file."""
    for tab in ("ssh", "tun", "cmd"):
        if what in items(data, tab):
            path, line = locate(data, tab, what)
            return tab, [path], line
    base = paths.data_dir()
    p = Path(what).expanduser()
    for cand in (p, base / what, base / f"{what}.toml"):
        if cand.is_file():
            return "", [cand.resolve()], None
    for tab in ("ssh", "tun", "cmd"):
        files = dir_files(data, tab, what)
        if files and what.strip("/"):
            return tab, files, None
    raise EditError(f"'{what}' not found: no entry, folder or file in {base}")


def unique_name(data: config.Data, tab: str) -> str:
    base, n, have = NEW_NAME[tab], 1, items(data, tab)
    name = base
    while name in have:
        n += 1
        name = f"{base}-{n}"
    return name


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Draft:
    """New entry: the template is appended to a file; if it is left untouched in the editor, the file is restored."""
    path: Path
    line: int
    name: str
    original: str | None  # None: the file did not exist
    written: str = ""

    def rollback_if_untouched(self) -> bool:
        try:
            now = self.path.read_text()
        except OSError:
            return False
        if now != self.written:
            return False
        if self.original is None:
            self.path.unlink()
        else:
            self.path.write_text(self.original)
        return True


def new_entry(data: config.Data, tab: str, cwd: str, new_file: str | None = None) -> Draft:
    """Append an entry template to the current folder's file (or create the file new_file in the section)."""
    sub, _, template = SECTIONS[tab]
    if new_file:
        rel = new_file.strip().strip("/")
        if "/" in rel or rel.startswith("."):
            raise EditError("file name without '/': the menu folder is set by group inside the file")
        if not rel.endswith(".toml"):
            rel += ".toml"
        path = paths.data_dir() / sub / rel
    else:
        own = [f for f in dir_files(data, tab, cwd)
               if any(x.group == config.norm_dir(cwd) and paths.data_dir() / x.file == f
                      for x in items(data, tab).values())]
        if not own:
            raise EditError("the folder has no file of its own")
        path = own[0]
    original = path.read_text() if path.exists() else None
    head = original if original is not None else ""
    if original is None:
        # menu folder = group; the file goes to the section root, so group is written as the full path
        want = config.norm_dir(cwd)
        auto = Path(rel).stem
        if want != auto:
            head = f"group = {toml_str(want or '/')}\n"
    if head and not head.endswith("\n"):
        head += "\n"
    name = unique_name(data, tab)
    body = template.format(name=name)
    text = head + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    line = len(head.splitlines()) + 2  # the '[hosts.new-server]' line
    return Draft(path=path, line=line, name=name, original=original, written=text)


@dataclass
class Result:
    ok: bool
    errors: list[str] = field(default_factory=list)
    line: int | None = None   # where the first error is (to reopen there)
    file: Path | None = None
    committed: bool = False


def after_edit(files: list[Path], message: str) -> Result:
    """Check the edited files; if all is good, rebuild ssh_config and commit to ~/qc."""
    from qc import sync

    base = paths.data_dir()
    rels = set()
    for f in files:
        try:
            rels.add(str(f.resolve().relative_to(base.resolve())))
        except ValueError:
            pass
    data = config.load()
    mine = {n for store in (data.hosts, data.tunnels, data.commands) for n, x in store.items() if x.file in rels}
    errors, line, bad_file = [], None, None
    for i in config.check(data):
        if i.level != "error":
            continue
        rel = next((r for r in rels if i.text.startswith(r + ":")), None)
        if rel or any(f"'{n}'" in i.text for n in mine):
            errors.append(str(i))
            if line is None and rel and (m := TOML_LINE_RE.search(i.text)):
                line, bad_file = int(m.group(1)), base / rel
    if errors:
        return Result(False, errors, line, bad_file or (files[0] if files else None))
    try:
        sshgen.write(data)
    except OSError:
        pass
    committed = False
    try:
        if sync.is_repo():
            committed = sync.commit_local(message)
    except Exception:  # the commit is optional: the edit is already on disk and goes out with qc sync
        pass
    return Result(True, committed=committed)


def snapshot(files: list[Path]) -> dict[Path, float]:
    out = {}
    for f in files:
        try:
            out[f] = f.stat().st_mtime_ns
        except OSError:
            out[f] = 0
    return out


def remember_password(data: config.Data, server: str, entry: str, secret: str) -> str:
    """Save a typed password in the vault; if the server has no `password = …` yet, add that line under
    its [hosts.<name>] header (the rest of the file, comments included, stays as it is). Returns what was done."""
    from qc import vault

    vault.put(entry, secret)
    done = f"password '{entry}' saved"
    h = data.hosts.get(server)
    if h and not h.password:
        path = paths.data_dir() / h.file
        line = entry_line(path, "hosts", server)
        if line:
            lines = path.read_text().splitlines(keepends=True)
            lines.insert(line, f"password = {toml_str(entry)}\n")
            path.write_text("".join(lines))
            done += f", server '{server}' now refers to it ({h.file})"
        else:
            done += f"; add password = \"{entry}\" to server '{server}' by hand"
    vault.commit(f"qc: password {entry} saved")
    return done
