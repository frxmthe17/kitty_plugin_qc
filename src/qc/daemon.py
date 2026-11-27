"""qcd — the tunnels daemon. Keeps `ssh -N -L` running as background processes without terminal tabs,
restarts them when they drop, watches ports. Takes commands over a unix socket (one JSON line per connection).
Also keeps the password vault unlocked for a while and answers qc-askpass, so ssh gets passwords without typing."""
from __future__ import annotations

import asyncio
import collections
import ctypes
import json
import os
import secrets
import signal
import sys
import time

from qc import askpass, config, paths, ports, settings, sshgen, vault

# (what to look for in ssh output, what to show, whether to retry)
# Login errors are not retried: it is pointless, and fail2ban on the server may ban our IP.
ERRORS = [
    ("Permission denied", "login denied (key/password rejected)", False),
    ("Too many authentication failures", "login denied (too many keys tried)", False),
    ("Host key verification failed", "server host key is unknown or changed — log in once: qc ssh {via}", False),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "server host key has changed — check and fix known_hosts", False),
    ("Could not resolve hostname", "server address not found (DNS)", True),
    ("Connection timed out", "server not responding — no VPN/network?", True),
    ("Operation timed out", "server not responding — no VPN/network?", True),
    ("No route to host", "no route to server — no VPN/network?", True),
    ("Network is unreachable", "no network", True),
    ("Connection refused", "server refused the connection (ssh port closed)", True),
    ("Address already in use", "local port is in use", True),
    ("cannot listen to port", "local port is in use", True),
    ("forwarding failed", "server refused the port forwarding", False),
    ("Connection closed by", "server closed the connection", True),
    ("Broken pipe", "connection lost", True),
    ("Timeout, server", "connection lost (server stopped responding)", True),
]


def _pdeathsig() -> None:
    """ssh dies together with the daemon, even if the daemon is killed with kill -9."""
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
    return f"ssh exited with code {rc}", True


def host_chain_key(data: config.Data, name: str) -> tuple:
    """If the server or any of its jump hosts changed, the tunnel must be restarted."""
    out, seen = [], set()
    while name and name in data.hosts and name not in seen:
        seen.add(name)
        out.append(repr(data.hosts[name]))
        name = data.hosts[name].jump
    return tuple(out)


def timeout_hours() -> float:
    try:
        return float(settings.get("vault.timeout_hours"))
    except (TypeError, ValueError):
        return 8.0


def locked_ok(d: "Daemon", st: "TunnelState") -> bool:
    """Passwords that are in the vault need it unlocked (unless the user already typed them this time);
    passwords that are not saved anywhere are asked in the menu when ssh needs them."""
    saved = set(vault.names()) if vault.initialized() else set()
    locked = [h for h in vault.host_chain(d.data, st.spec.via)
              if d.data.hosts[h].password in saved and h not in st.manual]
    return not (locked and d.identity is None)


def route(spec: config.Tunnel) -> str:
    return f"{spec.via} kept open" if spec.keep_open else ports.endpoint(spec.bind, spec.local)


def is_up(spec: config.Tunnel) -> bool:
    """Port tunnel: the local port listens. Kept-open connection: the ssh master socket is there."""
    if spec.keep_open:
        return paths.control_path(spec.via).is_socket()
    return ports.listening(spec.local, spec.bind)


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
        # typed passwords, browser-style: kept in memory while the tunnel is on (for reconnects), forgotten on stop
        self.manual: dict[str, str] = {}      # server (or user@host) → password the user typed
        self.logins: dict[str, str] = {}      # server → "user@host" for the questions in the menu
        self.used: set[str] = set()           # typed passwords used in the current connection
        self.unsaved: set[str] = set()        # typed passwords that worked and wait for "save?"
        self.ask: dict | None = None          # the menu is asked to type a password: {host, login, failed, …}
        self.no_answer = False                # the password was not entered (cancelled / no menu open)
        self.answer: asyncio.Future | None = None

    def note(self, line: str) -> None:
        self.log.append((time.time(), line))

    def to_dict(self) -> dict:
        s = self.spec
        return {
            "name": s.name, "group": s.group, "via": s.via, "local": s.local, "bind": s.bind,
            "remote": s.remote, "autostart": s.autostart, "description": s.description,
            "state": self.state, "error": self.error, "since": self.since, "retry_at": self.retry_at,
            "pid": self.proc.pid if self.proc and self.proc.returncode is None else None,
            "ask": self.ask, "unsaved": bool(self.unsaved),
        }

    def forget(self) -> None:
        """Typed passwords are not kept after the tunnel is stopped."""
        self.manual.clear()
        self.used.clear()
        self.unsaved.clear()
        if self.answer and not self.answer.done():
            self.answer.set_result(None)


class Daemon:
    def __init__(self) -> None:
        self.data = config.Data()
        self.issues: list[config.Issue] = []
        self.tunnels: dict[str, TunnelState] = {}
        self.mtime: float | None = None
        self.stopping = asyncio.Event()
        self.identity = None  # unlocked vault key (pyrage Identity) or None
        self.lock_at: float | None = None
        self.asked: dict[str, tuple[float, set[str]]] = {}  # askpass token → (time, servers already answered)

    # ---------- config ----------

    def scan_mtime(self) -> float:
        base, m = paths.data_dir(), 0.0
        for sub in ("ssh", "tunnels"):
            d = base / sub
            try:
                m = max(m, d.stat().st_mtime)  # deleting a file changes the folder mtime
                for f in d.rglob("*"):
                    if f.is_dir() or f.suffix == ".toml":
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
            log(f"cannot write ssh_config: {e}")
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
                log(f"{name}: settings changed — restarting")
                asyncio.create_task(self.restart(st))
        for st in old.values():
            log(f"{st.spec.name}: removed from settings — stopping")
            self.stop(st)
        self.tunnels, self.data = new, data

    async def watch_config(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(3)
            if self.lock_at and time.time() >= self.lock_at:
                self.lock()
                log("vault locked (timeout)")
            if self.scan_mtime() != self.mtime:
                log("settings in ~/qc changed — reloading")
                self.reload()

    # ---------- tunnels ----------

    def start(self, st: TunnelState) -> None:
        if st.task and not st.task.done():
            if st.state == "retry":
                st.wake.set()  # 'up' during the retry pause — try right away
            return
        st.want, st.error = True, ""
        st.task = asyncio.create_task(self.supervise(st))

    def stop(self, st: TunnelState) -> None:
        st.want = False
        st.wake.set()
        st.forget()
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
            return f"no server '{s.via}' in ~/qc/ssh", False
        if s.keep_open:
            if not locked_ok(self, st):
                return "the password vault is locked — unlock it in the Passwords tab or with qc unlock", True
            for other in self.tunnels.values():
                if other is not st and other.spec.keep_open and other.spec.via == s.via \
                        and other.state in ("starting", "running"):
                    return f"'{s.via}' is already kept open by '{other.spec.name}'", False
            return None
        if s.local < config.unprivileged_port_start():
            return f"port {s.local} cannot be opened without root — use a port above 1024", False
        if not locked_ok(self, st):
            return "the password vault is locked — unlock it in the Passwords tab or with qc unlock", True
        for other in self.tunnels.values():
            if (other is not st and other.spec.local == s.local and ports.overlap(other.spec.bind, s.bind)
                    and other.state in ("starting", "running")):
                return (f"{ports.endpoint(s.bind, s.local)} is used by tunnel '{other.spec.name}' "
                        f"({ports.endpoint(other.spec.bind, other.spec.local)})"), False
        if not ports.port_free(s.bind, s.local):
            return f"{ports.endpoint(s.bind, s.local)} is in use: {ports.owner(s.local, s.bind)}", True
        return None

    def ssh_command(self, st: TunnelState) -> list[str]:
        s = st.spec
        if s.keep_open:
            return [
                paths.ssh_bin(), *sshgen.config_args(), "-N",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=15",
                "-o", "ControlMaster=yes",
                "-o", "ControlPersist=no",
                "-o", f"ControlPath={paths.control_path(s.via)}",
                s.via,
            ]
        return [
            paths.ssh_bin(), *sshgen.config_args(), "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15",
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
                        delay = 1  # ran for a while — a dropped connection, not a permanent error
                text, retry = problem
                st.error = text
                st.note(f"qc: {text}" + (f" — retry in {delay} s" if retry else " — stopped"))
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
        if st.spec.keep_open:
            cp = paths.control_path(st.spec.via)
            cp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if cp.is_socket():
                cp.unlink()  # left from a killed ssh: with it in place ssh would refuse to become the master
        st.state, st.since = "starting", time.time()
        st.note("qc: starting " + " ".join(cmd))
        # every prompt of ssh (and of its jump hosts) goes to qc-askpass → the daemon → the vault or the menu
        st.used.clear()
        st.no_answer = False
        env = askpass.env(vault.host_chain(self.data, st.spec.via), secrets.token_hex(8), st.spec.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, env=env,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # without a controlling terminal ssh cannot hijack your terminal with a prompt
                preexec_fn=_pdeathsig,
            )
        except OSError as e:
            return f"cannot start ssh: {e}", False
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
                if st.state == "starting" and is_up(st.spec):
                    st.state, st.error, st.since = "running", "", time.time()
                    st.note("qc: tunnel is up")
                    st.unsaved |= {k for k in st.used if k in st.manual}  # typed and worked — offer to save
                    log(f"{st.spec.name}: up ({route(st.spec)})")
        await asyncio.gather(reader, return_exceptions=True)
        st.note(f"qc: ssh exited with code {rc}")
        if not st.want:
            return None
        if st.no_answer:
            return "a password is needed — start the tunnel from the menu (or qc t up) and type it", False
        return classify(st.log, rc, st.spec.via)

    # ---------- password vault ----------

    def vault_state(self) -> dict:
        return {"initialized": vault.initialized(), "unlocked": self.identity is not None, "lock_at": self.lock_at}

    def lock(self) -> None:
        self.identity, self.lock_at = None, None

    async def unlock(self, password: str) -> dict:
        try:
            ident = await asyncio.to_thread(vault.unlock, password)
        except vault.VaultError as e:
            return {"ok": False, "error": str(e)}
        hours = timeout_hours()
        self.identity, self.lock_at = ident, (time.time() + hours * 3600 if hours > 0 else None)
        log("vault unlocked")
        for st in self.tunnels.values():  # tunnels waiting for the vault — try right away
            if st.state == "retry":
                st.wake.set()
        return {"ok": True, **self.vault_state()}

    def secret(self, name: str) -> dict:
        if self.identity is None:
            return {"ok": False, "error": "the password vault is locked", "locked": True}
        try:
            return {"ok": True, "secret": vault.get(name, self.identity)}
        except vault.VaultError as e:
            return {"ok": False, "error": str(e)}

    async def answer_askpass(self, prompt: str, chain: list[str], token: str, tunnel: str = "") -> dict:
        if self.scan_mtime() != self.mtime:
            self.reload()
        low = prompt.lower()
        if "continue connecting" in low or "passphrase" in low:
            return {"ok": False, "error": "host key questions and key passphrases are not answered"}
        st = self.tunnels.get(tunnel) if tunnel else None
        parsed = vault.parse_prompt(prompt)
        host = vault.match_host(self.data, prompt, chain, any_host=st is not None) if parsed else None
        login = f"{parsed[0]}@{parsed[1]}" if parsed and parsed[0] and parsed[1] else prompt.strip()
        key = host or login
        now = time.time()
        self.asked = {t: v for t, v in self.asked.items() if now - v[0] < 3600}
        times = 0
        if token:
            _, done = self.asked.setdefault(token, (now, {}))
            times = done.get(key, 0)
            done[key] = times + 1
        entry = self.data.hosts[host].password if host else None

        if st is None:  # qc ssh: only the vault, and a saved password is never sent twice (fail2ban)
            if times:
                return {"ok": False, "error": f"the saved password for '{key}' was rejected — not trying again"}
            if not entry:
                return {"ok": False, "error": f"no server with a saved password for the prompt {prompt.strip()!r}"}
            r = self.secret(entry)
            if r.get("locked"):
                r["error"] = "the password vault is locked — unlock it in the Passwords tab or with qc unlock"
            return r

        if parsed is None:  # a one-time code and the like: ask the user, never keep it
            secret = await self.ask_user(st, key, prompt.strip(), failed=times > 0, entry=None)
            return {"ok": True, "secret": secret} if secret is not None else \
                {"ok": False, "error": "nothing was entered"}

        st.logins[key] = login
        if times == 0:
            if key in st.manual:  # typed earlier for this tunnel (reconnect)
                st.used.add(key)
                return {"ok": True, "secret": st.manual[key]}
            if entry and vault.initialized() and entry in vault.names() and self.identity is not None:
                r = self.secret(entry)
                if r.get("ok"):
                    return r
        else:
            st.manual.pop(key, None)  # the previous answer (saved or typed) was wrong — the saved one is not resent
        if times >= 99:
            return {"ok": False, "error": "nothing was entered"}
        if times >= 3:
            return {"ok": False, "error": f"3 wrong passwords for {login} — stopped (fail2ban)"}
        secret = await self.ask_user(st, key, login, failed=times > 0, entry=entry or host)
        if secret is None:
            if token:
                self.asked[token][1][key] = 99  # cancelled — do not ask again in this connection
            st.no_answer = True
            return {"ok": False, "error": "nothing was entered"}
        st.manual[key] = secret
        st.used.add(key)
        return {"ok": True, "secret": secret}

    async def ask_user(self, st: TunnelState, key: str, login: str, failed: bool, entry: str | None) -> str | None:
        """Wait (up to ~100 s, the server gives about 120) until the menu or `qc t up` types the password."""
        st.answer = asyncio.get_running_loop().create_future()
        st.ask = {"host": key, "login": login, "failed": failed, "entry": entry,
                  "server": key if key in self.data.hosts else None, "since": time.time()}
        prev, st.state = st.state, "password"
        st.note(f"qc: waiting for the password for {login}" + (" (the previous one was wrong)" if failed else ""))
        log(f"{st.spec.name}: waiting for the password for {login}")
        try:
            return await asyncio.wait_for(st.answer, 100)
        except TimeoutError:
            st.note("qc: no password was entered in time")
            return None
        finally:
            st.ask, st.answer = None, None
            if st.state == "password":
                st.state = prev if prev != "password" else "starting"

    def take_unsaved(self, st: TunnelState) -> list[dict]:
        """Typed passwords that worked: for the "save it?" question. Asked once."""
        saved = set(vault.names()) if vault.initialized() else set()
        out = []
        for key in sorted(st.unsaved):
            if key not in st.manual:
                continue
            server = key if key in self.data.hosts else None
            entry = (self.data.hosts[server].password or server) if server else None
            out.append({"host": key, "server": server, "login": st.logins.get(key, key), "entry": entry,
                        "update": bool(entry and entry in saved), "secret": st.manual[key]})
        st.unsaved.clear()
        return out

    # ---------- socket commands ----------

    def resolve(self, names: list[str]) -> tuple[list[TunnelState], list[str]]:
        found, unknown = [], []
        for n in names:
            if n == "all":
                found += list(self.tunnels.values())
            elif n in self.tunnels:
                found.append(self.tunnels[n])
            else:
                d = config.norm_dir(n)  # the whole folder, including subfolders
                group = [st for st in self.tunnels.values()
                         if st.spec.group == d or st.spec.group.startswith(d + "/")
                         or st.spec.file in (f"tunnels/{n}.toml", n)]
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
                "vault": self.vault_state(),
            }
        if cmd == "vault":
            return {"ok": True, **self.vault_state()}
        if cmd == "unlock":
            return await self.unlock(str(req.get("password", "")))
        if cmd == "lock":
            self.lock()
            log("vault locked")
            return {"ok": True, **self.vault_state()}
        if cmd == "secret":
            return self.secret(str(req.get("name", "")))
        if cmd == "askpass":
            return await self.answer_askpass(str(req.get("prompt", "")), list(req.get("chain") or []),
                                             str(req.get("token", "")), str(req.get("tunnel", "")))
        if cmd in ("password", "take_unsaved"):
            st = self.tunnels.get(str(req.get("tunnel", "")))
            if not st:
                return {"ok": False, "error": "no such tunnel"}
            if cmd == "take_unsaved":
                return {"ok": True, "items": self.take_unsaved(st)}
            if not (st.answer and not st.answer.done()):
                return {"ok": False, "error": "the tunnel is not waiting for a password"}
            st.answer.set_result(None if req.get("cancel") else str(req.get("secret", "")))
            return {"ok": True}
        if cmd in ("up", "down", "restart"):
            if self.scan_mtime() != self.mtime:
                self.reload()
            sts, unknown = self.resolve(req.get("names") or [])
            if unknown:
                return {"ok": False, "error": "no such tunnel or folder: " + ", ".join(unknown)}
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
                return {"ok": False, "error": "no such tunnel"}
            n = int(req.get("n", 30))
            return {"ok": True, "lines": [[t, line] for t, line in list(st.log)[-n:]]}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            resp = await self.dispatch(json.loads(line))
        except Exception as e:  # noqa: BLE001 — reply to the client instead of crashing the daemon
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    # ---------- startup ----------

    async def main(self) -> int:
        sock = paths.socket_path()
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            try:
                r, w = await asyncio.open_unix_connection(str(sock))
                w.close()
                log(f"daemon is already running ({sock})")
                return 1
            except OSError:
                sock.unlink()  # left over from a crashed daemon
        server = await asyncio.start_unix_server(self.handle, path=str(sock))
        os.chmod(sock, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        log(f"qcd started, data {paths.data_dir()}, socket {sock}")
        self.reload()
        watcher = asyncio.create_task(self.watch_config())
        await self.stopping.wait()
        log("shutting down: stopping tunnels")
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
