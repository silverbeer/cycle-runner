"""Deterministic tests of turning the recommender's decision into a Recommendation."""

import pytest

from cycle_runner.recommendation import (
    Recommendation,
    RecommendationRejected,
    RecommenderOutput,
    build_recommendation,
    render,
)


def _issue(issue_id, title, status="Todo", priority="High", blocked_by=()):
    return {
        "id": issue_id, "title": title, "status": status, "estimate": 2, "priority": priority,
        "labels": [], "age_days": 5, "blocked_by": list(blocked_by),
    }


EVIDENCE = {
    "cycle_number": 9,
    "cycle_goal": None,
    "open_issues": {
        "SB-1": _issue("SB-1", "Finish login fix", status="In Progress"),
        "SB-2": _issue("SB-2", "Add rate limiting", priority="Urgent"),
        "SB-3": _issue("SB-3", "Rate-limit dashboards", blocked_by=["SB-2"]),
    },
}


def _output(pick="SB-1", candidates=("SB-1", "SB-2"), unknowns=()):
    return {
        "candidates": [{"issue_id": c, "rationale": f"why {c}"} for c in candidates],
        "recommended_issue_id": pick,
        "unknowns": list(unknowns),
    }


def test_produces_a_valid_recommendation_model():
    recommendation = build_recommendation(_output(), EVIDENCE)

    assert isinstance(recommendation, Recommendation)
    assert Recommendation.model_validate(recommendation.model_dump()) == recommendation


def test_recommended_issue_is_an_issue_id_among_the_candidates():
    recommendation = build_recommendation(_output(pick="SB-2"), EVIDENCE)

    assert recommendation.recommended_issue_id == "SB-2"
    assert "SB-2" in [c.issue_id for c in recommendation.candidates]


def test_facts_and_titles_come_from_linear_not_the_model():
    output = _output()
    output["candidates"][0]["title"] = "An invented title"  # extra field the model might add

    candidate = build_recommendation(output, EVIDENCE).candidates[0]

    assert candidate.title == "Finish login fix"
    assert candidate.facts.model_dump() == {
        "status": "In Progress", "priority": "High", "estimate": 2, "age_days": 5, "blocked_by": [],
    }
    assert candidate.rationale == "why SB-1"


def test_a_blocked_issue_cannot_be_the_recommendation():
    with pytest.raises(RecommendationRejected, match="SB-3"):
        build_recommendation(_output(pick="SB-3", candidates=("SB-1", "SB-3")), EVIDENCE)


def test_blocked_candidates_are_dropped_and_listed_as_blocked():
    recommendation = build_recommendation(_output(candidates=("SB-1", "SB-3")), EVIDENCE)

    assert [c.issue_id for c in recommendation.candidates] == ["SB-1"]
    assert recommendation.blocked == {"SB-3": ["SB-2"]}


def test_an_id_that_is_not_an_open_issue_cannot_be_recommended():
    with pytest.raises(RecommendationRejected, match="SB-99"):
        build_recommendation(_output(pick="SB-99", candidates=("SB-99",)), EVIDENCE)


def test_missing_cycle_goal_is_always_an_unknown():
    recommendation = build_recommendation(_output(unknowns=["No due dates"]), EVIDENCE)

    assert recommendation.unknowns == ["No due dates", "The cycle has no goal set in Linear."]


def test_goal_unknown_is_not_duplicated_or_added_when_a_goal_exists():
    from_model = build_recommendation(_output(unknowns=["The cycle has no goal."]), EVIDENCE)
    with_goal = build_recommendation(_output(), {**EVIDENCE, "cycle_goal": "Ship v2"})

    assert from_model.unknowns == ["The cycle has no goal."]
    assert with_goal.unknowns == []


def test_nothing_actionable_is_a_valid_recommendation_with_no_pick():
    recommendation = build_recommendation(_output(pick=None, candidates=()), EVIDENCE)

    assert recommendation.recommended_issue_id is None
    assert recommendation.candidates == []
    assert "proceed with" not in recommendation.approval_question


def test_rejects_output_when_the_recommender_never_read_the_cycle():
    with pytest.raises(RecommendationRejected, match="did not read the cycle"):
        build_recommendation(_output(), None)


def test_rejects_output_when_the_cycle_could_not_be_read():
    with pytest.raises(RecommendationRejected, match="unavailable"):
        build_recommendation(_output(), {"error": "Cycle state is unavailable: boom"})


def test_the_model_schema_caps_candidates_at_three():
    with pytest.raises(ValueError):
        RecommenderOutput.model_validate(_output(candidates=("SB-1", "SB-2", "SB-3", "SB-4")))


def test_output_without_a_pick_survives_adk_dropping_none_fields():
    # ADK's validate_schema dumps with exclude_none, so a null pick arrives missing.
    output = _output(pick=None)
    del output["recommended_issue_id"]

    assert build_recommendation(output, EVIDENCE).recommended_issue_id is None


def test_rendering_keeps_facts_reasoning_and_pick_apart():
    text = render(build_recommendation(_output(candidates=("SB-1", "SB-3")), EVIDENCE))

    assert "Linear facts: In Progress, priority High, estimate 2, 5 days old, blocked by nothing." in text
    assert "Why: why SB-1" in text
    assert "My recommendation: SB-1." in text
    assert "- SB-3, blocked by SB-2" in text
    assert "- The cycle has no goal set in Linear." in text
    assert text.endswith("Do you want to proceed with SB-1?")
