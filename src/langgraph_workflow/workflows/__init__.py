"""Fixed, code-defined workflow registry. Requests select a workflow by name;
no workflow definition is ever loaded from data."""

from . import research, verified_change

WORKFLOWS = {"verified_change": verified_change, "research": research}
