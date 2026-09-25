# Tasks — add-prstate-core

Derived from `~/work/prstate-design/PLAN.md` work units 1.1-1.8 and its gated phases,
with the four review blockers (B1-B4, see `design.md`) folded into the units they touch.

Each phase has a verification gate. No phase starts before the previous one is green.

## 1. Contract

- [x] 1.1 `src/prstate/model.py` — enums, frozen dataclasses, `to_dict(full=)`,
      `BotFinding.open`, `SCHEMA_VERSION`. Serialization point: every other module
      imports it, it imports none of them.
- [ ] 1.2 Extend the model for B2: `ViewerReview` dataclass, `PullRequest.viewer_review`,
      `PullRequest.review_requested_from`, `PullRequest.head_oid`.

**Gate:** `python -c "import prstate.model"` with no other module present;
`json.dumps(sweep.to_dict())` round-trips with no datetime, Enum or dataclass surviving;
`minimized_reason="OUTDATED"` yields `open is False` and `"SPAM"` yields `open is True`.

## 2. Modules (parallel)

Five units, one agent each, no shared files. Each owns its own test file.

- [ ] 2.1 `query.py` — GraphQL text, page constants (`THREAD_PAGE=100`,
      `COMMENT_PAGE=50`, `REVIEW_PAGE=50`, `ROLLUP_PAGE=100`, `BATCH=10`), aliased
      per-PR batching, body trim at 1500. Selects node ids, `isMinimized`,
      `minimizedReason`, `contexts.pageInfo`, and for B2 `reviewRequests`,
      `viewerLatestReview`, `headRefOid`.
- [ ] 2.2 `classify.py` — every rule: `latest_per_check`, `ci_state`, `owed`,
      `bot_findings`, `reasons`, `partial`. Carries B1 (push counts only when
      `viewer == author`) and B4 (per-login minimize-or-recency mode switch).
- [ ] 2.3 `gh.py` — the only `subprocess` importer: argv allowlist, discovery, batched
      fetch, rollup pagination, `repoll_mergeable`, `Sweep` assembly. Carries B3's
      `fetch_details` entry point that bypasses discovery.
- [ ] 2.4 `render.py` — the bucketed report, `open` findings only, `--full` bodies.
- [ ] 2.5 `cli.py` — flags per `DESIGN.md` §5 plus B3's `--pr N` and `--any-author`,
      exit codes, `--json`.

**Gate:** each unit's own test file passes; no module outside `gh.py` imports
`subprocess`.

## 3. Integration

- [ ] 3.1 `__init__.py` export surface: `fetch`, `classify`, `Sweep`, `PullRequest`,
      `Ci`, `Check`, `Owed`, `BotFinding`, `ViewerReview`, the enums.
- [ ] 3.2 Fixture migration from `test_triage.py`, each fixture named for the rule it
      pins, not the PR it came from.
- [ ] 3.3 Read-only AST guard scanning the whole `src/prstate/` tree.

**Gate:** full `pytest` green; `uv run prstate --help` prints the flag set;
`uv build` produces a wheel containing `prstate/model.py`.

## 4. Live verification

- [ ] 4.1 Shape checks against real PRs: `prstate --json`, `--owed`,
      `--review-requested`, `--pr N`.
- [ ] 4.2 Differential against the original: `python3
      ~/.agents/skills/babysit-all-prs/triage.py --json` on the same scope. The ported
      classifier must agree except on the six changes the plan declares deliberate, plus
      B1's authorship gate. Every disagreement is adjudicated and recorded before the
      next phase.
- [ ] 4.3 Reviewer-perspective smoke: a sweep where `viewer != author`, proving B1 and
      B2 on live data.

**Gate:** no unexplained differential; reviewer sweep returns a triageable answer.

## 5. Skill cutover — gated on phase 4

Edits `/Users/filipeguerreiro/projects/dotfiles`, a different repo. A half-migrated
skill is a broken skill the user runs daily, so this phase starts only after phase 4 is
green, and each skill is cut over and verified one at a time.

- [ ] 5.1 `babysit-all-prs` — `triage.py` and `test_triage.py` deleted, SKILL.md points
      at `prstate`.
- [ ] 5.2 `reviews-needed` — phases 1 and 3 replaced by `prstate --review-requested
      --json`; bucketing and rendering prose stays.
- [ ] 5.3 `babysit-reviews` — both discovery searches and the enrich phase replaced.
- [ ] 5.4 `pr-comment-surfaces` — the GraphQL and jq blocks become documentation of
      `prstate --pr`, not instructions to re-derive it.
- [ ] 5.5 `review-swarm`, `pr-review-fanout`, `standup` — discovery and thread
      enumeration replaced; every mutation stays in the skill, since prstate never writes.

**Gate:** each migrated skill run once end to end against a live repo before the next is
touched.
