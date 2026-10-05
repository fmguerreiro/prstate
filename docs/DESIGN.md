# prstate — design pass

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
- a judgment engine — no LLM calls, no "should you merge" verdict beyond mechanical labels
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
docs-only PR. `UNKNOWN` never collapses into `PASS`: the allowlists are allowlists, and
an unrecognised conclusion ranks worse than green and better than red.

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

`pending` carries names, not only a count. A count cannot tell a consumer which check to
wait on; the count is `len()`.

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

`partial` is first-class, not a log line. Truncation, unknown conclusions, and an
uncomputed merge state must not each independently slip into "needs nothing." A consumer
that ignores `partial` is choosing to, visibly.

`bot_summaries` and `bot_findings` are one list. They differ by `state` and `surface`,
not by kind; `OPEN_THREAD` represents an unresolved bot-opened thread.

## 3. Normalization rules

### (a) Three-surface union

Reviewer feedback lives on review threads, review bodies, and issue comments; a query
reading one silently misses the rest. `gh pr view --json comments` omits review comments
entirely (cli/cli#11477), which is why this is a raw GraphQL query and not a `gh pr view`
wrapper.

All three are fetched in the same per-PR fragment. De-duplication is by
`(surface, thread_id, author, created_at)`; a review body and the thread comments
submitted with it are distinct rows and both survive — the review body is the summary,
the thread comments are the findings.

Thread comments are fetched as two connections, `opener: comments(first:1)` and
`recent: comments(last:1)`, plus `totalCount`. Indexing the tail of a `first:N` page
returns comment N, not the newest, and "who spoke last" is the entire decision. When
`totalCount > len(fetched)` the middle is unread and that fact goes in `partial`, never
silently into a verdict.

Every body is trimmed to 1500 chars **in the jq/GraphQL extraction layer**, not after
transport. The excerpt in the model is the first 200 chars of that; the 1500-char body is
what the CLI `--full` path prints. Acting on a trimmed body is out of scope: refetch by
thread id.

### (b) latest-per-check collapse

Raw `statusCheckRollup.contexts` keeps every historical run on the head commit, so a
check that failed and was re-run green still carries the old FAILURE (cli/cli#4946,
cli/cli#14253).

Collapse key is `(workflow_name, check_name)`, NOT check name:

- Supersession is per workflow. Within one workflow the higher `workflowRun.databaseId`
  wins. Finish time cannot decide it — concurrent runs interleave, so a newer run can
  finish before an older run.
- Run ids are monotonic per repo, not per workflow, so a higher id from a different
  workflow proves only "started later". Two workflows may each define `test`; both
  survive and `ci_state` unions them.
- Missing workflow provenance gets a unique synthetic key (`?{index}`), never a shared
  `""` bucket — sharing makes a StatusContext and a third-party check look like re-runs
  of each other, and one's green then supersedes the other's red.
- Tie on `(run, when)` resolves to the worse state.

Rollup pagination: `contexts(first:100)` with `totalCount`. `totalCount > len(nodes)`
appends to `partial`; `nodes` present with `totalCount` absent ALSO appends — defaulting
a missing count to 0 makes a truncated read look complete. prstate additionally follows
`pageInfo.hasNextPage` on the rollup, so >100 contexts is a real read rather than a
declared-partial one.

Then: any failed → `FAIL`; else any pending → `PENDING`; else any unknown → `UNKNOWN`;
else `PASS` if checks exist, `NONE` if not. The unknown list is reported whatever the
headline, so a single QUEUED check cannot swallow it.

### (c) owed = human signal with no later viewer activity

"Unresolved" and "unanswered" are different; GitHub's resolve button is left untouched
long after a conversation ends. An earlier sweep told an author to reply to a comment
they had answered three weeks earlier and to a review they had replied to that morning.

"Later" means strictly greater `createdAt`/`submittedAt` than the signal's timestamp.
Viewer activity = an issue comment, a submitted review, a review-thread comment, or the
head commit landing. Push time is `commits(last:1).committedDate`, deliberately the EARLY
bound of the push interval: `pushedDate` is null for every commit now, and reading the
push too late credits it with answering a review it may predate.

Per surface:

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
the concern being answered. It is carried as a field so a consumer asking "what still
needs fixing" can filter; a consumer asking "what do I owe" must not.

### (d) bot supersede

An author is automated when GitHub reports the actor type as `Bot`, or when its login
matches the fallback bot allowlist or `[bot]` suffix.

Order of evidence, strongest first:

1. **GitHub's own minimize state.** `isMinimized` / `minimizedReason` on IssueComment,
   PullRequestReviewComment and PullRequestReview. Sticky-comment bots commonly name a
   comment lane, minimize the prior comment as `OUTDATED`, then post a replacement. A
   minimized comment with reason `OUTDATED` or `RESOLVED` is superseded, full stop — no
   heuristic needed, and it is correct for bots that post several concurrent lanes, which
   newest-per-login gets wrong.
2. **Newest-per-bot-login fallback**, for bots that do not minimize. A bot's own
   re-review supersedes its earlier summary; keep only the latest summary-shaped comment
   or review body per login. Unlike a review thread, which stays open until the bot
   resolves it.

A summary is "summary-shaped" when it mentions a blocking count or opens a `Bug:`/`Issue:`
line. Keyword-grepping for "bug" trips on clean reviews, which is why the marker regex is
anchored.

Verdict → state:
- unparseable → `UNKNOWN` (staleness adds nothing to a verdict never known)
- predates the last push, count > 0 → `STALE` (real finding, unconfirmed against head)
- predates the last push, count == 0 → `STALE_CLEAN` (no finding; a distinct state
  because reusing `stale` pulls clean PRs into the actionable bucket)
- current, count == 0 → dropped, nothing owed
- current, count > 0 → `BLOCKING`

Viewer activity on the SAME surface after the summary suppresses it.
Bot-opened unresolved threads are `OPEN_THREAD` regardless of push: ownership follows the
opener, and a later human reply is evidence to inspect, not closure.

## 4. Fetch layer

**Transport: shell out to `gh api graphql`.** Not httpx + a token. `gh` inherits the
keyring auth the user already has (scopes `gist, read:org, repo, workflow`), handles
enterprise hosts and token refresh, and keeps the runtime dependency budget at zero.
The cost — one subprocess per batch — is irrelevant at ~10 requests per sweep.

**Discovery.** `gh search prs --state=open --author=@me --json number,repository` plus
the scope flag. `gh search prs --json` lacks `mergeStateStatus`/`reviewDecision`
(cli/cli#13239), so discovery yields identity only and the detail comes from GraphQL.
Scope resolution is `--repo` > `--all-orgs` > `--org` > the owner of the repo in the
working directory, labelled "owner" not "org" because gh reports users and organisations
through the same field.

**Batching.** Per-PR aliased fragments, `BATCH = 10` per request, each alias a
`repository(owner:,name:){pullRequest(number:)}`. Node-id batching is not used: aliases
keep the query readable and let one bad PR fail in isolation.

**Truncation detection.** Page sizes are named constants interpolated into the query
(`THREAD_PAGE = 100`, `COMMENT_PAGE = 50`, `REVIEW_PAGE = 50`); hardcoding them twice can
silently stop the truncation check from firing.
Rollup contexts follow `pageInfo.hasNextPage`. A thread whose `totalCount` exceeds its
deduplicated opener and newest comment follows the thread node's comment cursor, because
a viewer reply in the middle changes whether a human signal is owed. Either continuation
stops at a bounded page count and appends to `partial` if GitHub cannot complete it.
The top-level thread, issue-comment, and review connections still declare partial when
they hit their page limits.

**Rate limit.** One retry with backoff on secondary-rate-limit and 502/503, then give up
and mark the affected PRs partial rather than half-report. GraphQL cost per batch is
well under the 5000-point hourly budget; no client-side throttle.

## 5. Public API

### Python

```python
def fetch(
    *,
    author: str | None = "@me",      # PR author filter; None = any
    reviewer: str | None = None,     # review-requested filter
    owner: str | None = None,        # None = owner of the cwd repo
    repo: str | None = None,         # "owner/name", narrows to one repo
    all_owners: bool = False,
    viewer: str | None = None,       # login the owed/answered rules run against
    limit: int = 200,
) -> Sweep: ...

@dataclass(frozen=True)
class Sweep:
    scope: str                       # human label: "owner acme"
    viewer: str                      # resolved login
    fetched_at: datetime
    prs: tuple[PullRequest, ...]
    partial: tuple[str, ...]         # sweep-level, e.g. a batch that failed
```

`viewer` defaults to `gh api user --jq .login`, never hardcoded. It is the login the
owed rules test "later activity" against, and it is NOT necessarily the author: a viewer
can be a reviewer looking at someone else's PR, and the same `owed` computation answers
"what do I owe as reviewer" without a second code path.

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

Default output is a flat bucketed report (BUCKET, key, CI, MERGE, WHAT TO DO), because
some consumers print it nearly verbatim today.

### `--json` contract

```json
{
  "schema_version": 1,
  "fetched_at": "2026-09-25T08:00:00Z",
  "scope": "owner acme",
  "viewer": "alice",
  "partial": [],
  "prs": [{
    "repo": "acme/widgets",
    "number": 1262,
    "title": "...",
    "url": "https://github.com/...",
    "author": "alice",
    "draft": false,
    "base": "main",
    "updated_at": "2026-09-24T18:22:03Z",
    "mergeable": "MERGEABLE",
    "merge_state": "CLEAN",
    "review_decision": "APPROVED",
    "approved_by": ["bob"],
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
      "surface": "thread", "by": "bob", "at": "2026-09-24T09:11:00Z",
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

## 6. Consumer migration map

Ordered by duplication removed. **Gated: migrate downstream consumers only after prstate
passes live smoke tests against real PRs. A half-migrated consumer is broken.**

| # | Consumer concern | Replaced by |
|---|---|---|
| 1 | Pull-request state collection and normalization | `prstate --json` plus consumer-specific judgment prose |
| 2 | Review-requested pull-request discovery | `prstate --review-requested --json` |
| 3 | Reviewer-specific pull-request discovery | `prstate --reviewed-by @me --json` |
| 4 | Per-surface comment analysis | `prstate --repo owner/name --owed --json` |
| 5 | Repository pull-request state | `prstate --repo owner/name --json` |
| 6 | General pull-request discovery | `prstate` with matching scope flags |
| 7 | Open pull-request discovery for status reports | `prstate --json`; merged-PR history remains outside prstate's scope |


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
constants must be provably reachable in the emitted query text, and testing that needs
the string without the subprocess.

The boundary that matters: `gh.py` is the ONLY module that imports `subprocess`. Every
rule in §3 is a pure function from a payload dict to a dataclass, so the entire rules
suite runs offline against fixtures. A test that needs the network is a test of `gh.py`
itself, and there are two: discovery parses, and a malformed batch marks partial.

Read-only enforcement is a unit test, not a convention: `gh.py` builds argv through one
helper that rejects any subcommand outside `{search, api, auth, repo, pr}` and any
`api` call whose query text contains `mutation`.

## 8. Open decisions

| Decision | Recommendation |
|---|---|
| Package / PyPI name | `prstate` both. Free on PyPI and npm (checked 2026-09-25). Import name matches the command. |
| Python floor | **3.12.** `StrEnum` and the typing used here work on 3.12; requiring 3.13 would narrow who can `pipx install` it. |
| Runtime dependencies | **Zero.** argparse, json, dataclasses, enum, subprocess, datetime. `gh` is the one external binary, already required for auth. Dev deps: pytest only. |
| Fixture migration | Carry recorded payloads into `tests/fixtures/*.json`, one file per scenario, named for the rule rather than a pull request. Tests that pin rendering strings or internal function shapes do NOT come over. |
| Rendered report or JSON only | **Keep the report.** Existing consumers print it near-verbatim; dropping it moves formatting back into each consumer. `--json` is the contract, the report is the default. |
| Consumers shell out or import | **Shell out** to `prstate --json`. CLI consumers need a subprocess with a versioned JSON contract; Python callers can import `prstate.fetch`. |
| Repository visibility | Private by default; public release is a later decision. |
| Install path | `uv tool install` / `pipx install` from the git URL first. PyPI publish only if it outlives a month of daily use. |
