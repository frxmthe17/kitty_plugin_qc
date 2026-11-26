"""qc command line. Without arguments, opens the menu."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from qc import askpass, client, config, install, paths, settings, sshgen, sync, vault


def die(msg: str, code: int = 1) -> None:
    print(f"qc: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- ssh ----------

def do_ssh(name: str, extra: list[str]) -> None:
    data = config.load()
    if name not in data.hosts:
        near = [h for h in data.hosts if name.lower() in h.lower()]
        die(f"no server '{name}'" + (f"; similar: {', '.join(near)}" if near else ""))
    sshgen.write(data)
    env = ssh_env(data, name)
    os.execvpe(paths.ssh_bin(), [paths.ssh_bin(), *sshgen.config_args(), name, *extra], env or os.environ)


def ssh_env(data: config.Data, name: str) -> dict | None:
    """Passwords on the way to the server → take them from the vault (qc-askpass). None — type them by hand."""
    need = vault.chain_passwords(data, name)
    if not need:
        return None
    if not vault.initialized():
        return None
    missing = [n for n in need if n not in vault.names()]
    if missing:
        print(f"qc: password '{missing[0]}' is not in the vault — type it by hand (or qc pass add {missing[0]})",
              file=sys.stderr)
        return None
    try:
        client.ensure()
        if not client.request("vault").get("unlocked") and not unlock_interactive():
            return None
    except client.DaemonDown as e:
        print(f"qc: {e} — type the password by hand", file=sys.stderr)
        return None
    return askpass.env(vault.host_chain(data, name), secrets.token_hex(8))


# ---------- passwords ----------

def unlock_interactive(tries: int = 3) -> bool:
    """Ask the master password and unlock the vault in the daemon. False — gave up."""
    for _ in range(tries):
        try:
            pw = getpass.getpass("qc: master password: ")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return False
        if not pw:
            return False
        r = client.request("unlock", timeout=30, password=pw)
        if r.get("ok"):
            return True
        print(f"qc: {r.get('error', 'error')}", file=sys.stderr)
    return False


def ask_new(what: str) -> str:
    try:
        a = getpass.getpass(f"{what}: ")
        b = getpass.getpass(f"{what} (again): ")
    except (EOFError, KeyboardInterrupt):
        die("cancelled")
    if a != b:
        die("the two entries differ")
    if not a:
        die("empty — nothing saved")
    return a


def fetch_secret(name: str) -> str:
    """A password from the vault: via the daemon (unlocking it if needed), or directly if the daemon is down."""
    try:
        client.ensure()
        r = client.request("secret", name=name)
        if r.get("locked"):
            if not unlock_interactive():
                die("the vault is still locked")
            r = client.request("secret", name=name)
        if not r.get("ok"):
            die(r.get("error", "error"))
        return r["secret"]
    except client.DaemonDown:
        try:
            ident = vault.unlock(getpass.getpass("qc: master password: "))
            return vault.get(name, ident)
        except vault.VaultError as e:
            die(str(e))
    return ""


def do_pass(action: str, name: str | None) -> None:
    from qc import clipboard

    try:
        if action == "init":
            vault.init(ask_new("new master password"))
            vault.commit("qc: password vault created")
            print("✓ vault created in ~/qc/secure — remember the master password, it cannot be recovered")
            return
        if action == "passwd":
            if not vault.initialized():
                die("no vault yet — qc pass init")
            old = getpass.getpass("current master password: ")
            vault.change_master(old, ask_new("new master password"))
            vault.commit("qc: master password changed")
            print("✓ master password changed")
            return
        if action == "ls":
            if not vault.initialized():
                print("no vault yet — qc pass init (or the Passwords tab in the menu)")
                return
            data = config.load()
            used: dict[str, list[str]] = {}
            for h in data.hosts.values():
                if h.password:
                    used.setdefault(h.password, []).append(h.name)
            names = vault.names()
            w = max([len(n) for n in names] + [4])
            for n in names:
                print(f"{n:<{w}}  {', '.join(used.get(n, []))}")
            for n in sorted(set(used) - set(names)):
                print(f"{n:<{w}}  ✗ not in the vault (needed by {', '.join(used[n])})")
            return
        if not name:
            die(f"qc pass {action} <name>")
        if action in ("add", "set"):
            if not vault.initialized():
                print("qc: no vault yet — creating it")
                vault.init(ask_new("new master password"))
            if action == "add" and name in vault.names():
                die(f"'{name}' already exists — qc pass set {name} to replace it")
            vault.put(name, ask_new(f"password '{name}'"))
            vault.commit(f"qc: password {name}")
            print(f"✓ saved {name}")
        elif action == "rm":
            vault.remove(name)
            vault.commit(f"qc: password {name} removed")
            print(f"✓ removed {name}")
        elif action == "get":
            print(fetch_secret(name))
        elif action == "copy":
            seconds = int(settings.get("vault.clipboard_seconds") or 30)
            clipboard.copy(fetch_secret(name), seconds)
            print(f"✓ copied, the clipboard is cleared in {seconds} s")
    except (vault.VaultError, clipboard.ClipboardError) as e:
        die(str(e))


def do_lock(lock: bool) -> None:
    try:
        client.ensure()
        if lock:
            client.request("lock")
            print("✓ vault locked")
        elif client.request("vault").get("unlocked"):
            print("vault is already unlocked")
        elif unlock_interactive():
            print("✓ vault unlocked")
        else:
            sys.exit(1)
    except client.DaemonDown as e:
        die(str(e))


# ---------- tunnels ----------

ICON = {"running": "●", "starting": "◐", "retry": "↻", "error": "✗", "stopped": "○"}


def print_tunnels(resp: dict) -> None:
    ts = sorted(resp.get("tunnels", []), key=lambda t: (t["group"], t["name"]))
    if not ts:
        print("no tunnels: add a file to ~/qc/tunnels/ (see the example in ~/qc/README.md)")
    w = max([len(t["name"]) for t in ts] + [4])
    for t in ts:
        state = {"running": "running", "starting": "connecting…", "stopped": "stopped"}.get(t["state"], "")
        if t["state"] == "retry":
            state = f"{t['error']}: retry in {max(0, int(t['retry_at'] - time.time()))} s"
        elif t["state"] == "error":
            state = t["error"]
        auto = " [auto]" if t["autostart"] else ""
        print(f"{ICON.get(t['state'], '?')} {t['name']:<{w}}  {t['local']:>5} → {t['via']}:{t['remote']}"
              f"  {state}{auto}")
    for i in resp.get("issues", []):
        if i["level"] == "warning" or re.search(r"\b(tunnel|port)\b", i["text"]):
            print(config.Issue(i["level"], i["text"]))


def do_tunnels(args) -> None:
    try:
        client.ensure()
        if args.action in (None, "ls"):
            print_tunnels(client.request("status"))
            return
        if args.action == "logs":
            if not args.names:
                die("qc t logs <tunnel>")
            r = client.request("logs", name=args.names[0], n=args.n)
            if not r.get("ok"):
                die(r.get("error", "error"))
            for t, line in r["lines"]:
                print(time.strftime("%H:%M:%S", time.localtime(t)), line)
            return
        if not args.names:
            die(f"qc t {args.action} <tunnel|folder|all> …")
        r = client.request(args.action, names=args.names)
        if not r.get("ok"):
            die(r.get("error", "error"))
        if args.action in ("up", "restart"):
            # wait for the result (running or error), but no longer than 20 s
            names, deadline = set(r["names"]), time.time() + 20
            time.sleep(0.3)
            while time.time() < deadline:
                st = client.request("status")
                mine = [t for t in st["tunnels"] if t["name"] in names]
                if all(t["state"] in ("running", "error", "retry", "stopped") for t in mine):
                    break
                time.sleep(0.3)
            print_tunnels({"tunnels": mine})
        else:
            print("stopped: " + ", ".join(r["names"]))
    except client.DaemonDown as e:
        die(str(e))


# ---------- misc ----------

def do_check() -> int:
    data = config.load()
    issues = config.check(data)
    sshgen.write(data)
    print(f"servers {len(data.hosts)}, tunnels {len(data.tunnels)}, commands {len(data.commands)}")
    running = set()
    try:
        running = {t["local"] for t in client.request("status", timeout=2).get("tunnels", [])
                   if t["state"] in ("running", "starting")}
    except (client.DaemonDown, OSError):
        pass
    from qc import ports

    low = config.unprivileged_port_start()
    for port in sorted({t.local for t in data.tunnels.values() if t.local >= low} - running):
        if not ports.port_free("127.0.0.1", port):
            issues.append(config.Issue("warning", f"port {port} is busy now: {ports.owner(port)}"))
    if not sshgen.include_installed():
        issues.append(config.Issue("warning", "~/.ssh/config has no Include: `qc setup` will add it"))
    for i in issues:
        print(i)
    if not issues:
        print("✓ all good")
    return 1 if any(i.level == "error" for i in issues) else 0


def do_status() -> None:
    try:
        client.ensure()
        resp = client.request("status")
        ts = resp["tunnels"]
        up = [t["name"] for t in ts if t["state"] == "running"]
        bad = [f"{t['name']} ({t['error']})" for t in ts if t["state"] in ("error", "retry")]
        print(f"tunnels: {len(up)} of {len(ts)} running" + (f": {', '.join(up)}" if up else ""))
        for b in bad:
            print(f"  ✗ {b}")
    except client.DaemonDown as e:
        print(f"tunnels: {e}")
    print("sync: " + sync.status_line(sync.status())[1])
    if vault.initialized():
        try:
            v = client.request("vault", timeout=2)
            until = time.strftime(" until %H:%M", time.localtime(v["lock_at"])) if v.get("lock_at") else ""
            print("passwords: " + (f"unlocked{until}" if v.get("unlocked") else "locked"))
        except (client.DaemonDown, KeyError):
            print("passwords: locked")


def do_config(args) -> None:
    if not args.key:
        for section, values in settings.load().items():
            for k, v in values.items():
                print(f"{section}.{k} = {v}")
        return
    if args.value is None:
        print(settings.get(args.key) or "")
        return
    try:
        if args.key == "sync.remote":
            print(sync.attach(args.value))
        elif args.key == "sync.schedule":
            print("\n".join(install.apply_schedule(args.value)))
        else:
            settings.set_value(args.key, args.value)
    except (sync.SyncError, ValueError) as e:
        die(str(e))


def do_edit(what: str | None) -> None:
    """qc edit [name|folder|file]: open in the editor; after exit, check and commit."""
    from qc import edit

    data = config.load()
    try:
        if what:
            _, files, line = edit.find_any(data, what)
        else:
            files, line = [paths.data_dir()], None
        cmd = edit.editor_cmd(files, line)
    except edit.EditError as e:
        die(str(e))
    while True:
        before = edit.snapshot(files)
        subprocess.run(cmd)
        if what and edit.snapshot(files) == before:
            return
        r = edit.after_edit([f for f in files if f.is_file()] or files, f"qc: edit {what or 'manually'}")
        if r.ok:
            print("✓ saved" + (", committed to ~/qc" if r.committed else ""))
            return
        print("\n".join(r.errors), file=sys.stderr)
        if not what:
            sys.exit(1)
        try:
            ans = input("Open again? [Y/n] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("n", "no"):
            sys.exit(1)
        cmd = edit.editor_cmd([r.file] if r.file else files, r.line or line)


def popup_menu() -> tuple[tuple | None, bool]:
    """Menu in a separate window centered on screen (kitty panel). Returns (choice, whether the window opened)."""
    fd, path = tempfile.mkstemp(prefix="qc-popup.", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    os.close(fd)
    try:
        opts = [f"--{o}" for o in install.popup_opts()]
        cmd = [*install.qc_command(), "menu", "--in-popup", "--result", path]
        r = subprocess.run(["kitten", "panel", *opts, "--class", "qc-menu", *cmd])
        text = Path(path).read_text()
        if r.returncode != 0 or not text:
            return None, False
        value = json.loads(text)
        return (tuple(value) if value else None), True
    except (OSError, ValueError):
        return None, False
    finally:
        Path(path).unlink(missing_ok=True)


def do_menu(args) -> None:
    from qc import tui

    if not sys.stdin.isatty():
        die("the menu works only in a terminal")
    mode = "kitty" if args.kitty else ("bash" if args.out else "plain")
    window = settings.get("menu.window") or "popup"
    result = None
    if not args.in_popup and window == "popup" and install.popup_available():
        result, opened = popup_menu()
        if not opened:
            print("qc: could not open the menu window, showing the menu here (`qc config menu.window inline` stops trying)",
                  file=sys.stderr)
    else:
        opened = False
    if not opened:
        inline = not args.in_popup and window != "full"
        result = tui.run(mode, inline=inline, popup=bool(args.in_popup))
    if args.result:
        # menu in a window opened from a terminal: whoever opened the window acts on the choice
        Path(args.result).write_text(json.dumps(result or []))
        return
    if not result:
        return
    kind, value = result
    if kind == "ssh":
        if args.kitty:
            qc = install.qc_command()
            # --match: the tab opens next to the window the menu was called from (not in the menu window itself)
            match = ["--match", f"window_id:{args.kitty}"] if install.kitty_window_ok(args.kitty) else []
            subprocess.run(["kitty", "@", "launch", "--type=tab", *match, "--tab-title", value, *qc, "ssh", value])
            return
        do_ssh(value, [])
    elif kind == "type":
        # a password typed into the kitty window (no Enter, nothing in the shell history)
        if args.kitty and install.kitty_window_ok(args.kitty):
            subprocess.run(["kitty", "@", "send-text", "--match", f"id:{args.kitty}", "--stdin"],
                           input=value.encode())
    elif kind == "insert":
        if args.kitty and install.kitty_window_ok(args.kitty):
            subprocess.run(["kitty", "@", "send-text", "--match", f"id:{args.kitty}", "--stdin"],
                           input=value.encode())
        elif args.out:
            with open(args.out, "w") as f:
                f.write(value)
        else:
            print(value)


def main() -> None:
    p = argparse.ArgumentParser(prog="qc", description="SSH, tunnels, quick commands and passwords in the terminal")
    sub = p.add_subparsers(dest="cmd")

    m = sub.add_parser("menu", help="menu (same as qc without arguments)")
    m.add_argument("--out", help="file for the chosen command (bash hotkey)")
    m.add_argument("--kitty", metavar="WINDOW_ID", help="launched from kitty: the window to insert the command into")
    m.add_argument("--in-popup", action="store_true", help="already in its own window: draw over the whole window")
    m.add_argument("--result", help=argparse.SUPPRESS)  # the menu window returns the choice to the qc that opened it

    s = sub.add_parser("ssh", help="connect to a server")
    s.add_argument("name")
    s.add_argument("extra", nargs=argparse.REMAINDER, help="extra ssh arguments / command to run on the server")

    t = sub.add_parser("t", help="tunnels: ls | up | down | restart | logs")
    t.add_argument("action", nargs="?", choices=["ls", "up", "down", "restart", "logs"])
    t.add_argument("names", nargs="*", help="tunnel or folder names (a folder includes its subfolders); 'all' for every tunnel")
    t.add_argument("-n", type=int, default=30, help="log lines")

    e = sub.add_parser("edit", help="edit in vim: qc edit <server|tunnel|command|folder|file>; without a name opens ~/qc")
    e.add_argument("what", nargs="?")

    pw = sub.add_parser("pass", help="passwords: ls | add | set | rm | get | copy | init | passwd")
    pw.add_argument("action", nargs="?", default="ls",
                    choices=["ls", "add", "set", "rm", "get", "copy", "init", "passwd"])
    pw.add_argument("name", nargs="?")
    sub.add_parser("unlock", help="unlock the password vault (asks the master password)")
    sub.add_parser("lock", help="lock the password vault")

    sub.add_parser("check", help="check ~/qc: port conflicts, broken references")
    sub.add_parser("status", help="short summary: tunnels and sync")
    sy = sub.add_parser("sync", help="sync ~/qc with git")
    sy.add_argument("--notify", action="store_true", help="KDE notification on error (for the timer)")

    c = sub.add_parser("config", help="settings: qc config sync.remote <url> | sync.schedule daily|hourly|off")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")

    st = sub.add_parser("setup", help="install: services, Include in ~/.ssh/config, bash and kitty hotkeys")
    st.add_argument("remote", nargs="?", help="URL of the git repository with the data")
    sub.add_parser("uninstall", help="remove services and qc blocks from configs (data is left untouched)")
    sub.add_parser("daemon", help="run the tunnels daemon (usually started by systemd)")
    si = sub.add_parser("shell-init", help="code for .bashrc")
    si.add_argument("shell", choices=["bash"])

    args = p.parse_args()
    if args.cmd in (None, "menu"):
        if args.cmd is None:
            args.out = args.kitty = None
            args.in_popup, args.result = False, None
        do_menu(args)
    elif args.cmd == "ssh":
        do_ssh(args.name, args.extra)
    elif args.cmd == "t":
        do_tunnels(args)
    elif args.cmd == "pass":
        do_pass(args.action, args.name)
    elif args.cmd in ("unlock", "lock"):
        do_lock(args.cmd == "lock")
    elif args.cmd == "edit":
        do_edit(args.what)
    elif args.cmd == "check":
        sys.exit(do_check())
    elif args.cmd == "status":
        do_status()
    elif args.cmd == "sync":
        r = sync.sync(notify=args.notify)
        print(("✓ " if r["ok"] else "✗ ") + r["message"])
        sys.exit(0 if r["ok"] else 1)
    elif args.cmd == "config":
        do_config(args)
    elif args.cmd == "setup":
        try:
            for line in install.setup(args.remote):
                print("•", line)
        except sync.SyncError as e:
            die(str(e))
    elif args.cmd == "uninstall":
        for line in install.uninstall():
            print("•", line)
    elif args.cmd == "daemon":
        from qc import daemon

        sys.exit(daemon.run())
    elif args.cmd == "shell-init":
        print(install.shell_init_bash())
