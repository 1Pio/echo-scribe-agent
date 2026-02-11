# services/daemon_client.py
from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


ROOT = _project_root()
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

DAEMON_HOST = os.getenv("DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.getenv("DAEMON_PORT", "8790"))
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"

DEFAULT_HEARTBEAT_S = float(os.getenv("DAEMON_HEARTBEAT_S", "5"))
STATUS_POLL_TIMEOUT_S = float(os.getenv("DAEMON_STATUS_POLL_TIMEOUT_S", "180"))


def _is_windows() -> bool:
    return os.name == "nt"


def _tcp_port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 0.8) -> tuple[int, dict[str, Any] | None]:
    url = f"{DAEMON_URL}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw.decode("utf-8", errors="ignore")) if raw else (r.status, None)  # type: ignore[misc]
    except Exception:
        return 0, None


def _spawn_daemon_hidden() -> None:
    py = os.sys.executable
    cmd = [py, "-m", "services.service_daemon"]

    kwargs: dict[str, Any] = dict(
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        close_fds=not _is_windows(),
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

    subprocess.Popen(cmd, **kwargs)


def ensure_daemon_running() -> None:
    if _tcp_port_open(DAEMON_HOST, DAEMON_PORT):
        return

    _spawn_daemon_hidden()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if _tcp_port_open(DAEMON_HOST, DAEMON_PORT):
            return
        time.sleep(0.08)

    raise RuntimeError("service daemon did not start (port not reachable)")


@dataclass
class StackLease:
    lease_id: str
    heartbeat_s: float
    _stop: threading.Event
    _thread: threading.Thread

    def close(self) -> None:
        self._stop.set()
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass
        _http("POST", "/lease/release", {"lease_id": self.lease_id}, timeout=0.6)


def acquire_lease(meta: dict[str, Any] | None = None) -> StackLease:
    ensure_daemon_running()
    code, body = _http("POST", "/lease/acquire", meta or {}, timeout=1.2)
    if code != 200 or not body or not body.get("lease_id"):
        raise RuntimeError(f"lease acquire failed code={code} body={body}")

    lease_id = str(body["lease_id"])
    hb = float(body.get("heartbeat_s") or DEFAULT_HEARTBEAT_S)

    stop = threading.Event()

    def beat() -> None:
        while not stop.is_set():
            _http("POST", "/lease/heartbeat", {"lease_id": lease_id}, timeout=0.6)
            stop.wait(hb)

    t = threading.Thread(target=beat, daemon=True)
    t.start()

    lease = StackLease(lease_id=lease_id, heartbeat_s=hb, _stop=stop, _thread=t)
    atexit.register(lease.close)
    return lease


def ensure_stack(ollama_model: str | None = None) -> None:
    ensure_daemon_running()
    _http("POST", "/ensure", {"ollama_model": ollama_model or ""}, timeout=1.2)

    deadline = time.monotonic() + STATUS_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        code, body = _http("GET", "/status", None, timeout=1.2)
        if code == 200 and body:
            if body.get("last_error") and body.get("ensuring") is False:
                raise RuntimeError(f"daemon ensure failed at stage={body.get('stage')}\n{body.get('last_error')}\nlogs={body.get('logs')}")
            services = body.get("services") or {}
            ready = (
                (services.get("stt") or {}).get("running") is True
                and (services.get("tts") or {}).get("running") is True
                and (services.get("ollama") or {}).get("running") is True
            )
            warm = body.get("warm") or {}
            warm_done = (warm.get("done") is True) if ollama_model else True

            if ready and warm_done and body.get("ensuring") is False:
                return

        time.sleep(0.15)

    # last pull for diagnostics
    _, body = _http("GET", "/status", None, timeout=1.2)
    raise RuntimeError(f"stack ensure timed out\nstatus={body}")


def acquire_and_ensure(ollama_model: str | None = None, meta: dict[str, Any] | None = None) -> StackLease:
    lease = acquire_lease(meta=meta)
    ensure_stack(ollama_model=ollama_model)
    return lease
