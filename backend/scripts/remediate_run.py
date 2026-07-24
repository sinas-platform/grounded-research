"""Run one remediation pass over a query run's answer.

Usage (from the backend directory, venv python):
    python -m scripts.remediate_run <query_run_id>
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid


async def main() -> int:
    from app.services.remediation import remediate_answer

    run_id = uuid.UUID(sys.argv[1])
    audit = await remediate_answer(run_id)
    print(json.dumps(audit, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
