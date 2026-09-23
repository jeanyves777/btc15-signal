"""Compatibility shim. The dataset itself now lives in the package.

It moved to `btc15_signal.learning_data` because the service has to be able to
build it: while it lived here, "the learning loop" meant a person remembering
to run a script, and the training path reached the feature definitions by a
different route from the live path. One definition, one module, both callers.

Everything below re-exports the package functions unchanged, so the research
scripts that import this keep working and cannot drift from the deployed
loader - there is only one implementation left to drift from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.learning_data import (  # noqa: E402,F401
    brti_context,
    choose_minute,
    load_brti_rows,
    load_policy_rows,
)

__all__ = ["brti_context", "choose_minute", "load_brti_rows", "load_policy_rows"]


if __name__ == "__main__":
    from collections import Counter

    for label, loader in (("POLICY (scans 660s->360s)", load_policy_rows),
                          ("first-minute only (660s)", load_brti_rows)):
        rows = loader()
        taken = [r for r in rows if r["rule_match"]]
        print(f"{label:<28} markets {len(rows):<6} qualified {len(taken):<6} "
              f"rejected {len(rows) - len(taken)}")
        if taken:
            entry = Counter(r["remaining_s"] for r in taken)
            print("      entry minute of qualified:",
                  ", ".join(f"{s}s:{n}" for s, n in sorted(entry.items(),
                                                           reverse=True)))
    rows = load_policy_rows()
    print("\nsessions:", Counter(r["session"] for r in rows).most_common())
    print("vol:", Counter(r["vol_regime"] for r in rows).most_common())
    print("contexts:", len({brti_context(r) for r in rows}), "distinct cells")
