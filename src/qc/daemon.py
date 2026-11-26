"""qcd — демон туннелей. Держит `ssh -N -L` фоновыми процессами без вкладок терминала,
перезапускает при обрыве, следит за портами. Команды принимает через unix-сокет (одна JSON-строка на подключение)."""
from __future__ import annotations

import asyncio
import collections
import ctypes
import json
import os
import signal
import sys
import time

from qc import config, paths, ports, sshgen

# (что ищем в выводе ssh, что показать, повторять ли попытки)
# Ошибки входа не повторяем: без толку, а fail2ban на сервере может забанить наш IP.
ERRORS = [
    ("Permission denied", "отказ во входе (ключ/пароль не подошёл)", False),
    ("Too many authentication failures", "отказ во входе (слишком много ключей)", False),
    ("Host key verification failed", "ключ сервера неизвестен или сменился — зайди один раз: qc ssh {via}", False),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "ключ сервера сменился — проверь и поправь known_hosts", False),
    ("Could not resolve hostname", "адрес сервера не находится (DNS)", True),
    ("Connection timed out", "сервер не отвечает — нет VPN/сети?", True),
    ("Operation timed out", "сервер не отвечает — нет VPN/сети?", True),
    ("No route to host", "нет маршрута до сервера — нет VPN/сети?", True),
    ("Network is unreachable", "нет сети", True),
    ("Connection refused", "сервер отклонил подключение (ssh закрыт)", True),
    ("Address already in use", "локальный порт занят", True),
    ("cannot listen to port", "локальный порт занят", True),
    ("forwarding failed", "сервер не дал пробросить порт", False),
    ("Connection closed by", "сервер закрыл подключение", True),
    ("Broken pipe", "связь оборвалась", True),
    ("Timeout, server", "связь оборвалась (сервер перестал отвечать)", True),
]


def _pdeathsig() -> None:
    """ssh умирает вместе с демоном, даже если демон убит kill -9."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def classify(lines, rc: int | None, via: str) -> tuple[str, bool]:
    for _, line in reversed(lines):
        for needle, text, retry in ERRORS:
            if needle in line:
                return text.format(via=via), retry
    for _, line in reversed(lines):
        if line and not line.startswith(("Warning:", "qc:")):
            return line, True
    return f"ssh завершился с кодом {rc}", True


def host_chain_key(data: config.Data, name: str) -> tuple:
    """Изменился сервер или любой его прыжок — туннель надо перезапустить."""
    out, seen = [], set()
    while name and name in data.hosts and name not in seen:
        seen.add(name)
        out.append(repr(data.hosts[name]))
        name = data.hosts[name].jump
    return tuple(out)


def chain_has_password(data: config.Data, name: str) -> str | None:
    seen = set()
    while name and name in data.hosts and name not in seen:
        seen.add(name)
        if data.hosts[name].password:
            return name
        name = data.hosts[name].jump
    return None


class TunnelState:
    def __init__(self, spec: config.Tunnel, host_key: tuple):
        self.spec = spec
        self.host_key = host_key
        self.state = "stopped"  # stopped | starting | running | retry | error
        self.error = ""
        self.want = False
        self.proc: asyncio.subprocess.Process | None = None
        self.since: float | None = None
        self.retry_at: float | None = None
        self.log: collections.deque[tuple[float, str]] = collections.deque(maxlen=200)
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()

    def note(self, line: str) -> None:
        self.log.append((time.time(), line))

    def to_dict(self) -> dict:
        s = self.spec
        return {
            "name": s.name, "group": s.group, "via": s.via, "local": s.local, "bind": s.bind,
            "remote": s.remote, "autostart": s.autostart, "description": s.description,
            "state": self.state, "error": self.error, "since": self.since, "retry_at": self.retry_at,
            "pid": self.proc.pid if self.proc and self.proc.returncode is None else None,
        }


class Daemon:
    def __init__(self) -> None:
        self.data = config.Data()
        self.issues: list[config.Issue] = []
        self.tunnels: dict[str, TunnelState] = {}
        self.mtime: float | None = None
        self.stopping = asyncio.Event()

    # ---------- конфиг ----------

    def scan_mtime(self) -> float:
        base, m = paths.data_dir(), 0.0
        for sub in ("ssh", "tunnels"):
            d = base / sub
            try:
                m = max(m, d.stat().st_mtime)  # удаление файла меняет время папки
                for f in d.glob("*.toml"):
                    m = max(m, f.stat().st_mtime)
            except OSError:
                pass
        return m

    def reload(self) -> None:
        self.mtime = self.scan_mtime()
        data = config.load()
        self.issues = config.check(data)
        try:
            sshgen.write(data)
        except OSError as e:
            log(f"не записать ssh_config: {e}")
        old, new = self.tunnels, {}
        for name, spec in data.tunnels.items():
            key = host_chain_key(data, spec.via)
            st = old.pop(name, None)
            if st is None:
                st = TunnelState(spec, key)
                new[name] = st
                if spec.autostart:
                    self.data = data
                    self.start(st)
                continue
            changed = st.spec.spec_key() != spec.spec_key() or st.host_key != key
            st.spec, st.host_key = spec, key
            new[name] = st
            if changed and st.want:
                log(f"{name}: настройки изменились — перезапуск")
                asyncio.create_task(self.restart(st))
        for st in old.values():
            log(f"{st.spec.name}: удалён из настроек — остановка")
            self.stop(st)
        self.tunnels, self.data = new, data

    async def watch_config(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(3)
            if self.scan_mtime() != self.mtime:
                log("настройки в ~/qc изменились — перечитываю")
                self.reload()

    # ---------- туннели ----------

    def start(self, st: TunnelState) -> None:
        if st.task and not st.task.done():
            if st.state == "retry":
                st.wake.set()  # «подними» во время паузы — пробуем сразу
            return
        st.want, st.error = True, ""
        st.task = asyncio.create_task(self.supervise(st))

    def stop(self, st: TunnelState) -> None:
        st.want = False
        st.wake.set()
        if st.proc and st.proc.returncode is None:
            st.proc.terminate()
        if not (st.task and not st.task.done()):
            st.state, st.error = "stopped", ""

    async def restart(self, st: TunnelState) -> None:
        self.stop(st)
        if st.task:
            await asyncio.gather(st.task, return_exceptions=True)
        self.start(st)

    def preflight(self, st: TunnelState) -> tuple[str, bool] | None:
        s = st.spec
        if s.via not in self.data.hosts:
            return f"нет сервера «{s.via}» в ~/qc/ssh", False
        if s.local < config.unprivileged_port_start():
            return f"порт {s.local} без root не открыть — возьми порт выше 1024", False
        pw = chain_has_password(self.data, s.via)
        if pw:
            return f"сервер «{pw}» входит по паролю — хранилище паролей будет на следующем шаге", False
        for other in self.tunnels.values():
            if other is not st and other.spec.local == s.local and other.state in ("starting", "running"):
                return f"порт {s.local} занят туннелем «{other.spec.name}»", False
        if not ports.port_free(s.bind, s.local):
            return f"порт {s.local} занят: {ports.owner(s.local)}", True
        return None

    def ssh_command(self, st: TunnelState) -> list[str]:
        s = st.spec
        return [
            paths.ssh_bin(), *sshgen.config_args(), "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-L", f"{s.bind}:{s.local}:{s.remote}",
            s.via,
        ]

    async def supervise(self, st: TunnelState) -> None:
        delay = 1
        try:
            while st.want:
                st.wake.clear()
                st.retry_at = None
                problem = self.preflight(st)
                if problem is None:
                    started = time.monotonic()
                    problem = await self.run_once(st)
                    if not st.want:
                        break
                    if time.monotonic() - started > 60:
                        delay = 1  # долго проработал — обрыв, а не постоянная ошибка
                text, retry = problem
                st.error = text
                st.note(f"qc: {text}" + (f" — повтор через {delay} с" if retry else " — остановлен"))
                log(f"{st.spec.name}: {text}")
                if not retry:
                    st.state, st.want = "error", False
                    break
                st.state, st.retry_at = "retry", time.time() + delay
                try:
                    await asyncio.wait_for(st.wake.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 60)
        finally:
            st.proc, st.retry_at = None, None
            if st.state != "error":
                st.state, st.error = "stopped", ""

    async def run_once(self, st: TunnelState) -> tuple[str, bool] | None:
        cmd = self.ssh_command(st)
        st.state, st.since = "starting", time.time()
        st.note("qc: запуск " + " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # без управляющего терминала ssh не перехватит твой терминал вопросом
                preexec_fn=_pdeathsig,
            )
        except OSError as e:
            return f"ssh не запускается: {e}", False
        st.proc = proc

        async def read_stderr() -> None:
            assert proc.stderr
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    st.note(line)

        reader = asyncio.create_task(read_stderr())
        while True:
            try:
                rc = await asyncio.wait_for(proc.wait(), 0.5)
                break
            except TimeoutError:
                if st.state == "starting" and ports.listening(st.spec.local):
                    st.state, st.error, st.since = "running", "", time.time()
                    st.note("qc: туннель работает")
                    log(f"{st.spec.name}: работает (порт {st.spec.local})")
        await asyncio.gather(reader, return_exceptions=True)
        st.note(f"qc: ssh завершился с кодом {rc}")
        if not st.want:
            return None
        return classify(st.log, rc, st.spec.via)

    # ---------- команды через сокет ----------

    def resolve(self, names: list[str]) -> tuple[list[TunnelState], list[str]]:
        found, unknown = [], []
        for n in names:
            if n == "all":
                found += list(self.tunnels.values())
            elif n in self.tunnels:
                found.append(self.tunnels[n])
            else:
                group = [st for st in self.tunnels.values()
                         if st.spec.group == n or st.spec.file in (f"tunnels/{n}.toml", n)]
                if group:
                    found += group
                else:
                    unknown.append(n)
        uniq = list({id(st): st for st in found}.values())
        return uniq, unknown

    async def dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        if cmd == "reload":
            self.reload()
            return {"ok": True}
        if cmd == "status":
            if self.scan_mtime() != self.mtime:
                self.reload()
            return {
                "ok": True,
                "tunnels": [st.to_dict() for st in self.tunnels.values()],
                "issues": [{"level": i.level, "text": i.text} for i in self.issues],
            }
        if cmd in ("up", "down", "restart"):
            if self.scan_mtime() != self.mtime:
                self.reload()
            sts, unknown = self.resolve(req.get("names") or [])
            if unknown:
                return {"ok": False, "error": "нет такого туннеля или группы: " + ", ".join(unknown)}
            for st in sts:
                if cmd == "up":
                    self.start(st)
                elif cmd == "down":
                    self.stop(st)
                else:
                    asyncio.create_task(self.restart(st))
            return {"ok": True, "names": [st.spec.name for st in sts]}
        if cmd == "logs":
            st = self.tunnels.get(req.get("name", ""))
            if not st:
                return {"ok": False, "error": "нет такого туннеля"}
            n = int(req.get("n", 30))
            return {"ok": True, "lines": [[t, line] for t, line in list(st.log)[-n:]]}
        return {"ok": False, "error": f"неизвестная команда {cmd!r}"}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            resp = await self.dispatch(json.loads(line))
        except Exception as e:  # noqa: BLE001 — ответить клиенту, а не уронить демон
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    # ---------- запуск ----------

    async def main(self) -> int:
        sock = paths.socket_path()
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            try:
                r, w = await asyncio.open_unix_connection(str(sock))
                w.close()
                log(f"демон уже запущен ({sock})")
                return 1
            except OSError:
                sock.unlink()  # остался от упавшего демона
        server = await asyncio.start_unix_server(self.handle, path=str(sock))
        os.chmod(sock, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        log(f"qcd запущен, данные {paths.data_dir()}, сокет {sock}")
        self.reload()
        watcher = asyncio.create_task(self.watch_config())
        await self.stopping.wait()
        log("остановка: гашу туннели")
        watcher.cancel()
        server.close()
        for st in self.tunnels.values():
            self.stop(st)
        tasks = [st.task for st in self.tunnels.values() if st.task]
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        try:
            sock.unlink()
        except OSError:
            pass
        return 0


def run() -> int:
    return asyncio.run(Daemon().main())
