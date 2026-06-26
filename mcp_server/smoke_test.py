"""Live smoke test against the real openFDA API — no LLM / OpenRouter needed.

Validates that the four tool queries actually work end-to-end against
api.fda.gov before you wire in the agent. Resolves a drug name, then runs the
label, adverse-event, and recall lookups against the resolved generic name.

Usage:
    python smoke_test.py            # defaults to "Tylenol"
    python smoke_test.py ibuprofen
    OPENFDA_API_KEY=... python smoke_test.py "Advil"

Exit code is non-zero if resolve_drug fails (the rest are informational —
empty results are valid for many drugs).
"""

import asyncio
import json
import sys

import fda_client


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, default=str)[:2000])


async def main(name: str) -> int:
    print(f"Looking up: {name!r}")

    resolved = await fda_client.resolve_drug(name)
    show("resolve_drug", resolved)
    if not resolved["found"]:
        print(f"\n❌ Could not resolve {name!r}. Check the spelling or try a generic name.")
        return 1

    generic = resolved["generic_name"]
    print(f"\nResolved to generic: {generic!r}  — running remaining tools against it.")

    show("get_label", await fda_client.get_label(generic))
    show("count_adverse_events (top 5)",
         await fda_client.count_adverse_events(generic, limit=5))
    show("check_recalls", await fda_client.check_recalls(generic))

    print("\n✅ Smoke test complete (resolve succeeded).")
    return 0


if __name__ == "__main__":
    drug = sys.argv[1] if len(sys.argv) > 1 else "Tylenol"
    sys.exit(asyncio.run(main(drug)))
