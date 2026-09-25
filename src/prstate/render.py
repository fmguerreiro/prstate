"""The bucketed report. Pure: a Sweep in, a string out.

Buckets are not computed here — `PullRequest.reasons` arrives filled by classify (D1).
The exact columns are INTERNAL, not contractual (DESIGN §5); `--json` is the contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from prstate.model import (
    BotFinding,
    BotState,
    CiState,
    Owed,
    PullRequest,
    ReasonKind,
    Sweep,
)

# Priority order is this dict's order; dict literals preserve it, so there is no
# second ORDER map to keep in sync.
HEADINGS: dict[ReasonKind, str] = {
    ReasonKind.BLOCKED: "Blocked on requested changes",
    ReasonKind.REPLY: "Waiting on your reply",
    ReasonKind.BOTFIX: (
        "Unresolved bot findings (blocks the merge until current re-review clears them)"
    ),
    ReasonKind.STALE_VERDICT: "Bot verdict predates the last push (re-review pending)",
    ReasonKind.MERGE: "Approved, green, GitHub will take the merge",
    ReasonKind.NOT_READY: "Approved and green, but GitHub still refuses the merge",
    ReasonKind.CONFLICT: "Merge conflicts",
    ReasonKind.CI: "CI red",
    ReasonKind.PARTIAL: "Read came back inconclusive (check by hand)",
    ReasonKind.RUNNING: "Checks still running",
}

_PRIORITY = list(HEADINGS)

TITLE_CHARS = 70
TEXT_CHARS = 110
MAX_OWED = 6
MAX_BOTS = 4
STALE_DRAFT_DAYS = 7


def buckets(prs: Sequence[PullRequest]) -> list[tuple[ReasonKind, list[PullRequest]]]:
    """Each flagged PR under its single highest-priority reason.

    Listing a PR under every reason printed the whole block once per reason and left
    the group counts not summing to the headline; the "[also ...]" tag carries the rest.
    """
    grouped: dict[ReasonKind, list[PullRequest]] = {kind: [] for kind in HEADINGS}
    for pr in prs:
        if not pr.reasons:
            continue
        top = min(pr.reasons, key=lambda reason: _PRIORITY.index(reason.kind))
        grouped[top.kind].append(pr)
    out = []
    for kind, group in grouped.items():
        if not group:
            continue
        # Idle days descending is oldest updated_at first, and needs no clock.
        group.sort(key=lambda pr: pr.updated_at)
        out.append((kind, group))
    return out


def _idle(now: datetime, when: datetime) -> int:
    return (now - when).days


def _text(item: Owed | BotFinding, *, full: bool) -> str:
    # --full trades the 200-char excerpt for the ≤1500-char body (D4); trimming it
    # again to one line's worth would make the flag do nothing.
    flat = " ".join((item.body if full else item.excerpt).split())
    return flat if full else flat[:TEXT_CHARS]


def _bot_label(finding: BotFinding) -> str:
    match finding.state:
        case BotState.BLOCKING:
            return f"{finding.verdict} blocking"
        case BotState.STALE:
            return f"{finding.verdict} blocking, unconfirmed after a push"
        case BotState.STALE_CLEAN:
            return "clean verdict predates a push, re-review pending"
        case BotState.OPEN_THREAD:
            return finding.path or "open thread"
        case _:
            return "unparseable"


def _owed_line(signal: Owed, now: datetime, *, full: bool) -> str:
    where = f" on {signal.path}" if signal.path else ""
    flag = " (outdated)" if signal.outdated else ""
    age = _idle(now, signal.at)
    return f"      > {signal.by}{where}, {age}d ago{flag}: {_text(signal, full=full)}"


def _bot_line(finding: BotFinding, now: datetime, *, full: bool) -> str:
    flag = " (outdated)" if finding.outdated else ""
    age = _idle(now, finding.at)
    return (
        f"      ~ {finding.bot} ({finding.surface.value}), {age}d ago{flag}, "
        f"{_bot_label(finding)}: {_text(finding, full=full)}"
    )


def _pr_block(
    pr: PullRequest, kind: ReasonKind, now: datetime, *, full: bool
) -> list[str]:
    tags = [reason.kind.value for reason in pr.reasons if reason.kind != kind]
    suffix = f'  [also {"/".join(tags)}]' if tags else ""
    draft = "[draft] " if pr.draft else ""
    out = [
        f"  {pr.key}  {draft}{pr.title[:TITLE_CHARS]}{suffix}",
        f"    ci={pr.ci.state.value} mergeable={pr.mergeable}/{pr.merge_state} "
        f"review={pr.review_decision} base={pr.base} "
        f"idle={_idle(now, pr.updated_at)}d",
    ]
    out += [f"    - {reason.detail}" for reason in pr.reasons]
    out += [_owed_line(signal, now, full=full) for signal in pr.owed[:MAX_OWED]]
    # Superseded and OUTDATED/RESOLVED findings ride along in the model but are not
    # actionable, so they are not printed (D2).
    live = [finding for finding in pr.bot_findings if finding.open]
    out += [_bot_line(finding, now, full=full) for finding in live[:MAX_BOTS]]
    if pr.ci.failed:
        out.append(f'      checks: {", ".join(pr.ci.failed)}')
    if pr.partial:
        out.append(f'      inconclusive: {"; ".join(pr.partial)}')
    return out


def render(sweep: Sweep, now: datetime, *, full: bool = False) -> str:
    prs = sweep.prs
    flagged = [pr for pr in prs if pr.reasons]
    out = [f"{len(flagged)} of {len(prs)} open PRs in {sweep.scope} need you."]
    # A shortfall belongs in the headline, not in a stderr line the reader sees
    # after the report, where the confident count has already landed.
    if sweep.partial:
        out[0] += " The read was incomplete, so this count is a floor, not a total."
        out += [f"  ! {entry}" for entry in sweep.partial]
    out.append("")

    for kind, group in buckets(prs):
        out.append(f"== {HEADINGS[kind]} ({len(group)}) ==")
        for pr in group:
            out += _pr_block(pr, kind, now, full=full)
        out.append("")

    no_checks = [pr.key for pr in prs if pr.ci.state is CiState.NONE]
    quiet = len(prs) - len(flagged)
    out.append(
        f"{quiet} PRs need nothing (waiting on reviewers or already answered). "
        "Every PR whose read was inconclusive is flagged above, not counted here."
    )
    if no_checks:
        shown = ", ".join(no_checks[:8])
        more = f" (+{len(no_checks) - 8} more)" if len(no_checks) > 8 else ""
        out.append(f"{len(no_checks)} have no checks at all: {shown}{more}")
    stale_drafts = sorted(
        (
            pr
            for pr in prs
            if pr.draft
            and not pr.reasons
            and _idle(now, pr.updated_at) >= STALE_DRAFT_DAYS
        ),
        key=lambda pr: pr.updated_at,
    )
    if stale_drafts:
        listed = ", ".join(
            f"{pr.key} {_idle(now, pr.updated_at)}d" for pr in stale_drafts[:10]
        )
        out.append(f"{len(stale_drafts)} stale drafts (close or finish): {listed}")
    return "\n".join(out)
