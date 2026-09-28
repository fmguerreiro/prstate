"""render.py owns grouping and printing only; the buckets arrive on PullRequest.reasons."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unicodedata import category

from prstate.model import (
    BotFinding,
    BotState,
    Ci,
    CiState,
    Owed,
    PullRequest,
    Reason,
    ReasonKind,
    Surface,
    Sweep,
)
from prstate.render import HEADINGS, buckets, render

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def make_ci(state: CiState = CiState.PASS, failed: tuple[str, ...] = ()) -> Ci:
    return Ci(state=state, failed=failed, pending=(), unknown=(), checks=())


def make_owed(*, by: str = "alice", excerpt: str = "please rename this", **kw) -> Owed:
    return Owed(
        surface=kw.get("surface", Surface.THREAD),
        by=by,
        at=kw.get("at", days_ago(2)),
        reason=kw.get("reason", "unanswered thread"),
        thread_id=kw.get("thread_id", "T1"),
        path=kw.get("path"),
        outdated=kw.get("outdated", False),
        excerpt=excerpt,
        body=kw.get("body", excerpt),
    )


def make_finding(
    *,
    bot: str = "coderabbitai",
    state: BotState = BotState.BLOCKING,
    excerpt: str = "two actionable comments",
    **kw,
) -> BotFinding:
    return BotFinding(
        bot=bot,
        surface=kw.get("surface", Surface.REVIEW),
        state=state,
        at=kw.get("at", days_ago(1)),
        thread_id=kw.get("thread_id", "B1"),
        path=kw.get("path"),
        resolved=kw.get("resolved", False),
        outdated=kw.get("outdated", False),
        minimized=kw.get("minimized", False),
        minimized_reason=kw.get("minimized_reason"),
        verdict=kw.get("verdict", 2),
        superseded_by=kw.get("superseded_by"),
        excerpt=excerpt,
        body=kw.get("body", excerpt),
    )


def make_pr(
    number: int = 1,
    *,
    reasons: tuple[Reason, ...] = (),
    idle: int = 1,
    **kw,
) -> PullRequest:
    return PullRequest(
        repo=kw.get("repo", "acme/widgets"),
        number=number,
        title=kw.get("title", "Add the thing"),
        url=f"https://github.com/acme/widgets/pull/{number}",
        author=kw.get("author", "fmguerreiro"),
        draft=kw.get("draft", False),
        base=kw.get("base", "main"),
        updated_at=days_ago(idle),
        mergeable=kw.get("mergeable", "MERGEABLE"),
        merge_state=kw.get("merge_state", "CLEAN"),
        review_decision=kw.get("review_decision"),
        viewer_review=kw.get("viewer_review"),
        review_requested_from=kw.get("review_requested_from", ()),
        head_oid=kw.get("head_oid", "deadbeef"),
        approved_by=kw.get("approved_by", ()),
        changes_requested_by=kw.get("changes_requested_by", ()),
        ci=kw.get("ci", make_ci()),
        owed=kw.get("owed", ()),
        bot_findings=kw.get("bot_findings", ()),
        partial=kw.get("partial", ()),
        reasons=reasons,
    )


def make_sweep(
    *prs: PullRequest, partial: tuple[str, ...] = (), scope: str = "owner acme"
) -> Sweep:
    return Sweep(
        scope=scope,
        viewer="fmguerreiro",
        fetched_at=NOW,
        prs=prs,
        partial=partial,
    )


def reason(kind: ReasonKind, detail: str = "because") -> Reason:
    return Reason(kind=kind, detail=detail)


def test_a_pr_appears_in_exactly_one_bucket():
    pr = make_pr(reasons=(reason(ReasonKind.CONFLICT), reason(ReasonKind.BLOCKED)))
    grouped = buckets([pr])
    assert [kind for kind, _ in grouped] == [ReasonKind.BLOCKED]
    assert [len(group) for _, group in grouped] == [1]


def test_the_lower_priority_reason_rides_along_as_an_also_tag():
    pr = make_pr(reasons=(reason(ReasonKind.BLOCKED), reason(ReasonKind.CONFLICT)))
    report = render(make_sweep(pr), NOW)
    title_line = next(line for line in report.splitlines() if pr.key in line)
    assert "[also conflict]" in title_line
    assert "blocked" not in title_line


def test_buckets_come_out_in_headings_order():
    low = make_pr(1, reasons=(reason(ReasonKind.CI),))
    high = make_pr(2, reasons=(reason(ReasonKind.REPLY),))
    assert [kind for kind, _ in buckets([low, high])] == [
        ReasonKind.REPLY,
        ReasonKind.CI,
    ]
    order = list(HEADINGS)
    assert order.index(ReasonKind.REPLY) < order.index(ReasonKind.CI)


def test_the_idlest_pr_leads_its_bucket():
    fresh = make_pr(1, idle=1, reasons=(reason(ReasonKind.CI),))
    stale = make_pr(2, idle=40, reasons=(reason(ReasonKind.CI),))
    [(_, group)] = buckets([fresh, stale])
    assert [pr.number for pr in group] == [2, 1]


def test_a_pr_with_no_reasons_is_in_no_bucket():
    assert buckets([make_pr()]) == []


def test_a_superseded_bot_finding_is_not_printed():
    pr = make_pr(
        reasons=(reason(ReasonKind.BOTFIX),),
        bot_findings=(
            make_finding(excerpt="old run said four", superseded_by="NEWER"),
            make_finding(excerpt="current run says two"),
        ),
    )
    report = render(make_sweep(pr), NOW)
    assert "current run says two" in report
    assert "old run said four" not in report


def test_a_finding_minimized_as_outdated_is_not_printed():
    pr = make_pr(
        reasons=(reason(ReasonKind.BOTFIX),),
        bot_findings=(
            make_finding(
                excerpt="retired by github",
                minimized=True,
                minimized_reason="OUTDATED",
            ),
        ),
    )
    assert "retired by github" not in render(make_sweep(pr), NOW)


def test_a_finding_minimized_as_spam_is_still_printed():
    # SPAM hides a comment; it does not mean a newer one replaced it.
    pr = make_pr(
        reasons=(reason(ReasonKind.BOTFIX),),
        bot_findings=(
            make_finding(
                excerpt="hidden but unanswered",
                minimized=True,
                minimized_reason="SPAM",
            ),
        ),
    )
    assert "hidden but unanswered" in render(make_sweep(pr), NOW)


def test_bot_lines_distinguish_the_finding_states():
    pr = make_pr(
        reasons=(reason(ReasonKind.BOTFIX),),
        bot_findings=(
            make_finding(excerpt="a", verdict=3),
            make_finding(excerpt="b", state=BotState.STALE, verdict=3),
            make_finding(excerpt="c", state=BotState.STALE_CLEAN, verdict=0),
            make_finding(excerpt="d", state=BotState.UNKNOWN, verdict=None),
        ),
    )
    lines = {
        line.rsplit(": ", 1)[1]: line
        for line in render(make_sweep(pr), NOW).splitlines()
        if line.lstrip().startswith("~")
    }
    assert "3 blocking" in lines["a"] and "after a push" not in lines["a"]
    assert "after a push" in lines["b"]
    assert "re-review pending" in lines["c"]
    assert "unparseable" in lines["d"]


def test_an_open_thread_finding_is_labelled_by_its_path():
    pr = make_pr(
        reasons=(reason(ReasonKind.BOTFIX),),
        bot_findings=(
            make_finding(
                excerpt="nit here",
                state=BotState.OPEN_THREAD,
                surface=Surface.THREAD,
                path="src/widgets/core.py",
            ),
        ),
    )
    assert "src/widgets/core.py" in render(make_sweep(pr), NOW)


def test_scope_appears_in_the_report_headline():
    report = render(make_sweep(make_pr(), scope="owner sakana"), NOW)
    assert "owner sakana" in report.splitlines()[0]


def test_an_incomplete_sweep_says_so_in_the_headline():
    complete = render(make_sweep(make_pr()), NOW)
    short = render(
        make_sweep(make_pr(), partial=("search page 3 failed: HTTP 502",)), NOW
    )
    assert "floor" not in complete
    assert "floor" in short.splitlines()[0]
    assert "search page 3 failed: HTTP 502" in short


def test_rendered_lines_strip_terminal_controls_from_github_text():
    malicious = (
        "Normal café\nINJECTED\x1b[31mRED\x1b]0;pwned\x07"
        "\u0085\u202eRTL\u2066"
    )
    pr = make_pr(
        reasons=(reason(ReasonKind.REPLY, malicious),),
        title=malicious,
        owed=(make_owed(body=malicious),),
    )
    report = render(
        make_sweep(pr, partial=(malicious,), scope=malicious), NOW, full=True
    )

    assert "Normal caféINJECTED[31mRED]0;pwnedRTL" in report
    assert all(
        char == "\n" or category(char) not in {"Cc", "Cf"} for char in report
    )
    assert "\nINJECTED" not in report


def test_a_pr_whose_read_was_inconclusive_renders_its_caveat():
    pr = make_pr(
        reasons=(reason(ReasonKind.PARTIAL),),
        partial=("mergeable never computed",),
    )
    assert "mergeable never computed" in render(make_sweep(pr), NOW)


def test_full_renders_the_body_and_the_default_the_excerpt():
    body = "prefix. " + "tail detail " * 40
    pr = make_pr(
        reasons=(reason(ReasonKind.REPLY),),
        owed=(make_owed(excerpt="prefix. short form", body=body),),
    )
    sweep = make_sweep(pr)
    assert "short form" in render(sweep, NOW)
    assert "short form" not in render(sweep, NOW, full=True)
    assert body.strip() in render(sweep, NOW, full=True)


def test_the_per_pr_lists_are_capped():
    pr = make_pr(
        reasons=(reason(ReasonKind.REPLY),),
        owed=tuple(make_owed(excerpt=f"owed {i}") for i in range(9)),
        bot_findings=tuple(make_finding(excerpt=f"bot {i}") for i in range(7)),
    )
    lines = render(make_sweep(pr), NOW).splitlines()
    assert sum(1 for line in lines if line.lstrip().startswith(">")) == 6
    assert sum(1 for line in lines if line.lstrip().startswith("~")) == 4


def test_failed_checks_are_listed():
    pr = make_pr(
        reasons=(reason(ReasonKind.CI),),
        ci=make_ci(CiState.FAIL, failed=("build", "lint")),
    )
    report = render(make_sweep(pr), NOW)
    assert "build" in report and "lint" in report
    assert "ci=fail" in report
