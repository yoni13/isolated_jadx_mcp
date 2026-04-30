#!/usr/bin/env python3
"""
Benchmark every MCP tool exposed by server.py against a real APK.

Required environment variables
--------------------------------
  JADX_JAR   – absolute path to jadx-dev-all.jar
  APK_PATH   – path to the APK to analyse (default: /tmp/newpipe.apk)

Optional
--------
  BENCH_RUNS    – timed iterations for "fast" functions (default: 3)
  RESULTS_PATH  – where to write the JSON results   (default: benchmark_results.json)

Measured functions (in order)
------------------------------
  load_apk                          – load APK / start jadx (once, no warm-up)
  list_sessions
  get_all_classes
  search_class_names
  get_class_source
  get_smali_source
  get_methods_of_class
  get_fields_of_class
  get_method_by_name
  search_classes_by_keyword         – decompiles all not-yet-compiled classes; slow
  xrefs_to_class
  xrefs_to_method
  get_android_manifest
  get_strings
  get_resource
  get_all_resource_file_names (flat)
  get_all_resource_file_names (recursive)
  search_resource_name
  close_session                     – once, no warm-up
"""

from __future__ import annotations

import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# sys.path – add the project root (parent of this benchmarks/ directory) so
# that "import server" resolves to server.py sitting next to pyproject.toml.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Read configuration from the environment
# ---------------------------------------------------------------------------
APK_PATH: str = os.environ.get("APK_PATH", "/tmp/newpipe.apk")
BENCH_RUNS: int = int(os.environ.get("BENCH_RUNS", "3"))
RESULTS_PATH: Path = Path(os.environ.get("RESULTS_PATH", "benchmark_results.json"))
GITHUB_STEP_SUMMARY: str = os.environ.get("GITHUB_STEP_SUMMARY", "")

# Set JADX_JAR *before* importing server.py (which starts the JVM at import time).
jadx_jar = os.environ.get("JADX_JAR", "")
if jadx_jar:
    os.environ["JADX_JAR"] = jadx_jar

# ---------------------------------------------------------------------------
# Validate inputs before touching the JVM
# ---------------------------------------------------------------------------
if not os.path.exists(APK_PATH):
    print(f"[FATAL] APK not found: {APK_PATH}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Import server – starts JVM as a side-effect
# ---------------------------------------------------------------------------
SEP = "─" * 78
print(SEP)
print("  Jadx MCP Benchmark")
print(f"  APK : {APK_PATH}")
print(f"  Runs: {BENCH_RUNS} (+ 1 warm-up) for fast functions")
print(SEP)
print()
print("⏳  Loading server module (starts JVM and locates jadx jar) …", flush=True)

_jvm_t0 = time.perf_counter()
import server as _server  # noqa: E402
_jvm_ms = (time.perf_counter() - _jvm_t0) * 1000

print(f"✅  Server ready  ({_jvm_ms:,.0f} ms)\n", flush=True)

# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------
RESULTS: list[dict[str, Any]] = []


def _rss_mb() -> float:
    """
    Current process RSS in MiB, read from /proc/self/status (Linux).
    Captures both Python heap and JVM heap in one number.
    Returns 0.0 on non-Linux platforms.
    """
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB → MiB
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def run_bench(
    name: str,
    fn: Callable[..., Any],
    *args: Any,
    runs: int = BENCH_RUNS,
    warmup: int = 1,
    **kwargs: Any,
) -> Any:
    """
    Warm-up *warmup* times (untimed), then time *runs* executions.
    Appends a record to RESULTS and prints one summary line.

    Per-run memory is tracked two ways:
      • RSS delta  – change in process RSS (Python + JVM heap) via /proc/self/status
      • Python heap peak – peak Python-only allocation via tracemalloc

    ``runs`` and ``warmup`` are consumed by this function; every other
    keyword argument is forwarded to *fn*.
    """
    last: Any = None

    for _ in range(warmup):
        last = fn(*args, **kwargs)

    times: list[float] = []
    rss_deltas: list[float] = []   # MiB delta per run (process RSS)
    py_peaks: list[float] = []     # MiB peak Python allocation per run

    for _ in range(runs):
        rss_before = _rss_mb()

        tracemalloc.start()
        t0 = time.perf_counter()
        last = fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        _cur, py_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        rss_after = _rss_mb()

        times.append(elapsed)
        rss_deltas.append(rss_after - rss_before)
        py_peaks.append(py_peak_bytes / 1024 / 1024)

    avg_ms   = sum(times) / len(times) * 1000
    min_ms   = min(times) * 1000
    max_ms   = max(times) * 1000
    avg_rss  = sum(rss_deltas) / len(rss_deltas)  # mean RSS delta (MiB)
    max_rss  = max(rss_deltas)                     # worst-case RSS delta
    avg_py   = sum(py_peaks)   / len(py_peaks)     # mean Python-heap peak
    max_py   = max(py_peaks)                       # worst-case Python-heap peak

    ok: bool | None = last.get("ok") if isinstance(last, dict) else None
    icon = "✅" if ok is True else ("⚠️ " if ok is None else "❌")

    RESULTS.append(
        {
            "function": name,
            "runs": runs,
            "avg_ms":       round(avg_ms,  2),
            "min_ms":       round(min_ms,  2),
            "max_ms":       round(max_ms,  2),
            "avg_rss_delta_mb": round(avg_rss, 2),
            "max_rss_delta_mb": round(max_rss, 2),
            "avg_py_peak_mb":   round(avg_py,  2),
            "max_py_peak_mb":   round(max_py,  2),
            "ok": ok,
        }
    )
    print(
        f"{icon}  {name:<45}  "
        f"avg={avg_ms:9.1f} ms  "
        f"min={min_ms:9.1f} ms  "
        f"max={max_ms:9.1f} ms  "
        f"rss_delta={avg_rss:+7.1f} MiB  "
        f"py_peak={avg_py:6.1f} MiB",
        flush=True,
    )
    return last


# ---------------------------------------------------------------------------
# Helper: pick a good representative class from the APK
# ---------------------------------------------------------------------------
def _pick_test_class(names: list[str]) -> str:
    """
    Return the first concrete, non-anonymous, non-inner class whose FQN
    contains 'org.schabi.newpipe'.  Falls back to the first class in the list.
    """
    for c in names:
        if "org.schabi.newpipe" in c and "$" not in c:
            return c
    return names[0] if names else "org.schabi.newpipe.MainActivity"


def _pick_test_method(methods: list[dict[str, str]]) -> str:
    """Return the first non-synthetic (non-<…>) method name."""
    for m in methods:
        if not m["name"].startswith("<"):
            return m["name"]
    return methods[0]["name"] if methods else "toString"


# ===========================================================================
# ── Benchmarks ──────────────────────────────────────────────────────────────
# ===========================================================================

SESSION_ID = "bench"

# ── 1. load_apk ─────────────────────────────────────────────────────────────
# Run once (cold), no warm-up — this is inherently a one-shot operation.
result = run_bench(
    "load_apk",
    _server.load_apk, APK_PATH, SESSION_ID,
    runs=1, warmup=0,
)
if not result.get("ok"):
    print(f"\n[FATAL] load_apk failed: {result}", file=sys.stderr)
    sys.exit(1)
print(f"         → {result.get('class_count', '?')} classes loaded\n")

# ── 2. list_sessions ────────────────────────────────────────────────────────
run_bench("list_sessions", _server.list_sessions)

# ── 3. get_all_classes  (also discovers the test class) ─────────────────────
result = run_bench("get_all_classes", _server.get_all_classes, SESSION_ID)
all_classes: list[str] = result.get("items", [])
TEST_CLASS = _pick_test_class(all_classes)
print(f"         → test class  : {TEST_CLASS}")

# ── 4. search_class_names ───────────────────────────────────────────────────
run_bench(
    "search_class_names[Activity]",
    _server.search_class_names, "Activity", SESSION_ID,
)

# ── 5. get_class_source ─────────────────────────────────────────────────────
run_bench("get_class_source", _server.get_class_source, TEST_CLASS, SESSION_ID)

# ── 6. get_smali_source ─────────────────────────────────────────────────────
# May return ok=False if this jadx build doesn't expose getSmali().
run_bench("get_smali_source", _server.get_smali_source, TEST_CLASS, SESSION_ID)

# ── 7. get_methods_of_class  (also discovers the test method) ───────────────
result = run_bench(
    "get_methods_of_class",
    _server.get_methods_of_class, TEST_CLASS, SESSION_ID,
)
TEST_METHOD = _pick_test_method(result.get("methods", []))
print(f"         → test method : {TEST_METHOD}")

# ── 8. get_fields_of_class ──────────────────────────────────────────────────
run_bench("get_fields_of_class", _server.get_fields_of_class, TEST_CLASS, SESSION_ID)

# ── 9. get_method_by_name ───────────────────────────────────────────────────
run_bench(
    "get_method_by_name",
    _server.get_method_by_name, TEST_CLASS, TEST_METHOD, SESSION_ID,
)

# ── 10. search_classes_by_keyword ───────────────────────────────────────────
# WARNING: forces decompilation of every un-compiled class in the APK.
# This can take several minutes on large APKs.
# We use limit=10 so the scan stops after the first 10 hits, bounding
# worst-case time.  case_sensitive=False keeps the benchmark deterministic
# regardless of capitalisation conventions.
print()
print("   ⚠️  search_classes_by_keyword decompiles un-cached classes — may be slow …",
      flush=True)
run_bench(
    "search_classes_by_keyword[youtube,lim=10]",
    _server.search_classes_by_keyword, "youtube", SESSION_ID,
    limit=10, case_sensitive=False,
    runs=1, warmup=0,
)

# ── 11. xrefs_to_class ──────────────────────────────────────────────────────
run_bench("xrefs_to_class", _server.xrefs_to_class, TEST_CLASS, SESSION_ID)

# ── 12. xrefs_to_method ─────────────────────────────────────────────────────
run_bench(
    "xrefs_to_method",
    _server.xrefs_to_method, TEST_CLASS, TEST_METHOD, SESSION_ID,
)

# ── 13. get_android_manifest ────────────────────────────────────────────────
run_bench("get_android_manifest", _server.get_android_manifest, SESSION_ID)

# ── 14. get_strings ─────────────────────────────────────────────────────────
run_bench("get_strings", _server.get_strings, SESSION_ID)

# ── 15. get_resource[AndroidManifest.xml] ───────────────────────────────────
run_bench(
    "get_resource[AndroidManifest.xml]",
    _server.get_resource, "AndroidManifest.xml", SESSION_ID,
)

# ── 16. get_all_resource_file_names (flat / top-level only) ─────────────────
run_bench(
    "get_all_resource_file_names[flat]",
    _server.get_all_resource_file_names, SESSION_ID,
)

# ── 17. get_all_resource_file_names (recursive / full arsc walk) ────────────
# Run once — arsc parsing is cached by jadx after the first call.
run_bench(
    "get_all_resource_file_names[recursive]",
    _server.get_all_resource_file_names, SESSION_ID,
    recursive=True,
    runs=1, warmup=0,
)

# ── 18. search_resource_name ────────────────────────────────────────────────
run_bench(
    "search_resource_name[drawable]",
    _server.search_resource_name, "drawable", SESSION_ID,
)

# ── 19. close_session ───────────────────────────────────────────────────────
run_bench("close_session", _server.close_session, SESSION_ID, runs=1, warmup=0)

# ===========================================================================
# ── Output ──────────────────────────────────────────────────────────────────
# ===========================================================================
print()
print(SEP)

# JSON artifact ──────────────────────────────────────────────────────────────
RESULTS_PATH.write_text(json.dumps(RESULTS, indent=2))
print(f"📁  JSON results  → {RESULTS_PATH}")

# Console summary table ──────────────────────────────────────────────────────
col = [
    ("Function",           "function",          ":<47"),
    ("Runs",               "runs",               ":>4"),
    ("Avg (ms)",           "avg_ms",             ":>10"),
    ("Min (ms)",           "min_ms",             ":>10"),
    ("Max (ms)",           "max_ms",             ":>10"),
    ("RSS delta (MiB)",    "avg_rss_delta_mb",   ":>16"),
    ("Py peak (MiB)",      "avg_py_peak_mb",     ":>14"),
    ("ok",                 "ok",                 ":>5"),
]
print()
hdr  = "  ".join(f"{h:{fmt[1:]}}" for h, _, fmt in col)
line = "  ".join("-" * (len(f"{h:{fmt[1:]}}")) for h, _, fmt in col)
print(hdr)
print(line)
for r in RESULTS:
    row = "  ".join(
        f"{str(r[key]):{fmt[1:]}}"
        for _, key, fmt in col
    )
    print(row)

# GitHub step summary (markdown) ─────────────────────────────────────────────
if GITHUB_STEP_SUMMARY:
    with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as fh:
        fh.write("## 📊 Jadx MCP — Function Benchmark Results\n\n")
        fh.write(f"> **APK:** `{APK_PATH}`  \n")
        fh.write(f"> **JVM startup:** {_jvm_ms:,.0f} ms\n\n")
        fh.write(
            "| Function | Runs | Avg (ms) | Min (ms) | Max (ms) "
            "| RSS delta (MiB) | Py peak (MiB) | Status |\n"
            "|:---|---:|---:|---:|---:|---:|---:|:---:|\n"
        )
        for r in RESULTS:
            status = "✅" if r["ok"] is True else ("—" if r["ok"] is None else "❌")
            fh.write(
                f"| `{r['function']}` | {r['runs']} "
                f"| {r['avg_ms']:.1f} | {r['min_ms']:.1f} | {r['max_ms']:.1f} "
                f"| {r['avg_rss_delta_mb']:+.1f} | {r['avg_py_peak_mb']:.1f} "
                f"| {status} |\n"
            )
    print(f"📋  Step summary   → {GITHUB_STEP_SUMMARY}")

print()
print("✅  Benchmark complete.")
