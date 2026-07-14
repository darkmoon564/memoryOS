import os
import sys

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.core.event_parser import parse_events

def test_event_parser():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Event Parser Unit Tests")
    print("============================================================")
    
    test_cases = [
        (
            "Alice loves coding in Rust and lives in Seattle.",
            ["Alice loves coding in Rust.", "Alice lives in Seattle."]
        ),
        (
            "Alice works at Acme Corp and she enjoys Neovim.",
            ["Alice works at Acme Corp.", "She enjoys Neovim."]
        ),
        (
            "Bob works at Google. He lives in San Francisco.",
            ["Bob works at Google.", "He lives in San Francisco."]
        )
    ]
    
    all_passed = True
    for text, expected in test_cases:
        print(f"\nInput:  '{text}'")
        parsed = parse_events(text)
        print(f"Parsed: {parsed}")
        print(f"Expected: {expected}")
        
        if len(parsed) != len(expected):
            print(f"  [FAIL] Count mismatch (got {len(parsed)}, expected {len(expected)})")
            all_passed = False
            continue
            
        match = True
        for p, exp in zip(parsed, expected):
            if p.lower().strip(".") != exp.lower().strip("."):
                match = False
                break
        if match:
            print("  [PASS]")
        else:
            print("  [FAIL] Content mismatch")
            all_passed = False
            
    print("\n" + "=" * 60)
    if all_passed:
        print("  Event Parser tests completed successfully!")
    else:
        print("  Some Event Parser tests failed!")
        sys.exit(1)
    print("=" * 60)

if __name__ == "__main__":
    test_event_parser()
