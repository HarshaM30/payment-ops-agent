import pytest

from payops.db.models.enums import ScenarioType
from scripts.seed import MIN_PER_SCENARIO, scenario_counts


def test_default_distribution() -> None:
    counts = scenario_counts(60)
    assert sum(counts.values()) == 60
    assert set(counts) == set(ScenarioType)
    assert all(n >= MIN_PER_SCENARIO for s, n in counts.items() if s is not ScenarioType.CURRENT)


def test_minimum_size() -> None:
    counts = scenario_counts(30)
    assert counts[ScenarioType.CURRENT] == 0
    with pytest.raises(ValueError, match="too small"):
        scenario_counts(29)
