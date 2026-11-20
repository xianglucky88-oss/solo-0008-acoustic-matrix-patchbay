import argparse
import json
from pathlib import Path
import sys

from .pipeline import run


def main():
    parser = argparse.ArgumentParser(description='topological propagation, signed cancellation, and separate arrival buckets')
    parser.add_argument('input', type=Path, help='source JSON document')
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8'))
        report = run(payload)
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        print(f'input rejected: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
