# services/service_daemon.py
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------- paths + env ----------
def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()

load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

DAEMON_HOST = os.getenv("DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.getenv("DAEMON_PORT", "8790"))

IDLE_TIMEOUT_S = float(os.getenv("DAEMON_IDLE_TIMEOUT_S", "25"))
LEASE_TTL_S = float(os.getenv("DAEMON_LEASE_TTL_S", "15"))
DEFAULT_HEARTBEAT_S = float(os.getenv("DAEMON_HEARTBEAT_S", "5"))

STT_HOST = os.getenv("STT_HOST", "127.0.0.1")
STT_PORT = int(os.getenv("STT_PORT", "8001"))
STT_HEALTH_URL = os.getenv("STT_HEALTH_URL", f"http://{STT_HOST}:{STT_PORT}/health")
STT_READY_TIMEOUT_S = float(os.getenv("STT_READY_TIMEOUT_S", "90"))

KOKORO_HOST = os.getenv("KOKORO_HOST", "127.0.0.1")
KOKORO_PORT = int(os.getenv("KOKORO_PORT", "8880"))
KOKORO_BASE_URL = os.getenv("KOKORO_BASE_URL", f"http://{KOKORO_HOST}:{KOKORO_PORT}")
KOKORO_VOICES_URL = f"{KOKORO_BASE_URL}/v1/audio/voices"  # Kokoro-FastAPI doc :contentReference[oaicite:3]{index=3}
KOKORO_READY_TIMEOUT_S = float(os.getenv("KOKORO_READY_TIMEOUT_S", "120"))
KOKORO_COMPOSE_FILE = os.getenv("KOKORO_COMPOSE_FILE", "services/kokoro.compose.yml")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api")
OLLAMA_VERSION_URL = f"{OLLAMA_API_BASE}/version"  # documented 
OLLAMA_GENERATE_URL = f"{OLLAMA_API_BASE}/generate"  # documented :contentReference[oaicite:5]{index=5}
OLLAMA_CMD = os.getenv("OLLAMA_CMD", "ollama")
OLLAMA_MANAGE_SERVER = os.getenv("OLLAMA_MANAGE_SERVER", "1") != "0"
OLLAMA_MANAGE_MODEL_UNLOAD = os.getenv("OLLAMA_MANAGE_MODEL_UNLOAD", "1") != "0"
OLLAMA_READY_TIMEOUT_S = float(os.getenv("OLLAMA_READY_TIMEOUT_S", "30"))
OLLAMA_WARM_TIMEOUT_S = float(os.getenv("OLLAMA_WARM_TIMEOUT_S", "180"))  # first load can be slow
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_WARM_KEEP_ALIVE", "5m")  # keep model resident while active :contentReference[oaicite:6]{index=6}

LOG_DIR = Path(os.getenv("DAEMON_LOG_DIR", str(ROOT / ".daemon")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
DAEMON_LOG = LOG_DIR / "daemon.log"
STT_LOG = LOG_DIR / "stt.log"
OLLAMA_LOG = LOG_DIR / "ollama.log"
DOCKER_LOG = LOG_DIR / "docker.log"


# ---------- small utils ----------
def _now() -> float:
    return time.monotonic()


def _is_windows() -> bool:
    return os.name == "nt"


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(DAEMON_LOG, "a", encoding="utf-8", errors="ignore") as f:
        f.write(f"[{ts}] {msg}\n")


def _tail(path: Path, max_bytes: int = 12_000) -> str:
    try:
        if not path.exists():
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = f.read()
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _popen_hidden(cmd: list[str], *, cwd: Path, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    stdout_f = open(stdout_path, "ab", buffering=0)
    stderr_f = open(stderr_path, "ab", buffering=0)

    kwargs: dict[str, Any] = dict(
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        close_fds=not _is_windows(),
        env=os.environ.copy(),
    )

    if _is_windows():
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = creationflags

        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **kwargs)


def _http_json(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 0.8) -> tuple[int, dict[str, Any] | None]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return r.status, None
            try:
                return r.status, json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            if raw:
                try:
                    return e.code, json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    return e.code, None
            return e.code, None
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _tcp_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _wait_ready(check_fn, timeout_s: float, step_s: float = 0.15) -> bool:
    deadline = _now() + timeout_s
    while _now() < deadline:
        if check_fn():
            return True
        time.sleep(step_s)
        step_s = min(step_s * 1.25, 0.8)
    return False


def _run_cmd(cmd: list[str], *, cwd: Path, log_path: Path, timeout_s: float = 60.0) -> int:
    with open(log_path, "ab", buffering=0) as lf:
        kwargs: dict[str, Any] = dict(
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            env=os.environ.copy(),
        )
        if _is_windows():
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kwargs["startupinfo"] = si

        try:
            r = subprocess.run(cmd, **kwargs, timeout=timeout_s, check=False)
            return int(r.returncode)
        except Exception as e:
            _log(f"cmd failed: {cmd} err={e}")
            return 999


def _docker_compose_cmd() -> list[str] | None:
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


# ---------- state ----------
@dataclass
class Lease:
    lease_id: str
    last_seen: float
    meta: dict[str, Any]


class DaemonState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ensuring = False
        self.stage = "idle"
        self.last_error: str | None = None

        self.leases: dict[str, Lease] = {}
        self.last_zero_lease_time = _now()

        self.stt_proc: subprocess.Popen | None = None
        self.ollama_proc: subprocess.Popen | None = None
        self.ollama_managed = False

        self.warm_model: str | None = None
        self.warm_done: bool = False

        self.shutdown_flag = False

    def set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage

    def set_error(self, err: str | None) -> None:
        with self.lock:
            self.last_error = err


STATE = DaemonState()


# ---------- service checks ----------
def _stt_running() -> bool:
    code, body = _http_json("GET", STT_HEALTH_URL, timeout=0.6)
    return code == 200 and isinstance(body, dict) and body.get("ok") is True


def _kokoro_running() -> bool:
    if not _tcp_port_open(KOKORO_HOST, KOKORO_PORT, timeout=0.25):
        return False
    code, body = _http_json("GET", KOKORO_VOICES_URL, timeout=0.8)
    return code == 200 and isinstance(body, dict) and "voices" in body  # Kokoro-FastAPI doc :contentReference[oaicite:7]{index=7}


def _ollama_running() -> bool:
    code, body = _http_json("GET", OLLAMA_VERSION_URL, timeout=0.6)
    return code == 200 and isinstance(body, dict) and "version" in body  # documented 


# ---------- ensure/start/stop ----------
def _ensure_stt() -> None:
    if _stt_running():
        return

    if STATE.stt_proc and STATE.stt_proc.poll() is not None:
        STATE.stt_proc = None

    if STATE.stt_proc is None:
        py = os.sys.executable
        cmd = [py, str(ROOT / "services" / "local_stt_server.py")]
        STATE.stt_proc = _popen_hidden(cmd, cwd=ROOT, stdout_path=STT_LOG, stderr_path=STT_LOG)
        _log(f"started STT pid={STATE.stt_proc.pid}")

    deadline = _now() + STT_READY_TIMEOUT_S
    while _now() < deadline:
        if _stt_running():
            return
        p = STATE.stt_proc
        if p and p.poll() is not None:
            tail = _tail(STT_LOG)
            raise RuntimeError(f"STT exited early rc={p.returncode}\n{tail}")
        time.sleep(0.2)

    raise RuntimeError(f"STT not ready after {STT_READY_TIMEOUT_S}s\n{_tail(STT_LOG)}")


def _ensure_kokoro() -> None:
    if _kokoro_running():
        return

    dc = _docker_compose_cmd()
    if not dc:
        raise RuntimeError("docker not found (required for Kokoro compose)")

    compose = str((ROOT / KOKORO_COMPOSE_FILE).resolve())
    rc = _run_cmd([*dc, "-f", compose, "up", "-d"], cwd=ROOT, log_path=DOCKER_LOG, timeout_s=90.0)
    if rc != 0:
        raise RuntimeError(f"docker compose up failed rc={rc}\n{_tail(DOCKER_LOG)}")

    ok = _wait_ready(_kokoro_running, timeout_s=KOKORO_READY_TIMEOUT_S)
    if not ok:
        raise RuntimeError(f"Kokoro not ready after {KOKORO_READY_TIMEOUT_S}s\n{_tail(DOCKER_LOG)}")


def _ensure_ollama_server() -> None:
    if _ollama_running():
        return
    if not OLLAMA_MANAGE_SERVER:
        raise RuntimeError("Ollama is not running and OLLAMA_MANAGE_SERVER=0")

    if STATE.ollama_proc and STATE.ollama_proc.poll() is not None:
        STATE.ollama_proc = None
        STATE.ollama_managed = False

    if STATE.ollama_proc is None:
        exe = shutil.which(OLLAMA_CMD) or OLLAMA_CMD
        cmd = [exe, "serve"]
        STATE.ollama_proc = _popen_hidden(cmd, cwd=ROOT, stdout_path=OLLAMA_LOG, stderr_path=OLLAMA_LOG)
        STATE.ollama_managed = True
        _log(f"started Ollama pid={STATE.ollama_proc.pid}")

    ok = _wait_ready(_ollama_running, timeout_s=OLLAMA_READY_TIMEOUT_S)
    if not ok:
        raise RuntimeError(f"Ollama not ready after {OLLAMA_READY_TIMEOUT_S}s\n{_tail(OLLAMA_LOG)}")


def _ollama_warm(model: str) -> None:
    # keep_alive supports duration strings/seconds/negative/0. :contentReference[oaicite:9]{index=9}
    payload = {
        "model": model,
        "prompt": "",
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    code, body = _http_json("POST", OLLAMA_GENERATE_URL, payload, timeout=OLLAMA_WARM_TIMEOUT_S)
    if code != 200:
        raise RuntimeError(f"ollama warm failed code={code} body={body}")


def _ollama_unload(model: str) -> None:
    if not OLLAMA_MANAGE_MODEL_UNLOAD:
        return
    # unload rule is documented: empty prompt + keep_alive=0. :contentReference[oaicite:10]{index=10}
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    _http_json("POST", OLLAMA_GENERATE_URL, payload, timeout=15.0)


def _stop_stt() -> None:
    p = STATE.stt_proc
    STATE.stt_proc = None
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=4)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    _log("stopped STT")


def _stop_kokoro() -> None:
    dc = _docker_compose_cmd()
    if not dc:
        return
    compose = str((ROOT / KOKORO_COMPOSE_FILE).resolve())
    _run_cmd([*dc, "-f", compose, "stop"], cwd=ROOT, log_path=DOCKER_LOG, timeout_s=45.0)
    _log("stopped Kokoro")


def _stop_ollama() -> None:
    if not STATE.ollama_managed:
        return
    p = STATE.ollama_proc
    STATE.ollama_proc = None
    STATE.ollama_managed = False
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=4)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    _log("stopped Ollama (managed)")


def _ensure_all(model: str | None) -> None:
    STATE.set_stage("ensure:ollama_server")
    _ensure_ollama_server()

    STATE.set_stage("ensure:stt")
    _ensure_stt()

    STATE.set_stage("ensure:kokoro")
    _ensure_kokoro()

    with STATE.lock:
        STATE.warm_model = model
        STATE.warm_done = False

    if model:
        STATE.set_stage("ensure:ollama_warm")
        _ollama_warm(model)
        with STATE.lock:
            STATE.warm_done = True

    STATE.set_stage("ready")


# ---------- reaper ----------
def _reaper_thread() -> None:
    while not STATE.shutdown_flag:
        time.sleep(0.5)
        now = _now()

        with STATE.lock:
            stale = [k for k, v in STATE.leases.items() if now - v.last_seen > LEASE_TTL_S]
            for k in stale:
                STATE.leases.pop(k, None)

            lease_count = len(STATE.leases)
            if lease_count == 0:
                do_shutdown = (now - STATE.last_zero_lease_time) >= IDLE_TIMEOUT_S
            else:
                STATE.last_zero_lease_time = now
                do_shutdown = False

            warm_model = STATE.warm_model

        if do_shutdown:
            try:
                if warm_model:
                    _ollama_unload(warm_model)
                _stop_stt()
                _stop_kokoro()
                _stop_ollama()
            except Exception as e:
                _log(f"idle shutdown error: {e}")
            with STATE.lock:
                STATE.last_zero_lease_time = _now()
                STATE.stage = "idle"
                STATE.warm_done = False


# ---------- HTTP server ----------
def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or "0")
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return {}


def _write_json(handler: BaseHTTPRequestHandler, code: int, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/status":
            _write_json(self, 404, {"ok": False, "error": "not_found"})
            return

        with STATE.lock:
            leases = len(STATE.leases)
            ensuring = STATE.ensuring
            stage = STATE.stage
            err = STATE.last_error
            warm_model = STATE.warm_model
            warm_done = STATE.warm_done
            managed = STATE.ollama_managed

        _write_json(
            self,
            200,
            {
                "ok": True,
                "leases": leases,
                "ensuring": ensuring,
                "stage": stage,
                "last_error": err,
                "idle_timeout_s": IDLE_TIMEOUT_S,
                "lease_ttl_s": LEASE_TTL_S,
                "services": {
                    "stt": {"running": _stt_running(), "health": STT_HEALTH_URL},
                    "tts": {"running": _kokoro_running(), "voices_url": KOKORO_VOICES_URL},
                    "ollama": {"running": _ollama_running(), "base_url": OLLAMA_API_BASE, "managed": managed},
                },
                "warm": {"model": warm_model, "done": warm_done, "keep_alive": OLLAMA_KEEP_ALIVE},
                "logs": str(LOG_DIR),
            },
        )

    def do_POST(self) -> None:
        if self.path == "/lease/acquire":
            meta = _read_json(self)
            lease_id = f"l{int(time.time()*1000)}_{os.getpid()}_{threading.get_ident()}"
            with STATE.lock:
                STATE.leases[lease_id] = Lease(lease_id=lease_id, last_seen=_now(), meta=meta or {})
                STATE.last_zero_lease_time = _now()
            _write_json(self, 200, {"ok": True, "lease_id": lease_id, "heartbeat_s": DEFAULT_HEARTBEAT_S, "lease_ttl_s": LEASE_TTL_S})
            return

        if self.path == "/lease/heartbeat":
            body = _read_json(self)
            lease_id = str(body.get("lease_id") or "")
            if not lease_id:
                _write_json(self, 400, {"ok": False, "error": "missing_lease_id"})
                return
            with STATE.lock:
                l = STATE.leases.get(lease_id)
                if not l:
                    _write_json(self, 404, {"ok": False, "error": "unknown_lease"})
                    return
                l.last_seen = _now()
            _write_json(self, 200, {"ok": True})
            return

        if self.path == "/lease/release":
            body = _read_json(self)
            lease_id = str(body.get("lease_id") or "")
            if not lease_id:
                _write_json(self, 400, {"ok": False, "error": "missing_lease_id"})
                return
            with STATE.lock:
                STATE.leases.pop(lease_id, None)
                if len(STATE.leases) == 0:
                    STATE.last_zero_lease_time = _now()
            _write_json(self, 200, {"ok": True})
            return

        if self.path == "/ensure":
            body = _read_json(self)
            model = str(body.get("ollama_model") or "").strip() or None

            with STATE.lock:
                if STATE.ensuring:
                    _write_json(self, 202, {"ok": True, "ensuring": True})
                    return
                STATE.ensuring = True
                STATE.last_error = None
                STATE.stage = "starting"

            def worker() -> None:
                try:
                    _ensure_all(model)
                except Exception as e:
                    STATE.set_error(str(e))
                    STATE.set_stage("error")
                    _log(f"ensure error: {e}")
                finally:
                    with STATE.lock:
                        STATE.ensuring = False

            threading.Thread(target=worker, daemon=True).start()
            _write_json(self, 202, {"ok": True, "ensuring": True})
            return

        _write_json(self, 404, {"ok": False, "error": "not_found"})


def _shutdown() -> None:
    STATE.shutdown_flag = True
    try:
        with STATE.lock:
            m = STATE.warm_model
        if m:
            _ollama_unload(m)
    except Exception:
        pass
    for fn in (_stop_stt, _stop_kokoro, _stop_ollama):
        try:
            fn()
        except Exception:
            pass


def main() -> None:
    _log(f"daemon starting on {DAEMON_HOST}:{DAEMON_PORT} root={ROOT}")

    threading.Thread(target=_reaper_thread, daemon=True).start()
    atexit.register(_shutdown)

    def _sig(*_a: Any) -> None:
        _log("daemon signal shutdown")
        _shutdown()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    httpd = ThreadingHTTPServer((DAEMON_HOST, DAEMON_PORT), Handler)
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
