from __future__ import annotations

import json
from pathlib import Path
import sys

from sumbi.core.privacy import read_salt
from sumbi.measure.report import collect, text_summary


from sumbi.cli.common import atomic_write


def run(args, window, agents, rules):
    try:
        salt = read_salt(args.salt_file)
        report, review = collect(args.home, window, agents=agents, rules=rules,
                                 idle_minutes=args.idle_minutes, local_review=bool(args.local_review), salt=salt)
        data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.json == "-":
            print(data, end="")
        else:
            atomic_write(Path(args.json), data)
        if args.local_review:
            atomic_write(args.local_review, review, private=True)
    except (OSError, ValueError):
        print("Collection or output failed; no source log was modified.", file=sys.stderr)
        return 1
    print(text_summary(report), file=sys.stderr if args.json == "-" else sys.stdout)
    return 0
