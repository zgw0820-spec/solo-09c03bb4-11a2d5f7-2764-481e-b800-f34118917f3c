"""Unit tests for SymbolStore compare-and-publish semantics (no service needed).

Usage: python3 tests/symbol_store_test.py

Covers: first store, identical-content reuse, conflicting content (original
kept), concurrent identical/conflicting publishers, and persistence across
re-opening the store directory (service-restart simulation).
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.builder import SymbolConflictError, SymbolStore  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="symstore-test-"))
    store = SymbolStore(tmp)
    bid = "a" * 40

    # 1. first store publishes the file
    status = store.store(bid, b"SYMBOLS-V1")
    check("first store -> 'stored'", status == "stored", status)
    check("file on disk with expected name", (tmp / f"{bid}.debug").is_file())
    check("get() returns content", store.get(bid) == b"SYMBOLS-V1")

    # 2. identical content is reused
    status = store.store(bid, b"SYMBOLS-V1")
    check("identical content -> 'reused'", status == "reused", status)

    # 3. conflicting content raises and keeps the original
    try:
        store.store(bid, b"SYMBOLS-V2-DIFFERENT")
        check("conflicting content raises SymbolConflictError", False)
    except SymbolConflictError as exc:
        check("conflicting content raises SymbolConflictError", exc.build_id == bid)
    check("original file kept after conflict", store.get(bid) == b"SYMBOLS-V1")
    check("conflict counted", store.stats.conflicts_total == 1)

    # 4. persistence: a fresh SymbolStore on the same dir sees the file
    #    (simulates a service restart with a persistent volume)
    reopened = SymbolStore(tmp)
    check("symbols survive re-open (restart)", reopened.get(bid) == b"SYMBOLS-V1")
    check("list_ids finds stored ID", reopened.list_ids() == [bid])
    check("reopened store reuses identical content",
          reopened.store(bid, b"SYMBOLS-V1") == "reused")

    # 5. concurrent identical publishers: exactly one stores, rest reuse
    bid2 = "b" * 40
    results: list[str] = []
    errors: list[BaseException] = []

    def publish_same():
        try:
            results.append(store.store(bid2, b"SAME-CONTENT"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=publish_same) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("16 concurrent identical publishers: no errors", not errors,
          f"errors={errors!r}")
    check("exactly one 'stored', rest 'reused'",
          results.count("stored") == 1 and results.count("reused") == 15,
          f"results={sorted(results)}")
    check("concurrent content intact", store.get(bid2) == b"SAME-CONTENT")

    # 6. concurrent conflicting publishers: one content wins, losers conflict,
    #    the stored file is never overwritten
    bid3 = "c" * 40
    outcomes: list[tuple[str, bytes]] = []  # ("stored"/"reused"/"conflict", content)

    def publish(content: bytes):
        try:
            outcomes.append((store.store(bid3, content), content))
        except SymbolConflictError:
            outcomes.append(("conflict", content))

    threads = [threading.Thread(target=publish, args=(b"CONTENT-A",))
               for _ in range(8)]
    threads += [threading.Thread(target=publish, args=(b"CONTENT-B",))
                for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = store.get(bid3)
    stored = [c for s, c in outcomes if s == "stored"]
    reused = [c for s, c in outcomes if s == "reused"]
    conflicts = [c for s, c in outcomes if s == "conflict"]
    check("concurrent conflict: exactly one 'stored'", len(stored) == 1,
          f"stored={stored}")
    check("concurrent conflict: final content is the stored one",
          final == stored[0] if stored else False, f"final={final!r}")
    check("concurrent conflict: winners reused, losers conflicted, none lost",
          len(reused) == 7 and len(conflicts) == 8
          and all(c == final for c in reused)
          and all(c != final for c in conflicts),
          f"reused={len(reused)} conflicts={len(conflicts)}")
    check("all 16 outcomes accounted for", len(outcomes) == 16)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        return 1
    print("All SymbolStore checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
