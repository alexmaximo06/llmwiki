"""Run the LLM Wiki API and web app in the background on Windows, logging as a TOML file says.

Meant to run as a Windows service (see windows/README.md). To test, run it with python.exe
and --foreground to see the log in the terminal.
"""

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import tomllib
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "api"
WEB_DIR = REPO_ROOT / "web"
LLMWIKI_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LLMWiki"
DEFAULT_CONFIG = LLMWIKI_HOME / "background.toml"
# Fixed like `llmwiki serve`: web/next.config.ts proxies to 127.0.0.1:8000.
API_PORT = 8000
WEB_PORT = 3000
CREATE_NO_WINDOW = 0x08000000
LEVELS = ("debug", "info", "warning", "error")
MAX_RESTART_DELAY = 300

DEFAULTS = {
    "workspace": "",
    "index_path": "",
    "web": {"enabled": True, "mode": "dev"},
    "log": {"dir": "", "level": "warning", "access": False, "web": False, "max_mb": 5, "backups": 3},
    "startup": {"restart_delay_seconds": 5},
}

log = logging.getLogger("llmwiki.background")


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        user = tomllib.load(f)
    cfg = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DEFAULTS.items()}
    for key, value in user.items():
        if isinstance(cfg.get(key), dict) and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value

    if not str(cfg["workspace"]).strip():
        raise ValueError("`workspace` is not set")
    if cfg["log"]["level"] not in LEVELS:
        raise ValueError(f"log.level must be one of {', '.join(LEVELS)}")
    if cfg["web"]["mode"] not in ("dev", "start"):
        raise ValueError('web.mode must be "dev" or "start"')
    return cfg


def setup_logging(log_dir: Path, max_mb: float, backups: int, foreground: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / "llmwiki.log",
            maxBytes=int(max_mb * 1024 * 1024),
            backupCount=int(backups),
            encoding="utf-8",
        )
    ]
    if foreground:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers, force=True)


def uvicorn_log_config(level: str, access: bool) -> dict:
    """One dictConfig for uvicorn and the app: the API itself configures no logging."""
    level = level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(levelname)s %(name)s: %(message)s"}},
        "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "plain", "stream": "ext://sys.stdout"}},
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            "uvicorn": {"level": level},
            "uvicorn.error": {"level": level},
            "uvicorn.access": {"level": "INFO" if access else "WARNING"},
        },
    }


def kill_children_with_us() -> None:
    """Put this process in a kill-on-close job: when the service is stopped (or this crashes),
    Windows kills both servers too, so nothing is left holding ports 8000/3000."""

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    job_object_extended_limit_information = 9
    if not (
        job
        and kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), job_object_extended_limit_information, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        and kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(kernel32.GetCurrentProcess()))
    ):
        log.warning("[launcher] could not create a job object (error %s); servers may outlive the service",
                    ctypes.get_last_error())
    # The handle is deliberately never closed: closing it is what kills the servers.


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Server:
    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict, capture: bool):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.capture = capture
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if self.capture else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if self.capture else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        self.started_at = time.monotonic()
        log.info("[launcher] started %s (pid %s)", self.name, self.proc.pid)
        if self.capture:
            threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True).start()

    def _pump(self, stream) -> None:
        for line in stream:
            line = line.rstrip()
            if line:
                log.info("[%s] %s", self.name, line)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def build_servers(cfg: dict, workspace: Path, log_dir: Path) -> list[Server]:
    env = os.environ.copy()
    env.update({
        "MODE": "local",
        "WORKSPACE_PATH": str(workspace),
        "DATABASE_URL": "",
        "APP_URL": f"http://localhost:{WEB_PORT}",
        "API_URL": f"http://localhost:{API_PORT}",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
    })
    if str(cfg["index_path"]).strip():
        env["LLMWIKI_INDEX_PATH"] = str(cfg["index_path"])

    log_config = log_dir / "uvicorn-log-config.json"
    log_config.write_text(json.dumps(uvicorn_log_config(cfg["log"]["level"], cfg["log"]["access"]), indent=2),
                          encoding="utf-8")
    python = Path(sys.executable).with_name("python.exe")
    servers = [Server(
        "api",
        [str(python if python.exists() else sys.executable), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(API_PORT), "--log-config", str(log_config)],
        API_DIR, env, capture=True,
    )]

    if cfg["web"]["enabled"]:
        web_env = dict(env, NEXT_PUBLIC_MODE="local", NEXT_PUBLIC_API_URL=f"http://localhost:{API_PORT}")
        next_bin = WEB_DIR / "node_modules" / ".bin" / "next.cmd"
        if not next_bin.exists():
            raise RuntimeError(f"web dependencies missing: run `npm install` in {WEB_DIR}")
        if cfg["web"]["mode"] == "start":
            if not (WEB_DIR / ".next" / "BUILD_ID").exists():
                raise RuntimeError(f'web.mode = "start" needs a build: run `npm run build` in {WEB_DIR}')
            args = ["start"]
        else:
            args = ["dev", "--webpack"]
        servers.append(Server(
            "web",
            [str(next_bin), *args, "--port", str(WEB_PORT), "--hostname", "127.0.0.1"],
            WEB_DIR, web_env, capture=cfg["log"]["web"],
        ))
    return servers


def wait_for_workspace(workspace: Path) -> None:
    if workspace.is_dir():
        return
    log.info("[launcher] waiting for %s (is the vault unlocked?)", workspace)
    while not workspace.is_dir():
        time.sleep(5)
    log.info("[launcher] %s is available", workspace)


def supervise(servers: list[Server], base_delay: float) -> None:
    delays = {s.name: base_delay for s in servers}
    for server in servers:
        server.start()
    while True:
        time.sleep(1)
        for server in servers:
            code = server.proc.poll()
            if code is None:
                continue
            # A server that keeps dying right away (e.g. a port in use) backs off instead of spinning.
            quick = time.monotonic() - server.started_at < 60
            delay = delays[server.name] if quick else base_delay
            log.warning("[launcher] %s exited with code %s; restarting in %ss", server.name, code, int(delay))
            time.sleep(delay)
            delays[server.name] = min(delay * 2, MAX_RESTART_DELAY) if quick else base_delay
            server.start()


def run(config_path: Path, foreground: bool) -> int:
    cfg = load_config(config_path)
    log_dir = Path(cfg["log"]["dir"]) if str(cfg["log"]["dir"]).strip() else LLMWIKI_HOME / "logs"
    setup_logging(log_dir, cfg["log"]["max_mb"], cfg["log"]["backups"], foreground)
    log.info("[launcher] config %s; log level %s, access log %s, web log %s",
             config_path, cfg["log"]["level"], "on" if cfg["log"]["access"] else "off",
             "on" if cfg["log"]["web"] else "off")

    for port in (API_PORT, WEB_PORT):
        if port_in_use(port):
            log.error("[launcher] port %s is already in use (is `llmwiki open` or another copy running?)", port)
            return 1

    workspace = Path(cfg["workspace"])
    wait_for_workspace(workspace)
    index_root = Path(cfg["index_path"]) if str(cfg["index_path"]).strip() else workspace / ".llmwiki"
    if not (index_root / "index.db").exists():
        log.error("[launcher] no index at %s; run `llmwiki init %s` once first", index_root, workspace)
        return 1

    servers = build_servers(cfg, workspace, log_dir)
    kill_children_with_us()
    try:
        supervise(servers, float(cfg["startup"]["restart_delay_seconds"]))
    except KeyboardInterrupt:
        log.info("[launcher] stopping")
    finally:
        for server in servers:
            server.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the LLM Wiki API and web app in the background.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"TOML settings (default {DEFAULT_CONFIG})")
    parser.add_argument("--foreground", action="store_true", help="also print the log to this terminal")
    args = parser.parse_args()
    try:
        return run(args.config, args.foreground)
    except KeyboardInterrupt:
        # The service stop sends Ctrl+C; it can arrive while still waiting for the vault.
        log.info("[launcher] stopped")
        return 0
    except Exception:
        # A service has no console, so a startup error would otherwise vanish.
        if not logging.getLogger().handlers:
            setup_logging(LLMWIKI_HOME / "logs", 5, 3, args.foreground)
        log.exception("[launcher] failed to start with config %s", args.config)
        return 1


if __name__ == "__main__":
    sys.exit(main())
