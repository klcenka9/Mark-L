# SELF_IMPROVE — self-modifying layer

JARVIS can read, analyze and improve its own code inside safe, auditable
boundaries. The layer has four parts:

| Part | File | Role |
| --- | --- | --- |
| Engine | `core/self_mod.py` | read/analyze/classify/apply diffs, status system, audit log |
| Mode | `actions/self_improve.py` | bounded improvement loop + `self_improve` voice tool |
| Registry | `actions/registry.py` + `actions/registry.json` | dynamically added actions (born YELLOW) |
| Approval backend | `core/review_gate.py` | shared state machine pending→approved/rejected→applied, always re-classifies from diff content |
| Human gate (CLI) | `scripts/review_changes.py` | terminal review: paged walkthrough, typed hash-bound phrase |
| Human gate (HUD) | `ui_review.py` + ⛨ REVIEW CHANGES button in the ⚙ drawer | PyQt dialogs, see below |

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
  (`core/self_mod.py`, `core/review_gate.py`, `core/prompt.txt`,
  `config/api_keys.json`, the review CLI, the CI gate, `ui_review.py`,
  `tests/test_self_mod.py`). The agent can only *queue* these;
  `apply_diff()` refuses them unconditionally. Approval requires a separate
  heavier flow (CLI or HUD, below), and the change is applied on a
  dedicated `core-safety/*` branch, never mixed with other changes.

## HUD review (ui_review.py)

The ⚙ drawer has a **⛨ REVIEW CHANGES** button (with a pending count).
It opens `PendingChangesDialog` — list + full detail (diff, rationale,
expected effect, rollback plan) of every waiting change. Ordinary
DANGEROUS changes are approved with an explicit yes/no and applied
immediately (hash-matched), or rejected with a logged reason.

A core-safety record never gets the ordinary button. It opens
`CoreSafetyDialog` — a **separate red screen** titled CORE SAFETY CHANGE:

1. the approve button is **greyed out** until the reviewer scrolls through
   the entire diff + rationale + rollback plan **and** a time delay
   (`CORE_SAFETY_DELAY_S`, 5 s) has elapsed since the dialog opened;
2. the first click only *arms* the dialog — a second confirmation appears
   in the same window ("Really? This change modifies the safety mechanism
   of the agent itself.");
3. only the second click approves and applies, via
   `core/review_gate.approve_and_apply_core_safety()`.

Both surfaces (CLI and HUD) drive the same protected backend
`core/review_gate.py`; every approval/rejection is audit-logged with
`approved_by`, timestamp and diff hash regardless of the UI used. Diffs
that merely *call* the safety API (`review_gate`, `apply_diff`,
`classify_change`, …) are themselves classified at least DANGEROUS, so the
agent cannot generate code that self-approves.

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
