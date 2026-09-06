from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, TextIO

from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jadx-mcp")

WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "worker.py")
MAX_SESSIONS = max(1, int(os.environ.get("JADX_MAX_SESSIONS", "3")))
MAX_CONCURRENT_STARTUPS = max(
    1, int(os.environ.get("JADX_MAX_CONCURRENT_STARTUPS", "2"))
)
STARTUP_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("JADX_STARTUP_TIMEOUT_SECONDS", "600"))
)
REQUEST_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("JADX_REQUEST_TIMEOUT_SECONDS", "900"))
)
REQUEST_QUEUE_TIMEOUT_SECONDS = max(
    0.0, float(os.environ.get("JADX_REQUEST_QUEUE_TIMEOUT_SECONDS", "2"))
)
REUSE_APK_SESSIONS = os.environ.get("JADX_REUSE_APK_SESSIONS", "1").lower() not in {
    "0",
    "false",
    "no",
}


def _ok(**kwargs: Any) -> Dict[str, Any]:
    return {"ok": True, **kwargs}


def _err(msg: str, **kwargs: Any) -> Dict[str, Any]:
    return {"ok": False, "error": msg, **kwargs}


def _process_rss_mb(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass
class Session:
    apk_path: str
    proc: subprocess.Popen[str]
    stderr_path: str
    class_count: int
    request_lock: threading.Lock
    close_lock: threading.Lock
    closing: threading.Event
    responses: queue.Queue[Optional[str]]
    created_at: float
    last_activity: float
    active_op: Optional[str] = None

    def start_reader(self) -> None:
        threading.Thread(
            target=self._read_responses,
            name=f"jadx-reader-{self.proc.pid}",
            daemon=True,
        ).start()

    def _read_responses(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.responses.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put(None)

    def _read_stderr_tail(self) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4000), os.SEEK_SET)
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return data.strip()

    def _worker_error(self, msg: str) -> Dict[str, Any]:
        tail = self._read_stderr_tail()
        if tail:
            msg += f"\n{tail}"
        return _err(msg)

    def request(self, op: str, **kwargs: Any) -> Dict[str, Any]:
        if self.closing.is_set():
            return _err("session is closing")
        if not self.request_lock.acquire(timeout=REQUEST_QUEUE_TIMEOUT_SECONDS):
            return _err(
                f"session is busy with '{self.active_op or 'another operation'}'; retry shortly",
                retryable=True,
            )
        transport_failed = False
        try:
            if self.closing.is_set():
                return _err("session is closing")
            if self.proc.poll() is not None:
                transport_failed = True
                return self._worker_error(
                    f"worker exited with code {self.proc.returncode}"
                )

            assert self.proc.stdin is not None
            self.active_op = op
            self.last_activity = time.monotonic()
            try:
                self.proc.stdin.write(
                    json.dumps({"op": op, **kwargs}, ensure_ascii=False) + "\n"
                )
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                transport_failed = True
                return self._worker_error(f"failed to write to worker: {e}")

            try:
                line = self.responses.get(timeout=REQUEST_TIMEOUT_SECONDS)
            except queue.Empty:
                transport_failed = True
                return _err(
                    f"worker operation '{op}' timed out after "
                    f"{REQUEST_TIMEOUT_SECONDS:g}s; worker terminated"
                )
            if line is None:
                transport_failed = True
                return self._worker_error(
                    f"worker exited with code {self.proc.poll()}"
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as e:
                transport_failed = True
                return self._worker_error(
                    f"invalid worker response: {e}: {line[:1000]!r}"
                )
            if not isinstance(response, dict):
                transport_failed = True
                return _err("invalid worker response envelope; worker terminated")
            return response
        finally:
            self.active_op = None
            self.last_activity = time.monotonic()
            self.request_lock.release()
            if transport_failed:
                self.close(graceful=False)

    def close(self, graceful: bool = True) -> None:
        with self.close_lock:
            if self.closing.is_set() and self.proc.stdin is None:
                return
            self.closing.set()
            if self.proc.poll() is None:
                can_gracefully_close = graceful and self.request_lock.acquire(
                    blocking=False
                )
                if can_gracefully_close:
                    try:
                        if self.proc.stdin is not None:
                            self.proc.stdin.write('{"op":"close"}\n')
                            self.proc.stdin.flush()
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        self.request_lock.release()
                try:
                    self.proc.wait(timeout=10 if can_gracefully_close else 0.1)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=5)
            for stream_name in ("stdin", "stdout"):
                stream = getattr(self.proc, stream_name)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                    setattr(self.proc, stream_name, None)
            try:
                os.unlink(self.stderr_path)
            except OSError:
                pass


def _prune_dead_sessions() -> None:
    with _sessions_lock:
        dead_entries = [
            (sid, sess)
            for sid, sess in _sessions.items()
            if sess.proc.poll() is not None
        ]
        for sid, sess in dead_entries:
            if _sessions.get(sid) is sess:
                _sessions.pop(sid, None)
        dead = {id(sess): sess for _, sess in dead_entries}.values()
    for sess in dead:
        sess.close(graceful=False)


_sessions: Dict[str, Session] = {}
_loading_sessions: set[str] = set()
_loading_paths: Dict[str, str] = {}
_sessions_lock = threading.Lock()
_startup_slots = threading.BoundedSemaphore(MAX_CONCURRENT_STARTUPS)
_shutting_down = False
_janitor_stop = threading.Event()


def _cleanup_startup_process(
    proc: Optional[subprocess.Popen[str]], stderr_path: str
) -> None:
    if proc is not None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream_name in ("stdin", "stdout"):
            stream: Optional[TextIO] = getattr(proc, stream_name)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    try:
        os.unlink(stderr_path)
    except OSError:
        pass


def _spawn_worker(
    input_path: str, *, is_project: bool = False
) -> tuple[Optional[Session], Dict[str, Any]]:
    stderr_file = tempfile.NamedTemporaryFile(
        prefix="jadx_worker_", suffix=".log", delete=False
    )
    stderr_path = stderr_file.name
    stderr_file.close()
    proc: Optional[subprocess.Popen[str]] = None
    err_handle = open(stderr_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, WORKER_SCRIPT]
            + (["--project", input_path] if is_project else [input_path]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_handle,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except Exception:
        err_handle.close()
        _cleanup_startup_process(proc, stderr_path)
        raise
    finally:
        if not err_handle.closed:
            err_handle.close()

    assert proc.stdout is not None
    ready, _, _ = select.select(
        [proc.stdout], [], [], STARTUP_TIMEOUT_SECONDS
    )
    if not ready:
        _cleanup_startup_process(proc, stderr_path)
        return None, _err(
            f"worker startup timed out after {STARTUP_TIMEOUT_SECONDS:g}s"
        )
    line = proc.stdout.readline()
    if not line:
        code = proc.poll()
        try:
            with open(stderr_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4000), os.SEEK_SET)
                tail = f.read().decode("utf-8", errors="replace").strip()
        except OSError:
            tail = ""
        _cleanup_startup_process(proc, stderr_path)
        msg = f"worker failed to start (exit {code})"
        if tail:
            msg += f"\n{tail}"
        return None, _err(msg)
    try:
        startup = json.loads(line)
    except json.JSONDecodeError as e:
        _cleanup_startup_process(proc, stderr_path)
        return None, _err(f"invalid worker startup response: {e}: {line[:1000]!r}")
    if not isinstance(startup, dict) or not isinstance(startup.get("ok"), bool):
        _cleanup_startup_process(proc, stderr_path)
        return None, _err("invalid worker startup response envelope")
    if not startup["ok"]:
        _cleanup_startup_process(proc, stderr_path)
        return None, startup
    now = time.monotonic()
    sess = Session(
        apk_path=os.path.realpath(str(startup.get("apk_path", input_path))),
        proc=proc,
        stderr_path=stderr_path,
        class_count=int(startup.get("class_count", 0)),
        request_lock=threading.Lock(),
        close_lock=threading.Lock(),
        closing=threading.Event(),
        responses=queue.Queue(),
        created_at=now,
        last_activity=now,
    )
    sess.start_reader()
    return sess, startup


def _shutdown_all() -> None:
    global _shutting_down
    _janitor_stop.set()
    with _sessions_lock:
        _shutting_down = True
        sessions = list({id(sess): sess for sess in _sessions.values()}.values())
        _sessions.clear()
        _loading_sessions.clear()
        _loading_paths.clear()
    for sess in sessions:
        try:
            sess.close(graceful=False)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_shutdown_all)


def _session_janitor() -> None:
    while not _janitor_stop.wait(30):
        _prune_dead_sessions()


threading.Thread(
    target=_session_janitor,
    name="jadx-session-janitor",
    daemon=True,
).start()


def _signal_handler(signum: int, frame: Any) -> None:
    _shutdown_all()
    os._exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _require(session_id: str) -> Session:
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if sess is None:
        raise KeyError(f"Session '{session_id}' not found. Call load_apk first.")
    if sess.proc.poll() is not None:
        with _sessions_lock:
            for sid, candidate in list(_sessions.items()):
                if candidate is sess:
                    _sessions.pop(sid, None)
        error = sess._worker_error(f"worker exited with code {sess.proc.returncode}")
        sess.close(graceful=False)
        raise RuntimeError(error["error"])
    return sess


def _proxy(session_id: str, op: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        sess = _require(session_id)
        result = sess.request(op, **kwargs)
        if sess.proc.poll() is not None or sess.closing.is_set():
            with _sessions_lock:
                for sid, candidate in list(_sessions.items()):
                    if candidate is sess:
                        _sessions.pop(sid, None)
        return result
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


mcp = FastMCP("Jadx MCP Server")


@mcp.tool()
def load_apk(
    apk_path: str,
    session_id: str,
    project_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Load an APK/DEX/JAR, optionally restoring a native .jadx project."""
    global _shutting_down
    reserved = False
    startup_slot_acquired = False
    sess: Optional[Session] = None
    session_id = session_id.strip()
    try:
        if not session_id:
            return _err("session_id must not be empty")
        apk_path = os.path.realpath(apk_path)
        if not os.path.isfile(apk_path):
            return _err(f"File not found: {apk_path}")
        if project_path is not None:
            project_path = os.path.realpath(project_path)
            if not os.path.isfile(project_path):
                return _err(f"JADX project not found: {project_path}")
        _prune_dead_sessions()
        with _sessions_lock:
            if _shutting_down:
                return _err("server is shutting down")
            if session_id in _sessions or session_id in _loading_sessions:
                return _err(
                    f"Session '{session_id}' already exists. "
                    "Close it first or use a different id."
                )
            if REUSE_APK_SESSIONS and project_path is None:
                for existing_id, existing in _sessions.items():
                    if existing.apk_path == apk_path and not existing.closing.is_set():
                        _sessions[session_id] = existing
                        return _ok(
                            session_id=session_id,
                            apk_path=apk_path,
                            class_count=existing.class_count,
                            pid=existing.proc.pid,
                            reused=True,
                            shared_with=existing_id,
                        )
                loading_id = _loading_paths.get(apk_path)
                if loading_id is not None:
                    return _err(
                        f"APK is already loading in session '{loading_id}'; "
                        "use that session or retry shortly",
                        retryable=True,
                    )
            active_workers = len({id(existing) for existing in _sessions.values()})
            if active_workers + len(_loading_sessions) >= MAX_SESSIONS:
                return _err(
                    f"session capacity reached ({MAX_SESSIONS}); close an idle session first",
                    retryable=True,
                    max_sessions=MAX_SESSIONS,
                )
            if not _startup_slots.acquire(blocking=False):
                return _err(
                    "worker startup capacity reached; retry shortly",
                    retryable=True,
                    max_concurrent_startups=MAX_CONCURRENT_STARTUPS,
                )
            startup_slot_acquired = True
            _loading_sessions.add(session_id)
            _loading_paths[apk_path] = session_id
            reserved = True
        sess, startup = _spawn_worker(apk_path)
        if sess is None:
            return startup
        restored: Optional[Dict[str, Any]] = None
        if project_path is not None:
            restored = sess.request("load_project_data", project_path=project_path)
            if not restored.get("ok"):
                return restored
        with _sessions_lock:
            if _shutting_down:
                return _err("server is shutting down")
            _sessions[session_id] = sess
            _loading_sessions.discard(session_id)
            _loading_paths.pop(apk_path, None)
            reserved = False
        return _ok(
            session_id=session_id,
            apk_path=apk_path,
            class_count=startup.get("class_count", 0),
            pid=sess.proc.pid,
            restored_renames=(restored or {}).get("rename_count", 0),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("load_apk failed")
        return _err(str(e))
    finally:
        if reserved:
            with _sessions_lock:
                _loading_sessions.discard(session_id)
                if _loading_paths.get(apk_path) == session_id:
                    _loading_paths.pop(apk_path, None)
        if sess is not None:
            with _sessions_lock:
                published = _sessions.get(session_id) is sess
            if not published:
                sess.close(graceful=False)
        if startup_slot_acquired:
            _startup_slots.release()


@mcp.tool()
def load_project(project_path: str, session_id: str) -> Dict[str, Any]:
    """Load a native JADX .jadx project into an isolated worker session."""
    reserved = False
    startup_slot_acquired = False
    sess: Optional[Session] = None
    session_id = session_id.strip()
    try:
        if not session_id:
            return _err("session_id must not be empty")
        project_path = os.path.realpath(project_path)
        if not os.path.isfile(project_path):
            return _err(f"JADX project not found: {project_path}")
        _prune_dead_sessions()
        with _sessions_lock:
            if _shutting_down:
                return _err("server is shutting down")
            if session_id in _sessions or session_id in _loading_sessions:
                return _err(
                    f"Session '{session_id}' already exists. "
                    "Close it first or use a different id."
                )
            active_workers = len({id(existing) for existing in _sessions.values()})
            if active_workers + len(_loading_sessions) >= MAX_SESSIONS:
                return _err(
                    f"session capacity reached ({MAX_SESSIONS}); close an idle session first",
                    retryable=True,
                    max_sessions=MAX_SESSIONS,
                )
            if not _startup_slots.acquire(blocking=False):
                return _err(
                    "worker startup capacity reached; retry shortly",
                    retryable=True,
                    max_concurrent_startups=MAX_CONCURRENT_STARTUPS,
                )
            startup_slot_acquired = True
            _loading_sessions.add(session_id)
            reserved = True
        sess, startup = _spawn_worker(project_path, is_project=True)
        if sess is None:
            return startup
        with _sessions_lock:
            if _shutting_down:
                return _err("server is shutting down")
            _sessions[session_id] = sess
            _loading_sessions.discard(session_id)
            reserved = False
        return _ok(
            session_id=session_id,
            project_path=project_path,
            apk_path=sess.apk_path,
            class_count=startup.get("class_count", 0),
            restored_renames=startup.get("restored_renames", 0),
            project_version=startup.get("project_version"),
            pid=sess.proc.pid,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("load_project failed")
        return _err(str(e))
    finally:
        if reserved:
            with _sessions_lock:
                _loading_sessions.discard(session_id)
        if sess is not None:
            with _sessions_lock:
                published = _sessions.get(session_id) is sess
            if not published:
                sess.close(graceful=False)
        if startup_slot_acquired:
            _startup_slots.release()


@mcp.tool()
def close_session(session_id: str) -> Dict[str, Any]:
    """Dispose of a worker session and free its JVM memory."""
    with _sessions_lock:
        if session_id in _loading_sessions:
            return _err(f"Session '{session_id}' is still loading; retry shortly")
        sess = _sessions.get(session_id)
    if sess is None:
        return _err(f"Session '{session_id}' does not exist")
    with _sessions_lock:
        aliases = [sid for sid, candidate in _sessions.items() if candidate is sess]
        if len(aliases) > 1:
            _sessions.pop(session_id, None)
            return _ok(
                closed=session_id,
                worker_closed=False,
                remaining_aliases=[sid for sid in aliases if sid != session_id],
            )
        sess.closing.set()
    sess.close()
    with _sessions_lock:
        if _sessions.get(session_id) is sess:
            _sessions.pop(session_id, None)
    return _ok(closed=session_id)


@mcp.tool()
def list_sessions() -> Dict[str, Any]:
    """Return all loaded and loading isolated sessions."""
    _prune_dead_sessions()
    with _sessions_lock:
        alias_counts: Dict[int, int] = {}
        for sess in _sessions.values():
            alias_counts[id(sess)] = alias_counts.get(id(sess), 0) + 1
        data = [
            {
                "session_id": sid,
                "apk_path": s.apk_path,
                "pid": s.proc.pid,
                "state": "closing" if s.closing.is_set() else "active",
                "active_op": s.active_op,
                "idle_seconds": round(time.monotonic() - s.last_activity, 1),
                "shared": alias_counts[id(s)] > 1,
                "rss_mb": _process_rss_mb(s.proc.pid),
            }
            for sid, s in _sessions.items()
        ]
        data.extend(
            {
                "session_id": sid,
                "state": "loading",
            }
            for sid in _loading_sessions
        )
    return _ok(
        sessions=data,
        active_workers=len(alias_counts),
        max_sessions=MAX_SESSIONS,
        max_concurrent_startups=MAX_CONCURRENT_STARTUPS,
    )


@mcp.tool()
def save_session(
    session_id: str,
    project_path: str,
) -> Dict[str, Any]:
    """Save the live session as a native JADX .jadx project; keep it open."""
    return _proxy(
        session_id,
        "save_session",
        project_path=project_path,
    )


@mcp.tool()
def save_and_exit_session(
    session_id: str,
    project_path: str,
) -> Dict[str, Any]:
    """Save a native JADX .jadx project, then close this session alias."""
    result = save_session(session_id, project_path)
    if not result.get("ok"):
        return result
    close_result = close_session(session_id)
    if not close_result.get("ok"):
        return _err(
            f"Session saved but close failed: {close_result.get('error')}",
            save=result,
        )
    result.update(
        closed=True,
        worker_closed=close_result.get("worker_closed", True),
        remaining_aliases=close_result.get("remaining_aliases", []),
    )
    return result


@mcp.tool()
def get_renames(session_id: str) -> Dict[str, Any]:
    """List persistent user renames currently applied to a session."""
    return _proxy(session_id, "get_renames")


@mcp.tool()
def rename_class(class_name: str, new_name: str, session_id: str) -> Dict[str, Any]:
    """Rename a class; top-level classes may use a qualified new name."""
    return _proxy(
        session_id,
        "rename_class",
        class_name=class_name,
        new_name=new_name,
    )


@mcp.tool()
def rename_method(
    class_name: str,
    method_name: str,
    new_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename a method/function; pass short_id as signature for overloads."""
    return _proxy(
        session_id,
        "rename_method",
        class_name=class_name,
        method_name=method_name,
        new_name=new_name,
        signature=signature,
    )


@mcp.tool()
def rename_field(
    class_name: str,
    field_name: str,
    new_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename a field; pass short_id as signature if the name is ambiguous."""
    return _proxy(
        session_id,
        "rename_field",
        class_name=class_name,
        field_name=field_name,
        new_name=new_name,
        signature=signature,
    )


@mcp.tool()
def list_method_variables(
    class_name: str,
    method_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """List method arguments and local register/SSA selectors for renaming."""
    return _proxy(
        session_id,
        "list_method_variables",
        class_name=class_name,
        method_name=method_name,
        signature=signature,
    )


@mcp.tool()
def rename_method_argument(
    class_name: str,
    method_name: str,
    argument_index: int,
    new_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename a method argument by its zero-based JADX argument index."""
    return _proxy(
        session_id,
        "rename_method_argument",
        class_name=class_name,
        method_name=method_name,
        argument_index=argument_index,
        new_name=new_name,
        signature=signature,
    )


@mcp.tool()
def rename_local_variable(
    class_name: str,
    method_name: str,
    register: int,
    ssa: int,
    new_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename a local variable using register/SSA values from list_method_variables."""
    return _proxy(
        session_id,
        "rename_local_variable",
        class_name=class_name,
        method_name=method_name,
        register=register,
        ssa=ssa,
        new_name=new_name,
        signature=signature,
    )


@mcp.tool()
def get_all_classes(session_id: str, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    return _proxy(session_id, "get_all_classes", offset=offset, limit=limit)


@mcp.tool()
def search_class_names(keyword: str, session_id: str, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    return _proxy(session_id, "search_class_names", keyword=keyword, offset=offset, limit=limit)


@mcp.tool()
def get_class_source(class_name: str, session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_class_source", class_name=class_name)


@mcp.tool()
def get_smali_source(class_name: str, session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_smali_source", class_name=class_name)


@mcp.tool()
def get_methods_of_class(class_name: str, session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_methods_of_class", class_name=class_name)


@mcp.tool()
def get_fields_of_class(class_name: str, session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_fields_of_class", class_name=class_name)


@mcp.tool()
def get_method_by_name(
    class_name: str,
    method_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    return _proxy(
        session_id,
        "get_method_by_name",
        class_name=class_name,
        method_name=method_name,
        signature=signature,
    )


@mcp.tool()
def search_classes_by_keyword(
    keyword: str,
    session_id: str,
    limit: int = 50,
    case_sensitive: bool = True,
) -> Dict[str, Any]:
    return _proxy(
        session_id,
        "search_classes_by_keyword",
        keyword=keyword,
        limit=limit,
        case_sensitive=case_sensitive,
    )


@mcp.tool()
def xrefs_to_class(class_name: str, session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "xrefs_to_class", class_name=class_name)


@mcp.tool()
def xrefs_to_method(
    class_name: str,
    method_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    return _proxy(
        session_id,
        "xrefs_to_method",
        class_name=class_name,
        method_name=method_name,
        signature=signature,
    )


@mcp.tool()
def get_android_manifest(session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_android_manifest")


@mcp.tool()
def get_strings(session_id: str) -> Dict[str, Any]:
    return _proxy(session_id, "get_strings")


@mcp.tool()
def get_resource(resource_name: str, session_id: str, max_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
    return _proxy(session_id, "get_resource", resource_name=resource_name, max_bytes=max_bytes)


@mcp.tool()
def get_all_resource_file_names(
    session_id: str,
    offset: int = 0,
    limit: int = 500,
    recursive: bool = False,
) -> Dict[str, Any]:
    return _proxy(
        session_id,
        "get_all_resource_file_names",
        offset=offset,
        limit=limit,
        recursive=recursive,
    )


@mcp.tool()
def search_resource_name(
    keyword: str,
    session_id: str,
    offset: int = 0,
    limit: int = 200,
    recursive: bool = True,
) -> Dict[str, Any]:
    return _proxy(
        session_id,
        "search_resource_name",
        keyword=keyword,
        offset=offset,
        limit=limit,
        recursive=recursive,
    )


if __name__ == "__main__":
    if "sse" in sys.argv:
        mcp.run(transport="sse")
    else:
        mcp.run()
