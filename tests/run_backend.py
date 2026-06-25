from __future__ import annotations

import sys
import traceback
from collections.abc import Callable

import tests.test_backend as backend


def main() -> int:
    failures = 0
    tests: list[tuple[str, Callable[[], None]]] = [
        (name, getattr(backend, name))
        for name in sorted(dir(backend))
        if name.startswith("test_") and callable(getattr(backend, name))
    ]
    for name, test in tests:
        try:
            test()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    total = len(tests)
    if failures:
        print(f"{total - failures}/{total} passed, {failures} failed.")
        return 1
    print(f"{total}/{total} passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
