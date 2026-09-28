"""The rules, each test named for what it pins rather than the PR it came from.

Offline and deterministic: every payload comes from the builders in conftest.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import approved_pr, check, comment, commits, pr, review, status, thread

from prstate.classify import (
    bot_findings,
    ci_state,
    classify,
    head_landed_at,
    latest_per_check,
    owed,
    parse_bot_verdict,
    thread_comments,
)
from prstate.model import BotState, CiState, ReasonKind, Surface
from prstate.query import THREAD_PAGE

ME = "me"
NOW = dt.datetime(2026, 1, 11, tzinfo=dt.timezone.utc)


def kinds(payload, viewer=ME):
    return [r.kind for r in classify(payload, viewer, NOW).reasons]


def ci_of(*nodes):
    return ci_state(latest_per_check(list(nodes)))


def at(day, hour=0):
    return f"2026-01-{day:02d}T{hour:02d}:00:00Z"


# --- owed: issue comments ----------------------------------------------------

def test_later_reply_of_mine_answers_a_comment():
    payload = pr(comments={"nodes": [
        comment("them", at(5), "q?"),
        comment(ME, at(5, 6), "a"),
    ]})
    assert owed(payload, ME) == ()


def test_comment_with_no_reply_is_owed():
    payload = pr(comments={"nodes": [comment("them", at(5), "q?")]})
    signals = owed(payload, ME)
    assert [(s.by, s.surface) for s in signals] == [("them", Surface.COMMENT)]


def test_my_earlier_reply_does_not_answer_a_later_ask():
    payload = pr(comments={"nodes": [
        comment(ME, at(4), "a"),
        comment("them", at(5), "q?"),
    ]})
    assert [s.by for s in owed(payload, ME)] == ["them"]


def test_bot_comments_are_never_owed():
    payload = pr(comments={"nodes": [
        comment("claude", at(5), "nit"),
        comment("greptile-apps[bot]", at(6), "x"),
    ]})
    assert owed(payload, ME) == ()


def test_github_bot_actor_is_not_owed_without_a_bot_login_suffix():
    payload = pr(comments={"nodes": [
        comment("sakana-ai-github-app", at(5), "plan", actor_type="Bot"),
    ]})
    assert owed(payload, ME) == ()


def test_minimized_human_comment_is_not_owed():
    payload = pr(comments={"nodes": [
        comment("them", at(5), "q?", minimized=True, reason="OFF_TOPIC"),
    ]})
    assert owed(payload, ME) == ()


def test_minimized_review_body_is_not_owed():
    payload = pr(reviews={"nodes": [
        review("them", "CHANGES_REQUESTED", body="fix", at=at(5),
               minimized=True, reason="SPAM"),
    ]})
    assert owed(payload, ME) == ()


def test_a_hidden_thread_end_is_skipped_and_the_live_ask_underneath_stands():
    # D3 on the thread surface: the walk steps past the hidden comment rather
    # than treating the thread as answered, so the older live ask still counts.
    hidden_only = pr(reviewThreads={"nodes": [
        thread(comment("them", at(5), "q", minimized=True, reason="RESOLVED")),
    ]})
    assert owed(hidden_only, ME) == ()
    over_a_live_ask = pr(reviewThreads={"nodes": [
        thread(comment("them", at(3), "the real ask"),
               comment("them", at(5), "hidden", minimized=True, reason="OFF_TOPIC")),
    ]})
    signals = owed(over_a_live_ask, ME)
    assert [(s.by, s.at.day) for s in signals] == [("them", 3)]


# --- owed: review threads ----------------------------------------------------

def test_thread_where_i_spoke_last_is_not_owed():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("them", at(5), "q"), comment(ME, at(6), "a")),
    ]})
    assert owed(payload, ME) == ()


def test_minimized_viewer_reply_still_answers_thread():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("them", at(5), "q"),
               comment(ME, at(6), "a", minimized=True, reason="OFF_TOPIC")),
    ]})
    assert owed(payload, ME) == ()


def test_unresolved_thread_with_their_last_word_is_owed():
    payload = pr(reviewThreads={"nodes": [thread(comment("them", at(5), "q"))]})
    assert [(s.by, s.surface) for s in owed(payload, ME)] == [("them", Surface.THREAD)]


def test_unresolved_outdated_human_thread_remains_owed():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("colleague", at(5), "still broken"), outdated=True),
    ]})
    row = classify(payload, ME, NOW)
    assert [(signal.by, signal.surface, signal.outdated) for signal in row.owed] == [
        ("colleague", Surface.THREAD, True),
    ]


def test_resolved_thread_is_not_owed():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("them", at(5), "q"), resolved=True),
    ]})
    assert owed(payload, ME) == ()


def test_owed_follows_the_recent_connection_not_the_first_page():
    # opener and last come from two separate connections, so a thread longer than
    # one page still reports who actually spoke last, attributed to the LAST
    # comment: that is the ask to answer, not the opener.
    payload = pr(reviewThreads={"nodes": [
        thread(comment("them", at(3), "q"), comment("them", at(8), "bump"), total=57),
    ]})
    signals = owed(payload, ME)
    assert [s.by for s in signals] == ["them"]
    assert signals[0].at == dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc)


def test_bot_opened_thread_stays_open_through_replies_push_and_outdated():
    opener = comment("greptile-apps[bot]", at(5), "Bug: this breaks x")
    node = thread(opener, comment(ME, at(6), "fixed"))
    payload = pr(reviewThreads={"nodes": [node]})

    row = classify(payload, ME, NOW)
    assert [(finding.state, finding.open) for finding in row.bot_findings] == [
        (BotState.OPEN_THREAD, True),
    ]

    node["recent"]["nodes"] = [comment("colleague", at(7), "still broken")]
    node["recent"]["totalCount"] = 3
    row = classify(payload, ME, NOW)
    assert [signal.by for signal in row.owed] == ["colleague"]
    assert [(finding.state, finding.open) for finding in row.bot_findings] == [
        (BotState.OPEN_THREAD, True),
    ]
    assert [reason.kind for reason in row.reasons] == [ReasonKind.REPLY, ReasonKind.BOTFIX]

    payload["commits"] = commits(committed=at(8), rollup=False)
    row = classify(payload, ME, NOW)
    assert [(finding.state, finding.open) for finding in row.bot_findings] == [
        (BotState.OPEN_THREAD, True),
    ]

    node["isOutdated"] = True
    row = classify(payload, ME, NOW)
    assert [(finding.state, finding.open, finding.outdated)
            for finding in row.bot_findings] == [
        (BotState.OPEN_THREAD, True, True),
    ]

    node["isResolved"] = True
    assert classify(payload, ME, NOW).bot_findings == ()


def test_bot_only_thread_owes_nothing():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("greptile-apps[bot]", at(5), "nit")),
    ]})
    assert owed(payload, ME) == ()


def test_trailing_bot_does_not_close_a_human_ask():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("colleague", at(5), "real bug, please fix"),
               comment("claude", at(6), "summarised the thread")),
    ]})
    assert [s.by for s in owed(payload, ME)] == ["colleague"]


def test_trailing_bot_over_an_unread_middle_is_partial_not_owed():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("colleague", at(5), "real bug"),
               comment("claude", at(7), "summarised"), total=3),
    ]})
    assert owed(payload, ME) == ()
    assert classify(payload, ME, NOW).partial


def test_an_empty_partial_thread_does_not_discard_other_signals():
    payload = pr(
        comments={"nodes": [comment("colleague", at(5), "real bug")]},
        reviewThreads={"nodes": [thread(total=1)]},
    )
    assert [signal.by for signal in classify(payload, ME, NOW).owed] == ["colleague"]


def test_my_reply_at_the_newest_end_closes_the_thread():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("colleague", at(5), "q"), comment(ME, at(6), "done")),
    ]})
    assert owed(payload, ME) == ()


def test_thread_comments_unions_deduplicates_and_orders_all_connections():
    opener = comment("colleague", at(3), "ask", id="opener")
    middle = comment(ME, at(5), "fixed", id="middle")
    newest = comment("claude", at(7), "summary", id="newest")
    node = thread(opener, newest, total=3)
    node["activity"] = {"nodes": [newest, middle, opener]}
    assert [item["id"] for item in thread_comments(node)] == ["opener", "middle", "newest"]


def test_middle_viewer_reply_before_bot_answers_earlier_human():
    opener = comment("colleague", at(3), "ask")
    middle = comment(ME, at(5), "fixed")
    newest = comment("claude", at(7), "summary")
    node = thread(opener, newest, total=3)
    node["activity"] = {"nodes": [opener, middle, newest]}
    assert owed(pr(reviewThreads={"nodes": [node]}), ME) == ()


def test_human_after_middle_viewer_reply_is_still_owed_before_bot():
    opener = comment("colleague", at(3), "first ask")
    mine = comment(ME, at(5), "fixed")
    later = comment("other", at(6), "still broken")
    newest = comment("claude", at(7), "summary")
    node = thread(opener, newest, total=4)
    node["activity"] = {"nodes": [later, opener, mine, newest]}
    signals = owed(pr(reviewThreads={"nodes": [node]}), ME)
    assert [(signal.by, signal.at.day) for signal in signals] == [("other", 6)]


def test_deleted_author_is_owed_not_dropped():
    # A deleted account is an unknown author, not a bot: treating it as one drops
    # whatever it said. It also has to survive classify, where a None author once
    # reached a ", ".join and took the whole report down with it.
    payload = pr(reviewThreads={"nodes": [
        thread(comment(None, at(5), "this breaks prod")),
    ]})
    assert [s.by for s in owed(payload, ME)] == ["(deleted account)"]
    assert kinds(payload) == [ReasonKind.REPLY]


def test_both_ends_bots_with_unread_middle_is_partial():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("greptile-apps[bot]", at(5), "nit"),
               comment("claude", at(9), "done"), total=57),
    ]})
    assert owed(payload, ME) == ()
    assert classify(payload, ME, NOW).partial


def test_my_comment_at_the_oldest_end_does_not_close_an_unread_thread():
    # An annotation I left at the bottom is not a read of the comments above it.
    payload = pr(reviewThreads={"nodes": [
        thread(comment(ME, at(5), "x"), comment("claude", at(9), "y"), total=57),
    ]})
    assert classify(payload, ME, NOW).partial


def test_absent_thread_comment_count_is_not_zero():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("claude", at(5), "nit"), comment("claude", at(9), "done"),
               total=None),
    ]})
    assert "reported no comment count" in " ".join(classify(payload, ME, NOW).partial)


def test_short_bot_thread_has_no_unread_middle():
    payload = pr(reviewThreads={"nodes": [
        thread(comment("greptile-apps[bot]", at(5), "nit"),
               comment("claude", at(9), "done")),
    ]})
    assert classify(payload, ME, NOW).partial == ()


# --- owed: review bodies -----------------------------------------------------

def test_push_after_a_review_body_answers_it():
    payload = pr(reviews={"nodes": [
        review("them", "COMMENTED", body="please fix", at=at(1)),
    ]})
    assert owed(payload, ME) == ()


def test_middle_thread_reply_answers_an_earlier_review_body():
    opener = comment("colleague", at(3), "thread ask")
    mine = comment(ME, at(4), "fixed")
    newest = comment("claude", at(5), "summary")
    node = thread(opener, newest, total=3)
    node["activity"] = {"nodes": [opener, mine, newest]}
    payload = pr(
        reviewThreads={"nodes": [node]},
        reviews={"nodes": [review("other", "COMMENTED", body="please fix", at=at(2))]},
    )
    assert owed(payload, ME) == ()


def test_unread_thread_middle_only_hides_review_body_it_might_answer():
    node = thread(comment("other", at(3), "thread ask"),
                  comment("claude", at(7), "summary"), total=3)
    node["_activity_complete"] = False
    payload = pr(
        _thread_activity_complete=False,
        _partial=["thread activity incomplete"],
        reviewThreads={"nodes": [node]},
        reviews={"nodes": [review("other", "COMMENTED", body="earlier ask", at=at(5)),
                           review("other", "COMMENTED", body="later ask", at=at(8))]},
    )
    row = classify(payload, ME, NOW)
    assert [(signal.by, signal.at.day) for signal in row.owed] == [("other", 8)]
    assert "thread activity incomplete" in row.partial


def test_push_inside_the_commit_to_check_window_does_not_answer_an_earlier_review():
    # The push time is only bounded, between the committer timestamp and the
    # start of the checks it triggered. Resolving to the TOP of that interval
    # would let a commit whose checks started at 06:00 claim to have answered a
    # 05:00 review it may predate.
    payload = pr(
        commits=commits(check("ci", "SUCCESS", run=1, started=at(1, 6),
                              completed=at(1, 6)), committed=at(1)),
        reviews={"nodes": [review("them", "CHANGES_REQUESTED", body="fix", at=at(1, 5))]},
    )
    assert [s.by for s in owed(payload, ME)] == ["them"]


def test_a_push_by_the_author_is_not_viewer_activity_for_a_reviewer():
    payload = pr(
        author={"login": "them"},
        reviews={"nodes": [review("other", "COMMENTED", body="please fix", at=at(5))]},
        commits=commits(committed=at(6), rollup=False),
    )
    assert [s.by for s in owed(payload, ME)] == ["other"]


def test_a_push_by_the_pr_author_answers_a_review_body_when_i_am_the_author():
    payload = pr(
        author={"login": ME},
        reviews={"nodes": [review("other", "COMMENTED", body="please fix", at=at(5))]},
        commits=commits(committed=at(6), rollup=False),
    )
    assert owed(payload, ME) == ()


def test_an_empty_review_body_is_not_an_ask():
    payload = pr(reviews={"nodes": [review("them", "COMMENTED", body="   ", at=at(5))]})
    assert owed(payload, ME) == ()


@pytest.mark.parametrize("revs", [
    [review(None, "APPROVED"), review("colleague", "APPROVED")],
    [review(None, "APPROVED")],
    [review(None, "CHANGES_REQUESTED")],
], ids=["deleted with named approver", "lone deleted approver", "deleted requested changes"])
def test_deleted_reviewer_does_not_crash_the_row(revs):
    row = classify(pr(reviewDecision="APPROVED", reviews={"nodes": revs}), ME, NOW)
    assert all(isinstance(a, str) for a in row.approved_by)


# --- CI collapse -------------------------------------------------------------

def test_rerun_green_supersedes_the_old_failure():
    ci = ci_of(check("audit", "FAILURE", run=1, completed=at(1, 10)),
               check("audit", "SUCCESS", run=2, completed=at(2, 10)))
    assert (ci.state, ci.failed) == (CiState.PASS, ())


def test_rerun_red_supersedes_the_old_pass():
    ci = ci_of(check("audit", "SUCCESS", run=1, completed=at(1, 10)),
               check("audit", "FAILURE", run=2, completed=at(2, 10)))
    assert (ci.state, ci.failed) == (CiState.FAIL, ("audit",))


def test_higher_run_id_wins_over_earlier_finish_time():
    # Two runs of the same workflow race and the NEWER run's job finishes first
    # (komb-enterprise#530). Ordering by completedAt picks the superseded verdict.
    ci = ci_of(check("lint", "FAILURE", run=100, completed="2026-01-01T00:10:00Z"),
               check("lint", "SUCCESS", run=200, completed="2026-01-01T00:09:50Z"))
    assert ci.state is CiState.PASS


@pytest.mark.parametrize("order", [("SUCCESS", "FAILURE"), ("FAILURE", "SUCCESS")])
def test_tie_without_run_ids_resolves_to_the_worse_state(order):
    ci = ci_of(check("audit", order[0]), check("audit", order[1]))
    assert (ci.state, ci.failed) == (CiState.FAIL, ("audit",))


def test_same_check_name_in_two_workflows_both_survive():
    # A higher run id from a DIFFERENT workflow means "started later", never
    # "supersedes", so one workflow's green must not bury the other's red.
    ci = ci_of(check("test", "FAILURE", run=100, workflow="backend", completed=at(1, 10)),
               check("test", "SUCCESS", run=200, workflow="web", completed=at(1, 20)))
    assert (ci.state, ci.failed) == (CiState.FAIL, ("test",))


def test_same_workflow_higher_run_id_supersedes():
    ci = ci_of(check("test", "FAILURE", run=100, workflow="backend", completed=at(1, 10)),
               check("test", "SUCCESS", run=200, workflow="backend", completed=at(1, 20)))
    assert ci.state is CiState.PASS


def test_error_conclusion_is_a_failure():
    # ERROR is how an external provider reports a red check. Missing it put a
    # genuinely failing PR under "inconclusive" instead of "CI red".
    ci = ci_of(status("buildkite", "ERROR", created=at(2)))
    assert (ci.state, ci.failed) == (CiState.FAIL, ("buildkite",))


@pytest.mark.parametrize("node", [
    check("x", "MYSTERY", run=1, completed=at(2)),
    status("x", None, created=at(2)),
], ids=["unrecognised conclusion", "no state at all"])
def test_unrecognised_conclusion_is_unknown_not_pass(node):
    ci = ci_of(node)
    assert (ci.state, ci.unknown) == (CiState.UNKNOWN, ("x",))


def test_expected_is_pending_and_skipped_is_pass():
    assert ci_of(status("x", "EXPECTED", created=at(2))).state is CiState.PENDING
    assert ci_of(check("x", "SKIPPED", run=1, completed=at(2))).state is CiState.PASS


def test_no_checks_is_none_not_pass():
    assert ci_of().state is CiState.NONE


def test_unknown_survives_a_pending_headline():
    # Collapsing to one entry per name needed a severity order, and any order
    # makes "unknown" comparable and so droppable: pending outranked the
    # unrecognised conclusion and it vanished.
    ci = ci_of(check("test", "QUEUED", run=1, workflow="backend"),
               check("test", "MYSTERY", run=2, workflow="web", completed=at(2, 1)))
    assert (ci.state, ci.pending, ci.unknown) == (CiState.PENDING, ("test",), ("test",))


def test_status_context_and_check_run_do_not_share_a_bucket():
    ci = ci_of(status("build", "FAILURE", created=at(2, 10)),
               check("build", "SUCCESS", run=None, workflow=None, completed=at(2, 20)))
    assert (ci.state, ci.failed) == (CiState.FAIL, ("build",))


def test_in_progress_check_with_null_conclusion_is_pending():
    live = check("ci", None, run=1, started=at(2, 1))
    live["status"] = "IN_PROGRESS"
    assert ci_of(live).state is CiState.PENDING


def test_pending_carries_check_names_one_per_check():
    ci = ci_of(check("test", "QUEUED", run=1, workflow="backend"),
               check("test", "QUEUED", run=2, workflow="web"))
    assert ci.pending == ("test", "test")


# --- push time ---------------------------------------------------------------

def test_head_landed_at_is_the_committed_date():
    payload = pr(commits=commits(
        check("ci", "SUCCESS", run=1, started=at(1, 6), completed=at(1, 6)),
        committed=at(1)))
    assert head_landed_at(payload) == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def test_head_landed_at_is_none_without_commits():
    assert head_landed_at(pr(commits={"nodes": []})) is None


# --- partial -----------------------------------------------------------------

def test_truncated_rollup_is_partial_not_merge_ready():
    payload = approved_pr(commits=commits(
        check("ci", "SUCCESS", run=1, completed=at(2, 1)), total=140))
    assert kinds(payload) == [ReasonKind.PARTIAL]


def test_rollup_without_a_total_count_is_partial():
    # Defaulting the count to 0 made the absent field read as "nothing dropped".
    payload = approved_pr(commits=commits(
        check("ci", "SUCCESS", run=1, completed=at(2, 1)), total=None))
    assert kinds(payload) == [ReasonKind.PARTIAL]


def test_thread_page_at_its_limit_is_partial():
    answered = thread(comment("them", at(5), "q"), comment(ME, at(6), "a"))
    payload = pr(reviewThreads={"nodes": [answered] * THREAD_PAGE})
    assert kinds(payload) == [ReasonKind.PARTIAL]


def test_one_long_thread_is_not_partial():
    # Its opener and its last comment decide the verdict and both are read, so
    # flagging it buries a ready PR (it buried hl#3270 on three comments).
    payload = pr(reviewThreads={"nodes": [
        thread(comment("them", at(3), "q"), comment(ME, at(8), "done"), total=57),
    ]})
    assert classify(payload, ME, NOW).partial == ()


def test_unknown_check_state_is_partial():
    payload = approved_pr(commits=commits(
        check("queued", "QUEUED", run=1),
        check("deploy", "MYSTERY", run=1, completed=at(2, 1))))
    assert ReasonKind.PARTIAL in kinds(payload)
    assert ReasonKind.MERGE not in kinds(payload)


def test_uncomputed_merge_state_is_partial():
    assert kinds(approved_pr(mergeable="UNKNOWN")) == [ReasonKind.PARTIAL]


def test_no_checks_on_an_otherwise_ready_pr_is_partial():
    # No checks at all is normal, and inconclusive only where "nothing ran" and
    # "everything passed" would otherwise be the same claim.
    assert kinds(approved_pr(commits=commits(rollup=False))) == [ReasonKind.PARTIAL]


# --- buckets -----------------------------------------------------------------

def test_approved_green_answered_pr_is_merge():
    assert kinds(approved_pr()) == [ReasonKind.MERGE]


def test_red_check_is_ci_not_partial():
    # A red check is not an inconclusive read, it is a conclusive bad one, so it
    # needs its own gate: consolidating the predicates dropped this clause once
    # and printed a FAILURE under "approved, green, will take it".
    payload = approved_pr(commits=commits(
        check("ci", "FAILURE", run=1, completed=at(2, 1))))
    assert kinds(payload) == [ReasonKind.CI]


def test_queued_check_is_running():
    payload = approved_pr(commits=commits(check("ci", "QUEUED", run=1)))
    assert kinds(payload) == [ReasonKind.RUNNING]


def test_bot_thread_keeps_an_approved_green_pr_out_of_merge():
    nit = thread(comment("greptile-apps[bot]", at(5), "Bug: this breaks x"))
    assert kinds(approved_pr(reviewThreads={"nodes": [nit]})) == [ReasonKind.BOTFIX]
    resolved = thread(comment("greptile-apps[bot]", at(5), "Bug: this breaks x"),
                      resolved=True)
    assert kinds(approved_pr(reviewThreads={"nodes": [resolved]})) == [ReasonKind.MERGE]


@pytest.mark.parametrize("merge_state", ["BLOCKED", "BEHIND", "UNSTABLE"])
def test_blocked_and_behind_and_unstable_are_not_ready(merge_state):
    # mergeable only says the diff applies; GitHub still refuses these.
    assert kinds(approved_pr(mergeStateStatus=merge_state)) == [ReasonKind.NOT_READY]


def test_has_hooks_is_mergeable():
    assert kinds(approved_pr(mergeStateStatus="HAS_HOOKS")) == [ReasonKind.MERGE]


def test_changes_requested_and_a_conflict_both_reach_the_reasons():
    payload = pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY",
                 reviewDecision="CHANGES_REQUESTED",
                 reviews={"nodes": [review("colleague", "CHANGES_REQUESTED")]})
    assert kinds(payload) == [ReasonKind.BLOCKED, ReasonKind.CONFLICT]


# --- bot verdict parsing -----------------------------------------------------

def test_blocking_heading_count():
    assert parse_bot_verdict("### Blocking: 2\n1. thing\n2. other") == 2


def test_bolded_zero_is_clean():
    assert parse_bot_verdict("**Blocking: 0**") == 0


def test_bug_lines_count_as_findings():
    assert parse_bot_verdict("Bug: this breaks x\nBug: that breaks y") == 2


def test_clean_phrase_is_zero():
    assert parse_bot_verdict("no blocking findings here") == 0


def test_unparseable_verdict_is_none_not_zero():
    # Shaped like a verdict but the count cannot be read: never resolve toward
    # green, so this is unknown, not clean and not dropped.
    assert parse_bot_verdict("blocking concerns noted above") is None


# --- bot summaries -----------------------------------------------------------

BLOCKING_BODY = "**Claude finished @me's task in 5m** — ### Blocking: 2\n1. bug\n2. bug"
CLEAN_BODY = "**Claude finished @me's task** — ### Blocking: 0"


def summary_pr(*comments, pushed=None, **over):
    """An approved, green PR carrying bot summaries; `pushed` moves the head."""
    if pushed:
        over["commits"] = commits(check("ci", "SUCCESS", run=1, completed=pushed),
                                  committed=pushed)
    return approved_pr(comments={"nodes": list(comments)}, **over)


def test_blocking_summary_blocks_the_merge():
    payload = summary_pr(comment("claude", at(5), BLOCKING_BODY))
    findings = bot_findings(payload, ME)
    assert [(f.state, f.verdict, f.surface) for f in findings] == [
        (BotState.BLOCKING, 2, Surface.COMMENT)]
    assert kinds(payload) == [ReasonKind.BOTFIX]


def test_pending_viewer_review_does_not_hide_bot_review_summary():
    pending = review(ME, "PENDING", at=None)
    summary = review("claude", "COMMENTED", body=BLOCKING_BODY, at=at(5))
    row = classify(pr(reviews={"nodes": [pending, summary]}), ME, NOW)
    assert [(finding.surface, finding.state) for finding in row.bot_findings] == [
        (Surface.REVIEW, BotState.BLOCKING),
    ]


def test_clean_current_summary_is_dropped():
    payload = summary_pr(comment("claude", at(5), CLEAN_BODY))
    assert bot_findings(payload, ME) == ()
    assert kinds(payload) == [ReasonKind.MERGE]


def test_unparseable_summary_still_blocks():
    payload = summary_pr(comment("claude", at(5), "Claude reviewed; see blocking concerns."))
    assert [f.state for f in bot_findings(payload, ME)] == [BotState.UNKNOWN]
    assert kinds(payload) == [ReasonKind.BOTFIX]


def test_blocking_summary_before_a_push_is_stale_and_still_blocking():
    payload = summary_pr(comment("claude", at(5), BLOCKING_BODY), pushed=at(6))
    assert [(f.state, f.verdict) for f in bot_findings(payload, ME)] == [(BotState.STALE, 2)]
    assert kinds(payload) == [ReasonKind.BOTFIX]


def test_clean_summary_before_a_push_is_stale_clean_not_botfix():
    # Six PRs whose bot said "Blocking: 0" before a later push were reported as
    # owing a reply for a finding that never existed, and pulled an approved,
    # green PR (#711) out of the merge bucket (2026-08-24).
    clean = summary_pr(comment("claude", at(5), CLEAN_BODY), pushed=at(6))
    assert [(f.state, f.verdict) for f in bot_findings(clean, ME)] == [
        (BotState.STALE_CLEAN, 0)]
    assert kinds(clean) == [ReasonKind.STALE_VERDICT]
    blocking = summary_pr(comment("claude", at(5), BLOCKING_BODY), pushed=at(6))
    texts = {r.kind: r.detail for r in classify(blocking, ME, NOW).reasons}
    clean_texts = {r.kind: r.detail for r in classify(clean, ME, NOW).reasons}
    assert "current re-review" in texts[ReasonKind.BOTFIX]
    assert "current re-review" not in clean_texts[ReasonKind.STALE_VERDICT]


def test_my_reply_on_the_same_surface_suppresses_a_summary():
    payload = summary_pr(comment("claude", at(5), BLOCKING_BODY),
                         comment(ME, at(6), "fixed in #abc123"))
    assert bot_findings(payload, ME) == ()
    assert kinds(payload) == [ReasonKind.MERGE]


def test_a_reply_on_another_surface_does_not_suppress_a_summary():
    # Suppression is per surface. An issue comment of mine is not a disposition
    # of a summary the bot left as a review body.
    payload = approved_pr(
        comments={"nodes": [comment(ME, at(6), "fixed in #abc123")]},
        reviews={"nodes": [review("them", "APPROVED"),
                           review("claude", "COMMENTED", body=BLOCKING_BODY, at=at(5))]},
    )
    assert [f.state for f in bot_findings(payload, ME)] == [BotState.BLOCKING]


def test_newer_clean_re_review_supersedes_the_older_blocking_summary():
    payload = summary_pr(
        comment("claude", at(3), "### Blocking: 2\n1. bug\n2. bug"),
        comment("claude", at(5), "Re-review; earlier threads resolved. ### Blocking: 0"))
    assert kinds(payload) == [ReasonKind.MERGE]


# --- minimize vs recency (CHANGE 3, B4) --------------------------------------

def test_minimized_outdated_summary_is_not_open():
    payload = summary_pr(comment("claude", at(5), BLOCKING_BODY,
                                 minimized=True, reason="OUTDATED"))
    findings = bot_findings(payload, ME)
    assert [(f.state, f.open) for f in findings] == [(BotState.BLOCKING, False)]
    assert kinds(payload) == [ReasonKind.MERGE]


def test_minimize_beats_newest_per_login():
    # Newest-per-login retires the older summary; GitHub's own minimize state
    # says the NEWER one is the one that was withdrawn.
    payload = summary_pr(
        comment("claude", at(3), BLOCKING_BODY, id="live"),
        comment("claude", at(5), BLOCKING_BODY, minimized=True, reason="OUTDATED"))
    live = [f for f in bot_findings(payload, ME) if f.open]
    assert [f.at.day for f in live] == [3]
    assert kinds(payload) == [ReasonKind.BOTFIX]


@pytest.mark.parametrize("reason", ["SPAM", "ABUSE", "OFF_TOPIC", "LOW_QUALITY"])
def test_minimized_reason_spam_does_not_supersede(reason):
    # Hidden is not replaced. SPAM and LOW_QUALITY both occur in the wild.
    payload = summary_pr(comment("claude", at(5), BLOCKING_BODY,
                                 minimized=True, reason=reason))
    assert [f.open for f in bot_findings(payload, ME)] == [True]
    assert kinds(payload) == [ReasonKind.BOTFIX]


def test_superseded_summary_carries_the_newer_node_id():
    payload = summary_pr(
        comment("claude", at(3), BLOCKING_BODY, id="older"),
        comment("claude", at(5), BLOCKING_BODY, id="newer"))
    findings = {f.at.day: f for f in bot_findings(payload, ME)}
    assert findings[3].superseded_by == "newer" and findings[3].open is False
    assert findings[5].superseded_by is None
    botfix = {r.kind: r.detail for r in classify(payload, ME, NOW).reasons}
    assert botfix[ReasonKind.BOTFIX].startswith("1 unresolved")


def test_a_minimizing_bot_keeps_every_live_lane_open():
    # One login, two concurrent lanes (sticky-pull-request-comment with
    # hide_and_recreate). Lane 2's replacement must not retire lane 1.
    payload = summary_pr(
        comment("claude", at(1), BLOCKING_BODY, id="A"),
        comment("claude", at(2), BLOCKING_BODY, id="B",
                minimized=True, reason="OUTDATED"),
        comment("claude", at(3), BLOCKING_BODY, id="C"))
    findings = {f.at.day: f for f in bot_findings(payload, ME)}
    assert findings[1].open is True and findings[1].superseded_by is None
    assert findings[2].open is False
    assert findings[3].open is True


def test_bot_threads_and_summaries_share_one_list():
    payload = summary_pr(
        comment("claude", at(5), BLOCKING_BODY),
        reviewThreads={"nodes": [thread(comment("greptile-apps[bot]", at(3), "Bug: x"))]})
    findings = bot_findings(payload, ME)
    assert [f.state for f in findings] == [BotState.OPEN_THREAD, BotState.BLOCKING]


# --- the viewer as reviewer --------------------------------------------------

def test_viewer_review_state_survives_into_the_contract():
    payload = pr(
        author={"login": "them"},
        reviews={"nodes": [review(ME, "APPROVED", at=at(3)),
                           review("colleague", "APPROVED", at=at(4))]},
        commits=commits(committed=at(6), rollup=False),
    )
    row = classify(payload, ME, NOW)
    assert row.viewer_review.state == "APPROVED"
    assert row.viewer_review.submitted_at < row.updated_at
    assert row.approved_by == ("colleague",)


def test_pr_and_viewer_review_head_oids_are_mapped_independently():
    mine = review(ME, "APPROVED", at=at(3))
    mine["commit"] = {"oid": "reviewed-head"}
    row = classify(pr(headRefOid="current-head", reviews={"nodes": [mine]}), ME, NOW)
    assert row.head_oid == "current-head"
    assert row.viewer_review.head_oid == "reviewed-head"


def test_review_requests_carry_users_and_team_slugs():
    payload = pr(reviewRequests={"nodes": [
        {"requestedReviewer": {"login": "colleague"}},
        {"requestedReviewer": {"slug": "platform"}},
    ]})
    assert classify(payload, ME, NOW).review_requested_from == ("colleague", "platform")
