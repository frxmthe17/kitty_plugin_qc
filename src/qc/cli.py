"""Командная строка qc. Без аргументов — меню."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from qc import client, config, install, paths, settings, sshgen, sync


def die(msg: str, code: int = 1) -> None:
    print(f"qc: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- ssh ----------

def do_ssh(name: str, extra: list[str]) -> None:
    data = config.load()
    if name not in data.hosts:
        near = [h for h in data.hosts if name.lower() in h.lower()]
        die(f"нет сервера «{name}»" + (f"; похожие: {', '.join(near)}" if near else ""))
    sshgen.write(data)
    os.execvp(paths.ssh_bin(), [paths.ssh_bin(), *sshgen.config_args(), name, *extra])


# ---------- туннели ----------

ICON = {"running": "●", "starting": "◐", "retry": "↻", "error": "✗", "stopped": "○"}


def print_tunnels(resp: dict) -> None:
    ts = sorted(resp.get("tunnels", []), key=lambda t: (t["group"], t["name"]))
    if not ts:
        print("туннелей нет — добавь файл в ~/qc/tunnels/ (пример в ~/qc/README.md)")
    w = max([len(t["name"]) for t in ts] + [4])
    for t in ts:
        state = {"running": "работает", "starting": "подключается…", "stopped": "остановлен"}.get(t["state"], "")
        if t["state"] == "retry":
            state = f"{t['error']} — повтор через {max(0, int(t['retry_at'] - time.time()))} с"
        elif t["state"] == "error":
            state = t["error"]
        auto = " [авто]" if t["autostart"] else ""
        print(f"{ICON.get(t['state'], '?')} {t['name']:<{w}}  {t['local']:>5} → {t['via']}:{t['remote']}"
              f"  {state}{auto}")
    for i in resp.get("issues", []):
        if i["level"] == "warning" or "туннель" in i["text"] or "порт" in i["text"]:
            print(config.Issue(i["level"], i["text"]))


def do_tunnels(args) -> None:
    try:
        client.ensure()
        if args.action in (None, "ls"):
            print_tunnels(client.request("status"))
            return
        if args.action == "logs":
            if not args.names:
                die("qc t logs <туннель>")
            r = client.request("logs", name=args.names[0], n=args.n)
            if not r.get("ok"):
                die(r.get("error", "ошибка"))
            for t, line in r["lines"]:
                print(time.strftime("%H:%M:%S", time.localtime(t)), line)
            return
        if not args.names:
            die(f"qc t {args.action} <туннель|группа|all> …")
        r = client.request(args.action, names=args.names)
        if not r.get("ok"):
            die(r.get("error", "ошибка"))
        if args.action in ("up", "restart"):
            # ждём результата: работает или ошибка, но не дольше 20 с
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
            print("остановлены: " + ", ".join(r["names"]))
    except client.DaemonDown as e:
        die(str(e))


# ---------- прочее ----------

def do_check() -> int:
    data = config.load()
    issues = config.check(data)
    sshgen.write(data)
    print(f"серверов {len(data.hosts)}, туннелей {len(data.tunnels)}, команд {len(data.commands)}")
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
            issues.append(config.Issue("warning", f"порт {port} сейчас занят: {ports.owner(port)}"))
    if not sshgen.include_installed():
        issues.append(config.Issue("warning", "в ~/.ssh/config нет Include — `qc setup` подключит"))
    for i in issues:
        print(i)
    if not issues:
        print("✓ всё в порядке")
    return 1 if any(i.level == "error" for i in issues) else 0


def do_status() -> None:
    try:
        client.ensure()
        resp = client.request("status")
        ts = resp["tunnels"]
        up = [t["name"] for t in ts if t["state"] == "running"]
        bad = [f"{t['name']} ({t['error']})" for t in ts if t["state"] in ("error", "retry")]
        print(f"туннели: работает {len(up)} из {len(ts)}" + (f" — {', '.join(up)}" if up else ""))
        for b in bad:
            print(f"  ✗ {b}")
    except client.DaemonDown as e:
        print(f"туннели: {e}")
    print("синхронизация: " + sync.status_line(sync.status())[1])


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


def do_menu(args) -> None:
    from qc import tui

    if not sys.stdin.isatty():
        die("меню работает только в терминале")
    mode = "kitty" if args.kitty else ("bash" if args.out else "plain")
    result = tui.run(mode)
    if not result:
        return
    kind, value = result
    if kind == "ssh":
        if args.kitty:
            qc = install.qc_command()
            subprocess.run(["kitty", "@", "launch", "--type=tab", "--tab-title", value, *qc, "ssh", value])
            return
        do_ssh(value, [])
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
    p = argparse.ArgumentParser(prog="qc", description="SSH, туннели, быстрые команды и пароли в терминале")
    sub = p.add_subparsers(dest="cmd")

    m = sub.add_parser("menu", help="меню (то же, что qc без аргументов)")
    m.add_argument("--out", help="файл для выбранной команды (горячая клавиша bash)")
    m.add_argument("--kitty", metavar="WINDOW_ID", help="запуск из kitty: окно, куда вставлять команду")

    s = sub.add_parser("ssh", help="подключиться к серверу")
    s.add_argument("name")
    s.add_argument("extra", nargs=argparse.REMAINDER, help="доп. аргументы ssh / команда на сервере")

    t = sub.add_parser("t", help="туннели: ls | up | down | restart | logs")
    t.add_argument("action", nargs="?", choices=["ls", "up", "down", "restart", "logs"])
    t.add_argument("names", nargs="*", help="туннели или группы (имя файла), all — все")
    t.add_argument("-n", type=int, default=30, help="строк лога")

    sub.add_parser("check", help="проверить ~/qc: конфликты портов, битые ссылки")
    sub.add_parser("status", help="коротко: туннели и синхронизация")
    sy = sub.add_parser("sync", help="синхронизировать ~/qc с гитом")
    sy.add_argument("--notify", action="store_true", help="уведомление KDE при ошибке (для таймера)")

    c = sub.add_parser("config", help="настройки: qc config sync.remote <адрес> | sync.schedule daily|hourly|off")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")

    st = sub.add_parser("setup", help="установить: службы, Include в ~/.ssh/config, горячие клавиши bash и kitty")
    st.add_argument("remote", nargs="?", help="адрес git-репозитория с данными")
    sub.add_parser("uninstall", help="убрать службы и блоки qc из конфигов (данные не трогает)")
    sub.add_parser("daemon", help="запустить демон туннелей (обычно его запускает systemd)")
    si = sub.add_parser("shell-init", help="код для .bashrc")
    si.add_argument("shell", choices=["bash"])

    args = p.parse_args()
    if args.cmd in (None, "menu"):
        if args.cmd is None:
            args.out = args.kitty = None
        do_menu(args)
    elif args.cmd == "ssh":
        do_ssh(args.name, args.extra)
    elif args.cmd == "t":
        do_tunnels(args)
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
