"""Graph-level limits.

These only ever narrow the host's own limits. The effective value of each
limit is the minimum of the package default, the host admission, and the
request's own tighter settings; nothing in graph state can raise it. The
defaults are conservative engineering choices, not measured optima.
"""

from __future__ import annotations

DEFAULT_LIMITS = {
    "max_parallel_reads": 2,  # concurrent read-only jobs
    "max_writers": 1,  # concurrent writer jobs (the graph never exceeds one)
    "max_jobs": 12,  # total admitted jobs per attempt, across resumes
    "max_repair_rounds": 2,
    "max_review_rounds": 2,
    "max_investigations": 4,
    "max_plan_steps": 16,
    "max_plan_edits": 3,
    "max_transitions": 60,  # LangGraph recursion limit (super-steps)
    "delegation_depth": 0,  # no recursive child teams
}


def effective_limits(*layers: dict | None) -> dict[str, int]:
    """Intersect limit layers. Unknown keys and non-integers are ignored."""
    limits = dict(DEFAULT_LIMITS)
    for layer in layers:
        for key, value in (layer or {}).items():
            if key in limits and isinstance(value, int) and not isinstance(value, bool):
                limits[key] = max(0, min(limits[key], value))
    limits["max_writers"] = min(limits["max_writers"], 1)
    limits["max_parallel_reads"] = max(1, limits["max_parallel_reads"])
    limits["max_transitions"] = max(10, limits["max_transitions"])
    return limits
