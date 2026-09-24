---
name: langgraph-workflow
description: Run a durable, verified workflow for a code change or a read-only research question. Use when the user asks for a change that must be verified by checks, needs plan approval, or should survive restarts; or for research that combines several independent investigations.
---

# LangGraph workflows

The `workflows` MCP server keeps the workflow state; you do the work.

## Start

- **Change**: `workflow_start` with `workflow="verified_change"`, the user's
  `goal`, and acceptance `checks` that prove the result, for example
  `{"id": "readme", "kind": "file_contains", "path": "README.md", "value": "Install", "requirement": "README explains installation"}`.
  Kinds `file_exists`, `file_contains` and `json_value` (`pointer`, `value`)
  are verified by the plugin. `command` and `human_review` checks always end in
  needs-review. Pass `plan_steps` only if the user already agreed on a plan.
- **Research**: `workflow_start` with `workflow="research"`, the `goal`, and
  up to four independent `investigations`.

## Loop

Each result has a `status` and, while `waiting_for_job`, a list of `jobs`:

1. Do each job with your normal tools. A job with `access: "read"` must not
   change any file. The plugin compares the workspace before and after, and
   blocks the workflow if a read job changed something.
2. Call `workflow_report` with the `attempt_id`, the job's `operation_id`,
   `outcome` (`completed`, `failed`, or `refused` if a permission was denied),
   and a `result` shaped like `result_shape`. Report only what happened:
   file changes and checks are verified independently of your report.
3. Continue with the next result until the status is final.

Plan approval and conflict choices are asked of the user in a Locus prompt
during `workflow_start` or `workflow_report`. You cannot answer them. If the
status is `waiting_for_input`, ask the user to answer the prompt, then call
`workflow_status`.

## Finish

- `verified`: every declared check passed on the current files.
- `needs_review`: tell the user which requirement is not machine-verified
  (see `blocker`).
- `denied`, `blocked`, `failed`, `cancelled`: explain the `blocker`.

Use `workflow_cancel` if the user wants to stop; stop working on its jobs.
