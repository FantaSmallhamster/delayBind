"""Offline only: exercise all three REBIND outcomes through the real text runner."""
import asyncio
import json
from delaybind_core.smoke_r2 import run_smoke


async def main():
    rows = []
    for outcome in ("keep", "replace", "unbound"):
        result = await run_smoke(outcome=outcome)
        assert result["status"] in {"ANSWERED", "INSUFFICIENT"}, result["reason_codes"]
        assert result["r2_metrics"]["transaction_replay_consistency"] == 1
        rows.append(dict(outcome=outcome, status=result["status"], answer=result["answer"]["answer"],
                         interface_calls=result["interface_calls"], metrics=result["r2_metrics"]))
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
