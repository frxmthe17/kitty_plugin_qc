"""Меню qc: Подключения / Туннели / Команды / Пароли / Настройки + строка статуса синхронизации."""
from __future__ import annotations

import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static, TabbedContent, TabPane

from qc import client, config, install, paths, settings, sync

TABS = ["ssh", "tun", "cmd", "pass", "set"]
SCHEDULE_TEXT = {"daily": "раз в день", "hourly": "раз в час", "off": "выключена"}


class AskScreen(ModalScreen[str | None]):
    """Спросить одну строку (адрес гита, значение {{подстановки}})."""

    BINDINGS = [Binding("escape", "cancel", "отмена")]

    def __init__(self, title: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.title_text, self.value, self.placeholder = title, value, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.value, placeholder=self.placeholder, id="ask")
            yield Label("Enter — готово, Esc — отмена", classes="hint")

    @on(Input.Submitted)
    def done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    """Показать текст (лог туннеля, лог синхронизаций, проблемы в настройках)."""

    BINDINGS = [Binding("escape", "close", "закрыть"), Binding("q", "close", show=False),
                Binding("enter", "close", show=False)]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(self.title_text, classes="title")
            yield Static(self.text or "(пусто)", id="text")
            yield Label("Esc — закрыть", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class QcApp(App[tuple | None]):
    TITLE = "qc"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; margin: 0 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $panel; }
    #status.ok { color: $success; }
    #status.warn { color: $warning; }
    #status.error { color: $error; text-style: bold; }
    #status.off { color: $text-muted; }
    AskScreen, TextScreen { align: center middle; }
    #dialog { width: 80; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    #dialog.wide { width: 90%; }
    #text { height: auto; max-height: 30; overflow-y: auto; }
    .hint { color: $text-muted; }
    .title { text-style: bold; }
    """
    BINDINGS = [
        Binding("tab", "tab(1)", "раздел", priority=True),
        Binding("shift+tab", "tab(-1)", show=False, priority=True),
        *[Binding(f"f{i + 1}", f"goto('{t}')", show=False) for i, t in enumerate(TABS)],
        Binding("enter", "activate", "выбрать"),
        Binding("up", "cursor(-1)", show=False),
        Binding("down", "cursor(1)", show=False),
        Binding("pageup", "cursor(-10)", show=False),
        Binding("pagedown", "cursor(10)", show=False),
        Binding("ctrl+r", "restart", "туннель: перезапуск"),
        Binding("ctrl+l", "logs", "туннель: лог"),
        Binding("ctrl+s", "sync", "синхронизировать"),
        Binding("escape", "escape", "выход"),
        Binding("ctrl+q", "quit_menu", show=False, priority=True),
    ]

    def __init__(self, mode: str = "plain") -> None:
        super().__init__()
        self.mode = mode  # plain | bash | kitty — от этого зависят подсказки
        self.data = config.Data()
        self.issues: list[config.Issue] = []
        self.tunnels: list[dict] = []
        self.daemon_error = ""
        self.keys: dict[str, list[str]] = {t: [] for t in TABS}
        self.syncing = False

    # ---------- разметка ----------

    def compose(self) -> ComposeResult:
        yield Input(placeholder="поиск…  (Tab — раздел, Enter — выбрать, Esc — выход)", id="search")
        with TabbedContent(initial="ssh"):
            for tab, title in zip(TABS, ["Подключения", "Туннели", "Команды", "Пароли", "Настройки"]):
                with TabPane(title, id=tab):
                    yield DataTable(id=f"t-{tab}", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        cols = {
            "ssh": ["Имя", "Группа", "Адрес", "Через", "Описание"],
            "tun": ["", "Имя", "Порт", "Куда", "Группа", "Состояние"],
            "cmd": ["Команда", "Что делает", "Группа", "Теги"],
            "pass": ["Пароли"],
            "set": ["Настройка", "Значение"],
        }
        for tab, names in cols.items():
            self.table(tab).add_columns(*names)
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

    # ---------- данные и фильтр ----------

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
        if old in self.keys[tab]:
            t.move_cursor(row=self.keys[tab].index(old), animate=False)

    def render_all(self) -> None:
        for tab in TABS:
            getattr(self, f"render_{tab}")()

    def render_ssh(self) -> None:
        rows = []
        for h in sorted(self.data.hosts.values(), key=lambda h: (h.group, h.name)):
            if self.matches(h.name, h.group, h.address, h.jump, h.description):
                rows.append((h.name, (Text(h.name, style="bold"), h.group, h.address, h.jump or "",
                                      h.description)))
        if not rows and not self.data.hosts:
            rows = [("-", (Text("серверов пока нет — добавь файл в ~/qc/ssh/ (пример в ~/qc/README.md)",
                                style="dim"), "", "", "", ""))]
        self.fill("ssh", rows)

    def render_tun(self) -> None:
        rows = []
        if self.daemon_error:
            rows.append(("-", (Text("✗", style="red"), Text(self.daemon_error, style="red"), "", "", "", "")))
        icons = {"running": ("●", "green"), "starting": ("◐", "yellow"), "retry": ("↻", "yellow"),
                 "error": ("✗", "red"), "stopped": ("○", "dim")}
        for t in sorted(self.tunnels, key=lambda t: (t["group"], t["name"])):
            if not self.matches(t["name"], t["group"], t["local"], t["via"], t["remote"], t["description"]):
                continue
            icon, color = icons.get(t["state"], ("?", ""))
            if t["state"] == "running":
                state = "работает" + (f" с {time.strftime('%H:%M', time.localtime(t['since']))}" if t["since"] else "")
            elif t["state"] == "starting":
                state = "подключается…"
            elif t["state"] == "retry":
                left = max(0, int((t["retry_at"] or 0) - time.time()))
                state = f"{t['error']} — повтор через {left} с"
            elif t["state"] == "error":
                state = t["error"]
            else:
                state = "остановлен" + (" (автостарт)" if t["autostart"] else "")
            rows.append((t["name"], (Text(icon, style=color), Text(t["name"], style="bold"), str(t["local"]),
                                     f"{t['via']} → {t['remote']}", t["group"], Text(state, style=color))))
        self.fill("tun", rows)

    def render_cmd(self) -> None:
        rows = []
        for c in sorted(self.data.commands.values(), key=lambda c: (c.group, c.name)):
            if self.matches(c.name, c.title, c.run, c.group, " ".join(c.tags), c.description):
                run = c.run.replace("\n", " ⏎ ")
                rows.append((c.name, (Text(run[:90] + ("…" if len(run) > 90 else ""), style="bold"),
                                      c.title or c.name, c.group, " ".join(c.tags))))
        self.fill("cmd", rows)

    def render_pass(self) -> None:
        self.fill("pass", [("-", (Text("хранилище паролей — следующий шаг (зашифрованные файлы в ~/qc/secure)",
                                       style="dim"),))])

    def render_set(self) -> None:
        remote = sync.remote_url() or settings.get("sync.remote")
        schedule = settings.get("sync.schedule") or "daily"
        errors = sum(1 for i in self.issues if i.level == "error")
        warns = len(self.issues) - errors
        rows = [
            ("remote", ("Репозиторий данных (git)",
                        remote or Text("не задан — Enter, чтобы добавить", style="yellow"))),
            ("schedule", ("Синхронизация по расписанию", SCHEDULE_TEXT.get(schedule, schedule) + "  (Enter — сменить)")),
            ("sync", ("Синхронизировать сейчас", "Enter или Ctrl+S")),
            ("log", ("Лог синхронизаций", "Enter — показать")),
            ("check", ("Проверка настроек",
                       Text(f"ошибок {errors}, предупреждений {warns} — Enter", style="red" if errors else "yellow")
                       if self.issues else Text("всё в порядке", style="green"))),
            ("where", ("Папка данных", str(paths.data_dir()))),
        ]
        self.fill("set", [r for r in rows if self.matches(*[str(x) for x in r[1]])] or rows)

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.render_all()

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        self.query_one("#search", Input).focus()

    # ---------- фон: туннели и синхронизация ----------

    @work(thread=True, exclusive=True, group="tun")
    def poll_tunnels(self) -> None:
        try:
            if self.daemon_error.startswith("демон"):
                client.ensure()
            resp = client.request("status", timeout=3)
            err = "" if resp.get("ok") else resp.get("error", "ошибка демона")
            tunnels = resp.get("tunnels", [])
        except client.DaemonDown as e:
            err, tunnels = str(e) if str(e).startswith("демон") else "демон qcd не запущен — qc setup", []
        self.call_from_thread(self._set_tunnels, tunnels, err)

    def _set_tunnels(self, tunnels: list[dict], err: str) -> None:
        self.tunnels, self.daemon_error = tunnels, err
        self.render_tun()

    @work(thread=True, exclusive=True, group="syncstatus")
    def refresh_sync_status(self) -> None:
        level, text = sync.status_line(sync.status())
        if self.syncing:
            level, text = "warn", "↻ синхронизация…"
        self.call_from_thread(self._set_status, level, text)

    def _set_status(self, level: str, text: str) -> None:
        w = self.query_one("#status", Static)
        w.set_classes(level)
        w.update(text + ("    Ctrl+S — синхронизировать" if level != "off" else ""))

    @work(thread=True, group="fetch")
    def fetch_remote(self) -> None:
        if sync.fetch():
            self.call_from_thread(self.refresh_sync_status)

    @work(thread=True, exclusive=True, group="sync")
    def run_sync(self) -> None:
        self.syncing = True
        self.call_from_thread(self._set_status, "warn", "↻ синхронизация…")
        try:
            r = sync.sync()
        finally:
            self.syncing = False
        self.call_from_thread(self._after_sync, r)

    def _after_sync(self, r: dict) -> None:
        self.notify(r["message"], title="синхронизация", severity="information" if r["ok"] else "error")
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
        self.notify(msg, title="репозиторий данных", severity="information" if ok else "error", timeout=8)
        self.load_data()
        self.refresh_sync_status()

    # ---------- действия ----------

    def current_key(self) -> str | None:
        t, keys = self.table(self.tab), self.keys[self.tab]
        if not keys or not (0 <= t.cursor_row < len(keys)) or keys[t.cursor_row] == "-":
            return None
        return keys[t.cursor_row]

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
        if tab == "ssh":
            self.exit(("ssh", key))
        elif tab == "tun":
            t = next((t for t in self.tunnels if t["name"] == key), None)
            if t:
                cmd = "down" if t["state"] in ("running", "starting", "retry") else "up"
                self.tunnel_cmd(cmd, key)
        elif tab == "cmd":
            self.pick_command(self.data.commands[key])
        elif tab == "set":
            self.settings_action(key)

    @work(thread=True, group="tuncmd")
    def tunnel_cmd(self, cmd: str, name: str) -> None:
        try:
            r = client.request(cmd, names=[name])
            if not r.get("ok"):
                self.call_from_thread(self.notify, r.get("error", "ошибка"), severity="error")
        except client.DaemonDown as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.poll_tunnels()

    def action_restart(self) -> None:
        if self.tab == "tun" and (key := self.current_key()):
            self.tunnel_cmd("restart", key)

    @work(thread=True, group="logs")
    def action_logs(self) -> None:
        if self.tab != "tun" or not (key := self.current_key()):
            return
        try:
            r = client.request("logs", name=key, n=40)
            lines = [f"{time.strftime('%H:%M:%S', time.localtime(t))}  {line}" for t, line in r.get("lines", [])]
            text = "\n".join(lines)
        except client.DaemonDown as e:
            text = str(e)
        self.call_from_thread(self.push_screen, TextScreen(f"Лог туннеля {key}", text))

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

            self.push_screen(AskScreen(f"{cmd.title or cmd.name}: значение для {{{{{names[i]}}}}}"), got)

        ask(0)

    def settings_action(self, key: str) -> None:
        if key == "remote":
            def got(url: str | None) -> None:
                if url and url.strip():
                    self.notify("подключаю репозиторий…", title="репозиторий данных")
                    self.run_attach(url.strip())

            self.push_screen(AskScreen("Адрес git-репозитория с данными (ПРИВАТНОГО: там адреса серверов)",
                                       sync.remote_url() or settings.get("sync.remote") or "",
                                       "git@github.com:<you>/qc-data.git"), got)
        elif key == "schedule":
            cur = settings.get("sync.schedule") or "daily"
            nxt = settings.SCHEDULES[(settings.SCHEDULES.index(cur) + 1) % len(settings.SCHEDULES)] \
                if cur in settings.SCHEDULES else "daily"
            msg = "; ".join(install.apply_schedule(nxt))
            self.notify(msg, title="расписание")
            self.render_set()
            self.refresh_sync_status()
        elif key == "sync":
            self.action_sync()
        elif key == "log":
            self.push_screen(TextScreen("Лог синхронизаций", "\n".join(sync.log_tail(40))))
        elif key == "check":
            self.push_screen(TextScreen("Проверка настроек ~/qc", "\n".join(map(str, self.issues))))


def run(mode: str = "plain") -> tuple | None:
    return QcApp(mode).run()
