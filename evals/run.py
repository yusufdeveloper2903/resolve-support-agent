"""Scenario evals for the support agent.

    python -m evals.run                    # replay recorded model turns (deterministic, no key)
    python -m evals.run --live             # score the real model (needs ANTHROPIC_API_KEY)
    python -m evals.run --live --record    # ...and save its turns as the new replay recordings

Each scenario runs against a freshly seeded database. Checks look at what actually happened
(the audit log, approvals and order rows) rather than at what the model claims, plus a few
regexes on the final reply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import yaml

from resolve import approvals, conversations
from resolve.agent import Agent, AnthropicModel, ModelClient, ModelStream
from resolve.config import Settings
from resolve.db import Database
from resolve.replay import ReplayModel, Turn
from resolve.seed import seed

EVALS_DIR = Path(__file__).parent
SCENARIOS_DIR = EVALS_DIR / "scenarios"
RECORDINGS_DIR = EVALS_DIR / "recordings"
RESULTS_DIR = EVALS_DIR / "results"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Outcome:
    scenario: dict[str, Any]
    checks: list[Check] = field(default_factory=list)
    reply: str = ""
    error: str | None = None
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


class RecordingModel:
    """Wraps a real client and keeps each final message as a replayable turn."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.turns: list[Turn] = []
        self.usage: dict[str, int] = {}

    @asynccontextmanager
    async def stream(self, params: dict[str, Any]) -> AsyncIterator[ModelStream]:
        async with self._inner.stream(params) as stream:
            yield stream
            message = await stream.get_final_message()
        blocks = [b.to_dict(mode="json") for b in message.content if b.type in ("text", "tool_use")]
        self.turns.append({"content": blocks, "stop_reason": message.stop_reason})
        for key, value in message.usage.to_dict().items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value


def load_scenarios(only: str | None = None) -> list[dict[str, Any]]:
    scenarios = [yaml.safe_load(p.read_text()) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))]
    return [s for s in scenarios if only is None or only in s["id"]]


async def run_scenario(scenario: dict[str, Any], model: ModelClient, settings: Settings) -> Outcome:
    outcome = Outcome(scenario)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eval.db")
        seed(db)
        agent = Agent(
            db,
            model,
            model_name=settings.model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
        )
        conversation = conversations.create(db, scenario["customer"])
        try:
            for message in scenario["messages"]:
                outcome.reply = await _drive(agent, db, conversation["id"], message, scenario)
        except Exception as exc:  # a crashed scenario is a failed scenario, not a crashed suite
            outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.checks = grade(scenario.get("expect", {}), db, conversation["id"], outcome.reply)
    outcome.seconds = time.perf_counter() - started
    return outcome


async def _drive(
    agent: Agent, db: Database, conversation_id: str, message: str, scenario: dict[str, Any]
) -> str:
    """Send one customer message; play the operator if the turn parks for approval."""
    events = [e async for e in agent.chat(conversation_id, message)]
    decision = scenario.get("decision")
    if events and events[-1].type == "approval_required" and decision:
        for approval_id in events[-1].data["approval_ids"]:
            approvals.decide(
                db, approval_id, approve=decision == "approve", operator="eval",
                note=scenario.get("decision_note"),
            )  # fmt: skip
        events += [e async for e in agent.resume(conversation_id)]
    errors = [e.data["message"] for e in events if e.type == "error"]
    if errors:
        raise RuntimeError("; ".join(errors))
    return "".join(e.data["delta"] for e in events if e.type == "text")


def grade(expect: dict[str, Any], db: Database, conversation_id: str, reply: str) -> list[Check]:
    calls = db.query(
        "SELECT tool, outcome FROM audit_log WHERE conversation_id = ? AND actor = 'agent' "
        "ORDER BY id",
        (conversation_id,),
    )
    called = [c["tool"] for c in calls]
    last_outcome = {c["tool"]: c["outcome"] for c in calls}
    checks: list[Check] = []

    if "tools_called" in expect:
        remaining = iter(called)  # ordered subsequence: other calls may happen in between
        ok = all(any(tool == c for c in remaining) for tool in expect["tools_called"])
        checks.append(Check("tools called in order", ok, f"called {called}"))
    for tool in expect.get("tools_not_called", []):
        checks.append(Check(f"never calls {tool}", tool not in called, f"called {called}"))
    for tool, wanted in expect.get("outcomes", {}).items():
        got = last_outcome.get(tool)
        checks.append(Check(f"{tool} -> {wanted}", got == wanted, f"got {got}"))
    if "approval_requested" in expect:
        n = len(db.query("SELECT id FROM approvals WHERE conversation_id = ?", (conversation_id,)))
        wanted = expect["approval_requested"]
        checks.append(Check(f"approval requested: {wanted}", (n > 0) == wanted, f"{n} approvals"))
    for order_id, fields in expect.get("orders", {}).items():
        row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,)) or {}
        for column, wanted in fields.items():
            got = row.get(column)
            ok = _matches(got, wanted)
            checks.append(Check(f"{order_id}.{column} = {wanted}", ok, f"got {got!r}"))
    for pattern in expect.get("reply_includes", []):
        found = re.search(pattern, reply, re.IGNORECASE) is not None
        checks.append(Check(f"reply mentions /{pattern}/", found))
    for pattern in expect.get("reply_excludes", []):
        found = re.search(pattern, reply, re.IGNORECASE) is not None
        checks.append(Check(f"reply avoids /{pattern}/", not found))
    return checks


def _matches(got: Any, wanted: Any) -> bool:
    """Equality, except that `"~text"` means "contains text"."""
    if isinstance(wanted, str) and wanted.startswith("~"):
        return wanted[1:] in str(got)
    return bool(got == wanted)


def report(outcomes: list[Outcome], mode: str) -> str:
    lines = [
        f"## Eval results ({mode})",
        "",
        "| Scenario | Category | Checks | Result |",
        "|---|---|---|---|",
    ]
    for o in outcomes:
        passed = sum(c.passed for c in o.checks)
        result = "pass" if o.passed else "FAIL"
        lines.append(
            f"| {o.scenario['title']} | {o.scenario['category']} | {passed}/{len(o.checks)} | "
            f"{result} |"
        )
    total = sum(o.passed for o in outcomes)
    checks = [c for o in outcomes for c in o.checks]
    lines += [
        "",
        f"**{total}/{len(outcomes)} scenarios passed**, "
        f"{sum(c.passed for c in checks)}/{len(checks)} checks.",
    ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.live and not settings.api_key:
        print("--live needs ANTHROPIC_API_KEY (or the key file); nothing was run.", file=sys.stderr)
        return 2
    client = anthropic.AsyncAnthropic(api_key=settings.api_key) if args.live else None
    outcomes: list[Outcome] = []
    usage: dict[str, int] = {}
    for scenario in load_scenarios(args.only):
        recording = RECORDINGS_DIR / f"{scenario['id']}.yaml"
        model: ReplayModel | RecordingModel
        if client is not None:
            model = RecordingModel(AnthropicModel(client))
        else:
            model = ReplayModel(yaml.safe_load(recording.read_text())["turns"])
        outcome = await run_scenario(scenario, model, settings)
        outcomes.append(outcome)
        status = "PASS" if outcome.passed else "FAIL"
        print(f"{status}  {scenario['id']}  ({outcome.seconds:.1f}s)")
        for check in outcome.checks:
            if not check.passed:
                print(f"      x {check.name}  {check.detail}")
        if outcome.error:
            print(f"      x error: {outcome.error}")
        if isinstance(model, RecordingModel):
            for key, value in model.usage.items():
                usage[key] = usage.get(key, 0) + value
            if args.record:
                recording.write_text(yaml.safe_dump({"turns": model.turns}, sort_keys=False))

    mode = f"live, {settings.model}" if args.live else "replay"
    summary = report(outcomes, mode)
    print("\n" + summary)
    if usage:
        print(f"\nToken usage: {json.dumps(usage)}")
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"{'live' if args.live else 'replay'}.md").write_text(summary + "\n")
    return 0 if all(o.passed for o in outcomes) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--live", action="store_true", help="call the real model")
    parser.add_argument("--record", action="store_true", help="with --live: save recordings")
    parser.add_argument("--only", help="run scenarios whose id contains this text")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
