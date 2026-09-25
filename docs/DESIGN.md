# prstate — design pass

Seed implementation: `~/.agents/skills/babysit-all-prs/triage.py` (965 LOC) + `test_triage.py` (852 LOC).
Citations below are `triage.py:LINE` unless stated otherwise.

## 1. Scope and non-goals

prstate answers one question for a set of pull requests: **what is true right now, normalized**.
Three axes: CI verdict, what the viewer owes a human, what automated reviewers still have open.

It is:
- read-only (`gh` search/api only, no mutating subcommand ever constructed)
- a library (`prstate.fetch(...)`) with a thin CLI (`prstate ...`, `--json`)
- pure classification over fetched data, network confined to one module

It is NOT:
- a merge queue, a bot, or a writer (never resolves a thread, never posts, never approves)
- a TUI (gh-dash exists; this emits JSON and a flat report)
- a judgment engine — no LLM calls, no "should you merge" beyond the mechanical `MERGE`/`NOT_READY` label triage.py already computes
- a persistent store — no cache, no DB, no state between runs
- a GitHub API client — it models exactly the fields these rules need, nothing more

## 2. Data model

`model.py`, stdlib `dataclasses` + `enum.StrEnum`, all frozen. Serialization is a
`to_dict()` per dataclass; the JSON in §5 is the contract, the dataclasses are the
in-process mirror.

```python
class CiState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    UNKNOWN = "unknown"   # a check state the allowlists do not recognise
    NONE = "none"         # no checks reported at all
```

Five, not four. `NONE` is separate from `UNKNOWN` because "nothing ran" and "something
ran and could not be read" are different claims, and only the first is normal for a
docs-only PR (triage.py:664-668). `UNKNOWN` never collapses into `PASS`: the allowlists
are allowlists, and an unrecognised conclusion ranks worse than green and better than
red (triage.py:243-250).

```python
@dataclass(frozen=True)
class Check:
    name: str
    state: str | None          # raw GitHub conclusion/status, preserved verbatim
    workflow: str | None       # None when provenance is unknown
    run_id: int | None
    at: datetime | None

@dataclass(frozen=True)
class Ci:
    state: CiState
    failed: tuple[str, ...]    # check names
    pending: tuple[str, ...]   # check names, not just a count
    unknown: tuple[str, ...]   # check names with unrecognised state
    checks: tuple[Check, ...]  # post-collapse, one per (workflow, name)
```

`pending` carries names, where triage.py:281 returned only a count. A count cannot tell
a skill which check to wait on; the count is `len()`.

```python
class Surface(StrEnum):
    THREAD = "thread"          # inline review thread comment
    REVIEW = "review"          # review body
    COMMENT = "comment"        # PR issue comment

@dataclass(frozen=True)
class Owed:
    surface: Surface
    by: str                    # login, "(deleted account)" when absent
    at: datetime
    reason: str                # "unresolved thread, last word is theirs" etc.
    thread_id: str | None
    path: str | None
    outdated: bool
    excerpt: str               # body, trimmed

class BotState(StrEnum):
    OPEN_THREAD = "open_thread"   # unresolved bot-opened review thread
    BLOCKING = "blocking"         # current summary, parsed count > 0
    STALE = "stale"               # blocking verdict predating the last push
    STALE_CLEAN = "stale_clean"   # clean verdict predating the last push
    UNKNOWN = "unknown"           # summary-shaped, count unparseable

@dataclass(frozen=True)
class BotFinding:
    bot: str
    surface: Surface
    state: BotState
    at: datetime
    thread_id: str | None
    path: str | None
    resolved: bool
    outdated: bool
    minimized: bool
    minimized_reason: str | None   # GitHub's own OUTDATED/RESOLVED/SPAM/...
    verdict: int | None            # parsed blocking count, None = unparseable
    superseded_by: str | None      # id of the newer summary from the same bot
    excerpt: str

@dataclass(frozen=True)
class PullRequest:
    repo: str                  # "owner/name"
    number: int
    title: str
    url: str
    author: str
    draft: bool
    base: str
    updated_at: datetime
    mergeable: str | None      # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str | None    # CLEAN | BLOCKED | BEHIND | HAS_HOOKS | ...
    review_decision: str | None
    approved_by: tuple[str, ...]
    changes_requested_by: tuple[str, ...]
    ci: Ci
    owed: tuple[Owed, ...]
    bot_findings: tuple[BotFinding, ...]
    partial: tuple[str, ...]   # every reason this read may be incomplete
```

`partial` is first-class, not a log line. triage.py collects it into one list consulted
once, precisely because five separate `and not` clauses let truncation, unknown
conclusions and an uncomputed merge state each slip into "needs nothing" in turn
(triage.py:592-615). Same rule here: a consumer that ignores `partial` is choosing to,
visibly.

`bot_summaries` and `bot_findings` are ONE list here, where triage.py kept two
(triage.py:466, 497). They differ by `state`/`surface`, not by kind, and every consumer
concatenated them anyway (triage.py:674). `OPEN_THREAD` is the old `bot_findings`.

## 3. Normalization rules

### (a) Three-surface union

Reviewer feedback lives on review threads, review bodies, and issue comments; a query
reading one silently misses the rest (`pr-comment-surfaces/SKILL.md:12-25`). `gh pr view
--json comments` omits review comments entirely (cli/cli#11477), which is why this is a
raw GraphQL query and not a `gh pr view` wrapper.

All three are fetched in the same per-PR fragment. De-duplication is by
`(surface, thread_id, author, created_at)`; a review body and the thread comments
submitted with it are distinct rows and both survive — the review body is the summary,
the thread comments are the findings.

Thread comments are fetched as two connections, `opener: comments(first:1)` and
`recent: comments(last:1)`, plus `totalCount` (triage.py:67-71). Indexing the tail of a
`first:N` page returns comment N, not the newest, and "who spoke last" is the entire
decision (triage.py:310-314). When `totalCount > len(fetched)` the middle is unread and
that fact goes in `partial`, never silently into a verdict (triage.py:333-354).

Every body is trimmed to 1500 chars **in the jq/GraphQL extraction layer**, not after
transport: a `claude` bot review body measured 6.2 KB peak / 2 KB typical across 60 komb
PRs, multiplied by every PR, every poll (`pr-comment-surfaces/SKILL.md:69-75`). The
excerpt in the model is the first 200 chars of that (triage.py:423); the 1500-char body
is what the CLI `--full` path prints. Acting on a trimmed body is out of scope: refetch
by thread id.

### (b) latest-per-check collapse

Raw `statusCheckRollup.contexts` keeps every historical run on the head commit, so a
check that failed and was re-run green still carries the old FAILURE (cli/cli#4946,
cli/cli#14253; independently refiled at rjmurillo/ai-agents#3978, vig-os/devkit#176).

Collapse key is `(workflow_name, check_name)`, NOT check name (triage.py:181-240):

- Supersession is per workflow. Within one workflow the higher `workflowRun.databaseId`
  wins. Finish time cannot decide it — concurrent runs interleave, and on
  komb-enterprise#530 the newer run's `lint` finished ten seconds before the older run's
  (triage.py:186-190).
- Run ids are monotonic per repo, not per workflow, so a higher id from a different
  workflow proves only "started later". Two workflows may each define `test`; both
  survive and `ci_state` unions them (triage.py:191-196).
- Missing workflow provenance gets a unique synthetic key (`?{index}`), never a shared
  `""` bucket — sharing makes a StatusContext and a third-party check look like re-runs
  of each other, and one's green then supersedes the other's red (triage.py:222-225).
- Tie on `(run, when)` resolves to the worse state (triage.py:232-233).

Rollup pagination: `contexts(first:100)` with `totalCount`. `totalCount > len(nodes)`
appends to `partial`; `nodes` present with `totalCount` absent ALSO appends — defaulting
a missing count to 0 makes a truncated read look complete (triage.py:599-604). prstate
additionally follows `pageInfo.hasNextPage` on the rollup, which triage.py does not,
so >100 contexts is a real read rather than a declared-partial one.

Then: any failed → `FAIL`; else any pending → `PENDING`; else any unknown → `UNKNOWN`;
else `PASS` if checks exist, `NONE` if not (triage.py:273-281). The unknown list is
reported whatever the headline, so a single QUEUED check cannot swallow it
(triage.py:258-263).

### (c) owed = human signal with no later viewer activity

"Unresolved" and "unanswered" are different; GitHub's resolve button is left untouched
long after a conversation ends. A sweep that conflated them told the author to reply to
a coral#48 comment he had answered three weeks earlier and to a hl#3270 review he had
replied to that morning (`pr-comment-surfaces/SKILL.md:110-131`).

"Later" means strictly greater `createdAt`/`submittedAt` than the signal's timestamp.
Viewer activity = an issue comment, a submitted review, a review-thread comment, or the
head commit landing (triage.py:357-371). Push time is `commits(last:1).committedDate`,
deliberately the EARLY bound of the push interval: `pushedDate` is null for every commit
now, and reading the push too late credits it with answering a review it may predate
(triage.py:284-297).

Per surface (triage.py:390-460):

- **thread** — unresolved, and walking the two fetched ends newest-first, the first
  non-bot author is not the viewer. A viewer comment at the newest end clears it; a
  trailing bot comment does not close a human ask. A thread whose last word is the
  viewer's is the reviewer's to resolve, not the viewer's to answer.
- **issue comment** — author is neither viewer nor bot, and no viewer comment has a
  later timestamp. Per-surface, not global: a push does not answer a question.
- **review body** — state in `CHANGES_REQUESTED`/`COMMENTED`, non-empty body, and NO
  viewer activity of any kind after it, push included, since a review body usually asks
  for a code change.

`isOutdated` is NOT a filter here. Outdated means the commented line moved, which is not
the concern being answered (`pr-comment-surfaces/SKILL.md:91-94`). It is carried as a
field so a consumer asking "what still needs fixing" can filter; a consumer asking "what
do I owe" must not.

### (d) bot supersede

Order of evidence, strongest first:

1. **GitHub's own minimize state.** `isMinimized` / `minimizedReason` on IssueComment,
   PullRequestReviewComment and PullRequestReview. `marocchino/sticky-pull-request-comment`
   (652★) made this the ecosystem norm write-side: a `header` key names the bot's comment
   lane and `hide_and_recreate` + `hide_classify: OUTDATED` minimizes the prior comment
   before posting the new one. A minimized comment with reason `OUTDATED` or `RESOLVED`
   is superseded, full stop — no heuristic needed, and it is correct for bots that post
   several concurrent lanes, which newest-per-login gets wrong.
2. **Newest-per-bot-login fallback**, for bots that do not minimize. A bot's own
   re-review supersedes its earlier summary; keep only the latest summary-shaped comment
   or review body per login (triage.py:530-548). Unlike a review thread, which stays open
   until the bot resolves it (triage.py:475-476).

A summary is "summary-shaped" when it mentions a blocking count or opens a `Bug:`/`Issue:`
line (triage.py:149-158). Keyword-grepping for "bug" trips on every clean review
(`pr-comment-surfaces/SKILL.md:65-68`), which is why the marker regex is anchored.

Verdict → state (triage.py:556-569):
- unparseable → `UNKNOWN` (staleness adds nothing to a verdict never known)
- predates the last push, count > 0 → `STALE` (real finding, unconfirmed against head)
- predates the last push, count == 0 → `STALE_CLEAN` (no finding; a distinct state
  because reusing `stale` pulled six clean PRs into the actionable bucket and knocked
  #711 out of the merge bucket, 2026-08-24)
- current, count == 0 → dropped, nothing owed
- current, count > 0 → `BLOCKING`

Viewer activity on the SAME surface after the summary suppresses it (triage.py:553-555).
Bot-opened unresolved threads are `OPEN_THREAD` regardless of push: ownership follows the
opener, and a later human reply is evidence to inspect, not closure (triage.py:466-476;
komb-enterprise#654/#655/#656).

## 4. Fetch layer

**Transport: shell out to `gh api graphql`.** Not httpx + a token. `gh` inherits the
keyring auth the user already has (scopes `gist, read:org, repo, workflow`), handles
enterprise hosts and token refresh, and keeps the runtime dependency budget at zero.
The cost — one subprocess per batch — is irrelevant at ~10 requests per sweep.

**Discovery.** `gh search prs --state=open --author=@me --json number,repository` plus
the scope flag. `gh search prs --json` lacks `mergeStateStatus`/`reviewDecision`
(cli/cli#13239), so discovery yields identity only and the detail comes from GraphQL.
Scope resolution mirrors triage.py:97-109: `--repo` > `--all-orgs` > `--org` > the owner
of the repo in the working directory, labelled "owner" not "org" because gh reports users
and organisations through the same field.

**Batching.** Per-PR aliased fragments, `BATCH = 10` per request (triage.py:43), each
alias a `repository(owner:,name:){pullRequest(number:)}`. Node-id batching is not used:
aliases keep the query readable and let one bad PR fail in isolation.

**Truncation detection.** Page sizes are named constants interpolated into the query
(`THREAD_PAGE = 100`, `COMMENT_PAGE = 50`, `REVIEW_PAGE = 50`), hardcoding them twice is
what let a smaller page silently stop the truncation check firing (triage.py:44-47).
Every connection that hits its page limit appends to `partial`. Rollup contexts page
through `hasNextPage`; the other three declare partial rather than paginate, matching
triage.py, because a PR with >100 threads is pathological and the flag is honest.

**Rate limit.** One retry with backoff on secondary-rate-limit and 502/503, then give up
and mark the affected PRs partial rather than half-report. GraphQL cost per batch is
well under the 5000-point hourly budget; no client-side throttle.

## 5. Public API

### Python

```python
def fetch(
    *,
    author: str | None = "@me",      # PR author filter; None = any
    reviewer: str | None = None,     # review-requested filter, for reviews-needed
    owner: str | None = None,        # None = owner of the cwd repo
    repo: str | None = None,         # "owner/name", narrows to one repo
    all_owners: bool = False,
    viewer: str | None = None,       # login the owed/answered rules run against
    limit: int = 200,
) -> Sweep: ...

@dataclass(frozen=True)
class Sweep:
    scope: str                       # human label: "owner SakanaAIBusiness"
    viewer: str                      # resolved login
    fetched_at: datetime
    prs: tuple[PullRequest, ...]
    partial: tuple[str, ...]         # sweep-level, e.g. a batch that failed
```

`viewer` defaults to `gh api user --jq .login`, never hardcoded. It is the login the
owed rules test "later activity" against, and it is NOT necessarily the author: for
`reviews-needed` the viewer is a reviewer looking at someone else's PR, and the same
`owed` computation answers "what do I owe as reviewer" without a second code path.

Exported: `fetch`, `Sweep`, `PullRequest`, `Ci`, `Check`, `Owed`, `BotFinding`, the
three enums, and `classify(raw_pr: dict, viewer: str, now: datetime) -> PullRequest`
for callers holding their own GraphQL payload. Everything else is internal.

### CLI

```
prstate [SCOPE] [FILTER] [--json] [--full] [--limit N]

SCOPE   --repo owner/name | --org owner | --all-orgs      (default: cwd repo's owner)
FILTER  --author LOGIN (default @me) | --review-requested | --reviewed-by LOGIN
        --owed          only PRs with at least one owed item
        --bot-findings  only PRs with an unresolved bot finding
        --ci fail|pending|unknown|none|pass
        --include-drafts    (drafts are excluded by default)
OUTPUT  --json     the contract in the next block
        --full     1500-char bodies instead of 200-char excerpts
```

One command, flags compose. No subcommands: every consumer wants the same sweep with a
different filter, and `prstate --owed` reads better than `prstate sweep --filter owed`.

Default output is the flat bucketed report triage.py already renders (BUCKET, key, CI,
MERGE, WHAT TO DO), because two skills print it nearly verbatim today.

### `--json` contract

```json
{
  "schema_version": 1,
  "fetched_at": "2026-09-25T08:00:00Z",
  "scope": "owner SakanaAIBusiness",
  "viewer": "fmguerreiro",
  "partial": [],
  "prs": [{
    "repo": "SakanaAIBusiness/komb-enterprise",
    "number": 1262,
    "title": "...",
    "url": "https://github.com/...",
    "author": "fmguerreiro",
    "draft": false,
    "base": "main",
    "updated_at": "2026-09-24T18:22:03Z",
    "mergeable": "MERGEABLE",
    "merge_state": "CLEAN",
    "review_decision": "APPROVED",
    "approved_by": ["ryukez"],
    "changes_requested_by": [],
    "ci": {
      "state": "fail",
      "failed": ["backend-tests"],
      "pending": [],
      "unknown": [],
      "checks": [{"name": "backend-tests", "state": "FAILURE",
                  "workflow": "CI", "run_id": 34973309168,
                  "at": "2026-09-24T18:40:11Z"}]
    },
    "owed": [{
      "surface": "thread", "by": "ryukez", "at": "2026-09-24T09:11:00Z",
      "reason": "unresolved thread, last human word is theirs",
      "thread_id": "PRRT_kw...", "path": "backend/api.py",
      "outdated": false, "excerpt": "this drops the None case ..."
    }],
    "bot_findings": [{
      "bot": "claude[bot]", "surface": "comment", "state": "stale",
      "at": "2026-09-23T10:02:00Z", "thread_id": null, "path": null,
      "resolved": false, "outdated": false,
      "minimized": false, "minimized_reason": null,
      "verdict": 2, "superseded_by": null,
      "excerpt": "### Blocking: 2 ..."
    }],
    "partial": ["review threads (hit the 100 page limit)"]
  }]
}
```

**Contractual**: `schema_version`, every key above, the enum vocabularies of `ci.state`
and `bot_findings[].state`, and the guarantee that `partial` non-empty means the read is
incomplete. Added keys bump nothing; removed or retyped keys bump `schema_version`.

**Internal**: the default report's exact columns, ordering within `checks`, excerpt
length, and every module below `prstate.fetch`.

## 6. Skill migration map

Ordered by duplication removed. **Gated: this edits `/Users/filipeguerreiro/projects/dotfiles`,
a different repo from `~/work/prstate`, and only after prstate passes live smoke tests
against real PRs. A half-migrated skill is a broken skill the user runs daily.**

| # | Skill | Deleted | Replaced by |
|---|---|---|---|
| 1 | `babysit-all-prs` | `triage.py` (965) + `test_triage.py` (852) move into prstate wholesale; SKILL.md `## Why a script` and `## What counts as answered` shrink to a pointer | `prstate --json` + the judgment prose that remains |
| 2 | `reviews-needed` | Phase 1 `gh pr list` block (:27-34), Phase 3 `gh pr view --json ... statusCheckRollup` + `--jq` projection (:48-55), the graphql reviewThreads block (:75-95), and the CI collapse paragraph (:69) | `prstate --review-requested --json`; Phases 4-5 (classify into buckets, render) stay — that is judgment |
| 3 | `babysit-reviews` | Phase 1 two `gh search prs` calls (:51-57), Phase 2 `gh pr view --json` (:70-72) and the graphql thread block (:79-112), `## Auth and rate limits` (:179-182) | `prstate --review-requested --json` and `prstate --reviewed-by @me --json`, deduped by prstate |
| 4 | `pr-comment-surfaces` | the GraphQL query (:42-53), the jq trim block (:77-82), and the per-surface answered rules (:110-131) become documentation OF prstate rather than instructions to re-derive | `prstate --repo X --owed --json`, single-PR via `--repo` + number |
| 5 | `review-swarm` | Step 4 "fold in the existing conversation" thread enumeration | `prstate --repo X --json`, then triage as today. Step 8's resolve mutation stays in the skill — prstate never writes |
| 6 | `pr-review-fanout` | the scope/own-pr reference blocks that re-derive discovery | `prstate` with the matching scope flags |
| 7 | `standup` | nothing mechanical is shared beyond discovery; only the `gh pr list --state open` sweep (:52-54) | `prstate --json` for the open set; merged-PR history stays `gh pr list --state merged` (out of prstate's scope: prstate is open-PR state, not history) |

`standup` is last and smallest on purpose — it is mostly a writing skill, and pretending
otherwise would inflate the payoff claim.

## 7. Module layout

```
prstate/
  __init__.py      exports fetch, classify, the dataclasses    (no I/O)
  model.py         dataclasses + enums + to_dict               (pure)
  query.py         GraphQL text, page constants, batching      (pure: builds strings)
  gh.py            subprocess wrapper, discovery, retry        (ALL network lives here)
  classify.py      latest_per_check, ci_state, owed, bots      (pure)
  render.py        the bucketed report                         (pure)
  cli.py           argparse, flag composition, exit codes      (I/O only via gh.py)
tests/
  fixtures/*.json  recorded GraphQL payloads
  test_classify.py behaviour tests over fixtures, no network
  test_query.py    page constants actually reach the query text
  test_cli.py      flag composition and JSON shape
```

Seven files, each one job. `query.py` is separate from `gh.py` because the page-size
constants must be provably reachable in the emitted query text — that is the whole point
of triage.py:44-47 — and testing that needs the string without the subprocess.

The boundary that matters: `gh.py` is the ONLY module that imports `subprocess`. Every
rule in §3 is a pure function from a payload dict to a dataclass, so the entire rules
suite runs offline against fixtures. A test that needs the network is a test of `gh.py`
itself, and there are two: discovery parses, and a malformed batch marks partial.

Read-only enforcement is a unit test, not a convention: `gh.py` builds argv through one
helper that rejects any subcommand outside `{search, api, auth, repo, pr}` and any
`api` call whose query text contains `mutation` (triage.py carries the same allowlist).

## 8. Open decisions

| Decision | Recommendation |
|---|---|
| Package / PyPI name | `prstate` both. Free on PyPI and npm (checked 2026-09-25). Import name matches the command. |
| Python floor | **3.12.** It is the machine default; `StrEnum` (3.11+) and the generics used here all land. 3.13 buys nothing and narrows who can `pipx install` it. |
| Runtime dependencies | **Zero.** argparse, json, dataclasses, enum, subprocess, datetime. `gh` is the one external binary, already required for auth. Dev deps: pytest only. |
| Fixture migration | Carry over test_triage.py's recorded payloads as `tests/fixtures/*.json`, one file per scenario, named for the RULE not the PR. Tests that pin rendering strings or internal function shapes do NOT come over. |
| Rendered report or JSON only | **Keep the report.** Two skills print it near-verbatim today; dropping it moves formatting back into prose, which is the thing being deleted. `--json` is the contract, the report is the default. |
| Skills shell out or import | **Shell out** to `prstate --json`. Skills are markdown run by an agent, not Python processes; a subprocess with a versioned JSON contract is the only interface that works from prose. |
| Where the repo lives | `~/work/prstate`, **private** (`gh repo create --private`): the design carries internal org names and a specific incident reference. Public is a one-flag change later; the reverse is not. |
| Install path | `uv tool install` / `pipx install` from the git URL first. PyPI publish only if it outlives a month of daily use. |
