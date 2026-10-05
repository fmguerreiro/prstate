"""The rules: a raw GraphQL PR payload in, dataclasses out.

Pure. No subprocess, no network, no clock of its own — every rule here is
reproducible from a fixture, which is what makes the rest of the project
testable offline.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from prstate.model import (
    DELETED,
    BotFinding,
    BotState,
    Check,
    Ci,
    CiState,
    Owed,
    PullRequest,
    Reason,
    ReasonKind,
    Surface,
    ViewerReview,
)
from prstate.query import COMMENT_PAGE, EXCERPT_CHARS, REVIEW_PAGE, THREAD_PAGE

# Anything here is never treated as a human awaiting a reply. Automated findings
# use the separate BOTFIX and re-review path.
BOT_LOGINS = {
    "claude", "cubic-dev-ai", "greptile-apps", "greptileai", "coderabbitai",
    "sonarcloud", "codecov", "vercel", "renovate", "dependabot",
    "github-actions", "sentry-io", "netlify", "graphite-app",
}

# Allowlists, not denylists. Anything unrecognised has to surface as unknown:
# defaulting an unfamiliar conclusion to green is the one error direction that
# matters here, because ci == pass is what gates the merge recommendation.
PASSED_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
FAILED_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE",
    "ERROR",  # StatusState, how an external provider reports an infra failure
}
PENDING_STATUSES = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}

# A PR-level bot summary (issue comment or review body) that never mentions a
# blocking count is noise — a deploy preview, a coverage report, a dependency
# bump note — not a review verdict, and is not worth flagging at all.
SUMMARY_MARKER_RE = re.compile(r"blocking|^\s*(?:bug|issue)\s*:", re.IGNORECASE | re.MULTILINE)
BLOCKING_COUNT_RE = re.compile(r"blocking:?\s*\**\s*(\d+)", re.IGNORECASE)
CLEAN_PHRASE_RE = re.compile(r"\bno blocking findings\b|\bnothing (?:is |was )?blocking\b",
                             re.IGNORECASE)
BUG_LINE_RE = re.compile(r"^\s*(?:bug|issue)\s*:", re.IGNORECASE | re.MULTILINE)

_ORDER_FLOOR = datetime.min.replace(tzinfo=UTC)
_ORDER_CEILING = datetime.max.replace(tzinfo=UTC)

THREAD_REASON = "unresolved thread, last human word is theirs"
COMMENT_REASON = "issue comment with no later reply from you"
REVIEW_REASON = "review body with no later activity from you"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(node: dict | None) -> str:
    """The author login of any comment/review node, never None.

    Normalised here rather than at each use site: a null author is rare enough to
    miss in review and lethal enough to matter. It sorted and joined fine in three
    surfaces and still crashed classify's fourth, and since rows is a comprehension
    over every PR, one deleted account meant a report of zero PRs.
    """
    if not node:
        return DELETED
    return (node.get("author") or {}).get("login") or DELETED


def is_bot(login: str | None) -> bool:
    """A known bot account. An absent login is NOT one.

    A missing author means a deleted account, and treating that as a bot silently
    drops whatever it said. "Unknown who wrote this" has to stay a signal.
    """
    if not login:
        return False
    low = login.lower()
    return "[bot]" in low or low in BOT_LOGINS


def author_is_bot(node: dict | None) -> bool:
    author = (node or {}).get("author") or {}
    return author.get("__typename") == "Bot" or is_bot(author.get("login"))


def is_bot_summary(text: str) -> bool:
    """A comment or review body shaped like a review verdict, not bot chatter."""
    return bool(text) and bool(SUMMARY_MARKER_RE.search(text))


def parse_bot_verdict(text: str) -> int | None:
    """The blocking-finding count in a bot summary, or None if it cannot be read.

    None is not "clean" — it means the text is shaped like a verdict (is_bot_summary
    already filtered out everything else) but the count could not be parsed, so the
    caller has to surface it as unknown rather than drop it.
    """
    if CLEAN_PHRASE_RE.search(text):
        return 0
    match = BLOCKING_COUNT_RE.search(text)
    if match:
        return int(match.group(1))
    bug_lines = BUG_LINE_RE.findall(text)
    if bug_lines:
        return len(bug_lines)
    return None


def _body_of(node: dict) -> str:
    return (node.get("body") or "").strip()


def _excerpt(body: str) -> str:
    return " ".join(body.split())[:EXCERPT_CHARS]


def _minimized(node: dict) -> bool:
    return bool(node.get("isMinimized"))


def latest_per_check(nodes: list[dict]) -> list[Check]:
    """Collapse runs per workflow and check using run order, not finish time."""
    newest: dict[tuple[str, str], tuple[int, datetime, Check]] = {}
    for index, node in enumerate(nodes):
        if node.get("__typename") == "CheckRun":
            name = node.get("name")
            when = parse_time(node.get("completedAt")) or parse_time(node.get("startedAt"))
            state = node.get("conclusion") or node.get("status")
            run = ((node.get("checkSuite") or {}).get("workflowRun") or {})
            run_id = run.get("databaseId")
            workflow = ((run.get("workflow") or {}).get("name")) or None
        else:
            name = node.get("context")
            when = parse_time(node.get("createdAt"))
            state = node.get("state")
            run_id = None
            workflow = None
        if not name:
            continue
        check = Check(name=name, state=state, workflow=workflow, run_id=run_id, at=when)
        order = (run_id or 0, when or _ORDER_FLOOR)
        # No workflow run means unknown provenance, not workflow "". A shared
        # bucket makes a StatusContext and a third-party check look like re-runs of
        # each other, and one's green then supersedes the other's red.
        key = (workflow, name) if workflow else (f"?{index}", name)
        if key not in newest:
            newest[key] = (*order, check)
            continue
        prev_run, prev_when, prev = newest[key]
        if order > (prev_run, prev_when):
            newest[key] = (*order, check)
        elif order == (prev_run, prev_when) and _worse(state, prev.state):
            newest[key] = (*order, check)

    # One entry per (workflow, check), NOT per check. Collapsing to a single entry
    # per name needed a severity ordering, and ranking made "unknown" comparable
    # and therefore droppable: a QUEUED in one workflow outranked and deleted a
    # MYSTERY conclusion in another. Handing every surviving entry to ci_state lets
    # it union the failed, pending and unknown sets, so nothing gets outvoted.
    return [check for _, _, check in newest.values()]


def _rank(state: str | None) -> int:
    if state in FAILED_CONCLUSIONS:
        return 0
    if state in PENDING_STATUSES:
        return 1
    if state in PASSED_CONCLUSIONS:
        return 3
    return 2  # unrecognised: worse than green, better than an outright failure


def _worse(state: str | None, other: str | None) -> bool:
    return _rank(state) < _rank(other)


def ci_state(checks: Sequence[Check]) -> Ci:
    """Worst-first summary, reporting unknown names whatever the headline state.

    `pending` carries one NAME per pending check, not a count: two workflows
    queueing a job called `test` is two pending checks, and a consumer that has to
    wait needs to know which. The unknown list is separate rather than folded into
    the headline because a single QUEUED check would otherwise make PENDING the
    state and swallow it, which is the same name-collapse bug one layer up.
    """
    failed = tuple(sorted({c.name for c in checks if c.state in FAILED_CONCLUSIONS}))
    pending = tuple(c.name for c in checks if c.state in PENDING_STATUSES)
    unknown = tuple(sorted({
        c.name for c in checks
        if c.state not in FAILED_CONCLUSIONS
        and c.state not in PENDING_STATUSES
        and c.state not in PASSED_CONCLUSIONS
    }))
    if failed:
        state = CiState.FAIL
    elif pending:
        state = CiState.PENDING
    elif unknown:
        state = CiState.UNKNOWN
    else:
        state = CiState.PASS if checks else CiState.NONE
    return Ci(state=state, failed=failed, pending=pending, unknown=unknown,
              checks=tuple(checks))


def head_landed_at(pr: dict) -> datetime | None:
    """Earliest time the head commit could have reached the PR.

    Commit.pushedDate returns null for every commit now, so the true push time is
    only bounded: somewhere between the committer timestamp and the start of the
    checks the push triggered. Take the bottom of that interval, which is always
    committedDate (it is NON_NULL in the schema, and a check cannot start before
    the commit that triggered it exists).

    Deliberately the early end. Reading the push too early only over-reports
    unanswered reviews, which is cheap and visible. Reading it too late credits
    the push with answering a review it may predate: a commit authored at 00:00
    whose checks start at 06:00 would mark a 05:00 CHANGES_REQUESTED as handled.
    """
    commits = pr["commits"]["nodes"]
    if not commits:
        return None
    return parse_time(commits[0]["commit"].get("committedDate"))


def thread_opener(thread: dict) -> dict | None:
    nodes = (thread.get("opener") or {}).get("nodes") or []
    return nodes[0] if nodes else None


def thread_last(thread: dict) -> dict | None:
    """The genuinely last comment on a thread.

    Queried as its own `last: 1` connection rather than indexing the tail of a
    `first: N` page: on a thread longer than the page size, that tail is comment
    N, not the newest, and "who spoke last" is the whole decision here.
    """
    nodes = (thread.get("recent") or {}).get("nodes") or []
    return nodes[-1] if nodes else None


def thread_comments(thread: dict) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for name in ("opener", "activity", "recent"):
        for comment in (thread.get(name) or {}).get("nodes") or []:
            key = ((comment.get("id"),) if comment.get("id") else
                   (login_of(comment), comment.get("createdAt"), comment.get("body")))
            if key in seen:
                continue
            seen.add(key)
            out.append(comment)
    out.sort(key=lambda comment: parse_time(comment.get("createdAt")) or _ORDER_FLOOR)
    return out


def unread_thread_middles(pr: dict, viewer: str) -> list[str]:
    """Return threads whose unread middle cannot be resolved from a trailing bot."""
    unread = []
    for thread in pr["reviewThreads"]["nodes"] or []:
        if thread["isResolved"]:
            continue
        total = (thread.get("recent") or {}).get("totalCount")
        ends = thread_comments(thread)
        seen = len(ends)
        if total is not None and total <= seen:
            continue
        if not ends:
            continue
        newest = login_of(ends[-1])
        if newest == viewer or not author_is_bot(ends[-1]):
            continue
        if total is None:
            # Same call as the check rollup: an absent count is not a zero.
            unread.append(f'a thread on {thread.get("path")} ends in a bot '
                          f'and reported no comment count')
        else:
            unread.append(f'a {total}-comment thread on {thread.get("path")} ends '
                          f'in a bot, {total - seen} unread')
    return unread


def viewer_activity_times(pr: dict, viewer: str) -> list[datetime]:
    """Every timestamp on this PR that proves the viewer engaged."""
    times: list[datetime | None] = []
    for comment in pr["comments"]["nodes"] or []:
        if login_of(comment) == viewer:
            times.append(parse_time(comment["createdAt"]))
    for review in pr["reviews"]["nodes"] or []:
        if login_of(review) == viewer:
            times.append(parse_time(review["submittedAt"]))
    for thread in pr["reviewThreads"]["nodes"] or []:
        for comment in thread_comments(thread):
            if login_of(comment) == viewer:
                times.append(parse_time(comment["createdAt"]))
    # Only the PR author's push counts as their activity, regardless of commit author.
    if viewer == login_of(pr):
        times.append(head_landed_at(pr))
    return [t for t in times if t]


def _thread_gap_until(pr: dict) -> datetime | None:
    """Latest time an unread thread comment may have answered a review body."""
    if pr.get("_thread_activity_complete") is not False:
        return None
    bounds = []
    for thread in pr["reviewThreads"]["nodes"] or []:
        if thread.get("_activity_complete") is not False:
            continue
        last = thread_last(thread)
        newest = parse_time(last.get("createdAt")) if last else None
        if newest is None:
            return _ORDER_CEILING
        bounds.append(newest)
    return max(bounds, default=_ORDER_CEILING)


def owed(pr: dict, viewer: str) -> tuple[Owed, ...]:
    """Human review signals with no later response from the viewer.

    Three surfaces, each with its own notion of "answered":
      - review thread: unresolved and its newest visible human comment is theirs
      - issue comment: no comment by the viewer after it
      - review body: no activity at all by the viewer after it (a push counts,
        since a review body usually asks for a code change)

    isOutdated is carried, never filtered on: outdated means the commented line
    moved, which is not the concern being answered.
    """
    signals: list[Owed] = []
    mine = viewer_activity_times(pr, viewer)
    gap_until = _thread_gap_until(pr)
    my_comment_times = [
        parse_time(c["createdAt"]) for c in (pr["comments"]["nodes"] or [])
        if login_of(c) == viewer
    ]

    for thread in pr["reviewThreads"]["nodes"] or []:
        if thread["isResolved"]:
            continue
        first = thread_opener(thread)
        if not first:
            continue
        comments = thread_comments(thread)
        last = comments[-1] if comments else first
        total = (thread.get("recent") or {}).get("totalCount")
        if author_is_bot(last) and (total is None or total > len(comments)):
            continue
        human = None
        for candidate in reversed(comments):
            who = login_of(candidate)
            if who == viewer:
                break
            if author_is_bot(candidate) or _minimized(candidate):
                continue
            human = candidate
            break
        if not human:
            continue
        body = _body_of(human)
        signals.append(Owed(
            surface=Surface.THREAD,
            by=login_of(human),
            at=parse_time(human["createdAt"]),
            reason=THREAD_REASON,
            thread_id=thread.get("id"),
            path=thread.get("path"),
            outdated=thread["isOutdated"],
            excerpt=_excerpt(body),
            body=body,
        ))

    for comment in pr["comments"]["nodes"] or []:
        author = login_of(comment)
        if author == viewer or author_is_bot(comment) or _minimized(comment):
            continue
        when = parse_time(comment["createdAt"])
        if any(t > when for t in my_comment_times):
            continue  # viewer answered in a later comment
        body = _body_of(comment)
        signals.append(Owed(
            surface=Surface.COMMENT,
            by=author,
            at=when,
            reason=COMMENT_REASON,
            thread_id=None,
            path=None,
            outdated=False,
            excerpt=_excerpt(body),
            body=body,
        ))

    for review in pr["reviews"]["nodes"] or []:
        author = login_of(review)
        if author == viewer or author_is_bot(review) or _minimized(review):
            continue
        if review["state"] not in ("CHANGES_REQUESTED", "COMMENTED"):
            continue
        body = _body_of(review)
        if not body:
            continue
        when = parse_time(review["submittedAt"])
        if gap_until is not None and when < gap_until:
            continue
        if any(t > when for t in mine):
            continue
        signals.append(Owed(
            surface=Surface.REVIEW,
            by=author,
            at=when,
            reason=REVIEW_REASON,
            thread_id=None,
            path=None,
            outdated=False,
            excerpt=_excerpt(body),
            body=body,
        ))

    signals.sort(key=lambda s: s.at)
    return tuple(signals)


def _summary_candidates(pr: dict) -> list[dict]:
    """Bot-authored issue comments and review bodies shaped like a verdict."""
    out: list[dict] = []
    for comment in pr["comments"]["nodes"] or []:
        login = login_of(comment)
        text = comment.get("body") or ""
        if not author_is_bot(comment) or not is_bot_summary(text):
            continue
        out.append({"id": comment.get("id"), "login": login, "surface": Surface.COMMENT,
                    "at": parse_time(comment["createdAt"]), "body": text.strip(),
                    "minimized": _minimized(comment),
                    "minimized_reason": comment.get("minimizedReason")})
    for review in pr["reviews"]["nodes"] or []:
        login = login_of(review)
        text = review.get("body") or ""
        if not author_is_bot(review) or not is_bot_summary(text):
            continue
        out.append({"id": review.get("id"), "login": login, "surface": Surface.REVIEW,
                    "at": parse_time(review["submittedAt"]), "body": text.strip(),
                    "minimized": _minimized(review),
                    "minimized_reason": review.get("minimizedReason")})
    return out


def _apply_supersede(candidates: list[dict]) -> None:
    """Set `superseded_by` per login, choosing ONE signal and never mixing them.

    A login that minimizes is trusted exclusively: GitHub's own state says which
    comment was replaced, so no recency rule is applied at all. Mixing the two
    retires the very case minimize exists for — `sticky-pull-request-comment`
    with `hide_and_recreate` posts several concurrent lanes, and lane 2's newer
    comment would otherwise supersede lane 1's live one. A login that never
    minimizes falls back to newest-per-login, where `superseded_by` means recency
    and nothing else.
    """
    by_login: dict[str, list[dict]] = {}
    for candidate in candidates:
        candidate["superseded_by"] = None
        by_login.setdefault(candidate["login"], []).append(candidate)
    for group in by_login.values():
        if any(c["minimized"] for c in group):
            continue
        newest = max(group, key=lambda c: c["at"])
        for candidate in group:
            if newest["at"] > candidate["at"]:
                candidate["superseded_by"] = newest["id"]


def _summary_state(verdict: int | None, stale: bool) -> BotState | None:
    """None means the summary is dropped: a current, parsed, clean verdict."""
    if verdict is None:
        # Never known either way; staleness adds no information.
        return BotState.UNKNOWN
    if stale:
        # A stale clean verdict needs re-review, not an actionable bot fix.
        return BotState.STALE if verdict > 0 else BotState.STALE_CLEAN
    if verdict == 0:
        return None
    return BotState.BLOCKING


def bot_findings(pr: dict, viewer: str) -> tuple[BotFinding, ...]:
    """Collect bot threads and summaries; replies and pushes do not resolve threads."""
    findings: list[BotFinding] = []
    for thread in pr["reviewThreads"]["nodes"] or []:
        if thread["isResolved"]:
            continue
        first = thread_opener(thread)
        if not first or not author_is_bot(first):
            continue
        body = _body_of(first)
        findings.append(BotFinding(
            bot=login_of(first),
            surface=Surface.THREAD,
            state=BotState.OPEN_THREAD,
            at=parse_time(first["createdAt"]),
            thread_id=thread.get("id"),
            path=thread.get("path"),
            resolved=False,
            outdated=thread["isOutdated"],
            minimized=_minimized(first),
            minimized_reason=first.get("minimizedReason"),
            verdict=None,
            superseded_by=None,
            excerpt=_excerpt(body),
            body=body,
        ))

    pushed_at = head_landed_at(pr)
    answered = {
        Surface.COMMENT: [parse_time(c["createdAt"]) for c in (pr["comments"]["nodes"] or [])
                          if login_of(c) == viewer],
        Surface.REVIEW: [parse_time(r["submittedAt"]) for r in (pr["reviews"]["nodes"] or [])
                         if login_of(r) == viewer and r.get("submittedAt")],
    }
    candidates = _summary_candidates(pr)
    _apply_supersede(candidates)
    for candidate in candidates:
        at = candidate["at"]
        # Preserve historical same-surface viewer dispositions: the current
        # workflow does not create replies to automated reviewers.
        if any(t > at for t in answered[candidate["surface"]]):
            continue
        verdict = parse_bot_verdict(candidate["body"])
        state = _summary_state(verdict, bool(pushed_at and pushed_at > at))
        if state is None:
            continue
        findings.append(BotFinding(
            bot=candidate["login"],
            surface=candidate["surface"],
            state=state,
            at=at,
            thread_id=None,
            path=None,
            resolved=False,
            outdated=False,
            minimized=candidate["minimized"],
            minimized_reason=candidate["minimized_reason"],
            verdict=verdict,
            superseded_by=candidate["superseded_by"],
            excerpt=_excerpt(candidate["body"]),
            body=candidate["body"],
        ))

    findings.sort(key=lambda f: f.at)
    return tuple(findings)


def partial_reasons(pr: dict, ci: Ci, viewer: str) -> list[str]:
    """Every way this read can have come back incomplete, in one list.

    Consulted once by the merge recommendation and once by the quiet tail, so a
    new inconclusive case cannot be forgotten in one of two places. Five separate
    `and not` clauses is what let truncation, unknown conclusions and an
    uncomputed merge state each slip into "needs nothing" in turn.
    """
    unresolved: list[str] = []
    commits = pr["commits"]["nodes"]
    commit = commits[0]["commit"] if commits else {}
    contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
    nodes = contexts.get("nodes") or []
    total_checks = contexts.get("totalCount")
    if nodes and total_checks is None:
        # Defaulting the count to 0 made a missing field read as "nothing dropped".
        unresolved.append("the check rollup reported no total, so it cannot be "
                          "confirmed complete")
    elif total_checks is not None and total_checks > len(nodes):
        unresolved.append(f"checks ({total_checks} total, read {len(nodes)})")
    if len(pr["reviewThreads"]["nodes"] or []) >= THREAD_PAGE:
        unresolved.append(f"review threads (hit the {THREAD_PAGE} page limit)")
    if len(pr["comments"]["nodes"] or []) >= COMMENT_PAGE:
        unresolved.append(f"comments (hit the {COMMENT_PAGE} page limit)")
    if len(pr["reviews"]["nodes"] or []) >= REVIEW_PAGE:
        unresolved.append(f"reviews (hit the {REVIEW_PAGE} page limit)")
    unresolved.extend(unread_thread_middles(pr, viewer))
    if ci.unknown:
        unresolved.append("unrecognised check state on " + ", ".join(ci.unknown))
    if pr["mergeable"] == "UNKNOWN":
        unresolved.append("GitHub never computed the merge state")
    unresolved.extend(pr.get("_partial", []))
    # gh.py records the same uncomputed merge state after its repoll gives up, so
    # the two sources collide on a PR GitHub will never compute one for.
    return list(dict.fromkeys(unresolved))


def reasons(pr: dict, ci: Ci, owed: tuple[Owed, ...], findings: tuple[BotFinding, ...],
            approved_by: Sequence[str], changes_requested_by: Sequence[str],
            partial: list[str]) -> tuple[Reason, ...]:
    """The buckets, in priority order — the first one is where a consumer files it."""
    out: list[Reason] = []
    if owed:
        who = sorted({s.by for s in owed})
        out.append(Reason(ReasonKind.REPLY, f'{len(owed)} unanswered from {", ".join(who)}'))
    findings_owed = [f for f in findings if f.open and f.state is not BotState.STALE_CLEAN]
    stale_clean = [f for f in findings if f.open and f.state is BotState.STALE_CLEAN]
    if findings_owed:
        who = sorted({f.bot for f in findings_owed})
        out.append(Reason(ReasonKind.BOTFIX,
                          f'{len(findings_owed)} unresolved bot finding(s) from '
                          f'{", ".join(who)} — fix valid findings in code '
                          f'and obtain a current re-review'))
    if stale_clean:
        who = sorted({f.bot for f in stale_clean})
        out.append(Reason(ReasonKind.STALE_VERDICT,
                          f'{len(stale_clean)} bot verdict(s) from '
                          f'{", ".join(who)} predate the last push '
                          f'(re-review pending)'))
    if changes_requested_by and pr["reviewDecision"] == "CHANGES_REQUESTED":
        out.append(Reason(ReasonKind.BLOCKED,
                          "changes requested by " + ", ".join(changes_requested_by)))
    if pr["mergeable"] == "CONFLICTING":
        out.append(Reason(ReasonKind.CONFLICT, "merge conflicts"))
    if ci.state is CiState.FAIL:
        out.append(Reason(ReasonKind.CI, "failing: " + ", ".join(ci.failed)))
    if ci.state is CiState.PENDING:
        out.append(Reason(ReasonKind.RUNNING, f"{len(ci.pending)} check(s) still running"))
    if partial:
        out.append(Reason(ReasonKind.PARTIAL, "; ".join(partial)))
    # mergeStateStatus is GitHub's own answer to "would the merge button work".
    # MERGEABLE only means the diff applies, so BLOCKED (a required review or
    # check is missing) and BEHIND (base moved) both still refuse to merge.
    if _ready_but_ci(pr, owed, findings, approved_by, changes_requested_by) \
            and ci.state is CiState.PASS and not partial:
        if pr.get("mergeStateStatus") in ("CLEAN", "HAS_HOOKS"):
            out.append(Reason(ReasonKind.MERGE, "approved by " + ", ".join(approved_by)))
        else:
            out.append(Reason(ReasonKind.NOT_READY,
                              f'approved and green, but GitHub reports '
                              f'{pr.get("mergeStateStatus")}'))
    return tuple(out)


def _ready_but_ci(pr: dict, owed: tuple[Owed, ...], findings: tuple[BotFinding, ...],
                  approved_by: Sequence[str], changes_requested_by: Sequence[str]) -> bool:
    """Everything except the check verdict.

    Asked at a different moment from the merge recommendation, which also demands
    a green CI: `partial` holds "I could not tell", never "I could tell and it is
    red", so a failing or still-running check needs its own gate.
    """
    return (
        bool(approved_by) and not pr["isDraft"]
        and not owed and not any(f.open for f in findings) and not changes_requested_by
        and pr["mergeable"] == "MERGEABLE"
    )


def _viewer_review(pr: dict, viewer: str) -> ViewerReview | None:
    """The viewer's own latest review, which approved_by deliberately omits.

    Scanned from `reviews` first: `viewerLatestReview` resolves against the
    token's identity, and `viewer` is a free-form argument, so the two agree only
    when nobody passed --viewer. The server field is the fallback for a reviews
    page too long to carry the viewer's own row.
    """
    mine = [r for r in (pr["reviews"]["nodes"] or [])
            if login_of(r) == viewer and r.get("submittedAt")]
    latest = max(mine, key=lambda r: parse_time(r["submittedAt"]), default=None)
    if latest is None:
        latest = pr.get("viewerLatestReview")
        if not latest or (latest.get("author") and login_of(latest) != viewer):
            return None
    submitted = parse_time(latest.get("submittedAt"))
    if not submitted:
        return None
    return ViewerReview(state=latest.get("state"), submitted_at=submitted,
                        head_oid=(latest.get("commit") or {}).get("oid"))


def _review_requested_from(pr: dict) -> tuple[str, ...]:
    out = []
    for node in (pr.get("reviewRequests") or {}).get("nodes") or []:
        who = node.get("requestedReviewer") or {}
        name = who.get("login") or who.get("slug")
        if name:
            out.append(name)
    return tuple(out)


def classify(raw_pr: dict, viewer: str, now: datetime) -> PullRequest:
    """The whole normalization, from one GraphQL PR payload to one PullRequest.

    `now` is part of the exported signature and feeds no field: idle age belongs
    to the consumer, which has `updated_at`.
    """
    commits = raw_pr["commits"]["nodes"]
    commit = commits[0]["commit"] if commits else {}
    contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
    ci = ci_state(latest_per_check(contexts.get("nodes") or []))
    partial = partial_reasons(raw_pr, ci, viewer)
    signals = owed(raw_pr, viewer)
    findings = bot_findings(raw_pr, viewer)

    latest_review_by: dict[str, dict] = {}
    for review in raw_pr["reviews"]["nodes"] or []:
        author = login_of(review)
        if author == viewer or author_is_bot(review) or review["state"] not in (
            "APPROVED", "CHANGES_REQUESTED", "DISMISSED"
        ):
            continue
        latest_review_by[author] = review
    approved_by = tuple(sorted(
        a for a, r in latest_review_by.items() if r["state"] == "APPROVED"))
    changes_requested_by = tuple(sorted(
        a for a, r in latest_review_by.items() if r["state"] == "CHANGES_REQUESTED"))

    # No checks at all is normal (path filters, docs-only). It is an incomplete
    # read only on a PR otherwise about to be called ready, where "nothing ran"
    # and "everything passed" are not the same claim.
    if ci.state is CiState.NONE and _ready_but_ci(
        raw_pr, signals, findings, approved_by, changes_requested_by
    ):
        partial.append("no checks reported at all")

    return PullRequest(
        repo=raw_pr["_repo"],
        number=raw_pr["number"],
        title=raw_pr["title"],
        url=raw_pr["url"],
        author=login_of(raw_pr),
        draft=raw_pr["isDraft"],
        base=raw_pr["baseRefName"],
        updated_at=parse_time(raw_pr["updatedAt"]),
        mergeable=raw_pr["mergeable"],
        merge_state=raw_pr.get("mergeStateStatus"),
        review_decision=raw_pr["reviewDecision"],
        viewer_review=_viewer_review(raw_pr, viewer),
        review_requested_from=_review_requested_from(raw_pr),
        head_oid=raw_pr.get("headRefOid"),
        approved_by=approved_by,
        changes_requested_by=changes_requested_by,
        ci=ci,
        owed=signals,
        bot_findings=findings,
        partial=tuple(partial),
        reasons=reasons(raw_pr, ci, signals, findings, approved_by,
                        changes_requested_by, partial),
    )
