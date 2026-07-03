"""
Jadx MCP Server — thin wrapper over jadx.

Design rule: jadx is the source of truth. Don't cache anything Python-side.

Resource handling notes
-----------------------
``ResourceFile.loadContent()`` returns a ``ResContainer`` whose ``DataType``
is one of:

* ``TEXT``         — has ``getText() -> ICodeInfo`` (XML, smali, etc.)
* ``RES_TABLE``    — has ``getSubFiles()`` (e.g. ``resources.arsc``)
* ``RES_LINK``     — has ``getResLink() -> ResourceFile``
* ``DECODED_DATA`` — has ``getDecodedData() -> byte[]``

Calling ``getText()`` on the wrong data type throws ClassCastException, and
files like ``res/values/strings.xml`` only exist as **sub-files of the
``resources.arsc`` ``RES_TABLE``** — they aren't top-level resources. The
helpers below encapsulate that.
"""

from __future__ import annotations

import atexit
import base64
import logging
import os
import signal
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import jpype
import jpype.imports
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jadx-mcp")


# ---------------------------------------------------------------------------
# JVM bootstrap
# ---------------------------------------------------------------------------

def _find_jadx_jar() -> str:
    candidates = [
        os.environ.get("JADX_JAR"),
        "/usr/share/java/jadx-git/lib/jadx-dev-all.jar",
        "/usr/local/share/jadx/lib/jadx-dev-all.jar",
        "/opt/jadx/lib/jadx-dev-all.jar",
        os.path.expanduser("~/jadx/lib/jadx-dev-all.jar"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "jadx-dev-all.jar not found. Set JADX_JAR or install jadx."
    )


JADX_JAR = _find_jadx_jar()
log.info("Using jadx jar: %s", JADX_JAR)

if not jpype.isJVMStarted():
    jpype.startJVM(
        jpype.getDefaultJVMPath(),
        "-Xmx4g",
        "-Djava.awt.headless=true",
        classpath=[JADX_JAR],
        convertStrings=False,
    )

from jadx.api import JadxArgs, JadxDecompiler                  # type: ignore # noqa: E402
from java.io import ByteArrayOutputStream, File, PrintStream   # type: ignore # noqa: E402
from java.lang import System                                   # type: ignore # noqa: E402

_devnull = PrintStream(ByteArrayOutputStream())
System.setOut(_devnull)
System.setErr(_devnull)


def _free_memory() -> int:
    """Return JVM free heap in bytes."""
    runtime = System.getRuntime()
    return int(runtime.freeMemory())


def _gc() -> None:
    """Hint the JVM to run garbage collection."""
    try:
        System.gc()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s(obj: Any) -> str:
    return "" if obj is None else str(obj)


def _ok(**kwargs: Any) -> Dict[str, Any]:
    return {"ok": True, **kwargs}


def _err(msg: str, **kwargs: Any) -> Dict[str, Any]:
    return {"ok": False, "error": msg, **kwargs}


def _page_bounds(offset: int, limit: int, total: int) -> Tuple[int, int, int]:
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    end = min(offset + limit, total)
    return offset, limit, end


# -- Resource container helpers --------------------------------------------

def _container_type(container: Any) -> str:
    if container is None:
        return ""
    try:
        return str(container.getDataType())
    except Exception:  # noqa: BLE001
        return ""


# Hard caps so a degenerate resource graph can't blow the stack or hang.
_MAX_LINK_HOPS = 16
_MAX_RESOURCE_ITERATIONS = 200_000


def _resolve_container(container: Any) -> Optional[Any]:
    """
    Follow ``RES_LINK`` chains to a leaf container. Iterative; bounded by
    ``_MAX_LINK_HOPS`` so a cyclic chain can't loop forever.
    """
    hops = 0
    while container is not None:
        if hops > _MAX_LINK_HOPS:
            log.debug("RES_LINK chain exceeded %d hops; bailing", _MAX_LINK_HOPS)
            return None
        if _container_type(container) != "RES_LINK":
            return container
        try:
            link = container.getResLink()
        except Exception:  # noqa: BLE001
            return None
        if link is None:
            return None
        try:
            container = link.loadContent()
        except Exception:  # noqa: BLE001
            return None
        hops += 1
    return None


def _container_to_text(container: Any) -> Optional[str]:
    """
    Text-only extraction. Returns the resource as a string if it's clearly
    textual (TEXT, or DECODED_DATA that decodes as UTF-8); ``None`` otherwise.
    Used by manifest / strings tools that should never return binary.
    """
    container = _resolve_container(container)
    if container is None:
        return None
    dt = _container_type(container)

    if dt == "TEXT":
        try:
            text = container.getText()
        except Exception as e:  # noqa: BLE001
            log.debug("getText() failed on TEXT container: %s", e)
            return None
        return _s(text.getCodeStr()) if text is not None else None

    if dt == "DECODED_DATA":
        try:
            data = container.getDecodedData()
        except Exception:  # noqa: BLE001
            return None
        if data is None:
            return None
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
        except Exception:  # noqa: BLE001
            return None

    # RES_TABLE has no direct text — caller should descend via getSubFiles().
    return None


def _container_to_content(container: Any) -> Optional[Dict[str, Any]]:
    """
    Generic extraction. Returns a dict::

        {"encoding": "text"|"base64", "data_type": str, "content": str, ...}

    Text-typed containers and UTF-8-decodable blobs come back as
    ``encoding="text"``; everything else (images, raw bytes) is base64-encoded
    so the caller can round-trip it. Returns ``None`` if the container has
    nothing extractable (e.g. ``RES_TABLE`` itself).
    """
    container = _resolve_container(container)
    if container is None:
        return None
    dt = _container_type(container)

    if dt == "TEXT":
        try:
            text = container.getText()
        except Exception as e:  # noqa: BLE001
            log.debug("getText() failed on TEXT container: %s", e)
            return None
        if text is None:
            return None
        return {
            "encoding": "text",
            "data_type": dt,
            "content": _s(text.getCodeStr()),
        }

    if dt == "DECODED_DATA":
        try:
            data = container.getDecodedData()
        except Exception:  # noqa: BLE001
            return None
        if data is None:
            return None
        py_bytes = bytes(data)
        try:
            return {
                "encoding": "text",
                "data_type": dt,
                "content": py_bytes.decode("utf-8"),
            }
        except UnicodeDecodeError:
            return {
                "encoding": "base64",
                "data_type": dt,
                "size": len(py_bytes),
                "content": base64.b64encode(py_bytes).decode("ascii"),
            }

    return None


def _iter_all_resources(decompiler: Any) -> Iterator[Tuple[str, Any]]:
    """
    Yield (name, container) for every resource in the APK — top-level files
    plus sub-files unpacked from RES_TABLE containers (e.g. ``resources.arsc``).

    Fully iterative: an explicit stack handles nested tables, and indexed
    ``List.get(i)`` access sidesteps any JPype iterator-protocol surprises.
    A hard iteration cap protects against pathological inputs.
    """
    iterations = 0
    stack: List[Any] = []  # RES_TABLE containers waiting to be expanded

    resources = decompiler.getResources()
    rn = int(resources.size())
    for i in range(rn):
        r = resources.get(i)
        rname = _s(r.getOriginalName())
        try:
            content = r.loadContent()
        except Exception:  # noqa: BLE001
            yield (rname, None)
            continue
        yield (rname, content)
        if content is not None and _container_type(content) == "RES_TABLE":
            stack.append(content)

    while stack:
        iterations += 1
        if iterations > _MAX_RESOURCE_ITERATIONS:
            log.warning(
                "Resource walk hit iteration cap (%d); aborting walk",
                _MAX_RESOURCE_ITERATIONS,
            )
            return

        container = stack.pop()
        try:
            subs = container.getSubFiles()
        except Exception:  # noqa: BLE001
            continue
        if subs is None:
            continue

        sn = int(subs.size())
        for j in range(sn):
            sub = subs.get(j)
            try:
                sub_name = _s(sub.getName())
            except Exception:  # noqa: BLE001
                sub_name = ""
            yield (sub_name, sub)
            if _container_type(sub) == "RES_TABLE":
                stack.append(sub)


def _find_resource(
    decompiler: Any, name: str
) -> Optional[Tuple[str, Any]]:
    """
    Find a resource by exact name match anywhere in the tree, falling back
    to the first suffix match. Short-circuits on exact match.
    """
    suffix_hit: Optional[Tuple[str, Any]] = None
    for cname, container in _iter_all_resources(decompiler):
        if cname == name:
            return (cname, container)
        if suffix_hit is None and cname.endswith(name):
            suffix_hit = (cname, container)
    return suffix_hit


def _read_zip_entry(apk_path: str, name: str) -> Optional[Tuple[str, bytes]]:
    """
    Read a single entry from the APK zip. Tries exact name first, then a
    suffix match. Returns ``(matched_name, data)`` or ``None``.

    Used as a fallback for ``get_resource`` so that binary entries jadx can't
    decode — PNGs, DEX files, raw assets — can still be returned to the
    caller as base64.
    """
    if not apk_path or not os.path.exists(apk_path):
        return None
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            try:
                return (name, z.read(name))
            except KeyError:
                pass
            for info in z.infolist():
                if info.filename.endswith(name):
                    return (info.filename, z.read(info))
    except (zipfile.BadZipFile, OSError) as e:
        log.debug("ZIP read failed for %s: %s", name, e)
    return None


def _bytes_to_payload(raw: bytes) -> Dict[str, Any]:
    """Format raw bytes as a text or base64 payload depending on UTF-8-ness."""
    try:
        return {"encoding": "text", "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode("ascii"),
        }


# ---------------------------------------------------------------------------
# Session — just a handle on the jadx decompiler
# ---------------------------------------------------------------------------

@dataclass
class Session:
    decompiler: Any
    apk_path: str

    def get_class(self, fqn: str) -> Optional[Any]:
        """O(1) lookup via jadx's own search APIs (alias, orig, raw)."""
        d = self.decompiler
        candidates = (fqn, fqn.replace("/", "."), fqn.replace(".", "/"))
        for method_name in (
            "searchJavaClassByAliasFullName",
            "searchJavaClassByOrigFullName",
            "searchJavaClassByRawName",
        ):
            fn = getattr(d, method_name, None)
            if fn is None:
                continue
            for cand in candidates:
                try:
                    cls = fn(cand)
                except Exception:  # noqa: BLE001
                    cls = None
                if cls is not None:
                    return cls
        return None

    def close(self) -> None:
        try:
            self.decompiler.close()
        except Exception as e:  # noqa: BLE001
            log.warning("decompiler.close() raised: %s", e)


_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _shutdown_all() -> None:
    """Close all open sessions and tear down the JVM."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for sess in sessions:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
    if jpype.isJVMStarted():
        try:
            jpype.shutdownJVM()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_shutdown_all)


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
    return sess


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP("Jadx MCP Server")


@mcp.tool()
def load_apk(apk_path: str, session_id: str) -> Dict[str, Any]:
    """Load an APK/DEX/JAR into a new session."""
    try:
        if not os.path.exists(apk_path):
            return _err(f"File not found: {apk_path}")

        with _sessions_lock:
            if session_id in _sessions:
                return _err(
                    f"Session '{session_id}' already exists. "
                    "Close it first or use a different id."
                )

        args = JadxArgs()
        args.setInputFile(File(apk_path))
        args.setShowInconsistentCode(True)
        args.setThreadsCount(int(os.environ.get("JADX_THREADS", str(max(1, os.cpu_count() or 2)))))

        decompiler = JadxDecompiler(args)
        try:
            decompiler.load()
        except Exception:
            try:
                decompiler.close()
            except Exception:  # noqa: BLE001
                pass
            raise

        sess = Session(decompiler=decompiler, apk_path=apk_path)
        with _sessions_lock:
            _sessions[session_id] = sess

        return _ok(
            session_id=session_id,
            apk_path=apk_path,
            class_count=int(decompiler.getClasses().size()),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("load_apk failed")
        return _err(str(e))


@mcp.tool()
def close_session(session_id: str) -> Dict[str, Any]:
    """Dispose of a session and free memory."""
    with _sessions_lock:
        sess = _sessions.pop(session_id, None)
    if sess is None:
        return _err(f"Session '{session_id}' does not exist")
    sess.close()
    return _ok(closed=session_id)


@mcp.tool()
def list_sessions() -> Dict[str, Any]:
    """Return all currently active sessions."""
    with _sessions_lock:
        data = [
            {"session_id": sid, "apk_path": s.apk_path}
            for sid, s in _sessions.items()
        ]
    return _ok(sessions=data)


@mcp.tool()
def get_all_classes(
    session_id: str, offset: int = 0, limit: int = 200
) -> Dict[str, Any]:
    """
    List class FQNs, paginated. Only the requested page crosses JNI, so this
    is fast even on huge APKs.
    """
    try:
        s = _require(session_id)
        jlist = s.decompiler.getClasses()
        total = int(jlist.size())
        offset, limit, end = _page_bounds(offset, limit, total)
        page = [str(jlist.get(i).getFullName()) for i in range(offset, end)]
        return _ok(
            total=total,
            offset=offset,
            limit=limit,
            returned=len(page),
            items=page,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def search_class_names(
    keyword: str, session_id: str, offset: int = 0, limit: int = 200
) -> Dict[str, Any]:
    """Case-insensitive substring search over FQNs."""
    try:
        s = _require(session_id)
        k = keyword.lower()
        jlist = s.decompiler.getClasses()
        n = int(jlist.size())
        hits: List[str] = []
        for i in range(n):
            name = str(jlist.get(i).getFullName())
            if k in name.lower():
                hits.append(name)
        offset, limit, end = _page_bounds(offset, limit, len(hits))
        page = hits[offset:end]
        return _ok(
            total=len(hits),
            offset=offset,
            limit=limit,
            returned=len(page),
            items=page,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_class_source(class_name: str, session_id: str) -> Dict[str, Any]:
    """Decompiled Java source for a class. Cached by jadx itself."""
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")
        return _ok(class_name=class_name, code=_s(cls.getCode()))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_smali_source(class_name: str, session_id: str) -> Dict[str, Any]:
    """
    Smali (DEX disassembly) for a class. Useful when the Java decompilation
    fails or hides control-flow detail you need to see.
    """
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")
        getter = getattr(cls, "getSmali", None)
        if getter is None:
            return _err(
                "This jadx build does not expose JavaClass.getSmali(); "
                "smali output unavailable."
            )
        try:
            smali = getter()
        except Exception as e:  # noqa: BLE001
            return _err(f"Failed to generate smali: {e}")
        return _ok(class_name=class_name, smali=_s(smali))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_methods_of_class(class_name: str, session_id: str) -> Dict[str, Any]:
    """List method signatures of a class."""
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")
        methods = [
            {"name": _s(m.getName()), "signature": _s(m.toString())}
            for m in cls.getMethods()
        ]
        return _ok(class_name=class_name, methods=methods)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_fields_of_class(class_name: str, session_id: str) -> Dict[str, Any]:
    """List fields of a class."""
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")
        fields = [_s(f.toString()) for f in cls.getFields()]
        return _ok(class_name=class_name, fields=fields)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_method_by_name(
    class_name: str,
    method_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Source for a method. If ``signature`` is given, pick that exact overload
    (substring match against ``method.toString()``); otherwise return every
    overload with that name.
    """
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")

        cls.getCode()  # force decompilation so per-method offsets exist

        overloads: List[Dict[str, Any]] = []
        for m in cls.getMethods():
            if _s(m.getName()) != method_name:
                continue
            sig = _s(m.toString())
            if signature and signature not in sig and signature != sig:
                continue
            code = m.getCodeStr()
            overloads.append(
                {"signature": sig, "code": _s(code) if code else None}
            )

        if not overloads:
            return _err(
                f"Method '{method_name}' not found in '{class_name}'"
                + (f" matching '{signature}'" if signature else "")
            )
        return _ok(
            class_name=class_name,
            method_name=method_name,
            overloads=overloads,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def search_classes_by_keyword(
    keyword: str,
    session_id: str,
    limit: int = 50,
    case_sensitive: bool = True,
) -> Dict[str, Any]:
    """
    Search decompiled source for a keyword. Forces decompilation of any class
    not yet decompiled — prefer narrow keywords. jadx caches each class's
    decompiled code on the node, so repeat searches don't redo work.

    Classes are decompiled in parallel using a ThreadPoolExecutor sized to
    ``JADX_SEARCH_THREADS`` (default 4).  JPype releases the GIL during JNI
    calls so true parallelism is achieved across the JVM's own thread pool.

    Heap pressure is monitored between batches: if free heap drops below a
    threshold a ``System.gc()`` is requested, and if it stays critically low
    the search aborts early to avoid a GC death spiral.
    """
    try:
        s = _require(session_id)
        needle = keyword if case_sensitive else keyword.lower()
        jlist = s.decompiler.getClasses()
        n = int(jlist.size())
        search_workers = int(
            os.environ.get("JADX_SEARCH_THREADS", "4")
        )
        max_heap = int(System.getRuntime().maxMemory())
        gc_threshold = int(max_heap * 0.25)
        abort_threshold = int(max_heap * 0.08)
        matches: List[str] = []
        stop = threading.Event()

        def _decompile_and_check(i: int) -> Optional[str]:
            if stop.is_set():
                return None
            cls = jlist.get(i)
            code = _s(cls.getCode())
            if stop.is_set():
                return None
            hay = code if case_sensitive else code.lower()
            if needle in hay:
                return str(cls.getFullName())
            return None

        batch_size = search_workers * 4
        with ThreadPoolExecutor(max_workers=search_workers) as pool:
            for start in range(0, n, batch_size):
                if stop.is_set():
                    break

                free = _free_memory()
                if free < gc_threshold:
                    _gc()
                    free = _free_memory()
                    if free < abort_threshold:
                        log.warning(
                            "JVM heap critically low (%d / %d free); "
                            "aborting search at class %d/%d",
                            free, max_heap, start, n,
                        )
                        break

                end = min(start + batch_size, n)
                futures = [
                    pool.submit(_decompile_and_check, i) for i in range(start, end)
                ]
                for fut in as_completed(futures):
                    if stop.is_set():
                        break
                    name = fut.result()
                    if name is not None:
                        matches.append(name)
                        if len(matches) >= limit:
                            stop.set()
                            break

        return _ok(
            keyword=keyword,
            matches=matches,
            truncated=len(matches) >= limit,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def xrefs_to_class(class_name: str, session_id: str) -> Dict[str, Any]:
    """Return every node that references this class."""
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")
        refs = [_s(n.toString()) for n in cls.getUseIn()]
        return _ok(class_name=class_name, count=len(refs), xrefs=refs)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def xrefs_to_method(
    class_name: str,
    method_name: str,
    session_id: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    """Return xrefs for a method. Use ``signature`` to disambiguate overloads."""
    try:
        s = _require(session_id)
        cls = s.get_class(class_name)
        if cls is None:
            return _err(f"Class '{class_name}' not found")

        grouped: List[Dict[str, Any]] = []
        for m in cls.getMethods():
            if _s(m.getName()) != method_name:
                continue
            sig = _s(m.toString())
            if signature and signature not in sig and signature != sig:
                continue
            grouped.append(
                {
                    "signature": sig,
                    "xrefs": [_s(n.toString()) for n in m.getUseIn()],
                }
            )
        if not grouped:
            return _err(f"Method '{method_name}' not found in '{class_name}'")
        return _ok(
            class_name=class_name,
            method_name=method_name,
            overloads=grouped,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


# -- Resource tools --------------------------------------------------------

@mcp.tool()
def get_android_manifest(session_id: str) -> Dict[str, Any]:
    """Return the decoded AndroidManifest.xml."""
    try:
        s = _require(session_id)
        hit = _find_resource(s.decompiler, "AndroidManifest.xml")
        if hit is None:
            return _err("AndroidManifest.xml not found")
        name, container = hit
        text = _container_to_text(container)
        if text is None:
            return _err(f"Could not extract text from '{name}'")
        return _ok(name=name, content=text)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_strings(session_id: str) -> Dict[str, Any]:
    """
    Return the default ``res/values/strings.xml``. On real APKs this lives
    inside the ``resources.arsc`` table; we walk into it automatically.
    """
    try:
        s = _require(session_id)
        # Try the canonical path first; if missing, fall back to any strings.xml.
        for target in ("res/values/strings.xml", "strings.xml"):
            hit = _find_resource(s.decompiler, target)
            if hit is None:
                continue
            name, container = hit
            text = _container_to_text(container)
            if text is not None:
                return _ok(name=name, content=text)
        return _err("strings.xml not found")
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_resource(
    resource_name: str,
    session_id: str,
    max_bytes: int = 10 * 1024 * 1024,
) -> Dict[str, Any]:
    """
    Fetch a resource by name (or path suffix).

    Resolution strategy:

    1. **jadx first** — handles arsc-decoded resources (``AndroidManifest.xml``,
       ``res/values/strings.xml``, etc.) and any text/binary resource jadx
       can extract. Returns ``source="jadx"``.
    2. **APK zip fallback** — if jadx didn't find the resource or couldn't
       extract its content (e.g. ``RES_LINK`` to a binary that jadx didn't
       decode, ``classes2.dex``, raw assets), reads the entry directly from
       the APK zip. Returns ``source="zip"``.

    The response always includes ``encoding`` (``"text"`` or ``"base64"``).
    Binary payloads also include ``size`` (original byte length).
    ``max_bytes`` caps the payload size so a giant DEX doesn't blow up the
    response — raise it if you need to.
    """
    try:
        s = _require(session_id)

        # 1. jadx
        jadx_name: Optional[str] = None
        jadx_dtype: Optional[str] = None
        hit = _find_resource(s.decompiler, resource_name)
        if hit is not None:
            jadx_name, container = hit
            jadx_dtype = _container_type(container)
            result = _container_to_content(container)
            if result is not None:
                content = result.get("content", "")
                if isinstance(content, str) and len(content) > max_bytes * 2:
                    # base64 inflates by ~4/3, so a generous text-length check
                    return _err(
                        f"Resource '{jadx_name}' content exceeds max_bytes={max_bytes}",
                        name=jadx_name,
                        size=result.get("size"),
                    )
                return _ok(name=jadx_name, source="jadx", **result)

        # 2. APK zip fallback — try the requested name, then jadx's resolved name.
        for candidate in (resource_name, jadx_name):
            if not candidate:
                continue
            entry = _read_zip_entry(s.apk_path, candidate)
            if entry is None:
                continue
            matched_name, raw = entry
            if len(raw) > max_bytes:
                return _err(
                    f"Resource '{matched_name}' is {len(raw)} bytes; "
                    f"exceeds max_bytes={max_bytes}. Increase max_bytes to retrieve.",
                    name=matched_name,
                    size=len(raw),
                )
            payload = _bytes_to_payload(raw)
            return _ok(name=matched_name, source="zip", **payload)

        # 3. Total miss.
        if hit is None:
            return _err(f"Resource '{resource_name}' not found")
        return _err(
            f"Resource '{jadx_name}' could not be extracted",
            name=jadx_name,
            data_type=jadx_dtype,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def get_all_resource_file_names(
    session_id: str,
    offset: int = 0,
    limit: int = 500,
    recursive: bool = False,
) -> Dict[str, Any]:
    """
    List resource file paths, paginated.

    By default lists only top-level resources (cheap). Pass ``recursive=True``
    to also walk into RES_TABLE containers like ``resources.arsc`` — that
    forces the arsc to be parsed, which is slower the first time.
    """
    try:
        s = _require(session_id)
        if recursive:
            names = [name for name, _ in _iter_all_resources(s.decompiler)]
        else:
            resources = s.decompiler.getResources()
            n = int(resources.size())
            names = [_s(resources.get(i).getOriginalName()) for i in range(n)]
        offset, limit, end = _page_bounds(offset, limit, len(names))
        page = names[offset:end]
        return _ok(
            total=len(names),
            offset=offset,
            limit=limit,
            returned=len(page),
            items=page,
            recursive=recursive,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@mcp.tool()
def search_resource_name(
    keyword: str,
    session_id: str,
    offset: int = 0,
    limit: int = 200,
    recursive: bool = True,
) -> Dict[str, Any]:
    """
    Case-insensitive substring search over resource paths. Defaults to
    ``recursive=True`` so paths like ``res/values/strings.xml`` inside
    ``resources.arsc`` are findable. Pass ``recursive=False`` for a fast
    top-level-only search.
    """
    try:
        s = _require(session_id)
        k = keyword.lower()
        if recursive:
            names = [name for name, _ in _iter_all_resources(s.decompiler)]
        else:
            resources = s.decompiler.getResources()
            n = int(resources.size())
            names = [_s(resources.get(i).getOriginalName()) for i in range(n)]
        hits = [name for name in names if k in name.lower()]
        offset, limit, end = _page_bounds(offset, limit, len(hits))
        page = hits[offset:end]
        return _ok(
            total=len(hits),
            offset=offset,
            limit=limit,
            returned=len(page),
            items=page,
            recursive=recursive,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "sse" in sys.argv:
        mcp.run(transport="sse")
    else:
        mcp.run()
