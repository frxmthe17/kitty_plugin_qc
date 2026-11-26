"""qc menu: Connections / Tunnels / Commands / Passwords / Settings + a sync status line.

Connections, tunnels and commands are shown as a file system: a group is a folder you can enter,
and an entry is a file.
"""
from __future__ import annotations

import re
import subprocess
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static, TabbedContent, TabPane

from qc import client, clipboard, config, edit, install, paths, settings, sync, vault

TABS = ["ssh", "tun", "cmd", "pass", "set"]
TREE_TABS = ("ssh", "tun", "cmd")  # folder-based tabs; passwords and settings are flat lists
UP = ".."
TUN_ICONS = {"running": ("●", "green"), "starting": ("◐", "yellow"), "retry": ("↻", "yellow"),
             "error": ("✗", "red"), "stopped": ("○", "dim")}
ACTIVE = ("running", "starting", "retry")


def is_dir(key: str | None) -> bool:
    return bool(key) and (key == UP or key.startswith("d:"))


def dir_label(name: str) -> Text:
    return Text(name + "/", style="bold blue")


class SearchInput(Input):
    """Search box. When empty: ← and Backspace go up a folder, → enters the folder under the cursor."""

    def action_cursor_left(self, select: bool = False) -> None:
        if not self.value:
            self.app.action_up()
        else:
            super().action_cursor_left(select)

    def action_cursor_right(self, select: bool = False) -> None:
        if not self.value:
            self.app.action_into()
        else:
            super().action_cursor_right(select)

    def action_delete_left(self) -> None:
        if not self.value:
            self.app.action_up()
        else:
            super().action_delete_left()
SCHEDULE_TEXT = {"daily": "daily", "hourly": "hourly", "off": "off"}


class AskScreen(ModalScreen[str | None]):
    """Ask for a single line (git URL, value of a {{placeholder}})."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, value: str = "", placeholder: str = "", password: bool = False) -> None:
        super().__init__()
        self.title_text, self.value, self.placeholder, self.password = title, value, placeholder, password

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.value, placeholder=self.placeholder, id="ask", password=self.password)
            yield Label("Enter — done, Esc — cancel", classes="hint")

    @on(Input.Submitted)
    def done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    """Show text (tunnel log, sync log, settings problems)."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", show=False),
                Binding("enter", "close", show=False)]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(self.title_text, classes="title")
            yield Static(self.text or "(empty)", id="text")
            yield Label("Esc — close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Text + question: Enter — yes, Esc — no."""

    BINDINGS = [Binding("escape", "no", "no"), Binding("enter", "yes", "yes"), Binding("n", "no", show=False)]

    def __init__(self, title: str, text: str, hint: str) -> None:
        super().__init__()
        self.title_text, self.text, self.hint = title, text, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(self.title_text, classes="title")
            yield Static(self.text, id="text")
            yield Label(self.hint, classes="hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class QcApp(App[tuple | None]):
    TITLE = "qc"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; margin: 0 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    .path { height: 1; padding: 0 1; color: $accent; text-style: bold; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $panel; }
    #status.ok { color: $success; }
    #status.warn { color: $warning; }
    #status.error { color: $error; text-style: bold; }
    #status.off { color: $text-muted; }
    Screen.popup { border: round $accent; border-title-color: $accent; }
    Screen:inline { height: 70vh; min-height: 18; border: round $accent; border-title-color: $accent; }
    AskScreen, TextScreen, ConfirmScreen { align: center middle; }
    #dialog { width: 80; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    #dialog.wide { width: 90%; }
    #text { height: auto; max-height: 30; overflow-y: auto; }
    .hint { color: $text-muted; }
    .title { text-style: bold; }
    """
    BINDINGS = [
        Binding("tab", "tab(1)", "next tab", priority=True),
        Binding("shift+tab", "tab(-1)", show=False, priority=True),
        *[Binding(f"f{i + 1}", f"goto('{t}')", show=False) for i, t in enumerate(TABS)],
        Binding("enter", "activate", "select"),
        Binding("up", "cursor(-1)", show=False),
        Binding("down", "cursor(1)", show=False),
        Binding("pageup", "cursor(-10)", show=False),
        Binding("pagedown", "cursor(10)", show=False),
        Binding("ctrl+e", "edit", "edit (vim)", priority=True),
        Binding("ctrl+n", "new", "new entry", priority=True),
        Binding("ctrl+u", "tunnels('up')", "start", priority=True),
        Binding("ctrl+d", "tunnels('down')", "stop", priority=True),
        Binding("ctrl+r", "restart", "tunnel: restart"),
        Binding("ctrl+l", "logs", "tunnel: log"),
        Binding("ctrl+e", "pass_change", "change password", priority=True),
        Binding("ctrl+n", "pass_new", "new password", priority=True),
        Binding("ctrl+d", "pass_delete", "delete", priority=True),
        Binding("ctrl+t", "pass_type", "type into window", priority=True),
        Binding("ctrl+l", "lock", "lock vault"),
        Binding("ctrl+s", "sync", "sync"),
        Binding("backspace", "up", show=False),  # when the table has focus (clicked with the mouse)
        Binding("escape", "escape", "quit"),
        Binding("ctrl+q", "quit_menu", show=False, priority=True),
    ]

    def __init__(self, mode: str = "plain", popup: bool = False) -> None:
        super().__init__()
        self.mode = mode  # plain | bash | kitty — hints depend on it
        self.popup = popup  # in its own window centered on screen — draw a border
        self.data = config.Data()
        self.issues: list[config.Issue] = []
        self.tunnels: list[dict] = []
        self.daemon_error = ""
        self.keys: dict[str, list[str]] = {t: [] for t in TABS}
        self.cwd: dict[str, str] = {t: "" for t in TREE_TABS}  # current folder of the tab: "prod/eu"
        self.select_next: dict[str, str] = {}  # where to put the cursor after changing folder
        self.syncing = False
        self.vault_info: dict = {}  # from the daemon: initialized, unlocked, lock_at

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield SearchInput(placeholder="search in folder…  (Enter — open, ← / Backspace — back, Tab — next tab, "
                                      "Esc — quit)", id="search")
        with TabbedContent(initial="ssh"):
            for tab, title in zip(TABS, ["Connections", "Tunnels", "Commands", "Passwords", "Settings"]):
                with TabPane(title, id=tab):
                    if tab in TREE_TABS or tab == "pass":
                        yield Static("/" if tab != "pass" else "", id=f"p-{tab}", classes="path")
                    yield DataTable(id=f"t-{tab}", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        cols = {
            "ssh": ["Name", "Address", "Via", "Description"],
            "tun": ["", "Name", "Port", "Target", "State"],
            "cmd": ["Name", "Command", "Description", "Tags"],
            "pass": ["Name", "Used by"],
            "set": ["Setting", "Value"],
        }
        for tab, names in cols.items():
            self.table(tab).add_columns(*names)
        if self.popup:
            self.screen.add_class("popup")
        self.screen.border_title = "qc"
        self.load_data()
        self.query_one("#search", Input).focus()
        self.poll_tunnels()
        self.set_interval(1.0, self.poll_tunnels)
        self.refresh_sync_status()
        self.set_interval(3.0, self.refresh_sync_status)
        self.fetch_remote()

    def table(self, tab: str) -> DataTable:
        return self.query_one(f"#t-{tab}", DataTable)

    @property
    def tab(self) -> str:
        return self.query_one(TabbedContent).active or "ssh"

    # ---------- data and filter ----------

    def load_data(self) -> None:
        self.data = config.load()
        self.issues = config.check(self.data)
        self.render_all()

    def matches(self, *fields) -> bool:
        q = self.query_one("#search", Input).value.lower().split()
        hay = " ".join(str(f) for f in fields if f).lower()
        return all(w in hay for w in q)

    def fill(self, tab: str, rows: list[tuple[str, tuple]]) -> None:
        t = self.table(tab)
        old = self.keys[tab][t.cursor_row] if self.keys[tab] and 0 <= t.cursor_row < len(self.keys[tab]) else None
        t.clear()
        self.keys[tab] = []
        for key, cells in rows:
            t.add_row(*cells, key=key)
            self.keys[tab].append(key)
        want = self.select_next.pop(tab, None)
        if want is not None:
            t.move_cursor(row=self.keys[tab].index(want) if want in self.keys[tab] else 0, animate=False)
        elif old in self.keys[tab]:
            t.move_cursor(row=self.keys[tab].index(old), animate=False)

    def render_all(self) -> None:
        for tab in TABS:
            getattr(self, f"render_{tab}")()

    # ---------- folders ----------

    def listing(self, tab: str, items: list, fields) -> tuple[list[str], list]:
        """Contents of the current folder: (subfolders, entries directly in it).
        While searching — entries from the folder and all subfolders, names with paths (like find)."""
        cwd = self.cwd[tab]
        prefix = cwd + "/" if cwd else ""
        inside = [x for x in items if x.group == cwd or x.group.startswith(prefix)]
        if self.query_one("#search", Input).value.strip():
            found = [x for x in inside if self.matches(x.group[len(prefix):], *fields(x))]
            return [], sorted(found, key=lambda x: (x.group, x.name))
        dirs = sorted({x.group[len(prefix):].split("/")[0] for x in inside if x.group != cwd}, key=str.lower)
        here = sorted((x for x in inside if x.group == cwd), key=lambda x: x.name)
        return dirs, here

    def rel_name(self, tab: str, x) -> str:
        cwd = self.cwd[tab]
        rel = x.group[len(cwd) + 1:] if cwd else x.group
        return f"{rel}/{x.name}" if rel else x.name

    def in_dir(self, tab: str, items: list, d: str) -> list:
        full = f"{self.cwd[tab]}/{d}" if self.cwd[tab] else d
        return [x for x in items if x.group == full or x.group.startswith(full + "/")]

    def up_row(self, tab: str, ncols: int, first: int = 0) -> list[tuple[str, tuple]]:
        if not self.cwd[tab] or self.query_one("#search", Input).value.strip():
            return []
        cells = [""] * ncols
        cells[first] = dir_label(UP)
        return [(UP, tuple(cells))]

    def set_path(self, tab: str) -> None:
        q = self.query_one("#search", Input).value.strip()
        path = "/" + (self.cwd[tab] + "/" if self.cwd[tab] else "")
        self.query_one(f"#p-{tab}", Static).update(path + (f"  — searching '{q}' here and below" if q else ""))

    def render_ssh(self) -> None:
        hosts = list(self.data.hosts.values())
        dirs, here = self.listing("ssh", hosts, lambda h: (h.name, h.address, h.jump, h.description))
        rows = self.up_row("ssh", 4)
        for d in dirs:
            n = len(self.in_dir("ssh", hosts, d))
            rows.append((f"d:{d}", (dir_label(d), "", "", Text(f"servers: {n}", style="dim"))))
        for h in here:
            rows.append((h.name, (Text(self.rel_name("ssh", h), style="bold"), h.address, h.jump or "",
                                  h.description)))
        if not hosts:
            rows = [("-", (Text("no servers yet — add a file to ~/qc/ssh/ (see the example in ~/qc/README.md)",
                                style="dim"), "", "", ""))]
        self.set_path("ssh")
        self.fill("ssh", rows)

    def render_tun(self) -> None:
        rows = []
        if self.daemon_error:
            rows.append(("-", (Text("✗", style="red"), Text(self.daemon_error, style="red"), "", "", "")))
        items = [config.Tunnel(name=t["name"], via=t["via"], local=t["local"], remote=t["remote"],
                               description=t["description"], group=t["group"]) for t in self.tunnels]
        by_name = {t["name"]: t for t in self.tunnels}
        dirs, here = self.listing("tun", items, lambda t: (t.name, t.local, t.via, t.remote, t.description))
        rows += self.up_row("tun", 5, first=1)
        for d in dirs:
            ts = [by_name[x.name] for x in self.in_dir("tun", items, d)]
            on = sum(1 for t in ts if t["state"] in ACTIVE)
            bad = sum(1 for t in ts if t["state"] in ("error", "retry"))
            icon, color = ("✗", "red") if bad else ("●", "green") if on else ("○", "dim")
            state = f"{on} of {len(ts)} running" + (f", {bad} failing" if bad else "") if on or bad \
                else f"tunnels: {len(ts)}"
            rows.append((f"d:{d}", (Text(icon, style=color), dir_label(d), "", "",
                                    Text(state, style=color if on or bad else "dim"))))
        for x in here:
            t = by_name[x.name]
            icon, color = TUN_ICONS.get(t["state"], ("?", ""))
            if t["state"] == "running":
                state = "running" + (f" since {time.strftime('%H:%M', time.localtime(t['since']))}" if t["since"] else "")
            elif t["state"] == "starting":
                state = "connecting…"
            elif t["state"] == "retry":
                left = max(0, int((t["retry_at"] or 0) - time.time()))
                state = f"{t['error']} — retry in {left}s"
            elif t["state"] == "error":
                state = t["error"]
            else:
                state = "stopped" + (" (autostart)" if t["autostart"] else "")
            rows.append((t["name"], (Text(icon, style=color), Text(self.rel_name("tun", x), style="bold"),
                                     str(t["local"]), f"{t['via']} → {t['remote']}", Text(state, style=color))))
        self.set_path("tun")
        self.fill("tun", rows)

    def render_cmd(self) -> None:
        cmds = list(self.data.commands.values())
        dirs, here = self.listing("cmd", cmds, lambda c: (c.name, c.title, c.run, " ".join(c.tags), c.description))
        rows = self.up_row("cmd", 4)
        for d in dirs:
            n = len(self.in_dir("cmd", cmds, d))
            rows.append((f"d:{d}", (dir_label(d), Text(f"commands: {n}", style="dim"), "", "")))
        for c in here:
            run = c.run.replace("\n", " ⏎ ")
            rows.append((c.name, (Text(self.rel_name("cmd", c), style="bold"),
                                  run[:80] + ("…" if len(run) > 80 else ""), c.title, " ".join(c.tags))))
        self.set_path("cmd")
        self.fill("cmd", rows)

    def vault_text(self) -> str:
        v = self.vault_info
        if not v.get("unlocked"):
            return "locked"
        return "unlocked" + (time.strftime(" until %H:%M", time.localtime(v["lock_at"])) if v.get("lock_at") else "")

    def render_pass(self) -> None:
        label = self.query_one("#p-pass", Static)
        if not vault.initialized():
            label.update("no password vault yet")
            self.fill("pass", [("init", (Text("create the password vault — press Enter and choose a master password",
                                              style="yellow"), ""))])
            return
        hint = "Enter — copy" + (", Ctrl+T — type into the window" if self.mode == "kitty" else "")
        label.update(f"vault: {self.vault_text()}    {hint}")
        used: dict[str, list[str]] = {}
        for h in self.data.hosts.values():
            if h.password:
                used.setdefault(h.password, []).append(h.name)
        names = vault.names()
        rows = [(n, (Text(n, style="bold"), ", ".join(used.get(n, []))))
                for n in names if self.matches(n, *used.get(n, []))]
        for n in sorted(set(used) - set(names)):
            if self.matches(n, *used[n]):
                rows.append((f"missing:{n}", (Text(n, style="red"),
                                              Text(f"not in the vault — Enter to add (needed by {', '.join(used[n])})",
                                                   style="red"))))
        if not rows and not names:
            rows = [("-", (Text("no passwords yet — Ctrl+N to add one", style="dim"), ""))]
        self.fill("pass", rows)

    def render_set(self) -> None:
        remote = sync.remote_url() or settings.get("sync.remote")
        schedule = settings.get("sync.schedule") or "daily"
        errors = sum(1 for i in self.issues if i.level == "error")
        warns = len(self.issues) - errors
        rows = [
            ("remote", ("Data repository (git)",
                        remote or Text("not set — press Enter to add", style="yellow"))),
            ("schedule", ("Scheduled sync", SCHEDULE_TEXT.get(schedule, schedule) + "  (Enter — change)")),
            ("sync", ("Sync now", "Enter or Ctrl+S")),
            ("log", ("Sync log", "Enter — show")),
            ("check", ("Settings check",
                       Text(f"{errors} errors, {warns} warnings — press Enter", style="red" if errors else "yellow")
                       if self.issues else Text("all good", style="green"))),
            ("where", ("Data folder", str(paths.data_dir()))),
            ("vault", ("Password vault", (self.vault_text() + "  (Enter — change the master password)")
                       if vault.initialized() else Text("not set up — press Enter to create", style="yellow"))),
            ("autolock", ("Lock the vault after", self.autolock_text() + "  (Enter — change)")),
        ]
        self.fill("set", [r for r in rows if self.matches(*[str(x) for x in r[1]])] or rows)

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.render_all()

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        self.query_one("#search", Input).focus()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Footer hints — only those that work in the current tab."""
        tab = self.query_one(TabbedContent).active if self.is_mounted else "ssh"
        if action in ("tunnels", "restart", "logs"):
            return tab == "tun"
        if action == "new":
            return tab in TREE_TABS
        if action == "edit":
            return tab != "pass"
        if action == "pass_type":
            return tab == "pass" and self.mode == "kitty"
        if action.startswith("pass_") or action == "lock":
            return tab == "pass"
        return True

    # ---------- background: tunnels and sync ----------

    @work(thread=True, exclusive=True, group="tun")
    def poll_tunnels(self) -> None:
        try:
            if self.daemon_error.startswith("daemon"):
                client.ensure()
            resp = client.request("status", timeout=3)
            err = "" if resp.get("ok") else resp.get("error", "daemon error")
            tunnels = resp.get("tunnels", [])
            vinfo = resp.get("vault") or {}
        except client.DaemonDown as e:
            err, tunnels = str(e) if str(e).startswith("daemon") else "daemon qcd is not running — run qc setup", []
            vinfo = {}
        self.call_from_thread(self._set_tunnels, tunnels, err, vinfo)

    def _set_tunnels(self, tunnels: list[dict], err: str, vinfo: dict | None = None) -> None:
        self.tunnels, self.daemon_error = tunnels, err
        self.render_tun()
        if vinfo is not None and vinfo != self.vault_info:
            self.vault_info = vinfo
            self.render_pass()
            self.render_set()

    @work(thread=True, exclusive=True, group="syncstatus")
    def refresh_sync_status(self) -> None:
        level, text = sync.status_line(sync.status())
        if self.syncing:
            level, text = "warn", "↻ syncing…"
        self.call_from_thread(self._set_status, level, text)

    def _set_status(self, level: str, text: str) -> None:
        w = self.query_one("#status", Static)
        w.set_classes(level)
        w.update(text + ("    Ctrl+S — sync" if level != "off" else ""))

    @work(thread=True, group="fetch")
    def fetch_remote(self) -> None:
        if sync.fetch():
            self.call_from_thread(self.refresh_sync_status)

    @work(thread=True, exclusive=True, group="sync")
    def run_sync(self) -> None:
        self.syncing = True
        self.call_from_thread(self._set_status, "warn", "↻ syncing…")
        try:
            r = sync.sync()
        finally:
            self.syncing = False
        self.call_from_thread(self._after_sync, r)

    def _after_sync(self, r: dict) -> None:
        self.notify(r["message"], title="sync", severity="information" if r["ok"] else "error")
        self.load_data()
        self.refresh_sync_status()

    @work(thread=True, group="attach")
    def run_attach(self, url: str) -> None:
        self.syncing = True
        try:
            msg, ok = sync.attach(url), True
        except sync.SyncError as e:
            msg, ok = str(e), False
        finally:
            self.syncing = False
        self.call_from_thread(self._after_attach, msg, ok)

    def _after_attach(self, msg: str, ok: bool) -> None:
        self.notify(msg, title="data repository", severity="information" if ok else "error", timeout=8)
        self.load_data()
        self.refresh_sync_status()

    # ---------- actions ----------

    def current_key(self) -> str | None:
        t, keys = self.table(self.tab), self.keys[self.tab]
        if not keys or not (0 <= t.cursor_row < len(keys)) or keys[t.cursor_row] == "-":
            return None
        return keys[t.cursor_row]

    def current_entry(self) -> str | None:
        """Entry (not a folder) under the cursor."""
        key = self.current_key()
        return None if is_dir(key) else key

    def chdir(self, tab: str, path: str, select: str | None = None) -> None:
        self.cwd[tab] = config.norm_dir(path)
        self.select_next[tab] = select or ""
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""  # Input.Changed will re-render
        else:
            getattr(self, f"render_{tab}")()

    def action_up(self) -> None:
        tab = self.tab
        if tab in TREE_TABS and self.cwd[tab]:
            parent, _, me = self.cwd[tab].rpartition("/")
            self.chdir(tab, parent, select=f"d:{me}")

    def action_into(self) -> None:
        key = self.current_key()
        if self.tab in TREE_TABS and key and key.startswith("d:"):
            cwd = self.cwd[self.tab]
            self.chdir(self.tab, f"{cwd}/{key[2:]}" if cwd else key[2:])

    def action_tab(self, step: int) -> None:
        tc = self.query_one(TabbedContent)
        tc.active = TABS[(TABS.index(self.tab) + step) % len(TABS)]

    def action_goto(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_cursor(self, step: int) -> None:
        t = self.table(self.tab)
        if t.row_count:
            t.move_cursor(row=max(0, min(t.row_count - 1, t.cursor_row + step)), animate=False)

    def action_escape(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        else:
            self.exit(None)

    def action_quit_menu(self) -> None:
        self.exit(None)

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self.action_activate()

    @on(DataTable.RowSelected)
    def row_selected(self) -> None:
        self.action_activate()

    def action_activate(self) -> None:
        key = self.current_key()
        if key is None:
            return
        tab = self.tab
        if key == UP:
            self.action_up()
        elif key.startswith("d:") and tab in TREE_TABS:
            self.action_into()
        elif tab == "ssh":
            self.connect(key)
        elif tab == "tun":
            t = next((t for t in self.tunnels if t["name"] == key), None)
            if t:
                cmd = "down" if t["state"] in ("running", "starting", "retry") else "up"
                self.tunnel_cmd(cmd, key)
        elif tab == "cmd":
            self.pick_command(self.data.commands[key])
        elif tab == "set":
            self.settings_action(key)
        elif tab == "pass":
            if key == "init":
                self.vault_init()
            elif key.startswith("missing:"):
                self.pass_set(key.removeprefix("missing:"))
            else:
                self.with_secret(key, self.copy_secret)

    def selected_tunnels(self) -> list[str]:
        """Tunnel under the cursor, or all tunnels of the folder under the cursor (including subfolders)."""
        key = self.current_key()
        if not key or key == UP:
            return []
        if key.startswith("d:"):
            items = [config.Tunnel(name=t["name"], via="", local=0, remote="", group=t["group"]) for t in self.tunnels]
            return [x.name for x in self.in_dir("tun", items, key[2:])]
        return [key]

    def action_tunnels(self, cmd: str) -> None:
        if self.tab != "tun":
            return
        names = self.selected_tunnels()
        if not names:
            return
        states = {t["name"]: t["state"] for t in self.tunnels}
        if cmd == "down":
            names = [n for n in names if states.get(n) in ACTIVE + ("error",)]
            if not names:
                self.notify("nothing running here", title="tunnels")
                return
        else:
            names = [n for n in names if states.get(n) not in ACTIVE]
            if not names:
                self.notify("everything here is already running", title="tunnels")
                return
        if len(names) > 1:
            self.notify(("stopping: " if cmd == "down" else "starting: ") + ", ".join(names), title="tunnels")
        self.tunnel_cmd(cmd, *names)

    @work(thread=True, group="tuncmd")
    def tunnel_cmd(self, cmd: str, *names: str) -> None:
        try:
            r = client.request(cmd, names=list(names))
            if not r.get("ok"):
                self.call_from_thread(self.notify, r.get("error", "error"), severity="error")
        except client.DaemonDown as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.poll_tunnels()

    def action_restart(self) -> None:
        if self.tab == "tun" and (key := self.current_entry()):
            self.tunnel_cmd("restart", key)

    @work(thread=True, group="logs")
    def action_logs(self) -> None:
        if self.tab != "tun" or not (key := self.current_entry()):
            return
        try:
            r = client.request("logs", name=key, n=40)
            lines = [f"{time.strftime('%H:%M:%S', time.localtime(t))}  {line}" for t, line in r.get("lines", [])]
            text = "\n".join(lines)
        except client.DaemonDown as e:
            text = str(e)
        self.call_from_thread(self.push_screen, TextScreen(f"Tunnel log: {key}", text))

    def action_sync(self) -> None:
        if not self.syncing:
            self.run_sync()

    def pick_command(self, cmd: config.Command) -> None:
        names = cmd.placeholders
        values: dict[str, str] = {}

        def ask(i: int) -> None:
            if i == len(names):
                self.exit(("insert", cmd.render(values)))
                return

            def got(v: str | None) -> None:
                if v is None:
                    return
                values[names[i]] = v
                ask(i + 1)

            self.push_screen(AskScreen(f"{cmd.title or cmd.name}: value for {{{{{names[i]}}}}}"), got)

        ask(0)

    def settings_action(self, key: str) -> None:
        if key == "remote":
            def got(url: str | None) -> None:
                if url and url.strip():
                    self.notify("connecting repository…", title="data repository")
                    self.run_attach(url.strip())

            self.push_screen(AskScreen("Git URL of the data repository (keep it PRIVATE: it holds server addresses)",
                                       sync.remote_url() or settings.get("sync.remote") or "",
                                       "git@github.com:<you>/qc-data.git"), got)
        elif key == "schedule":
            cur = settings.get("sync.schedule") or "daily"
            nxt = settings.SCHEDULES[(settings.SCHEDULES.index(cur) + 1) % len(settings.SCHEDULES)] \
                if cur in settings.SCHEDULES else "daily"
            msg = "; ".join(install.apply_schedule(nxt))
            self.notify(msg, title="schedule")
            self.render_set()
            self.refresh_sync_status()
        elif key == "sync":
            self.action_sync()
        elif key == "log":
            self.push_screen(TextScreen("Sync log", "\n".join(sync.log_tail(40))))
        elif key == "check":
            self.push_screen(TextScreen("Settings check: ~/qc", "\n".join(map(str, self.issues))))
        elif key == "vault":
            self.vault_passwd() if vault.initialized() else self.vault_init()
        elif key == "autolock":
            steps = [1, 4, 8, 24, 0]
            cur = self.autolock_hours()
            nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 8
            settings.set_value("vault.timeout_hours", nxt)
            self.notify("applies from the next unlock", title=f"lock the vault after: {self.autolock_text()}")
            self.render_set()

    # ---------- passwords ----------

    def autolock_hours(self) -> int:
        try:
            return int(float(settings.get("vault.timeout_hours")))
        except (TypeError, ValueError):
            return 8

    def autolock_text(self) -> str:
        h = self.autolock_hours()
        return f"{h} h" if h else "never (only by hand)"

    def ask_master(self, then, title: str = "Master password of the password vault") -> None:
        self.push_screen(AskScreen(title, password=True), lambda pw: then(pw) if pw else None)

    def ask_new_secret(self, title: str, then) -> None:
        """Ask a new password twice; then(value) when both entries match."""
        def first(a: str | None) -> None:
            if not a:
                return

            def second(b: str | None) -> None:
                if b is None:
                    return
                if a != b:
                    self.notify("the two entries differ — try again", severity="error")
                    self.ask_new_secret(title, then)
                    return
                then(a)

            self.push_screen(AskScreen(f"{title} (again)", password=True), second)

        self.push_screen(AskScreen(title, password=True), first)

    def vault_init(self, then=None) -> None:
        def got(master: str) -> None:
            try:
                vault.init(master)
            except vault.VaultError as e:
                self.notify(str(e), severity="error")
                return
            vault.commit("qc: password vault created")
            self.notify("remember the master password — it cannot be recovered", title="password vault created")
            self.load_data()
            if then:
                then()

        self.ask_new_secret("New master password for the vault (keys of all your passwords)", got)

    def vault_passwd(self) -> None:
        def got_old(old: str) -> None:
            def got_new(new: str) -> None:
                self.change_master(old, new)

            self.ask_new_secret("New master password", got_new)

        self.ask_master(got_old, "Current master password")

    @work(thread=True, group="vault")
    def change_master(self, old: str, new: str) -> None:
        try:
            vault.change_master(old, new)
            vault.commit("qc: master password changed")
            self.call_from_thread(self.notify, "master password changed", title="password vault")
        except vault.VaultError as e:
            self.call_from_thread(self.notify, str(e), severity="error")

    def with_secret(self, name: str, then) -> None:
        """Get a password (unlocking the vault first if needed) and call then(secret)."""
        self.fetch_secret(name, then, None)

    @work(thread=True, group="vault")
    def fetch_secret(self, name: str, then, master: str | None) -> None:
        try:
            if master is not None:
                r = client.request("unlock", timeout=30, password=master)
                if not r.get("ok"):
                    self.call_from_thread(self.notify, r.get("error", "error"), severity="error")
                    self.call_from_thread(self.ask_master, lambda pw: self.fetch_secret(name, then, pw))
                    return
                self.poll_tunnels()
            r = client.request("secret", name=name)
            if r.get("locked"):
                self.call_from_thread(self.ask_master, lambda pw: self.fetch_secret(name, then, pw))
                return
            if not r.get("ok"):
                self.call_from_thread(self.notify, r.get("error", "error"), severity="error")
                return
            self.call_from_thread(then, r["secret"])
        except client.DaemonDown:
            # no daemon: decrypt right here (nothing stays unlocked afterwards)
            if master is None:
                self.call_from_thread(self.ask_master, lambda pw: self.fetch_local(name, then, pw))
            else:
                self.fetch_local(name, then, master)

    @work(thread=True, group="vault")
    def fetch_local(self, name: str, then, master: str) -> None:
        try:
            secret = vault.get(name, vault.unlock(master))
        except vault.VaultError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(then, secret)

    def copy_secret(self, secret: str) -> None:
        seconds = int(settings.get("vault.clipboard_seconds") or 30)
        try:
            clipboard.copy(secret, seconds)
        except clipboard.ClipboardError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"the clipboard is cleared in {seconds} s", title="password copied")

    def pass_set(self, name: str) -> None:
        def got(secret: str) -> None:
            try:
                vault.put(name, secret)
            except vault.VaultError as e:
                self.notify(str(e), severity="error")
                return
            vault.commit(f"qc: password {name}")
            self.select_next["pass"] = name
            self.load_data()
            self.notify(f"'{name}' saved", title="password vault")

        if not vault.initialized():
            self.vault_init(lambda: self.pass_set(name))
            return
        self.ask_new_secret(f"Password '{name}'", got)

    def action_pass_new(self) -> None:
        def got(name: str | None) -> None:
            if not name or not name.strip():
                return
            try:
                name = vault.check_name(name)
            except vault.VaultError as e:
                self.notify(str(e), severity="error")
                return
            if name in vault.names():
                self.notify(f"'{name}' already exists — Ctrl+E changes it", severity="error")
                return
            self.pass_set(name)

        self.push_screen(AskScreen("Name of the new password (servers refer to it as password = \"name\")",
                                   placeholder="e.g. prod-db"), got)

    def action_pass_change(self) -> None:
        key = self.current_key()
        if key and key not in ("-", "init"):
            self.pass_set(key.removeprefix("missing:"))

    def action_pass_delete(self) -> None:
        key = self.current_key()
        if not key or key in ("-", "init") or key.startswith("missing:"):
            return

        def yes(ok: bool | None) -> None:
            if not ok:
                return
            try:
                vault.remove(key)
            except vault.VaultError as e:
                self.notify(str(e), severity="error")
                return
            vault.commit(f"qc: password {key} removed")
            self.load_data()
            self.notify(f"'{key}' deleted", title="password vault")

        self.push_screen(ConfirmScreen(f"Delete the password '{key}'?",
                                       "It stays in the git history in encrypted form.",
                                       "Enter — delete, Esc — cancel"), yes)

    def action_pass_type(self) -> None:
        key = self.current_key()
        if self.mode == "kitty" and key and key not in ("-", "init") and not key.startswith("missing:"):
            self.with_secret(key, lambda secret: self.exit(("type", secret)))

    @work(thread=True, group="vault")
    def action_lock(self) -> None:
        try:
            client.request("lock")
        except client.DaemonDown as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self.notify, "vault locked", title="password vault")
        self.poll_tunnels()

    def connect(self, name: str) -> None:
        """ssh to a server; if its passwords are in a locked vault — unlock it here first, then go."""
        need = vault.chain_passwords(self.data, name)
        if (need and vault.initialized() and not self.daemon_error and not self.vault_info.get("unlocked")
                and all(n in vault.names() for n in need)):
            self.ask_master(lambda pw: self.unlock_then_connect(name, pw),
                            f"{name} logs in with a password from the vault — master password")
            return
        self.exit(("ssh", name))

    @work(thread=True, group="vault")
    def unlock_then_connect(self, name: str, master: str) -> None:
        try:
            r = client.request("unlock", timeout=30, password=master)
        except client.DaemonDown as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        if not r.get("ok"):
            self.call_from_thread(self.notify, r.get("error", "error"), severity="error")
            self.call_from_thread(self.connect, name)
            return
        self.call_from_thread(self.exit, ("ssh", name))

    # ---------- editing in vim ----------

    def edit_target(self) -> tuple[list, int | None, str] | None:
        """What Ctrl+E opens: (files, line, what is being edited)."""
        tab, key = self.tab, self.current_key()
        cwd = self.cwd[tab]
        if key is None or key == UP:
            files = edit.dir_files(self.data, tab, cwd)
            return (files, None, f"/{cwd}") if files else None
        if key.startswith("d:"):
            full = f"{cwd}/{key[2:]}" if cwd else key[2:]
            return edit.dir_files(self.data, tab, full), None, f"/{full}/"
        path, line = edit.locate(self.data, tab, key)
        return [path], line, key

    def action_edit(self) -> None:
        tab = self.tab
        if tab == "set":
            self.open_editor([settings._file()], None, "settings", commit=False)
            return
        if tab not in TREE_TABS:
            return
        try:
            target = self.edit_target()
        except edit.EditError as e:
            self.notify(str(e), severity="error")
            return
        if not target:
            self.action_new()
            return
        files, line, what = target
        self.open_editor(files, line, what)

    def action_new(self) -> None:
        tab = self.tab
        if tab not in TREE_TABS:
            return
        cwd = self.cwd[tab]
        try:
            draft = edit.new_entry(self.data, tab, cwd)
        except edit.EditError:
            # the folder has no file of its own (root, or a folder made only of subfolders) — create one
            last = cwd.rpartition("/")[2]
            suggest = re.sub(r"[^A-Za-z0-9_-]+", "_", last).strip("_").lower() or "new"
            sub = edit.SECTIONS[tab][0]

            def got(name: str | None) -> None:
                if not name or not name.strip():
                    return
                try:
                    d = edit.new_entry(self.data, tab, cwd, new_file=name)
                except edit.EditError as e:
                    self.notify(str(e), severity="error")
                    return
                self.open_editor([d.path], d.line, d.name, draft=d)

            self.push_screen(AskScreen(f"New file in ~/qc/{sub}/ (menu folder: /{cwd})", suggest,
                                       "file name (Latin letters)"), got)
            return
        self.open_editor([draft.path], draft.line, draft.name, draft=draft)

    def open_editor(self, files: list, line: int | None, what: str, draft=None, commit: bool = True) -> None:
        try:
            cmd = edit.editor_cmd(files, line)
        except edit.EditError as e:
            if draft:
                draft.rollback_if_untouched()
            self.notify(str(e), severity="error")
            return
        before = edit.snapshot(files)
        with self.suspend():
            subprocess.run(cmd)
        if draft and draft.rollback_if_untouched():
            self.notify("template unchanged — no new entry added", title="edit")
            self.load_data()
            return
        if not commit:
            self.render_set()
            self.refresh_sync_status()
            return
        if edit.snapshot(files) == before:
            return
        rels = ", ".join(str(f.relative_to(paths.data_dir())) if paths.data_dir() in f.parents else str(f)
                         for f in files)
        r = edit.after_edit(files, f"qc: edit {what} ({rels})")
        if draft and self.tab in TREE_TABS:
            self.select_next[self.tab] = draft.name
        self.load_data()
        if r.ok:
            self.notify(f"{rels} — saved" + (", committed to ~/qc" if r.committed else ""), title="edit")
            return

        def again(yes: bool | None) -> None:
            if yes:
                self.open_editor([r.file] if r.file else files, r.line or line, what)

        self.push_screen(ConfirmScreen(f"Errors after editing {rels}", "\n".join(r.errors),
                                       "Enter — reopen in the editor, Esc — leave as is"), again)


def run(mode: str = "plain", inline: bool = False, popup: bool = False) -> tuple | None:
    """inline — below the prompt, not full-screen (like fzf); popup — already in its own window, fill the whole window."""
    return QcApp(mode, popup=popup).run(inline=inline)
