"""Fixed, code-defined workflow registry. Requests select a workflow by name.
``custom`` interprets a user-drawn graph that is plain data validated by
``definitions.validate_definition``; no code is ever loaded from data."""

from . import custom, research, verified_change

WORKFLOWS = {"verified_change": verified_change, "research": research, "custom": custom}
