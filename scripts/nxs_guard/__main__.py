from __future__ import annotations

import argparse
import json
import sys

from scripts.nxs_control.core import evaluate_guard, repository_root


def main() -> int:
    parser = argparse.ArgumentParser(description="NXS execution guard")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    result = evaluate_guard(repository_root(), arguments.phase)
    if arguments.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print("NXS EXECUTION GUARD")
        print(f"RESULT: {result.result}")
        print(f"CODE: {result.code}")
        for reason in result.reasons:
            print(f"REASON: {reason}")
    return 0 if result.result == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
