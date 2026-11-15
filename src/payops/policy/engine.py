"""Runs every guardrail and combines the results. Pure and deterministic."""

from dataclasses import dataclass

from payops.policy.rules import RULES, Outcome, PolicyInput, PolicyResult, Rule


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    results: tuple[PolicyResult, ...]

    @property
    def violations(self) -> list[PolicyResult]:
        return [r for r in self.results if r.outcome is Outcome.BLOCK]

    @property
    def approval_reasons(self) -> list[str]:
        return [r.rule for r in self.results if r.outcome is Outcome.NEEDS_APPROVAL]

    @property
    def violated_rules(self) -> list[str]:
        return [r.rule for r in self.violations]

    def to_json(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "results": [
                {"rule": r.rule, "outcome": r.outcome.value, "message": r.message}
                for r in self.results
            ],
        }


def evaluate(inp: PolicyInput, rules: tuple[Rule, ...] = RULES) -> PolicyDecision:
    """Block beats needs-approval beats allow. Every rule runs, so the audit shows all."""
    results = tuple(rule(inp) for rule in rules)
    if any(r.outcome is Outcome.BLOCK for r in results):
        outcome = Outcome.BLOCK
    elif any(r.outcome is Outcome.NEEDS_APPROVAL for r in results):
        outcome = Outcome.NEEDS_APPROVAL
    else:
        outcome = Outcome.ALLOW
    return PolicyDecision(outcome, results)
