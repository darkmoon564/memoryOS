import os
import sys
import subprocess

def run_all_tests():
    print("============================================================")

    # Script tests are deterministic integration fixtures, not a semantic
    # model benchmark. Production startup verifies the real models separately.
    test_env = os.environ.copy()
    test_env.setdefault("OFFLINE_MODE", "true")
    # Script fixtures can run without local services.  This only permits the
    # explicitly non-durable adapter when PostgreSQL is unavailable; it does
    # not force it, so CI's explicit `false` setting continues to exercise
    # the real PostgreSQL and Neo4j services.
    test_env.setdefault("MEMORYOS_ALLOW_IN_MEMORY_FALLBACK", "true")
    # Get all python files starting with test_ in the tests directory
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    test_files = sorted([
        f for f in os.listdir(tests_dir)
        if f.startswith("test_") and f.endswith(".py")
    ])

    failed = []
    passed = []

    print(f"Discovered {len(test_files)} script-based test files:")
    for f in test_files:
        print(f"  - {f}")
    print("============================================================")

    for f in test_files:
        filepath = os.path.join(tests_dir, f)
        print(f"\n[RUNNING] {f}...")
        # Run each test script in a separate process to avoid global state contamination
        res = subprocess.run([sys.executable, filepath], capture_output=False, env=test_env)
        if res.returncode == 0:
            print(f"[PASSED] {f}")
            passed.append(f)
        else:
            print(f"[FAILED] {f} (exit code: {res.returncode})")
            failed.append(f)

    print("\n============================================================")
    print("                      Test Run Summary                      ")
    print("============================================================")
    print(f"Total run:  {len(test_files)}")
    print(f"Passed:     {len(passed)}")
    print(f"Failed:     {len(failed)}")
    if failed:
        print("\nFailed tests list:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll tests completed successfully! 🎉")
        sys.exit(0)

if __name__ == "__main__":
    run_all_tests()
