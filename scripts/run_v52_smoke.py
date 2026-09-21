"""Run from repository root: python -m scripts.run_v52_smoke."""

import asyncio
import json

from delaybind_core.v52_smoke import run_smoke


if __name__ == "__main__":
    result = asyncio.run(run_smoke())
    print(json.dumps({k: result[k] for k in (
        "status", "reason_codes", "answer", "windows", "interface_calls", "v52_metrics")}, ensure_ascii=False, indent=2))
    if result["status"] != "ANSWERED":
        raise SystemExit(1)
