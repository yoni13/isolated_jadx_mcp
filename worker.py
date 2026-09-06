"""
Single-session jadx worker.

Protocol: line-delimited JSON over stdin/stdout.
Each worker owns exactly one JVM and one loaded APK, so heap pressure from one
APK cannot poison unrelated sessions.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import jpype
import jpype.imports

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jadx-worker")

IDLE_TIMEOUT_SECONDS = int(os.environ.get("JADX_IDLE_TIMEOUT_SECONDS", str(20 * 60)))
ACTIVE_PROCESSORS = max(1, int(os.environ.get("JADX_ACTIVE_PROCESSORS", "4")))
JADX_THREADS = max(1, int(os.environ.get("JADX_THREADS", str(ACTIVE_PROCESSORS))))
SEARCH_THREADS = max(1, int(os.environ.get("JADX_SEARCH_THREADS", "2")))
OVERNIGHT_IDLE_EXEMPTION = os.environ.get(
    "JADX_OVERNIGHT_IDLE_EXEMPTION", "1"
).lower() not in {"0", "false", "no"}


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


def _ensure_jvm() -> None:
    if jpype.isJVMStarted():
        return
    heap = os.environ.get("JADX_HEAP", "8g")
    initial_heap = os.environ.get("JADX_INITIAL_HEAP", "128m")
    jpype.startJVM(
        jpype.getDefaultJVMPath(),
        f"-Xms{initial_heap}",
        f"-Xmx{heap}",
        f"-XX:ActiveProcessorCount={ACTIVE_PROCESSORS}",
        "-XX:+UseG1GC",
        "-XX:+UseStringDeduplication",
        "-XX:+ExitOnOutOfMemoryError",
        "-Djava.awt.headless=true",
        classpath=[JADX_JAR],
        convertStrings=False,
    )


_ensure_jvm()

from jadx.api import JadxArgs, JadxDecompiler                  # type: ignore # noqa: E402
from jadx.api.data.impl import (                               # type: ignore # noqa: E402
    JadxCodeData,
    JadxCodeRef,
    JadxCodeRename,
    JadxNodeRef,
)
from jadx.api.metadata.annotations import (                    # type: ignore # noqa: E402
    NodeDeclareRef,
    VarNode,
)
from jadx.api.usage.impl import EmptyUsageInfoCache            # type: ignore # noqa: E402
from java.io import File, OutputStream, PrintStream            # type: ignore # noqa: E402
from java.lang import Runtime, System                          # type: ignore # noqa: E402
from java.nio.file import Paths                                # type: ignore # noqa: E402
from java.util import ArrayList, Collections                   # type: ignore # noqa: E402
from java.util.regex import Pattern                            # type: ignore # noqa: E402

NameMapper = jpype.JClass("jadx.core.deobf.NameMapper")
GsonUtils = jpype.JClass("jadx.core.utils.GsonUtils")
JadxProject = jpype.JClass("jadx.gui.settings.JadxProject")
ProjectData = jpype.JClass("jadx.gui.settings.data.ProjectData")
RelativePathTypeAdapter = jpype.JClass("jadx.gui.utils.RelativePathTypeAdapter")
JavaPath = jpype.JClass("java.nio.file.Path")
ICodeComment = jpype.JClass("jadx.api.data.ICodeComment")
ICodeRename = jpype.JClass("jadx.api.data.ICodeRename")
IJavaNodeRef = jpype.JClass("jadx.api.data.IJavaNodeRef")
IJavaCodeRef = jpype.JClass("jadx.api.data.IJavaCodeRef")
JadxCodeComment = jpype.JClass("jadx.api.data.impl.JadxCodeComment")

_devnull = PrintStream(OutputStream.nullOutputStream())
System.setOut(_devnull)
System.setErr(_devnull)

SimpleJadxPassInfo = jpype.JClass(
    "jadx.api.plugins.pass.impl.SimpleJadxPassInfo"
)
DiskCodeCache = jpype.JClass("jadx.gui.cache.code.disk.DiskCodeCache")
BufferCodeCache = jpype.JClass("jadx.gui.cache.code.disk.BufferCodeCache")


def _make_disk_cache_pass(cache_dir: str):
    path = Paths.get(cache_dir)
    buffered = os.environ.get("JADX_CODE_CACHE", "disk").lower() == "buffered"

    @jpype.JImplements("jadx.api.plugins.pass.types.JadxPreparePass")
    class _DiskCachePass:
        @jpype.JOverride
        def getInfo(self):
            return SimpleJadxPassInfo("DiskCacheInit")

        @jpype.JOverride
        def init(self, root):
            disk_cache = DiskCodeCache(root, path)
            root.getArgs().setCodeCache(
                BufferCodeCache(disk_cache) if buffered else disk_cache
            )

    return _DiskCachePass()


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


def _heap_stats() -> Tuple[int, int, int]:
    rt = Runtime.getRuntime()
    return int(rt.freeMemory()), int(rt.totalMemory()), int(rt.maxMemory())


def _gc() -> None:
    try:
        Runtime.getRuntime().gc()
    except Exception:  # noqa: BLE001
        pass


def _container_type(container: Any) -> str:
    if container is None:
        return ""
    try:
        return str(container.getDataType())
    except Exception:  # noqa: BLE001
        return ""


_MAX_LINK_HOPS = 16
_MAX_RESOURCE_ITERATIONS = 200_000


def _resolve_container(container: Any) -> Optional[Any]:
    hops = 0
    seen_links: set[str] = set()
    while container is not None:
        if hops >= _MAX_LINK_HOPS:
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
            link_key = _s(link.getOriginalName())
        except Exception:  # noqa: BLE001
            link_key = _s(link)
        if link_key in seen_links:
            return None
        seen_links.add(link_key)
        try:
            container = link.loadContent()
        except Exception:  # noqa: BLE001
            return None
        hops += 1
    return None


def _container_to_text(container: Any) -> Optional[str]:
    container = _resolve_container(container)
    if container is None:
        return None
    dt = _container_type(container)
    if dt == "TEXT":
        try:
            text = container.getText()
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            return None
    return None


class _ResourceTooLarge(Exception):
    def __init__(self, size: int):
        super().__init__(f"resource is {size} bytes")
        self.size = size


def _container_to_content(
    container: Any, max_bytes: int
) -> Optional[Dict[str, Any]]:
    container = _resolve_container(container)
    if container is None:
        return None
    dt = _container_type(container)
    if dt == "TEXT":
        try:
            text = container.getText()
        except Exception:  # noqa: BLE001
            return None
        if text is None:
            return None
        java_text = text.getCodeStr()
        java_length = int(java_text.length())
        if java_length > max_bytes:
            raise _ResourceTooLarge(java_length)
        content = _s(java_text)
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > max_bytes:
            raise _ResourceTooLarge(encoded_size)
        return {
            "encoding": "text",
            "data_type": dt,
            "size": encoded_size,
            "content": content,
        }
    if dt == "DECODED_DATA":
        try:
            data = container.getDecodedData()
        except Exception:  # noqa: BLE001
            return None
        if data is None:
            return None
        data_length = len(data)
        if data_length > max_bytes:
            raise _ResourceTooLarge(data_length)
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


def _iter_top_resources(decompiler: Any) -> Iterator[Tuple[str, Any]]:
    resources = decompiler.getResources()
    for i in range(int(resources.size())):
        resource = resources.get(i)
        yield _s(resource.getOriginalName()), resource


def _iter_subresources(container: Any) -> Iterator[Tuple[str, Any]]:
    iterations = 0
    stack: List[Any] = [container]
    while stack:
        container = stack.pop()
        try:
            subs = container.getSubFiles()
        except Exception:  # noqa: BLE001
            continue
        if subs is None:
            continue
        sn = int(subs.size())
        for j in range(sn):
            iterations += 1
            if iterations > _MAX_RESOURCE_ITERATIONS:
                log.warning(
                    "Resource walk hit iteration cap (%d)",
                    _MAX_RESOURCE_ITERATIONS,
                )
                return
            sub = subs.get(j)
            try:
                sub_name = _s(sub.getName())
            except Exception:  # noqa: BLE001
                sub_name = ""
            yield (sub_name, sub)
            if _container_type(sub) == "RES_TABLE":
                stack.append(sub)


def _load_resource(resource: Any) -> Optional[Any]:
    try:
        return resource.loadContent()
    except Exception:  # noqa: BLE001
        return None


def _iter_generated_resources(decompiler: Any) -> Iterator[Tuple[str, Any]]:
    for _, resource in _iter_top_resources(decompiler):
        try:
            if _s(resource.getType()) != "ARSC":
                continue
        except Exception:  # noqa: BLE001
            continue
        content = _load_resource(resource)
        if content is not None:
            yield from _iter_subresources(content)


def _iter_resource_names(decompiler: Any, recursive: bool) -> Iterator[str]:
    for name, _ in _iter_top_resources(decompiler):
        yield name
    if recursive:
        for name, _ in _iter_generated_resources(decompiler):
            yield name


def _find_resource(decompiler: Any, name: str) -> Optional[Tuple[str, Any]]:
    suffix_hit: Optional[Tuple[str, Any]] = None
    for cname, resource in _iter_top_resources(decompiler):
        if cname == name:
            return (cname, _load_resource(resource))
        if suffix_hit is None and cname.endswith(name):
            suffix_hit = (cname, resource)
    for cname, container in _iter_generated_resources(decompiler):
        if cname == name:
            return (cname, container)
        if suffix_hit is None and cname.endswith(name):
            suffix_hit = (cname, container)
    if suffix_hit is not None and hasattr(suffix_hit[1], "loadContent"):
        return (suffix_hit[0], _load_resource(suffix_hit[1]))
    return suffix_hit


def _read_zip_entry(
    apk_path: str, name: str, max_bytes: int
) -> Optional[Tuple[str, Optional[bytes], int]]:
    if not apk_path or not os.path.exists(apk_path):
        return None
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            try:
                info = z.getinfo(name)
                if info.file_size > max_bytes:
                    return (name, None, info.file_size)
                return (name, z.read(info), info.file_size)
            except KeyError:
                pass
            for info in z.infolist():
                if info.filename.endswith(name):
                    if info.file_size > max_bytes:
                        return (info.filename, None, info.file_size)
                    return (info.filename, z.read(info), info.file_size)
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _bytes_to_payload(raw: bytes) -> Dict[str, Any]:
    try:
        return {"encoding": "text", "content": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode("ascii"),
        }


@dataclass
class Session:
    decompiler: Any
    apk_path: str
    cache_dir: str

    def get_class(self, fqn: str) -> Optional[Any]:
        d = self.decompiler
        candidates = tuple(
            dict.fromkeys((fqn, fqn.replace("/", "."), fqn.replace(".", "/")))
        )
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
        shutil.rmtree(self.cache_dir, ignore_errors=True)


SESSION: Optional[Session] = None
LAST_ACTIVITY = time.monotonic()
SHUTDOWN = threading.Event()
STATE_LOCK = threading.Lock()
IN_FLIGHT = 0
CLOSING = False


def _touch() -> None:
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.monotonic()


def _idle_timeout_exempt_now() -> bool:
    if not OVERNIGHT_IDLE_EXEMPTION:
        return False
    hour = datetime.datetime.now().hour
    return 0 <= hour < 6


@contextlib.contextmanager
def _request_activity() -> Iterator[None]:
    global IN_FLIGHT
    with STATE_LOCK:
        if CLOSING:
            raise RuntimeError("session is closing")
        IN_FLIGHT += 1
        _touch()
    try:
        yield
    finally:
        with STATE_LOCK:
            IN_FLIGHT -= 1
            _touch()


def _idle_watchdog() -> None:
    global CLOSING
    while not SHUTDOWN.wait(timeout=30):
        with STATE_LOCK:
            if SESSION is None or IN_FLIGHT or CLOSING:
                continue
            if _idle_timeout_exempt_now():
                continue
            if time.monotonic() - LAST_ACTIVITY < IDLE_TIMEOUT_SECONDS:
                continue
            CLOSING = True
        log.info("idle timeout reached, shutting down worker")
        try:
            _close()
        finally:
            os._exit(0)


def _require() -> Session:
    if SESSION is None:
        raise RuntimeError("session not loaded")
    return SESSION


def _load_apk(
    apk_path: str,
    code_data: Optional[Any] = None,
    project_data: Optional[Any] = None,
) -> Dict[str, Any]:
    global SESSION
    _touch()
    if not os.path.exists(apk_path):
        return _err(f"File not found: {apk_path}")
    args = JadxArgs()
    args.setInputFile(File(apk_path))
    args.setUsageInfoCache(EmptyUsageInfoCache())
    args.setCodeData(code_data if code_data is not None else JadxCodeData())
    if project_data is not None:
        args.getPluginOptions().putAll(project_data.getPluginOptions())
        mappings_path = project_data.getMappingsPath()
        if mappings_path is not None:
            args.setUserRenamesMappingsPath(mappings_path)
    args.setThreadsCount(JADX_THREADS)
    cache_dir = tempfile.mkdtemp(prefix="jadx_cache_")
    decompiler = JadxDecompiler(args)
    decompiler.addCustomPass(_make_disk_cache_pass(cache_dir))
    try:
        decompiler.load()
    except Exception:
        try:
            decompiler.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(cache_dir, ignore_errors=True)
        raise
    SESSION = Session(decompiler=decompiler, apk_path=apk_path, cache_dir=cache_dir)
    _touch()
    return _ok(apk_path=apk_path, class_count=int(decompiler.getClasses().size()))


def _load_jadx_project(project_path: str) -> Dict[str, Any]:
    project_path = os.path.realpath(project_path)
    project_data = _read_jadx_project(project_path)
    apk_path = os.path.realpath(_s(project_data.getFiles().get(0)))
    code_data = project_data.getCodeData()
    if code_data is None:
        code_data = JadxCodeData()
    result = _load_apk(apk_path, code_data=code_data, project_data=project_data)
    if result.get("ok"):
        result.update(
            project_path=project_path,
            restored_renames=int(code_data.getRenames().size()),
            project_version=int(project_data.getProjectVersion()),
        )
    return result


def _close() -> Dict[str, Any]:
    global CLOSING, SESSION
    SHUTDOWN.set()
    with STATE_LOCK:
        CLOSING = True
        session = SESSION
        SESSION = None
    if session is not None:
        session.close()
    if jpype.isJVMStarted():
        try:
            jpype.shutdownJVM()
        except Exception:  # noqa: BLE001
            pass
    return _ok(closed=True)


def _validate_identifier(name: str, allow_full: bool = False) -> str:
    name = name.strip()
    valid = (
        NameMapper.isValidFullIdentifier(name)
        if allow_full
        else NameMapper.isValidIdentifier(name)
    )
    if not name or not valid:
        kind = "qualified Java name" if allow_full else "Java identifier"
        raise ValueError(f"'{name}' is not a valid {kind}")
    if not allow_full and NameMapper.isReserved(name):
        raise ValueError(f"'{name}' is a reserved Java identifier")
    if allow_full and any(NameMapper.isReserved(part) for part in name.split(".")):
        raise ValueError(f"'{name}' contains a reserved Java identifier")
    return name


def _method_details(method: Any) -> Dict[str, Any]:
    info = method.getMethodNode().getMethodInfo()
    return {
        "name": _s(method.getName()),
        "raw_name": _s(info.getName()),
        "signature": _s(method.toString()),
        "short_id": _s(info.getShortId()),
        "raw_full_id": _s(info.getRawFullId()),
    }


def _field_details(field: Any) -> Dict[str, Any]:
    info = field.getFieldNode().getFieldInfo()
    return {
        "name": _s(field.getName()),
        "raw_name": _s(info.getName()),
        "signature": _s(field.toString()),
        "short_id": _s(info.getShortId()),
        "raw_full_id": _s(info.getRawFullId()),
    }


def _select_method(
    cls: Any, method_name: str, signature: Optional[str]
) -> Any:
    candidates = []
    for method in cls.getMethods():
        details = _method_details(method)
        if method_name not in {details["name"], details["raw_name"]}:
            continue
        if signature and signature not in {
            details["signature"],
            details["short_id"],
            details["raw_full_id"],
        }:
            continue
        candidates.append((method, details))
    if not candidates:
        raise ValueError(
            f"Method '{method_name}' not found in '{_s(cls.getFullName())}'"
            + (f" matching '{signature}'" if signature else "")
        )
    if len(candidates) > 1:
        signatures = [details["short_id"] for _, details in candidates]
        raise ValueError(
            f"Method '{method_name}' is overloaded; pass signature as one of: "
            + ", ".join(signatures)
        )
    return candidates[0][0]


def _select_field(cls: Any, field_name: str, signature: Optional[str]) -> Any:
    candidates = []
    for field in cls.getFields():
        details = _field_details(field)
        if field_name not in {details["name"], details["raw_name"]}:
            continue
        if signature and signature not in {
            details["signature"],
            details["short_id"],
            details["raw_full_id"],
        }:
            continue
        candidates.append((field, details))
    if not candidates:
        raise ValueError(
            f"Field '{field_name}' not found in '{_s(cls.getFullName())}'"
            + (f" matching '{signature}'" if signature else "")
        )
    if len(candidates) > 1:
        signatures = [details["short_id"] for _, details in candidates]
        raise ValueError(
            f"Field '{field_name}' is ambiguous; pass signature as one of: "
            + ", ".join(signatures)
        )
    return candidates[0][0]


def _code_data() -> Any:
    args = _require().decompiler.getArgs()
    data = args.getCodeData()
    if data is None:
        data = JadxCodeData()
        args.setCodeData(data)
    return data


def _rename_to_dict(rename: Any) -> Dict[str, Any]:
    node_ref = rename.getNodeRef()
    code_ref = rename.getCodeRef()
    return {
        "node_type": _s(node_ref.getType()),
        "declaring_class": _s(node_ref.getDeclaringClass()),
        "short_id": _s(node_ref.getShortId()) or None,
        "code_type": _s(code_ref.getAttachType()) if code_ref is not None else None,
        "code_index": int(code_ref.getIndex()) if code_ref is not None else None,
        "new_name": _s(rename.getNewName()),
    }


def _apply_rename(rename: Any, affected_nodes: List[Any]) -> Dict[str, Any]:
    session = _require()
    data = _code_data()
    renames = ArrayList(data.getRenames())
    renames.removeAll(Collections.singleton(rename))
    renames.add(rename)
    Collections.sort(renames)
    data.setRenames(renames)
    session.decompiler.getArgs().setCodeData(data)
    session.decompiler.reloadCodeData()

    top_classes: Dict[str, Any] = {}
    for node in affected_nodes:
        if node is None:
            continue
        try:
            top = node.getTopParentClass()
            top_classes[_s(top.getRawName())] = top
        except Exception:  # noqa: BLE001
            continue
    for top in top_classes.values():
        top.unload()
    return _ok(rename=_rename_to_dict(rename), invalidated_classes=len(top_classes))


def _affected_nodes(node: Any, include_usages: bool = True) -> List[Any]:
    nodes = [node]
    if include_usages:
        try:
            nodes.extend(list(node.getUseIn()))
        except Exception:  # noqa: BLE001
            pass
    return nodes


def _method_variables(method: Any) -> List[Dict[str, Any]]:
    top = method.getTopParentClass()
    code_info = top.getCodeInfo()
    method_node = method.getMethodNode()
    variables: Dict[Tuple[int, int], Dict[str, Any]] = {}
    metadata = code_info.getCodeMetadata().getAsMap()
    for entry in metadata.entrySet():
        annotation = entry.getValue()
        if not NodeDeclareRef.class_.isInstance(annotation):
            continue
        node = annotation.getNode()
        if not VarNode.class_.isInstance(node) or not node.getMth().equals(method_node):
            continue
        key = (int(node.getReg()), int(node.getSsa()))
        variables[key] = {
            "kind": "local",
            "argument_index": None,
            "register": key[0],
            "ssa": key[1],
            "name": _s(node.getName()),
            "type": _s(node.getType()),
            "definition_position": int(node.getDefPosition()),
        }

    arg_types = method_node.getMethodInfo().getArgumentsTypes()
    arg_width = sum(
        int(arg_types.get(index).getRegCount())
        for index in range(int(arg_types.size()))
    )
    register = int(method_node.getRegsCount()) - arg_width
    for index in range(int(arg_types.size())):
        matching_keys = sorted(key for key in variables if key[0] == register)
        if matching_keys:
            variable = variables[matching_keys[0]]
            variable["kind"] = "argument"
            variable["argument_index"] = index
        register += int(arg_types.get(index).getRegCount())
    return sorted(
        variables.values(),
        key=lambda item: (
            item["kind"] != "argument",
            item["argument_index"] if item["argument_index"] is not None else 0,
            item["register"],
            item["ssa"],
        ),
    )


def _unload_all_classes() -> None:
    classes = _require().decompiler.getClasses()
    for index in range(int(classes.size())):
        classes.get(index).unload()


def _project_gson(project_path: str) -> Any:
    project_dir = Paths.get(project_path).toAbsolutePath().getParent()
    builder = GsonUtils.defaultGsonBuilder()
    builder.registerTypeHierarchyAdapter(
        JavaPath.class_, RelativePathTypeAdapter(project_dir)
    )
    for interface, implementation in (
        (ICodeComment, JadxCodeComment),
        (ICodeRename, JadxCodeRename),
        (IJavaNodeRef, JadxNodeRef),
        (IJavaCodeRef, JadxCodeRef),
    ):
        builder.registerTypeAdapter(
            interface.class_, GsonUtils.interfaceReplace(implementation.class_)
        )
    return builder.create()


def _normalize_project_path(project_path: str) -> str:
    project_path = os.path.realpath(project_path)
    if not project_path.lower().endswith(".jadx"):
        project_path += ".jadx"
    return project_path


def _write_jadx_project(project_path: str) -> str:
    session = _require()
    project_path = _normalize_project_path(project_path)
    project_dir = os.path.dirname(project_path)
    os.makedirs(project_dir, exist_ok=True)

    data = ProjectData()
    files = ArrayList()
    files.add(Paths.get(session.apk_path))
    data.setFiles(files)
    data.setCodeData(_code_data())
    data.getPluginOptions().putAll(session.decompiler.getArgs().getPluginOptions())
    mappings_path = session.decompiler.getArgs().getUserRenamesMappingsPath()
    if mappings_path is not None:
        data.setMappingsPath(mappings_path)

    gson = _project_gson(project_path)
    project_json = _s(gson.toJson(data))
    fd, temp_path = tempfile.mkstemp(
        prefix=".jadx-project-", suffix=".jadx", dir=project_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as project_file:
            project_file.write(project_json)
            project_file.write("\n")
        loaded = JadxProject.loadProjectData(Paths.get(temp_path))
        if loaded is None or int(loaded.getProjectVersion()) != int(data.getProjectVersion()):
            raise ValueError("Native JADX project validation failed")
        os.replace(temp_path, project_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return project_path


def _read_jadx_project(project_path: str) -> Any:
    project_path = os.path.realpath(project_path)
    if not os.path.isfile(project_path):
        raise FileNotFoundError(f"JADX project not found: {project_path}")
    data = JadxProject.loadProjectData(Paths.get(project_path))
    if data is None:
        raise ValueError(f"Failed to load JADX project: {project_path}")
    files = data.getFiles()
    if files is None or int(files.size()) != 1:
        count = 0 if files is None else int(files.size())
        raise ValueError(
            f"This isolated worker requires exactly one project input file; found {count}"
        )
    return data


def _load_project_into_session(project_path: str) -> Dict[str, Any]:
    session = _require()
    project_path = os.path.realpath(project_path)
    data = _read_jadx_project(project_path)
    project_apk = os.path.realpath(_s(data.getFiles().get(0)))
    if project_apk != os.path.realpath(session.apk_path):
        return _err(
            "JADX project input does not match the loaded APK",
            project_apk=project_apk,
            loaded_apk=session.apk_path,
        )
    code_data = data.getCodeData()
    if code_data is None:
        code_data = JadxCodeData()
    session.decompiler.getArgs().setCodeData(code_data)
    session.decompiler.getArgs().getPluginOptions().putAll(data.getPluginOptions())
    session.decompiler.reloadCodeData()
    _unload_all_classes()
    return _ok(
        project_path=project_path,
        rename_count=int(code_data.getRenames().size()),
        project_version=int(data.getProjectVersion()),
    )


def get_all_classes(offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    s = _require()
    jlist = s.decompiler.getClasses()
    total = int(jlist.size())
    offset, limit, end = _page_bounds(offset, limit, total)
    page = [str(jlist.get(i).getFullName()) for i in range(offset, end)]
    return _ok(total=total, offset=offset, limit=limit, returned=len(page), items=page)


def search_class_names(keyword: str, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    s = _require()
    k = keyword.lower()
    jlist = s.decompiler.getClasses()
    n = int(jlist.size())
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    page: List[str] = []
    total = 0
    for i in range(n):
        name = str(jlist.get(i).getFullName())
        if k in name.lower():
            if offset <= total < offset + limit:
                page.append(name)
            total += 1
    return _ok(total=total, offset=offset, limit=limit, returned=len(page), items=page)


def get_class_source(class_name: str) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    return _ok(class_name=class_name, code=_s(cls.getCode()))


def get_smali_source(class_name: str) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    getter = getattr(cls, "getSmali", None)
    if getter is None:
        return _err("This jadx build does not expose JavaClass.getSmali(); smali output unavailable.")
    try:
        smali = getter()
    except Exception as e:  # noqa: BLE001
        return _err(f"Failed to generate smali: {e}")
    return _ok(class_name=class_name, smali=_s(smali))


def get_methods_of_class(class_name: str) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    methods = [_method_details(method) for method in cls.getMethods()]
    return _ok(class_name=class_name, methods=methods)


def get_fields_of_class(class_name: str) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    fields = [_field_details(field) for field in cls.getFields()]
    return _ok(class_name=class_name, fields=fields)


def list_method_variables(
    class_name: str, method_name: str, signature: Optional[str] = None
) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        method = _select_method(cls, method_name, signature)
        return _ok(
            class_name=class_name,
            method=_method_details(method),
            variables=_method_variables(method),
        )
    except ValueError as e:
        return _err(str(e))


def get_renames() -> Dict[str, Any]:
    renames = _code_data().getRenames()
    items = [
        _rename_to_dict(renames.get(index))
        for index in range(int(renames.size()))
    ]
    return _ok(count=len(items), renames=items)


def rename_class(class_name: str, new_name: str) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        new_name = _validate_identifier(new_name, allow_full=not bool(cls.isInner()))
        affected = _affected_nodes(cls)
        rename = JadxCodeRename(JadxNodeRef.forCls(cls), new_name)
        result = _apply_rename(rename, affected)
        result.update(class_name=class_name, new_name=new_name)
        return result
    except ValueError as e:
        return _err(str(e))


def rename_method(
    class_name: str,
    method_name: str,
    new_name: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        new_name = _validate_identifier(new_name)
        method = _select_method(cls, method_name, signature)
        if method.isConstructor():
            return _err("Rename the declaring class to rename its constructor")
        if method.isClassInit():
            return _err("Static class initializers cannot be renamed")
        affected = _affected_nodes(method)
        for related in method.getOverrideRelatedMethods():
            affected.extend(_affected_nodes(related))
        rename = JadxCodeRename(JadxNodeRef.forMth(method), new_name)
        result = _apply_rename(rename, affected)
        result.update(method=_method_details(method), new_name=new_name)
        return result
    except ValueError as e:
        return _err(str(e))


def rename_field(
    class_name: str,
    field_name: str,
    new_name: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        new_name = _validate_identifier(new_name)
        field = _select_field(cls, field_name, signature)
        affected = _affected_nodes(field)
        rename = JadxCodeRename(JadxNodeRef.forFld(field), new_name)
        result = _apply_rename(rename, affected)
        result.update(field=_field_details(field), new_name=new_name)
        return result
    except ValueError as e:
        return _err(str(e))


def rename_method_argument(
    class_name: str,
    method_name: str,
    argument_index: int,
    new_name: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        new_name = _validate_identifier(new_name)
        method = _select_method(cls, method_name, signature)
        argument_index = int(argument_index)
        arg_count = int(method.getMethodNode().getMethodInfo().getArgsCount())
        if not 0 <= argument_index < arg_count:
            return _err(
                f"argument_index must be between 0 and {max(0, arg_count - 1)}",
                argument_count=arg_count,
            )
        rename = JadxCodeRename(
            JadxNodeRef.forMth(method),
            JadxCodeRef.forMthArg(argument_index),
            new_name,
        )
        result = _apply_rename(rename, [method])
        result.update(
            method=_method_details(method),
            argument_index=argument_index,
            new_name=new_name,
        )
        return result
    except ValueError as e:
        return _err(str(e))


def rename_local_variable(
    class_name: str,
    method_name: str,
    register: int,
    ssa: int,
    new_name: str,
    signature: Optional[str] = None,
) -> Dict[str, Any]:
    session = _require()
    cls = session.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    try:
        new_name = _validate_identifier(new_name)
        method = _select_method(cls, method_name, signature)
        register = int(register)
        ssa = int(ssa)
        variables = _method_variables(method)
        selected = next(
            (
                variable
                for variable in variables
                if variable["register"] == register and variable["ssa"] == ssa
            ),
            None,
        )
        if selected is None:
            return _err(
                f"Variable r{register}v{ssa} not found; call list_method_variables first"
            )
        rename = JadxCodeRename(
            JadxNodeRef.forMth(method),
            JadxCodeRef.forVar(register, ssa),
            new_name,
        )
        result = _apply_rename(rename, [method])
        result.update(method=_method_details(method), variable=selected, new_name=new_name)
        return result
    except ValueError as e:
        return _err(str(e))


def get_method_by_name(class_name: str, method_name: str, signature: Optional[str] = None) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    cls.getCode()
    overloads: List[Dict[str, Any]] = []
    for m in cls.getMethods():
        if _s(m.getName()) != method_name:
            continue
        sig = _s(m.toString())
        if signature and signature not in sig and signature != sig:
            continue
        code = m.getCodeStr()
        overloads.append({"signature": sig, "code": _s(code) if code else None})
    if not overloads:
        return _err(
            f"Method '{method_name}' not found in '{class_name}'"
            + (f" matching '{signature}'" if signature else "")
        )
    return _ok(class_name=class_name, method_name=method_name, overloads=overloads)


def search_classes_by_keyword(keyword: str, limit: int = 50, case_sensitive: bool = True) -> Dict[str, Any]:
    s = _require()
    limit = max(1, min(int(limit), 1000))
    flags = 0 if case_sensitive else Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE
    pattern = Pattern.compile(Pattern.quote(keyword), flags)
    jlist = s.decompiler.getClasses()
    n = int(jlist.size())
    search_workers = min(SEARCH_THREADS, JADX_THREADS)
    _, _, max_heap = _heap_stats()
    minimum_headroom = int(max_heap * 0.08)
    matches: List[str] = []
    stop = threading.Event()
    scanned_to = 0

    def _decompile_and_check(i: int) -> Optional[str]:
        if stop.is_set():
            return None
        cls = jlist.get(i)
        code = cls.getClassNode().getCode().getCodeStr()
        if stop.is_set():
            return None
        if pattern.matcher(code).find():
            return str(cls.getFullName())
        return None

    batch_size = search_workers * 4
    with ThreadPoolExecutor(max_workers=search_workers) as pool:
        for start in range(0, n, batch_size):
            if stop.is_set():
                break
            free, committed, _ = _heap_stats()
            used = committed - free
            if max_heap - used < minimum_headroom:
                _gc()
                free, committed, _ = _heap_stats()
                used = committed - free
                if max_heap - used < minimum_headroom:
                    log.warning(
                        "JVM heap critically low (%d / %d headroom); aborting search at class %d/%d",
                        max_heap - used, max_heap, start, n,
                    )
                    break
            end = min(start + batch_size, n)
            futures = [pool.submit(_decompile_and_check, i) for i in range(start, end)]
            for fut in as_completed(futures):
                if stop.is_set():
                    break
                name = fut.result()
                if name is not None:
                    matches.append(name)
                    if len(matches) >= limit:
                        stop.set()
                        for pending in futures:
                            pending.cancel()
                        break
            scanned_to = end
    if (
        scanned_to >= n
        and os.environ.get("JADX_GC_AFTER_FULL_SEARCH", "1").lower()
        not in {"0", "false", "no"}
    ):
        _gc()
    return _ok(keyword=keyword, matches=matches, truncated=len(matches) >= limit)


def xrefs_to_class(class_name: str) -> Dict[str, Any]:
    s = _require()
    cls = s.get_class(class_name)
    if cls is None:
        return _err(f"Class '{class_name}' not found")
    refs = [_s(n.toString()) for n in cls.getUseIn()]
    return _ok(class_name=class_name, count=len(refs), xrefs=refs)


def xrefs_to_method(class_name: str, method_name: str, signature: Optional[str] = None) -> Dict[str, Any]:
    s = _require()
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
        grouped.append({"signature": sig, "xrefs": [_s(n.toString()) for n in m.getUseIn()]})
    if not grouped:
        return _err(f"Method '{method_name}' not found in '{class_name}'")
    return _ok(class_name=class_name, method_name=method_name, overloads=grouped)


def get_android_manifest() -> Dict[str, Any]:
    s = _require()
    hit = _find_resource(s.decompiler, "AndroidManifest.xml")
    if hit is None:
        return _err("AndroidManifest.xml not found")
    name, container = hit
    text = _container_to_text(container)
    if text is None:
        return _err(f"Could not extract text from '{name}'")
    return _ok(name=name, content=text)


def get_strings() -> Dict[str, Any]:
    s = _require()
    for target in ("res/values/strings.xml", "strings.xml"):
        hit = _find_resource(s.decompiler, target)
        if hit is None:
            continue
        name, container = hit
        text = _container_to_text(container)
        if text is not None:
            return _ok(name=name, content=text)
    return _err("strings.xml not found")


def get_resource(resource_name: str, max_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
    s = _require()
    max_bytes = max(1, int(max_bytes))
    jadx_name: Optional[str] = None
    jadx_dtype: Optional[str] = None
    hit = _find_resource(s.decompiler, resource_name)
    if hit is not None:
        jadx_name, container = hit
        jadx_dtype = _container_type(container)
        try:
            result = _container_to_content(container, max_bytes)
        except _ResourceTooLarge as e:
            return _err(
                f"Resource '{jadx_name}' is {e.size} bytes; exceeds "
                f"max_bytes={max_bytes}. Increase max_bytes to retrieve.",
                name=jadx_name,
                size=e.size,
            )
        if result is not None:
            return _ok(name=jadx_name, source="jadx", **result)
    for candidate in (resource_name, jadx_name):
        if not candidate:
            continue
        entry = _read_zip_entry(s.apk_path, candidate, max_bytes)
        if entry is None:
            continue
        matched_name, raw, size = entry
        if raw is None:
            return _err(
                f"Resource '{matched_name}' is {size} bytes; exceeds max_bytes={max_bytes}. Increase max_bytes to retrieve.",
                name=matched_name,
                size=size,
            )
        payload = _bytes_to_payload(raw)
        return _ok(name=matched_name, source="zip", **payload)
    if hit is None:
        return _err(f"Resource '{resource_name}' not found")
    return _err(
        f"Resource '{jadx_name}' could not be extracted",
        name=jadx_name,
        data_type=jadx_dtype,
    )


def get_all_resource_file_names(offset: int = 0, limit: int = 500, recursive: bool = False) -> Dict[str, Any]:
    s = _require()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    page: List[str] = []
    total = 0
    for name in _iter_resource_names(s.decompiler, recursive):
        if offset <= total < offset + limit:
            page.append(name)
        total += 1
    return _ok(total=total, offset=offset, limit=limit, returned=len(page), items=page, recursive=recursive)


def search_resource_name(keyword: str, offset: int = 0, limit: int = 200, recursive: bool = True) -> Dict[str, Any]:
    s = _require()
    k = keyword.lower()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    page: List[str] = []
    total = 0
    for name in _iter_resource_names(s.decompiler, recursive):
        if k not in name.lower():
            continue
        if offset <= total < offset + limit:
            page.append(name)
        total += 1
    return _ok(total=total, offset=offset, limit=limit, returned=len(page), items=page, recursive=recursive)


def save_session(project_path: str) -> Dict[str, Any]:
    if not project_path.strip():
        return _err("project_path must not be empty")
    try:
        saved_path = _write_jadx_project(project_path)
        return _ok(
            project_path=saved_path,
            project_version=2,
            rename_count=int(_code_data().getRenames().size()),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("JADX project save failed")
        return _err(f"Failed to save JADX project: {e}", project_path=project_path)


def load_project_data(project_path: str) -> Dict[str, Any]:
    if not project_path.strip():
        return _err("project_path must not be empty")
    if not os.path.isfile(project_path):
        return _err(f"JADX project not found: {project_path}")
    try:
        return _load_project_into_session(project_path)
    except Exception as e:  # noqa: BLE001
        log.exception("JADX project restore failed")
        return _err(f"Failed to restore JADX project: {e}")


OPS = {
    "get_all_classes": get_all_classes,
    "search_class_names": search_class_names,
    "get_class_source": get_class_source,
    "get_smali_source": get_smali_source,
    "get_methods_of_class": get_methods_of_class,
    "get_fields_of_class": get_fields_of_class,
    "list_method_variables": list_method_variables,
    "get_renames": get_renames,
    "rename_class": rename_class,
    "rename_method": rename_method,
    "rename_field": rename_field,
    "rename_method_argument": rename_method_argument,
    "rename_local_variable": rename_local_variable,
    "get_method_by_name": get_method_by_name,
    "search_classes_by_keyword": search_classes_by_keyword,
    "xrefs_to_class": xrefs_to_class,
    "xrefs_to_method": xrefs_to_method,
    "get_android_manifest": get_android_manifest,
    "get_strings": get_strings,
    "get_resource": get_resource,
    "get_all_resource_file_names": get_all_resource_file_names,
    "search_resource_name": search_resource_name,
    "save_session": save_session,
    "load_project_data": load_project_data,
}


def _send(msg: Dict[str, Any]) -> None:
    json.dump(msg, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    threading.Thread(target=_idle_watchdog, name="idle-watchdog", daemon=True).start()
    if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and sys.argv[1] != "--project"):
        _send(_err("usage: worker.py <apk_path> | worker.py --project <project.jadx>"))
        raise SystemExit(2)
    try:
        try:
            if len(sys.argv) == 3:
                _send(_load_jadx_project(sys.argv[2]))
            else:
                _send(_load_apk(sys.argv[1]))
        except Exception as e:  # noqa: BLE001
            log.exception("worker startup failed")
            _send(_err(str(e)))
            raise SystemExit(1)
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
                op = req.pop("op")
                if op == "close":
                    _send(_close())
                    return
                fn = OPS.get(op)
                if fn is None:
                    _send(_err(f"unknown op: {op}"))
                    continue
                with _request_activity():
                    result = fn(**req)
                _send(result)
            except Exception as e:  # noqa: BLE001
                log.exception("worker request failed")
                try:
                    _send(_err(str(e)))
                except (BrokenPipeError, OSError):
                    return
    finally:
        _close()


if __name__ == "__main__":
    main()
