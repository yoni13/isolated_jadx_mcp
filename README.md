# Jadx MCP Server

A comprehensive Model Context Protocol (MCP) server that acts as a wrapper around the Jadx Android decompiler. It uses `JPype1` to load the `jadx-core` logic directly into the Python process, allowing for completely in-memory and headless APK analysis.

## Features

- **In-Memory Decompilation:** Decompile APKs without writing source files to disk.
- **Session Isolation:** Analyze multiple APKs simultaneously using unique session IDs.
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

- `load_apk(apk_path, session_id)`: Load an APK into a session.
- `get_all_classes(session_id)`: List all class names.
- `get_class_source(class_name, session_id)`: Decompile and return class source code.
- `get_method_by_name(class_name, method_name, session_id)`: Get source of a specific method.
- `get_android_manifest(session_id)`: Retrieve the `AndroidManifest.xml`.
- `search_classes_by_keyword(keyword, session_id)`: Search for code patterns.
- `xrefs_to_class(class_name, session_id)`: Find where a class is used.
- `xrefs_to_method(class_name, method_name, session_id)`: Find method invocations.
- `close_session(session_id)`: Dispose of the decompiler and free memory.

