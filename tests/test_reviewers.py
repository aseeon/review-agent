import pytest

from review_agent.reviewers import REVIEWERS

BUDGETS = {r.name: r for r in REVIEWERS}


def test_default_budgets():
    assert {name: r.budget() for name, r in BUDGETS.items()} == {"correctness": 2.0, "security": 4.0, "performance": 2.0}


def test_env_overrides_one_reviewer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REVIEW_AGENT_SECURITY_BUDGET_USD", "7.5")
    assert BUDGETS["security"].budget() == 7.5
    assert BUDGETS["correctness"].budget() == 2.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "nan", "inf"])
def test_invalid_override_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv("REVIEW_AGENT_PERFORMANCE_BUDGET_USD", raw)
    with pytest.raises(ValueError, match="REVIEW_AGENT_PERFORMANCE_BUDGET_USD"):
        _ = BUDGETS["performance"].budget()
