# Design — add-prstate-core

Full design: `docs/DESIGN.md` (462 lines). This summarizes the decisions the spec
depends on and records the four review blockers folded in before implementation.

## Transport

Shell out to `gh api graphql`, not httpx plus a token. `gh` inherits the keyring auth
the user already has, handles enterprise hosts and refresh, and keeps the runtime
dependency budget at zero. One subprocess per batch of ten PRs is irrelevant at sweep
scale.

Discovery is `gh search prs`; detail is raw GraphQL. `gh search prs --json` lacks
`mergeStateStatus` and `reviewDecision` that `gh pr list` has (cli/cli#13239), and
`gh pr view --json comments` omits review comments entirely (cli/cli#11477), so neither
`gh` porcelain can answer the question alone.

## The four normalization rules

1. **Three comment surfaces.** Review threads, review bodies, issue comments, unioned in
   one query. A `reviewThreads`-only read reports clean while a bot bug sits in an issue
   comment. Thread ends are fetched as separate `opener: comments(first:1)` and
   `recent: comments(last:1)` connections with `totalCount`, because the tail of a
   `first:N` page is comment N, not the newest. When the count exceeds the deduplicated
   ends, the thread node's cursor fetches every comment: a viewer reply in the middle
   changes whether the newest human signal is owed. Failure or a bounded page cap marks
   the pull request partial instead of guessing.

2. **Latest-per-check collapse**, keyed on `(workflow, check name)`. Within a workflow
   the higher `workflowRun.databaseId` wins; finish time cannot decide it, because
   concurrent runs interleave. Run ids are monotonic per repo, not per workflow, so two
   workflows each defining `test` both survive. Unknown conclusions rank worse than green
   and never collapse into pass.

3. **Owed = a human signal with no later viewer activity**, per surface. Unresolved is
   not unanswered; GitHub's resolve button is left untouched long after a conversation
   ends. Push time is `commits(last:1).committedDate`, the early bound of the push
   interval, because reading the push late credits it with answering a review it may
   predate.

4. **Bot supersede.** GitHub's own `isMinimized`/`minimizedReason` decides for bots that
   minimize; newest-per-login decides for bots that do not. See B4 below for the
   precedence correction.

## Review blockers folded in

The pre-implementation review raised four blockers. All four are accepted and are
requirements in `specs/pr-state/spec.md`.

- **B1 — the push must be gated on authorship.** Earlier logic appended the head commit
  time unconditionally. That was safe only when `viewer == author` (discovery was
  `--author=@me`). Generalized to a reviewer sweep it lets the PR author's push discharge
  a third party's review body. The push counts as viewer activity only when
  `viewer == pr.author`.

- **B2 — the contract must carry the viewer's own review state.** `approved_by` and
  `changes_requested_by` deliberately exclude the viewer, so
  "have I already reviewed this, and has the author pushed since?" — the question for
  reviewer-focused consumers — was unanswerable. Adds `viewer_review` (state +
  submitted_at), `review_requested_from`, and `head_oid`. `viewerLatestReview` resolves
  against the token's identity, so when `viewer` differs from the token login the value is
  computed by scanning `reviews` instead.

- **B3 — a single named PR must be addressable.** A consumer can be invoked on an
  arbitrary PR, usually one the viewer neither authored nor was asked to review, and
  discovery-by-search cannot reach it. Adds `--pr N` (requires `--repo`, bypasses
  discovery) and `--any-author`, which is the `author=None` the library already documented
  but the CLI could not produce.

- **B4 — minimize and recency must not be mixed in one ordering.** Taking "the newest
  live candidate per login" still retires a live lane when a bot posts several concurrent
  comment lanes, which is the exact case the change was introduced to fix. Precedence is
  a per-login mode switch: if any candidate from that login is minimized, trust minimize
  exclusively and apply no recency supersession; otherwise fall back to newest-per-login.
  `superseded_by` then means one thing — the recency fallback — instead of two.

`minimizedReason` values are uppercase and not limited to the set the plan assumed;
`LOW_QUALITY` and `SPAM` both occur in the wild. Only `OUTDATED` and `RESOLVED` count as
supersession. Spam is hidden, not replaced.

## Module boundary

`gh.py` is the only module that imports `subprocess`. Every rule above is a pure function
from a GraphQL payload dict to a dataclass, so the whole rules suite runs offline against
fixtures. `query.py` is separate from `gh.py` so the page-size constants are provably
reachable in the emitted query text without a subprocess — hardcoding a page size twice
is what previously let a smaller page silently stop a truncation check from firing.

Read-only is enforced by a test, not a convention. The command builder permits search,
read-only GraphQL and user reads, repository owner lookup, and exactly `gh auth status`.
It rejects GraphQL mutations, write HTTP flags, credential-changing auth commands, and
token display. `gh.py` remains the only module allowed to import `subprocess`.
