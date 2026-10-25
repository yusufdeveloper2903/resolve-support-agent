"""The eval harness: the replay suite passes, and the grader catches a misbehaving agent."""

from __future__ import annotations

import yaml

from evals.run import RECORDINGS_DIR, load_scenarios, run_scenario
from resolve.config import Settings
from resolve.replay import ReplayModel, text, tools


async def test_replay_suite_passes(settings: Settings) -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 15
    for scenario in scenarios:
        turns = yaml.safe_load((RECORDINGS_DIR / f"{scenario['id']}.yaml").read_text())["turns"]
        outcome = await run_scenario(scenario, ReplayModel(turns), settings)
        assert outcome.passed, (scenario["id"], outcome.error, outcome.checks)


async def test_grader_flags_a_false_refund_claim(settings: Settings) -> None:
    (scenario,) = load_scenarios("05-refund-needs-approval")
    liar = ReplayModel([text("All done, your refund is complete!")])
    outcome = await run_scenario(scenario, liar, settings)
    failed = {check.name for check in outcome.checks if not check.passed}
    assert "issue_refund -> pending_approval" in failed
    assert "approval requested: True" in failed
    assert any(name.startswith("reply avoids") for name in failed)


async def test_grader_reports_a_crash_as_a_failure(settings: Settings) -> None:
    (scenario,) = load_scenarios("01-order-tracking")
    outcome = await run_scenario(scenario, ReplayModel([tools(("lookup_customer", {}))]), settings)
    assert not outcome.passed
    assert outcome.error is not None
    assert "ScriptExhausted" in outcome.error
