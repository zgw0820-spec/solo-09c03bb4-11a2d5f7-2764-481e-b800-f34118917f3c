"""Core build logic: dependency-aware object caching with singleflight.

Cache key design
----------------
For each translation unit (.c file) the cache key is the SHA-256 of:

* the fixed GCC flags,
* the source file's project-relative path and content,
* the path and content of every *project* header transitively reachable
  from that source via #include directives (the dependency closure).

Include resolution mirrors GCC: quoted includes are searched relative to
the directory of the file containing the directive first, then the project
root (which is passed to GCC with -I). Angle includes are searched in the
project root only. Includes that do not resolve to a submitted project file
are treated as system headers and do not participate in the key.

Because resolution is part of the key, adding a same-named header that
shadows another one changes the dependency closure of the affected units
and therefore their keys — those units are recompiled while unaffected
units keep hitting the cache.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Fixed GCC parameters used for every compile and link. Part of the cache key.
CFLAGS = ("-O2", "-Wall", "-Wextra", "-std=c11")

# Split-symbols (debug) mode. DEBUG_EXTRA_CFLAGS is part of the debug cache
# key; the -ffile-prefix-map flag is added per build (its value contains the
# temp dir) and deliberately excluded from the key: it rewrites the temp build
# directory to DEBUG_SOURCE_ROOT inside the debug info, so the debug info —
# and therefore the ELF Build ID — does not depend on which temp directory the
# build happened in, and source paths in the symbols correspond to
# repo-relative paths under DEBUG_SOURCE_ROOT (GDB: set substitute-path).
DEBUG_EXTRA_CFLAGS = ("-g",)
DEBUG_SOURCE_ROOT = "/project"
DEBUG_LINK_FLAGS = ("-Wl,--build-id=sha1",)

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
COMPILE_TIMEOUT_S = 60
LINK_TIMEOUT_S = 60
SYMBOL_TOOL_TIMEOUT_S = 30

_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]', re.MULTILINE)
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")


class BuildError(Exception):
    """A build failure whose diagnostics should be returned to the caller."""

    def __init__(self, stage: str, message: str, unit: str | None = None, diagnostics: str = ""):
        super().__init__(message)
        self.stage = stage  # "validation" | "compile" | "link"
        self.unit = unit  # source file that failed, if any
        self.diagnostics = diagnostics or message


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_path(path: str) -> str:
    p = path.strip()
    if not p or p != path:
        raise ValueError(f"invalid path {path!r}: empty or surrounding whitespace")
    if len(p) > 255:
        raise ValueError(f"invalid path {path!r}: too long")
    if p.startswith("/") or "\\" in p or "\x00" in p:
        raise ValueError(f"invalid path {path!r}: must be a relative POSIX path")
    for seg in p.split("/"):
        if seg in (".", "..") or not _PATH_SEGMENT_RE.fullmatch(seg):
            raise ValueError(f"invalid path {path!r}: bad segment {seg!r}")
    if not (p.endswith(".c") or p.endswith(".h")):
        raise ValueError(f"invalid path {path!r}: only .c and .h files are accepted")
    return p


def validate_request(files) -> dict[str, str]:
    """Validate the submitted file list, return {relative_path: content}."""
    if not files:
        raise BuildError("validation", "request contains no files")
    if len(files) > MAX_FILES:
        raise BuildError("validation", f"too many files (max {MAX_FILES})")
    by_path: dict[str, str] = {}
    errors: list[str] = []
    for f in files:
        try:
            p = validate_path(f.path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if p in by_path:
            errors.append(f"duplicate path {p!r}")
            continue
        if len(f.content.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            errors.append(f"file {p!r} exceeds {MAX_FILE_BYTES} bytes")
            continue
        by_path[p] = f.content
    if errors:
        raise BuildError("validation", "; ".join(errors))
    if not any(p.endswith(".c") for p in by_path):
        raise BuildError("validation", "no .c source files submitted")
    return by_path


# ---------------------------------------------------------------------------
# Dependency closure and cache key
# ---------------------------------------------------------------------------

def _resolve_include(name: str, quoted: bool, including_dir: str,
                     files: dict[str, str]) -> str | None:
    candidates = []
    if quoted:
        # GCC searches the directory of the file containing the directive first.
        candidates.append(posixpath.normpath(posixpath.join(including_dir, name)))
    # Then the include path: the project root (passed as -I<root>).
    candidates.append(posixpath.normpath(name))
    for cand in candidates:
        if cand.startswith("/") or cand == ".." or cand.startswith("../"):
            continue
        if cand in files:
            return cand
    return None  # system header or missing file: not part of the cache key


def dependency_closure(src: str, files: dict[str, str]) -> dict[str, str]:
    """All project headers transitively included by src: {path: content}."""
    closure: dict[str, str] = {}
    stack = [src]
    while stack:
        current = stack.pop()
        current_dir = posixpath.dirname(current)
        for m in _INCLUDE_RE.finditer(files[current]):
            target = _resolve_include(m.group(2), m.group(1) == '"', current_dir, files)
            if target is None or target in closure:
                continue
            closure[target] = files[target]
            stack.append(target)
    return closure


def unit_cache_key(src: str, files: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(b"cbuild-v1\0")
    for flag in CFLAGS:
        h.update(flag.encode())
        h.update(b"\0")
    h.update(src.encode())
    h.update(b"\0")
    h.update(files[src].encode())
    h.update(b"\0")
    for path, content in sorted(dependency_closure(src, files).items()):
        h.update(path.encode())
        h.update(b"\0")
        h.update(content.encode())
        h.update(b"\0")
    return h.hexdigest()


def project_fingerprint(files: dict[str, str]) -> str:
    """SHA-256 over every submitted path and content — the whole project."""
    h = hashlib.sha256()
    for path, content in sorted(files.items()):
        h.update(path.encode())
        h.update(b"\0")
        h.update(content.encode())
        h.update(b"\0")
    return h.hexdigest()


def debug_unit_cache_key(src: str, files: dict[str, str]) -> str:
    """Split-symbols mode object key: the whole project identifies reuse.

    Unlike normal mode (per-unit dependency closure), every debug object is
    keyed on the entire submission, so a stored symbol set always corresponds
    to exactly one whole-project state. The version tag keeps the namespace
    distinct from the normal cache, which additionally lives in a separate
    cache directory.
    """
    h = hashlib.sha256()
    h.update(b"cbuild-debug-v1\0")
    for flag in CFLAGS + DEBUG_EXTRA_CFLAGS:
        h.update(flag.encode())
        h.update(b"\0")
    h.update(project_fingerprint(files).encode())
    h.update(b"\0")
    h.update(src.encode())
    h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Object cache with singleflight
# ---------------------------------------------------------------------------

@dataclass
class CacheStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests_total: int = 0
    failed_builds_total: int = 0
    compiled_total: int = 0
    reused_total: int = 0
    singleflight_waits_total: int = 0


class ObjectCache:
    """Content-addressed object-file cache with per-key singleflight.

    Identical compile inputs arriving concurrently are compiled exactly
    once; the other callers wait on the per-key lock and then read the
    result from the cache. Only successful compilations enter the cache.
    """

    def __init__(self, cache_dir: Path, stats: CacheStats | None = None):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats = stats if stats is not None else CacheStats()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def get_or_compile(self, key: str, compile_fn) -> tuple[bytes, str]:
        """Return (object_bytes, status); status in compiled|reused|waited."""
        path = self.dir / f"{key}.o"
        if path.is_file():
            with self.stats.lock:
                self.stats.reused_total += 1
            return path.read_bytes(), "reused"
        lock = self._lock_for(key)
        with lock:
            if path.is_file():
                # A concurrent request compiled this very key while we waited.
                with self.stats.lock:
                    self.stats.reused_total += 1
                    self.stats.singleflight_waits_total += 1
                return path.read_bytes(), "waited"
            data = compile_fn()  # raises BuildError on failure: nothing is cached
            tmp = self.dir / f"{key}.{threading.get_ident()}.tmp"
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic publish
            with self.stats.lock:
                self.stats.compiled_total += 1
            return data, "compiled"

    def cached_objects(self) -> int:
        return sum(1 for _ in self.dir.glob("*.o"))


# ---------------------------------------------------------------------------
# Split debug symbol store
# ---------------------------------------------------------------------------

class SymbolConflictError(Exception):
    """A *different* symbol file already exists under the same Build ID.

    The previously stored file is always kept; the new content is discarded.
    """

    def __init__(self, build_id: str):
        super().__init__(
            f"conflicting symbols already stored for build ID {build_id}")
        self.build_id = build_id


@dataclass
class SymbolStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    stored_total: int = 0
    reused_total: int = 0
    conflicts_total: int = 0


class SymbolStore:
    """Persistent, content-checked store for split debug symbol files.

    Files are named ``<build-id>.debug`` in one directory. Publishing is
    compare-and-set and safe across threads *and* processes: the new file is
    written to a temp file, fsynced, then hard-linked into place — ``os.link``
    fails with ``FileExistsError`` if the ID is already taken, so an existing
    file is never overwritten. When the ID is taken, identical content means
    "reused"; different content raises :class:`SymbolConflictError` and the
    original stays untouched. The directory is fsynced after a successful
    publish so symbols survive a restart before the response is returned.
    """

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats = SymbolStats()

    def path_for(self, build_id: str) -> Path:
        return self.dir / f"{build_id}.debug"

    def get(self, build_id: str) -> bytes | None:
        p = self.path_for(build_id)
        return p.read_bytes() if p.is_file() else None

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.debug"))

    def store(self, build_id: str, data: bytes) -> str:
        """Publish ``data`` under ``build_id``; return "stored" or "reused".

        Raises SymbolConflictError if different content already exists under
        this Build ID (the stored file is kept).
        """
        existing = self.get(build_id)
        if existing is not None:
            return self._check_existing(build_id, existing, data)

        fd, tmp_name = tempfile.mkstemp(prefix=".sym-", dir=self.dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp_name, self.path_for(build_id))  # atomic, never overwrites
            except FileExistsError:
                # A concurrent publisher won the race; compare what it wrote.
                return self._check_existing(build_id, self.get(build_id), data)
            self._fsync_dir()
            with self.stats.lock:
                self.stats.stored_total += 1
            return "stored"
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _check_existing(self, build_id: str, existing: bytes | None,
                        data: bytes) -> str:
        if existing == data:
            with self.stats.lock:
                self.stats.reused_total += 1
            return "reused"
        with self.stats.lock:
            self.stats.conflicts_total += 1
        raise SymbolConflictError(build_id)

    def _fsync_dir(self) -> None:
        dir_fd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    executable: bytes
    compiled: int
    reused: int
    units: int
    build_id: str | None = None
    symbols_status: str | None = None  # "stored" | "reused" (split-symbols mode)


_BUILD_ID_RE = re.compile(r"Build ID:\s*([0-9a-fA-F]{8,64})")


def _run_or_raise(cmd, *, cwd, timeout, stage, fail_msg, timeout_msg,
                  unit=None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise BuildError(stage, f"{fail_msg}: required tool {cmd[0]!r} is not installed",
                         unit=unit)
    except subprocess.TimeoutExpired:
        raise BuildError(stage, timeout_msg, unit=unit)
    if proc.returncode != 0:
        raise BuildError(stage, fail_msg, unit=unit, diagnostics=proc.stderr)
    return proc


def _extract_build_id(exe: Path) -> str:
    proc = _run_or_raise(["readelf", "-n", str(exe)], cwd=exe.parent,
                         timeout=SYMBOL_TOOL_TIMEOUT_S, stage="symbols",
                         fail_msg="failed to read notes from the linked executable",
                         timeout_msg="reading the Build ID timed out")
    m = _BUILD_ID_RE.search(proc.stdout)
    if m is None:
        raise BuildError("symbols", "linked executable carries no Build ID note",
                         diagnostics=proc.stdout)
    return m.group(1).lower()


def build_project(files: dict[str, str], cache: ObjectCache, work_root: Path, *,
                  split_symbols: bool = False,
                  debug_cache: ObjectCache | None = None,
                  symbol_store: SymbolStore | None = None) -> BuildResult:
    """Compile every .c unit (reusing cached objects) and link this
    submission's objects only into one executable.

    With split_symbols=True the units are compiled with -g, the temp build
    directory is prefix-mapped to DEBUG_SOURCE_ROOT (stable Build ID,
    repo-relative source paths), the debug info is split into a symbol file
    that is fully stored under the executable's Build ID *before* the
    stripped executable (Build ID retained) is returned.
    """
    if split_symbols and (debug_cache is None or symbol_store is None):
        raise ValueError("split_symbols requires debug_cache and symbol_store")
    work_root.mkdir(parents=True, exist_ok=True)
    # Resolve so the path matches the compiler's getcwd(); the prefix map then
    # reliably rewrites it to DEBUG_SOURCE_ROOT inside the debug info.
    tmp = Path(tempfile.mkdtemp(prefix="build-", dir=work_root)).resolve()
    try:
        for rel, content in files.items():
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        if split_symbols:
            unit_flags = CFLAGS + DEBUG_EXTRA_CFLAGS + (
                f"-ffile-prefix-map={tmp}={DEBUG_SOURCE_ROOT}",)
            link_flags = CFLAGS + DEBUG_LINK_FLAGS
            obj_cache = debug_cache
            key_fn = debug_unit_cache_key
        else:
            unit_flags = CFLAGS
            link_flags = CFLAGS
            obj_cache = cache
            key_fn = unit_cache_key

        sources = sorted(p for p in files if p.endswith(".c"))
        obj_dir = tmp / ".objs"
        obj_dir.mkdir()
        objects: list[Path] = []
        compiled = 0
        reused = 0

        for i, src in enumerate(sources):
            key = key_fn(src, files)

            def _compile(src=src, key=key) -> bytes:
                out = obj_dir / f".tmp-{key}.o"
                cmd = ["gcc", *unit_flags, f"-I{tmp}", "-c", str(tmp / src), "-o", str(out)]
                _run_or_raise(cmd, cwd=tmp, timeout=COMPILE_TIMEOUT_S, stage="compile",
                              fail_msg=f"compilation of {src} failed",
                              timeout_msg=f"compilation of {src} timed out", unit=src)
                return out.read_bytes()

            data, status = obj_cache.get_or_compile(key, _compile)
            if status == "compiled":
                compiled += 1
            else:  # "reused" or "waited": this request did not compile it
                reused += 1
            obj = obj_dir / f"{i:04d}-{key[:16]}.o"
            obj.write_bytes(data)
            objects.append(obj)

        exe = tmp / "a.out"
        cmd = ["gcc", *link_flags, "-o", str(exe), *(str(o) for o in objects)]
        _run_or_raise(cmd, cwd=tmp, timeout=LINK_TIMEOUT_S, stage="link",
                      fail_msg="linking failed", timeout_msg="linking timed out")

        build_id = None
        symbols_status = None
        if split_symbols:
            build_id = _extract_build_id(exe)
            sym = tmp / "symbols.debug"
            _run_or_raise(["objcopy", "--only-keep-debug", str(exe), str(sym)],
                          cwd=tmp, timeout=SYMBOL_TOOL_TIMEOUT_S, stage="symbols",
                          fail_msg="failed to extract debug symbols (objcopy)",
                          timeout_msg="extracting debug symbols timed out")
            # Symbols are fully persisted (or confirmed identical) before the
            # program is returned; a conflict aborts the build with 409.
            symbols_status = symbol_store.store(build_id, sym.read_bytes())
            _run_or_raise(["strip", "--strip-debug", str(exe)],
                          cwd=tmp, timeout=SYMBOL_TOOL_TIMEOUT_S, stage="symbols",
                          fail_msg="failed to strip debug info (strip)",
                          timeout_msg="stripping debug info timed out")

        return BuildResult(executable=exe.read_bytes(), compiled=compiled,
                           reused=reused, units=len(sources),
                           build_id=build_id, symbols_status=symbols_status)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
