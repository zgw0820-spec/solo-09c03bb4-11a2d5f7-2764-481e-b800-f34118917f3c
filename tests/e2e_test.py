"""End-to-end tests for the C build cache service.

Usage: python tests/e2e_test.py [base_url]   (default http://127.0.0.1:8000)

Covers: basic build, cache reuse on identical resubmission, header-change
invalidation, same-name header shadowing, concurrent identical builds
(singleflight), compile/link failure diagnostics, path validation, and the
split-symbols mode (Build ID stability, symbol download, debug-cache
separation, whole-project keying, concurrent symbol publishing).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def run_exe(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="cbuild-exe-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return subprocess.run([path], capture_output=True, text=True, timeout=10).stdout.strip()
    finally:
        os.unlink(path)


def post_build(client: httpx.Client, files: dict[str, str], output: str = "app",
               split_symbols: bool = False) -> httpx.Response:
    payload = {"output": output, "split_symbols": split_symbols,
               "files": [{"path": p, "content": c} for p, c in files.items()]}
    return client.post("/build", json=payload, timeout=60)


def counts(resp: httpx.Response) -> tuple[int, int, int]:
    return (int(resp.headers["X-Compiled-Count"]),
            int(resp.headers["X-Reused-Count"]),
            int(resp.headers["X-Unit-Count"]))


def readelf(args: list[str], path: str) -> str:
    return subprocess.run(["readelf", *args, path], capture_output=True,
                          text=True, timeout=30).stdout


# ---------------------------------------------------------------- project P1
P1_UTIL_H_V1 = (
    "#ifndef UTIL_H\n#define UTIL_H\n"
    "#define UTIL_ANSWER 42\n#define UTIL_MSG \"v1\"\n"
    "int util_answer(void);\nconst char *util_msg(void);\n#endif\n"
)
P1_UTIL_H_V2 = P1_UTIL_H_V1.replace("42", "43").replace('"v1"', '"v2"')

P1 = {
    "main.c": (
        "#include <stdio.h>\n#include \"util.h\"\n#include \"noinc.h\"\n"
        "int main(void) {\n"
        "    printf(\"msg=%s answer=%d extra=%d\\n\", util_msg(), util_answer(), noinc_value());\n"
        "    return 0;\n}\n"
    ),
    "util.c": (
        "#include \"util.h\"\n"
        "int util_answer(void) { return UTIL_ANSWER; }\n"
        "const char *util_msg(void) { return UTIL_MSG; }\n"
    ),
    "util.h": P1_UTIL_H_V1,
    "noinc.c": "#include \"noinc.h\"\nint noinc_value(void) { return 7; }\n",
    "noinc.h": "#ifndef NOINC_H\n#define NOINC_H\nint noinc_value(void);\n#endif\n",
}

# ---------------------------------------------------------------- project P2 (shadowing)
P2_BASE = {
    "main.c": (
        "#include <stdio.h>\n#include \"util.h\"\n#include \"sub/helper.h\"\n"
        "int main(void) {\n"
        "    printf(\"main=%s helper=%s\\n\", TAG, helper_tag());\n"
        "    return 0;\n}\n"
    ),
    "util.h": "#ifndef UTIL_H\n#define UTIL_H\n#define TAG \"root\"\n#endif\n",
    "sub/helper.h": "#ifndef HELPER_H\n#define HELPER_H\nconst char *helper_tag(void);\n#endif\n",
    "sub/helper.c": (
        "#include \"helper.h\"\n#include \"util.h\"\n"
        "const char *helper_tag(void) { return TAG; }\n"
    ),
}
P2_SHADOW = dict(P2_BASE)
P2_SHADOW["sub/util.h"] = "#ifndef SUB_UTIL_H\n#define SUB_UTIL_H\n#define TAG \"sub\"\n#endif\n"


def main() -> int:
    client = httpx.Client(base_url=BASE)
    tmpdir = tempfile.mkdtemp(prefix="cbuild-e2e-")
    os.chdir(tmpdir)

    r = client.get("/healthz", timeout=10)
    check("healthz", r.status_code == 200 and r.json()["status"] == "ok")

    stats0 = client.get("/stats", timeout=10).json()

    # 1. fresh build: all units compiled
    r = post_build(client, P1)
    check("P1 first build -> 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    c, u, n = counts(r)
    check("P1 first build compiles all 3 units", (c, u, n) == (3, 0, 3), f"compiled={c} reused={u} units={n}")
    out1 = run_exe(r.content)
    check("P1 executable runs", out1 == "msg=v1 answer=42 extra=7", f"output={out1!r}")

    # 2. identical resubmission: everything reused
    r = post_build(client, P1)
    c, u, n = counts(r)
    check("P1 resubmission fully reused", r.status_code == 200 and (c, u) == (0, 3), f"compiled={c} reused={u}")

    # 3. header change: only units including util.h recompile
    p1_v2 = dict(P1)
    p1_v2["util.h"] = P1_UTIL_H_V2
    r = post_build(client, p1_v2)
    c, u, n = counts(r)
    check("P1 util.h change recompiles affected units only",
          r.status_code == 200 and (c, u) == (2, 1), f"compiled={c} reused={u}")
    out2 = run_exe(r.content)
    check("P1 rebuilt executable reflects new header",
          out2 == "msg=v2 answer=43 extra=7", f"output={out2!r}")

    # 4. reverting the header hits the old cache entries again
    r = post_build(client, P1)
    c, u, n = counts(r)
    check("P1 revert to v1 fully reused", r.status_code == 200 and (c, u) == (0, 3), f"compiled={c} reused={u}")

    # 5. same-name header shadowing changes include resolution
    r = post_build(client, P2_BASE)
    c, u, n = counts(r)
    check("P2 first build compiles both units", r.status_code == 200 and (c, u, n) == (2, 0, 2),
          f"compiled={c} reused={u} units={n}")
    out3 = run_exe(r.content)
    check("P2 resolves util.h to project root", out3 == "main=root helper=root", f"output={out3!r}")

    r = post_build(client, P2_SHADOW)
    c, u, n = counts(r)
    check("P2 adding sub/util.h recompiles only the shadowed unit",
          r.status_code == 200 and (c, u) == (1, 1), f"compiled={c} reused={u}")
    out4 = run_exe(r.content)
    check("P2 shadowed include takes effect", out4 == "main=root helper=sub", f"output={out4!r}")

    # 6. concurrent identical builds compile each unit exactly once
    nonce = os.urandom(8).hex()
    p3 = {
        "main.c": (
            f"/* {nonce} */\n"
            "int fa(void); int fb(void);\n"
            "#include <stdio.h>\n"
            "int main(void){ printf(\"%d\\n\", fa()+fb()); return 0; }\n"
        ),
        "a.c": f"/* {nonce} */\n#include \"shared.h\"\nint fa(void){{ return SHARED; }}\n",
        "b.c": f"/* {nonce} */\n#include \"shared.h\"\nint fb(void){{ return SHARED * 2; }}\n",
        "shared.h": f"/* {nonce} */\n#ifndef SHARED_H\n#define SHARED_H\n#define SHARED 5\n#endif\n",
    }
    s_before = client.get("/stats", timeout=10).json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post_build(client, p3), range(8)))
    s_after = client.get("/stats", timeout=10).json()
    ok = all(r.status_code == 200 for r in responses)
    compiled_sum = sum(int(r.headers["X-Compiled-Count"]) for r in responses)
    delta_compiled = s_after["compiled_total"] - s_before["compiled_total"]
    waits = s_after["singleflight_waits_total"] - s_before["singleflight_waits_total"]
    check("8 concurrent identical builds all succeed", ok)
    check("each of the 3 units compiled exactly once globally",
          compiled_sum == 3 and delta_compiled == 3,
          f"sum(X-Compiled-Count)={compiled_sum} stats_delta={delta_compiled} singleflight_waits={waits}")
    check("concurrent executable output correct", run_exe(responses[0].content) == "15")

    # 6b. concurrent *different* projects (parallel CI branches) all succeed
    nonce2 = os.urandom(8).hex()
    p3b = {p: c + f"\n/* branch {nonce2} */\n" for p, c in p3.items()}
    mixed = [P1, p1_v2, P2_BASE, P2_SHADOW, p3b,
             {"main.c": "int main(void) { return broken(; }\n"}]
    with ThreadPoolExecutor(max_workers=6) as pool:
        mixed_resps = list(pool.map(lambda pf: post_build(client, pf), mixed))
    statuses = [r.status_code for r in mixed_resps]
    check("6 concurrent different builds behave independently",
          statuses == [200, 200, 200, 200, 200, 400], f"statuses={statuses}")

    # 7. compile failure: diagnostics returned, nothing cached
    cached_before = client.get("/stats", timeout=10).json()["cached_objects"]
    bad = {"main.c": "int main(void) { return does_not_exist(; }\n"}
    r = post_build(client, bad)
    detail = r.json().get("detail", {})
    check("syntax error -> 400 with compile diagnostics",
          r.status_code == 400 and detail.get("stage") == "compile"
          and "error" in detail.get("diagnostics", "").lower(),
          f"status={r.status_code} stage={detail.get('stage')}")
    cached_after = client.get("/stats", timeout=10).json()["cached_objects"]
    check("failed compile is not cached", cached_after == cached_before,
          f"cached_objects {cached_before} -> {cached_after}")

    # 8. link failure diagnostics
    r = post_build(client, {"foo.c": "int foo(void) { return 1; }\n"})
    detail = r.json().get("detail", {})
    check("missing main -> 400 with link diagnostics",
          r.status_code == 400 and detail.get("stage") == "link"
          and "main" in detail.get("diagnostics", ""),
          f"status={r.status_code} stage={detail.get('stage')}")

    # 9. validation
    r = post_build(client, {"../evil.c": "int x;\n"})
    check("path traversal rejected", r.status_code == 400
          and r.json().get("detail", {}).get("stage") == "validation")
    r = post_build(client, {"readme.txt": "hello\n"})
    check("non-C file rejected", r.status_code == 400)

    # ---------------------------------------------------------------- project P4 (split symbols)
    nonce4 = os.urandom(8).hex()
    P4 = {
        "main.c": (
            f"/* {nonce4} */\n"
            "#include <stdio.h>\n#include \"calc.h\"\n"
            "int main(void){ printf(\"calc=%d\\n\", calc_sum(20, 22)); return 0; }\n"
        ),
        "calc.c": f"/* {nonce4} */\n#include \"calc.h\"\nint calc_sum(int a, int b){{ return a + b; }}\n",
        "calc.h": f"/* {nonce4} */\n#ifndef CALC_H\n#define CALC_H\nint calc_sum(int a, int b);\n#endif\n",
    }
    have_readelf = shutil.which("readelf") is not None

    # 10. split-symbols build: Build ID header, symbols stored, program stripped
    r = post_build(client, P4, split_symbols=True)
    check("P4 split-symbols build -> 200", r.status_code == 200,
          f"got {r.status_code}: {r.text[:200]}")
    bid = r.headers.get("X-Build-Id", "")
    check("X-Build-Id is a 40-hex sha1", bool(re.fullmatch(r"[0-9a-f]{40}", bid)), bid)
    check("X-Symbols-Status == stored", r.headers.get("X-Symbols-Status") == "stored",
          r.headers.get("X-Symbols-Status"))
    c, u, n = counts(r)
    check("P4 split build compiled both units (debug cache separate)",
          (c, u, n) == (2, 0, 2), f"compiled={c} reused={u} units={n}")
    check("P4 stripped program runs", run_exe(r.content) == "calc=42")
    if have_readelf:
        exe_path = Path(tmpdir) / "p4-stripped"
        exe_path.write_bytes(r.content)
        notes = readelf(["-n"], str(exe_path))
        check("stripped program keeps its Build ID note", f"Build ID: {bid}" in notes)
        sections = readelf(["-S"], str(exe_path))
        check("stripped program has no .debug sections", ".debug" not in sections)

    # 11. identical split resubmission: same Build ID, symbols reused
    r = post_build(client, P4, split_symbols=True)
    check("P4 split resubmission: same Build ID (temp dir does not leak)",
          r.status_code == 200 and r.headers.get("X-Build-Id") == bid,
          r.headers.get("X-Build-Id", ""))
    c, u, _ = counts(r)
    check("P4 split resubmission fully reused from debug cache", (c, u) == (0, 2),
          f"compiled={c} reused={u}")
    check("P4 split resubmission: symbols reused",
          r.headers.get("X-Symbols-Status") == "reused",
          r.headers.get("X-Symbols-Status"))

    # 12. symbol download by Build ID
    r = client.get(f"/symbols/{bid}", timeout=10)
    check("GET /symbols/{build_id} -> 200", r.status_code == 200,
          f"got {r.status_code}")
    sym = r.content
    check("symbols reference repo-relative source paths",
          b"/project/main.c" in sym and b"/project/calc.c" in sym)
    check("symbols contain no build temp dir paths",
          b"/tmp/" not in sym and b"/build-" not in sym)
    if have_readelf:
        sym_path = Path(tmpdir) / "p4.debug"
        sym_path.write_bytes(sym)
        check("symbol file carries the same Build ID",
              f"Build ID: {bid}" in readelf(["-n"], str(sym_path)))
        check("symbol file holds .debug_info",
              ".debug_info" in readelf(["-S"], str(sym_path)))
    r = client.get("/symbols", timeout=10)
    check("GET /symbols lists the Build ID",
          r.status_code == 200 and bid in r.json().get("build_ids", []))
    r = client.get(f"/symbols/{'0' * 40}", timeout=10)
    check("unknown Build ID -> 404", r.status_code == 404)
    r = client.get("/symbols/nothex!", timeout=10)
    check("malformed Build ID -> 400", r.status_code == 400)

    # 13. debug cache is separate from the normal cache
    r = post_build(client, P4)  # normal build of the same project
    c, u, _ = counts(r)
    check("normal build after split build compiles again (caches separate)",
          r.status_code == 200 and (c, u) == (2, 0), f"compiled={c} reused={u}")
    check("normal build has no Build ID header", "X-Build-Id" not in r.headers)
    r = post_build(client, P4, split_symbols=True)
    c, u, _ = counts(r)
    check("split build still fully reused from debug cache", (c, u) == (0, 2),
          f"compiled={c} reused={u}")

    # 14. debug objects are keyed by the whole project
    P4X = dict(P4)
    P4X["unrelated.h"] = f"/* {nonce4} never included */\n"
    r = post_build(client, P4X, split_symbols=True)
    c, u, _ = counts(r)
    check("split build: unreferenced file change recompiles all units",
          r.status_code == 200 and (c, u) == (2, 0), f"compiled={c} reused={u}")
    check("...but produces the same Build ID (same compile inputs)",
          r.headers.get("X-Build-Id") == bid, r.headers.get("X-Build-Id", ""))
    r = post_build(client, P4X)  # normal mode ignores the unreferenced file
    c, u, _ = counts(r)
    check("normal build: unreferenced file does not invalidate", (c, u) == (0, 2),
          f"compiled={c} reused={u}")

    # 15. concurrent identical split builds: one compile set, one symbol store
    nonce5 = os.urandom(8).hex()
    P5 = {
        "main.c": (
            f"/* {nonce5} */\n#include <stdio.h>\n#include \"s.h\"\n"
            "int main(void){ printf(\"%d\\n\", s_val()); return 0; }\n"
        ),
        "s.c": f"/* {nonce5} */\n#include \"s.h\"\nint s_val(void){{ return S_VAL; }}\n",
        "s.h": f"/* {nonce5} */\n#ifndef S_H\n#define S_H\n#define S_VAL 9\nint s_val(void);\n#endif\n",
    }
    s_before = client.get("/stats", timeout=10).json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post_build(client, P5, split_symbols=True), range(8)))
    s_after = client.get("/stats", timeout=10).json()
    bids = {r.headers.get("X-Build-Id") for r in responses}
    check("8 concurrent split builds all succeed",
          all(r.status_code == 200 for r in responses))
    check("8 concurrent split builds agree on one Build ID", len(bids) == 1, str(bids))
    check("concurrent split: each unit compiled exactly once globally",
          sum(int(r.headers["X-Compiled-Count"]) for r in responses) == 2)
    stored_delta = s_after["symbols_stored_total"] - s_before["symbols_stored_total"]
    conflict_delta = s_after["symbols_conflicts_total"] - s_before["symbols_conflicts_total"]
    check("concurrent split: symbols stored exactly once, no conflicts",
          stored_delta == 1 and conflict_delta == 0,
          f"stored_delta={stored_delta} conflict_delta={conflict_delta}")

    stats1 = client.get("/stats", timeout=10).json()
    check("stats expose debug cache and symbol counters",
          {"cached_debug_objects", "symbols_on_disk", "symbols_stored_total",
           "symbols_reused_total", "symbols_conflicts_total"} <= set(stats1))
    print("\nstats before:", stats0)
    print("stats after: ", stats1)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
