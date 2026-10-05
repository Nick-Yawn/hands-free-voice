"""Per-project state on disk: the session pin, the lock, the private log.

Everything lives under ~/.local/state/hands-free-voice/projects/<slug>/ (or
$HANDS_FREE_VOICE_STATE_DIR), never inside the project folder, so a user's repo
is never touched. The slug is the folder's name plus a short hash of
its absolute path.

  session_id   the pinned claude session, resumed on the next start
  lock         JSON: the hands-free-voice host pid, the child claude pid and the
               session id — so a second driver refuses the session, and
               a stray child from a crashed host can be found and killed
  log.jsonl    every raw stream record and every heard, dropped, sent
               and spoken event, created 0600 (it can carry anything
               the session read)
  claude-contract.md   the voice contract handed to every spawn
"""

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

STATE_DIR_ENV = "HANDS_FREE_VOICE_STATE_DIR"
DEFAULT_STATE_DIR = Path("~/.local/state/hands-free-voice")


def project_slug(project_dir: str | os.PathLike) -> str:
    path = Path(project_dir).resolve()
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in path.name) or "root"
    return f"{name}-{digest}"


def state_dir(project_dir: str | os.PathLike, root: str | os.PathLike | None = None) -> Path:
    base = Path(root) if root else Path(os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
    d = base.expanduser() / "projects" / project_slug(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


class SessionPin:
    """The session id pinned on disk: --resume targets it explicitly,
    because a bare --continue means "most recent session in this
    directory" and once walked into the wrong room."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> str | None:
        try:
            sid = self.path.read_text().strip()
        except OSError:
            return None
        return sid or None

    def save(self, sid: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(sid)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class EventLog:
    """Append-only JSONL, created owner-only. Best effort, never fatal."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None

    def write(self, _ev: str, **fields) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps({"ts": time.time(), "ev": _ev, **fields},
                                   default=str) + "\n")
        except OSError:
            pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command(pid: int) -> str:
    """The command line of a pid, or "" (a dead or foreign pid)."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def is_claude_process(pid: int, command=process_command) -> bool:
    return "claude" in command(pid)


def kill_group(pid: int, grace_s: float = 2.0) -> None:
    """SIGTERM then SIGKILL to a whole process group (the child was
    spawned as a session leader). Synchronous: for atexit, signal
    handlers and stray-child cleanup. Never raises."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.05)


class LockFile:
    """Which hands-free-voice holds this project's session, and which child."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)

    def acquire(self, session_id: str | None, pid: int | None = None) -> None:
        self._write({"pid": pid or os.getpid(), "child_pid": None,
                     "session_id": session_id, "started": time.time()})

    def set_child(self, child_pid: int | None) -> None:
        data = self.read() or {"pid": os.getpid()}
        data["child_pid"] = child_pid
        self._write(data)

    def set_session(self, session_id: str | None) -> None:
        data = self.read() or {"pid": os.getpid()}
        data["session_id"] = session_id
        self._write(data)

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def holder(self, alive=pid_alive, self_pid: int | None = None) -> int | None:
        """The pid of a LIVE other host holding this lock, or None."""
        data = self.read()
        if not data:
            return None
        pid = data.get("pid")
        if pid == (self_pid or os.getpid()):
            return None
        return pid if isinstance(pid, int) and alive(pid) else None

    def stray_child(self, alive=pid_alive, is_claude=is_claude_process) -> int | None:
        """A child claude left running by a host that is gone."""
        data = self.read()
        if not data:
            return None
        if isinstance(data.get("pid"), int) and alive(data["pid"]) \
                and data["pid"] != os.getpid():
            return None  # its host is alive; not ours to touch
        child = data.get("child_pid")
        if isinstance(child, int) and alive(child) and is_claude(child):
            return child
        return None
