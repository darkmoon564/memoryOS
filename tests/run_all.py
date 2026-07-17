import os
import sys
import subprocess

def run_all_tests():
    print("============================================================")
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
        res = subprocess.run([sys.executable, filepath], capture_output=False)
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
