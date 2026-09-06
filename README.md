# Jadx MCP Server
[![MCP Function Benchmarks](https://github.com/yoni13/isolated_jadx_mcp/actions/workflows/benchmark.yml/badge.svg)](https://github.com/yoni13/isolated_jadx_mcp/actions/workflows/benchmark.yml)

A Model Context Protocol (MCP) server around the Jadx Android decompiler. Each APK is loaded in an isolated worker process and JVM so a failed or memory-heavy analysis does not poison unrelated sessions.

## Features

- **Process Isolation:** Analyze multiple APKs in independent worker JVMs.
- **Multi-Agent Reuse:** Session aliases for the same APK share one worker by default.
- **Bounded Resources:** Configurable worker, startup, CPU, heap, and request limits.
- **Resource Extraction:** Retrieve `AndroidManifest.xml`, `strings.xml`, and other resource files.
- **Advanced Analysis:** Search classes by keyword and find cross-references (xrefs) for classes and methods.

## Prerequisites

- **Java Runtime Environment (JRE):** Required by JPype to start the JVM.
- **Jadx JAR:** The server expects `jadx-dev-all.jar`. By default, it looks in:
  - `/usr/share/java/jadx-git/lib/jadx-dev-all.jar`
  - `/usr/local/share/jadx/lib/jadx-dev-all.jar`
  - `/opt/jadx/lib/jadx-dev-all.jar`
  - `~/jadx/lib/jadx-dev-all.jar`

## Installation

This project is managed by `uv`.

```bash
# Install dependencies
uv sync
```

## Running the Server

### 1. Standard MCP Mode (stdio)
Most MCP clients (like Claude Desktop) use the `stdio` transport.

```bash
uv run python server.py
```

### 2. SSE Mode (Web Server)
To run the server as a persistent background service using Server-Sent Events (SSE):

```bash
# Start in the foreground
uv run python server.py sse

# Start in the background (port 8000)
nohup uv run python server.py run --transport sse --port 8000 > server.log 2>&1 &
```

## Available Tools

- `load_apk(apk_path, session_id, project_path=None)`: Load an APK and optionally apply a native JADX project.
- `load_project(project_path, session_id)`: Load an APK directly from a native `.jadx` project.
- `save_session(session_id, project_path)`: Save a native `.jadx` project while keeping the worker open.
- `save_and_exit_session(session_id, project_path)`: Save a native `.jadx` project and close the session alias.
- `get_all_classes(session_id)`: List all class names.
- `get_class_source(class_name, session_id)`: Decompile and return class source code.
- `get_method_by_name(class_name, method_name, session_id)`: Get source of a specific method.
- `get_android_manifest(session_id)`: Retrieve the `AndroidManifest.xml`.
- `search_classes_by_keyword(keyword, session_id)`: Search for code patterns.
- `xrefs_to_class(class_name, session_id)`: Find where a class is used.
- `xrefs_to_method(class_name, method_name, session_id)`: Find method invocations.
- `get_renames(session_id)`: List persistent renames in the session.
- `rename_class(class_name, new_name, session_id)`: Rename a class.
- `rename_method(class_name, method_name, new_name, session_id, signature=None)`: Rename a method/function.
- `rename_field(class_name, field_name, new_name, session_id, signature=None)`: Rename a field.
- `list_method_variables(class_name, method_name, session_id, signature=None)`: Get argument indexes and local register/SSA selectors.
- `rename_method_argument(class_name, method_name, argument_index, new_name, session_id, signature=None)`: Rename an argument.
- `rename_local_variable(class_name, method_name, register, ssa, new_name, session_id, signature=None)`: Rename a local variable.
- `close_session(session_id)`: Dispose of the decompiler and free memory.

## Saving And Renaming

`save_session` writes JADX's native project version 2 format. A missing `.jadx` suffix is added automatically. The project stores the input APK path, code-data renames, mappings path, and plugin options using the same relative-path and Gson adapters as jadx-gui. Open it directly in jadx-gui or restore it with `load_project`.

Project saving does not export decompiled source files.

Methods and fields now include `short_id` in their listing responses. Pass that value as `signature` when a name is overloaded or ambiguous. Call `list_method_variables` before renaming variables:

- Arguments use the returned zero-based `argument_index`.
- Locals use the returned `register` and `ssa` pair.
- Constructors are renamed by renaming their declaring class.
- Static class initializers cannot be renamed.

Aliases that share one APK worker also share live renames. Set `JADX_REUSE_APK_SESSIONS=0` when agents require independent edit states for the same APK.

## Resource Tuning

The defaults target concurrent agent use while retaining enough heap for large APKs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `JADX_MAX_SESSIONS` | `3` | Maximum distinct worker JVMs. Shared aliases count once. |
| `JADX_MAX_CONCURRENT_STARTUPS` | `2` | Maximum workers loading at the same time. |
| `JADX_REUSE_APK_SESSIONS` | `1` | Share one worker when multiple IDs load the same canonical APK path. |
| `JADX_HEAP` | `8g` | Maximum heap per worker. |
| `JADX_INITIAL_HEAP` | `128m` | Initial heap per worker. |
| `JADX_ACTIVE_PROCESSORS` | `4` | CPU count exposed to JVM GC/JIT ergonomics. |
| `JADX_THREADS` | `4` | Jadx processing and disk-cache worker threads. |
| `JADX_SEARCH_THREADS` | `2` | Parallel class-source search threads. |
| `JADX_CODE_CACHE` | `disk` | Memory-first disk cache; use `buffered` for faster repeated reads. |
| `JADX_IDLE_TIMEOUT_SECONDS` | `1200` | Worker idle timeout. |
| `JADX_OVERNIGHT_IDLE_EXEMPTION` | `1` | Preserve the `00:00` to `06:00` idle-exit exemption. Set `0` for strict memory reclamation. |
| `JADX_REQUEST_QUEUE_TIMEOUT_SECONDS` | `2` | Time a same-session call waits before returning a retryable busy error. |
| `JADX_REQUEST_TIMEOUT_SECONDS` | `900` | Terminate a worker if one operation exceeds this deadline. |
| `JADX_STARTUP_TIMEOUT_SECONDS` | `600` | Terminate a worker that does not finish loading in time. |

Calls to one worker are serialized because Jadx and the worker protocol are single-session. Calls to different APK workers run concurrently. `list_sessions` reports aliases, active operations, worker PIDs, and Linux RSS so agents can coordinate cleanup.
