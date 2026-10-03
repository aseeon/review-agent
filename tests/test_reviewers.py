import pytest

from review_agent.reviewers import REVIEWERS

BUDGETS = {r.name: r for r in REVIEWERS}


def test_env_overrides_one_reviewer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REVIEW_AGENT_CORRECTNESS_BUDGET_USD", raising=False)
    monkeypatch.setenv("REVIEW_AGENT_SECURITY_BUDGET_USD", "7.5")
    assert BUDGETS["security"].budget() == 7.5
    assert BUDGETS["correctness"].budget() == BUDGETS["correctness"].budget_usd


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "nan", "inf"])
def test_invalid_override_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv("REVIEW_AGENT_PERFORMANCE_BUDGET_USD", raw)
    with pytest.raises(ValueError, match="REVIEW_AGENT_PERFORMANCE_BUDGET_USD"):
        _ = BUDGETS["performance"].budget()


def test_each_reviewer_prompt_is_filled_in_with_its_own_area():
    for r in REVIEWERS:
        prompt = r.system_prompt()
        assert f"{r.name} defects only" in prompt and "{" not in prompt
