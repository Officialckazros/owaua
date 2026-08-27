"""Run deterministic AI control-plane regression cases without provider calls."""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "eval-token")

from sefbot import ai_control, brain, config, db  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "evals" / "ai_core.json").read_text(encoding="utf-8"))
    old_path = config.DB_PATH
    config.DB_PATH = ":memory:"
    failures: list[dict] = []
    try:
        for case in cases:
            kind = case["kind"]
            if kind == "memory_gate":
                actual = brain.should_extract_turn_memories(case["input"])
            elif kind == "secret":
                actual = not bool(brain._safe_memory_content(case["input"]))
            elif kind == "schema":
                actual = ai_control.validate_structured(case["input"], case["schema"]) is not None
            elif kind == "route":
                user_id = f"eval-{case['id']}"
                ai_control.set_user_mode(user_id, case["mode"])
                actual = ai_control.route(case["task"], user_id=user_id).tier
            else:
                actual = "unknown case kind"
            if actual != case["expected"]:
                failures.append({"id": case["id"], "expected": case["expected"], "actual": actual})
    finally:
        db.close()
        config.DB_PATH = old_path
    print(json.dumps({"cases": len(cases), "passed": len(cases) - len(failures), "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
