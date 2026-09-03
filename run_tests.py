# Every suite in this repo, in one command.
#
#   python run_tests.py
#
# There is no CI on pull requests (the only workflow is the digest cron), so
# this is what stands between a change and Render. It was eight commands
# across two languages before, and the Node half needed a PATH prefix on
# Windows, which is exactly the kind of friction that ends with the Node
# suites quietly never being run -- they sat unrunnable here until
# 2026-09-03, and the fixture diagnostic they gate is the tool that
# separates an engine bug from a data bug without spending real job
# applications to find out.
#
# Exits non-zero if anything failed, so CI can use it directly.

import os
import shutil
import subprocess
import sys

# This script prints test output that contains non-ASCII. Force its own
# stdout to UTF-8 so the runner cannot fail for the reason it exists to
# work around (it did, first run).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "chrome-extension", "tests")

PYTHON_SUITES = [
    "test_location_filter.py",
    "test_work_history.py",
    "test_autopilot_requeue.py",
    "test_bookmarklet.py",
    "test_ats_classify.py",
]

NODE_SUITES = [
    "autofill.accuracy.test.mjs",
    "autofill.test.mjs",
]


def find_node():
    """node on PATH, or the default Windows install location.

    winget puts node in C:\\Program Files\\nodejs, which is not on the PATH
    of shells that were already open when it was installed -- so 'not on
    PATH' does not mean 'not installed'.
    """
    found = shutil.which("node")
    if found:
        return found
    fallback = r"C:\Program Files\nodejs\node.exe"
    return fallback if os.path.isfile(fallback) else None


def run(label, cmd, cwd, env=None):
    print(f"\n─── {label} " + "─" * max(0, 60 - len(label)))
    proc = subprocess.run(cmd, cwd=cwd, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in out.strip().split("\n") if ln.strip()]
    for ln in tail[-4:]:
        print("   " + ln)
    ok = proc.returncode == 0
    print(f"   -> {'PASS' if ok else 'FAIL'} (exit {proc.returncode})")
    return ok, out


def main():
    results = []

    # The Python suites print non-ASCII (a Polish city name, box drawing).
    # Windows consoles default to cp1252 and die on it, which reads as a
    # test failure when the assertions actually passed.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    for suite in PYTHON_SUITES:
        ok, _ = run(suite, [sys.executable, suite], ROOT, env)
        results.append((suite, ok, ""))

    node = find_node()
    if not node:
        for suite in NODE_SUITES:
            results.append((suite, False, "node not installed"))
        print("\n   node not found — install with: winget install OpenJS.NodeJS.LTS")
    elif not os.path.isdir(os.path.join(TESTS, "node_modules", "jsdom")):
        for suite in NODE_SUITES:
            results.append((suite, False, "jsdom missing"))
        print(f"\n   jsdom missing — run: npm install jsdom   (in {TESTS})")
    else:
        for suite in NODE_SUITES:
            ok, _ = run(suite, [node, suite], TESTS, env)
            results.append((suite, ok, ""))

    print("\n" + "=" * 68)
    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, note in results:
        if not ok:
            failed += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name.ljust(width)}  {note}")
    print("=" * 68)
    print(f"  {len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
