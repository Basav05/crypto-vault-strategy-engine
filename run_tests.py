"""Dependency-free test runner (use `python -m pytest` instead when pytest is installed)."""
import importlib, pkgutil, sys, time, traceback
import tests

failed = passed = 0
t0 = time.time()
for m in sorted(pkgutil.iter_modules(tests.__path__), key=lambda m: m.name):
    if not m.name.startswith("test_"):
        continue
    mod = importlib.import_module(f"tests.{m.name}")
    for name in sorted(dir(mod)):
        if name.startswith("test_") and callable(getattr(mod, name)):
            try:
                getattr(mod, name)()
                passed += 1
                print(f"PASS {m.name}::{name}")
            except Exception:
                failed += 1
                print(f"FAIL {m.name}::{name}")
                traceback.print_exc()
print(f"\n{passed} passed, {failed} failed in {time.time()-t0:.1f}s")
sys.exit(1 if failed else 0)
