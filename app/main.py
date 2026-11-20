"""FastAPI surface for the C build cache service."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response

from .builder import (
    CFLAGS,
    BuildError,
    ObjectCache,
    SymbolConflictError,
    SymbolStore,
    build_project,
    validate_request,
)
from .models import BuildRequest, ErrorResponse

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "./data/cache"))
DEBUG_CACHE_DIR = Path(os.environ.get("DEBUG_CACHE_DIR", "./data/cache-debug"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "./data/work"))
SYMBOLS_DIR = Path(os.environ.get("SYMBOLS_DIR", "./data/symbols"))

cache = ObjectCache(CACHE_DIR)
# Debug objects live in a separate cache directory but share the counters.
debug_cache = ObjectCache(DEBUG_CACHE_DIR, stats=cache.stats)
symbols = SymbolStore(SYMBOLS_DIR)

_BUILD_ID_PATH_RE = re.compile(r"^[0-9a-f]{8,64}$")

app = FastAPI(
    title="C Build Cache Service",
    version="1.1.0",
    description=(
        "Accepts a small C project as JSON (sources and headers by relative "
        "path), compiles it with fixed GCC flags "
        f"(`{' '.join(CFLAGS)}`, project root on the include path), reuses "
        "object files whose source and transitively included project headers "
        "are unchanged, links only this submission's objects, and returns "
        "the executable plus reuse counters.\n\n"
        "With `split_symbols: true` the project is compiled with `-g`; the "
        "returned program is stripped of debug info but keeps its ELF Build "
        "ID (`X-Build-Id` header), and the matching symbol file is stored "
        "under that ID — download it via `GET /symbols/{build_id}`. Debug "
        "objects are cached per whole project, separately from normal builds."
    ),
)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}


@app.get("/stats", tags=["meta"])
def stats():
    """Global cache and symbol-store counters (singleflight behaviour)."""
    s = cache.stats
    with s.lock, symbols.stats.lock:
        return {
            "cached_objects": cache.cached_objects(),
            "cached_debug_objects": debug_cache.cached_objects(),
            "symbols_on_disk": len(symbols.list_ids()),
            "requests_total": s.requests_total,
            "failed_builds_total": s.failed_builds_total,
            "compiled_total": s.compiled_total,
            "reused_total": s.reused_total,
            "singleflight_waits_total": s.singleflight_waits_total,
            "symbols_stored_total": symbols.stats.stored_total,
            "symbols_reused_total": symbols.stats.reused_total,
            "symbols_conflicts_total": symbols.stats.conflicts_total,
        }


@app.post(
    "/build",
    tags=["build"],
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
            "description": (
                "The linked executable (stripped of debug info in split-symbols "
                "mode, ELF Build ID retained). Reuse counters are returned in the "
                "`X-Compiled-Count`, `X-Reused-Count` and `X-Unit-Count` headers."
            ),
            "headers": {
                "X-Compiled-Count": {"schema": {"type": "integer"}, "description": "Objects compiled by this request"},
                "X-Reused-Count": {"schema": {"type": "integer"}, "description": "Objects reused from the cache (including singleflight waits)"},
                "X-Unit-Count": {"schema": {"type": "integer"}, "description": "Total translation units in this submission"},
                "X-Build-Id": {"schema": {"type": "string"}, "description": "ELF Build ID of the program (split-symbols mode only); fetch symbols via GET /symbols/{build_id}"},
                "X-Symbols-Status": {"schema": {"type": "string"}, "description": "stored | reused (split-symbols mode only)"},
            },
        },
        400: {"model": ErrorResponse, "description": "Validation, compile or link failure; GCC diagnostics included"},
        409: {"model": ErrorResponse, "description": "Symbol conflict: a different symbol file already exists under the same Build ID; the stored file was kept"},
        422: {"description": "Request body does not match the schema"},
        500: {"model": ErrorResponse, "description": "Symbol extraction/storage tooling failure"},
    },
)
def build(req: BuildRequest) -> Response:
    with cache.stats.lock:
        cache.stats.requests_total += 1
    try:
        files = validate_request(req.files)
        result = build_project(files, cache, WORK_DIR,
                               split_symbols=req.split_symbols,
                               debug_cache=debug_cache,
                               symbol_store=symbols)
    except SymbolConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "stage": "symbols",
                "message": str(exc),
                "unit": None,
                "diagnostics": (
                    f"Symbols for build ID {exc.build_id} already exist with "
                    "different content; the stored file was kept and this "
                    "build's program was not returned."
                ),
            },
        )
    except BuildError as exc:
        with cache.stats.lock:
            cache.stats.failed_builds_total += 1
        raise HTTPException(
            status_code=500 if exc.stage == "symbols" else 400,
            detail={
                "stage": exc.stage,
                "message": str(exc),
                "unit": exc.unit,
                "diagnostics": exc.diagnostics,
            },
        )
    headers = {
        "Content-Disposition": f'attachment; filename="{req.output}"',
        "X-Compiled-Count": str(result.compiled),
        "X-Reused-Count": str(result.reused),
        "X-Unit-Count": str(result.units),
    }
    if result.build_id is not None:
        headers["X-Build-Id"] = result.build_id
        headers["X-Symbols-Status"] = result.symbols_status or ""
    return Response(
        content=result.executable,
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/symbols", tags=["symbols"])
def list_symbols():
    """List the Build IDs for which symbol files are stored."""
    ids = symbols.list_ids()
    return {"count": len(ids), "build_ids": ids}


@app.get(
    "/symbols/{build_id}",
    tags=["symbols"],
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The symbol file (.debug) stored for this Build ID.",
        },
        400: {"description": "Malformed Build ID"},
        404: {"description": "No symbols stored for this Build ID"},
    },
)
def get_symbols(build_id: str) -> Response:
    """Download the split debug symbols for a Build ID (from X-Build-Id)."""
    bid = build_id.lower()
    if not _BUILD_ID_PATH_RE.fullmatch(bid):
        raise HTTPException(status_code=400,
                            detail="invalid build ID: expected 8-64 hex characters")
    data = symbols.get(bid)
    if data is None:
        raise HTTPException(status_code=404,
                            detail=f"no symbols stored for build ID {bid}")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{bid}.debug"'},
    )
