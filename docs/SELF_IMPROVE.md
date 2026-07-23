# SELF_IMPROVE — self-modifying layer

JARVIS can read, analyze and improve its own code inside safe, auditable
boundaries. The layer has four parts:

| Part | File | Role |
| --- | --- | --- |
| Engine | `core/self_mod.py` | read/analyze/classify/apply diffs, status system, audit log |
| Mode | `actions/self_improve.py` | bounded improvement loop + `self_improve` voice tool |
| Registry | `actions/registry.py` + `actions/registry.json` | dynamically added actions (born YELLOW) |
| Human gate | `scripts/review_changes.py` | the ONLY way to approve dangerous / core-safety changes |

## Risk levels

- **safe** — prompts, helpers, logging, docs, tests, refactors without data
  deletion. Applied automatically: new git branch → `git apply` → commit →
  tests before/after → automatic `git revert` on regression.
- **dangerous** — user-file manipulation, system actions (power/network),
  credentials, deployment, dependency changes. Never applied silently:
  queued in `pending_changes/<id>.json` with diff + rationale +
  expected_effect + rollback_plan, waits for explicit human approval
  (`python scripts/review_changes.py approve <id>`, typed `yes`).
  No response ≠ approval — there is no timeout-approve.
- **core_safety_change** — anything touching `PROTECTED_PATHS`
  (`core/self_mod.py`, `core/prompt.txt`, `config/api_keys.json`, the review
  CLI, the CI gate, `tests/test_self_mod.py`). The agent can only *queue*
  these; `apply_diff()` refuses them unconditionally. Approval requires the
  separate red CORE SAFETY flow in the CLI: full diff walkthrough, a
  seconds-long delay, typing `APPROVE CORE SAFETY <diff-hash>`, then a
  second `I UNDERSTAND` confirmation. The change is applied by the CLI
  itself on a dedicated `core-safety/*` branch, never mixed with other
  changes.

The classification is enforced three times independently:
1. `classify_change()` re-runs inside `apply_diff()` — the caller's label
   can only raise severity, never lower it;
2. `scripts/review_changes.py` re-classifies before approving;
3. CI (`.github/workflows/protected-paths.yml` →
   `scripts/check_protected_paths.py`, which keeps its own literal copy of
   the protected list) refuses any PR touching protected paths without a
   human core-safety approval record.

## Run limits

One SELF_IMPROVE run stops at: 10 iterations, 5 auto-modified files,
600 s wall clock, or 3 attempts on one module without a status change
(reported to the user instead of retrying). Scheduled runs fire at most
once per 24 h; a module crossing the RED threshold also triggers a run.

## Status system

`memory/module_status.json`, computed from real calls
(`self_mod.record_call` is wired into the tool dispatcher):

- **RED** — error rate > 20 % over the last 20 calls (min. 5 calls)
- **YELLOW** — errors present but under the RED threshold, or a new /
  never-exercised module (new registry actions are born YELLOW)
- **GREEN** — 0 errors over the tracked window and passing tests

Every status transition, application, approval and rejection is appended
to `logs/self_improve_audit.jsonl` as one JSON line:
`{timestamp, module, event, risk_level, applied, approved_by, test_result,
status_before, status_after, ...}`.

## Voice commands

- "How healthy are your modules?" → `self_improve(action='status')`
- "Improve your web search module" → `self_improve(action='run', target='web_search')`
- "What changes are waiting for me?" → `self_improve(action='pending')`
- "Show your self-modification history" → `self_improve(action='audit')`

## Rollback

Every applied change is a single commit on its own
`self-improve/<module>-<timestamp>` branch — rollback is always
`git revert <commit>`. Regressions detected by the post-apply test run are
reverted automatically.
