"""The CLI's behaviour with the network stubbed out: which kwargs each flag produces,
which PRs survive the post-filters, and what the exit codes say."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prstate import cli, gh, render
from prstate.model import (
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
    Sweep,
    ViewerReview,
)

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def make_owed() -> Owed:
    return Owed(
        surface=Surface.THREAD,
        by="ryukez",
        at=NOW,
        reason="unresolved thread, last human word is theirs",
        thread_id="PRRT_1",
        path="api.py",
        outdated=False,
        excerpt="this drops the None case",
        body="this drops the None case",
    )


def make_finding(*, superseded_by: str | None = None) -> BotFinding:
    return BotFinding(
        bot="claude[bot]",
        surface=Surface.COMMENT,
        state=BotState.BLOCKING,
        at=NOW,
        thread_id=None,
        path=None,
        resolved=False,
        outdated=False,
        minimized=False,
        minimized_reason=None,
        verdict=2,
        superseded_by=superseded_by,
        excerpt="### Blocking: 2",
        body="### Blocking: 2",
    )


def make_pr(
    number: int = 1,
    *,
    repo: str = "o/n",
    draft: bool = False,
    ci: CiState = CiState.PASS,
    owed: tuple[Owed, ...] = (),
    bot_findings: tuple[BotFinding, ...] = (),
) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/{repo}/pull/{number}",
        author="someone",
        draft=draft,
        base="main",
        updated_at=NOW,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        review_decision=None,
        viewer_review=None,
        review_requested_from=(),
        head_oid="deadbeef",
        approved_by=(),
        changes_requested_by=(),
        ci=Ci(state=ci, failed=(), pending=(), unknown=(), checks=()),
        owed=owed,
        bot_findings=bot_findings,
        partial=(),
        reasons=(),
    )


def make_sweep(*prs: PullRequest, partial: tuple[str, ...] = ()) -> Sweep:
    return Sweep(
        scope="owner o",
        viewer="fmguerreiro",
        fetched_at=NOW,
        prs=prs,
        partial=partial,
    )


class StubGh:
    """Records what the flags asked gh for, and hands back a canned sweep."""

    def __init__(self, sweep: Sweep):
        self.sweep = sweep
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> Sweep:
        self.calls.append(kwargs)
        return self.sweep

    @property
    def kwargs(self) -> dict:
        assert len(self.calls) == 1, f"expected one fetch, got {len(self.calls)}"
        return self.calls[0]


@pytest.fixture
def stub(monkeypatch):
    """gh.fetch canned, gh.discover fatal: discovery is a network call, and a flag
    that should not reach it must fail loudly rather than quietly hit GitHub."""

    def installed(sweep: Sweep) -> StubGh:
        fetch = StubGh(sweep)
        monkeypatch.setattr(gh, "fetch", fetch)
        return fetch

    def no_discovery(**kwargs):
        raise AssertionError("discovery ran")

    monkeypatch.setattr(gh, "discover", no_discovery, raising=False)
    monkeypatch.setattr(render, "render", lambda sweep, now, **kw: "REPORT")
    return installed


def run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_pr_flag_bypasses_discovery(stub, capsys):
    fetch = stub(make_sweep(make_pr(7)))

    code, out, _ = run(capsys, ["--repo", "o/n", "--pr", "7", "--json"])

    assert code == 0
    assert len(json.loads(out)["prs"]) == 1
    assert fetch.kwargs == {"refs": [("o/n", 7)], "repo": "o/n", "limit": 200}


def test_a_named_draft_is_returned_not_filtered_away(stub, capsys):
    # The draft filter keeps a sweep readable; --pr already named the one PR wanted,
    # so applying it there answers a question nobody asked.
    stub(make_sweep(make_pr(7, draft=True)))

    code, out, _ = run(capsys, ["--repo", "o/n", "--pr", "7", "--json"])

    assert code == 0
    assert [pr["number"] for pr in json.loads(out)["prs"]] == [7]


def test_all_orgs_with_any_author_is_refused(stub, capsys):
    # Neither an owner nor an author leaves gh search unconstrained, which returns
    # strangers' public repositories.
    stub(make_sweep(make_pr()))

    code, _out, err = run(capsys, ["--all-orgs", "--any-author", "--json"])

    assert code == 2
    assert "all of GitHub" in err


def test_all_orgs_with_a_reviewer_filter_is_allowed(stub, capsys):
    # The reviewer filter is the constraint, so the sweep is bounded after all.
    stub(make_sweep(make_pr()))

    code, _out, _err = run(capsys, ["--all-orgs", "--any-author",
                                    "--review-requested", "--json"])

    assert code == 0


def test_any_author_sends_no_author_filter(stub, capsys):
    stub_fetch = stub(make_sweep(make_pr()))

    run(capsys, ["--any-author", "--json"])

    assert stub_fetch.kwargs["author"] is None


def test_author_defaults_to_me_and_takes_a_login(stub, capsys):
    fetch = stub(make_sweep(make_pr()))
    run(capsys, ["--json"])
    assert fetch.kwargs["author"] == "@me"

    fetch.calls.clear()
    run(capsys, ["--author", "ryukez", "--json"])
    assert fetch.kwargs["author"] == "ryukez"


def test_reviewer_flags_become_reviewer_kwargs(stub, capsys):
    fetch = stub(make_sweep(make_pr()))

    run(capsys, ["--review-requested", "--reviewed-by", "ryukez", "--json"])

    assert fetch.kwargs["reviewer"] == "@me"
    assert fetch.kwargs["reviewed_by"] == "ryukez"
    # A reviewer sweep unioned with your own authored PRs is not a reviewer sweep.
    assert fetch.kwargs["author"] is None


def test_an_explicit_author_still_narrows_a_reviewer_sweep(stub, capsys):
    fetch = stub(make_sweep(make_pr()))

    run(capsys, ["--review-requested", "--author", "ryukez", "--json"])

    assert fetch.kwargs["author"] == "ryukez"
    assert fetch.kwargs["reviewer"] == "@me"


def test_scope_flags_reach_fetch(stub, capsys):
    fetch = stub(make_sweep(make_pr()))

    run(capsys, ["--org", "acme", "--limit", "5", "--json"])

    assert fetch.kwargs["owner"] == "acme"
    assert fetch.kwargs["all_owners"] is False
    assert fetch.kwargs["limit"] == 5


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "o/n", "--org", "x"],
        ["--repo", "o/n", "--all-orgs"],
        ["--org", "x", "--all-orgs"],
        ["--pr", "7"],
        ["--repo", "o/n", "--pr", "7", "--author", "ryukez"],
        ["--repo", "o/n", "--pr", "7", "--any-author"],
        ["--repo", "o/n", "--pr", "7", "--review-requested"],
        ["--repo", "o/n", "--pr", "7", "--reviewed-by", "ryukez"],
        ["--any-author", "--author", "ryukez"],
        # argparse expands an abbreviation, so the conflict check must see it too.
        ["--repo", "o/n", "--pr", "7", "--auth", "ryukez"],
        ["--ci", "green"],
    ],
)
def test_conflicting_flags_exit_two_with_a_message(stub, capsys, argv):
    stub(make_sweep(make_pr()))

    code, out, err = run(capsys, argv)

    assert code == 2
    assert err.strip()
    assert out == ""


def test_pr_with_post_filters_is_allowed(stub, capsys):
    fetch = stub(make_sweep(make_pr(7, owed=(make_owed(),))))

    code, out, _ = run(capsys, ["--repo", "o/n", "--pr", "7", "--owed", "--json"])

    assert code == 0
    assert fetch.kwargs["refs"] == [("o/n", 7)]
    assert len(json.loads(out)["prs"]) == 1


def test_json_carries_the_schema_version(stub, capsys):
    stub(make_sweep(make_pr()))

    code, out, _ = run(capsys, ["--json"])

    assert code == 0
    assert json.loads(out)["schema_version"] == 1


def test_default_output_is_the_report(stub, capsys):
    stub(make_sweep(make_pr()))

    code, out, _ = run(capsys, [])

    assert code == 0
    assert out.strip() == "REPORT"


def numbers(out: str) -> list[int]:
    return [pr["number"] for pr in json.loads(out)["prs"]]


def test_owed_keeps_only_prs_with_owed_items(stub, capsys):
    stub(make_sweep(make_pr(1), make_pr(2, owed=(make_owed(),))))

    _, out, _ = run(capsys, ["--owed", "--json"])

    assert numbers(out) == [2]


def test_bot_findings_keeps_only_open_findings(stub, capsys):
    stub(
        make_sweep(
            make_pr(1),
            make_pr(2, bot_findings=(make_finding(superseded_by="IC_2"),)),
            make_pr(3, bot_findings=(make_finding(),)),
        )
    )

    _, out, _ = run(capsys, ["--bot-findings", "--json"])

    assert numbers(out) == [3]


def test_ci_filter_matches_the_state(stub, capsys):
    stub(make_sweep(make_pr(1), make_pr(2, ci=CiState.FAIL)))

    _, out, _ = run(capsys, ["--ci", "fail", "--json"])

    assert numbers(out) == [2]


def test_drafts_are_excluded_until_asked_for(stub, capsys):
    sweep = make_sweep(make_pr(1), make_pr(2, draft=True))

    stub(sweep)
    _, out, _ = run(capsys, ["--json"])
    assert numbers(out) == [1]

    stub(sweep)
    _, out, _ = run(capsys, ["--include-drafts", "--json"])
    assert numbers(out) == [1, 2]


def test_full_prints_bodies(stub, capsys):
    sweep = make_sweep(make_pr(1, owed=(make_owed(),)))

    stub(sweep)
    _, out, _ = run(capsys, ["--json"])
    assert "body" not in json.loads(out)["prs"][0]["owed"][0]

    stub(sweep)
    _, out, _ = run(capsys, ["--full", "--json"])
    assert "body" in json.loads(out)["prs"][0]["owed"][0]


def test_an_empty_sweep_is_not_an_error(stub, capsys):
    stub(make_sweep())

    code, _, err = run(capsys, ["--json"])

    assert code == 0
    assert err == ""


def test_a_filter_that_keeps_nothing_is_not_an_error(stub, capsys):
    stub(make_sweep(make_pr(1)))

    code, out, _ = run(capsys, ["--owed", "--json"])

    assert code == 0
    assert numbers(out) == []


def test_reading_nothing_at_all_exits_one(stub, capsys):
    stub(make_sweep(partial=("3 of 3 PRs could not be read",)))

    code, out, err = run(capsys, ["--json"])

    assert code == 1
    assert "partial" in err
    assert out == ""


def test_a_gh_failure_is_one_terminal_safe_stderr_line(stub, capsys, monkeypatch):
    stub(make_sweep())

    def boom(**kwargs):
        raise gh.GhError("gh failed\nINJECTED\x1b[31mRED\u202e")

    monkeypatch.setattr(gh, "fetch", boom)

    code, out, err = run(capsys, ["--json"])

    assert code == 1
    assert err == "prstate: gh failedINJECTED[31mRED\n"
    assert out == ""


def test_the_parser_owns_scope_exclusivity(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.build_parser().parse_args(["--repo", "o/n", "--all-orgs"])

    assert exit_.value.code == 2


def test_json_public_contract_is_complete_and_round_trips():
    populated = replace(
        make_pr(1, owed=(make_owed(),), bot_findings=(make_finding(),)),
        review_decision="REVIEW_REQUIRED",
        viewer_review=ViewerReview(
            state="APPROVED",
            submitted_at=NOW,
            head_oid="reviewedbeef",
        ),
        review_requested_from=("alice",),
        approved_by=("bob",),
        changes_requested_by=("carol",),
        ci=Ci(
            state=CiState.FAIL,
            failed=("unit",),
            pending=("lint",),
            unknown=("deploy",),
            checks=(
                Check(
                    name="unit",
                    state="FAILURE",
                    workflow="ci",
                    run_id=42,
                    at=NOW,
                ),
            ),
        ),
        partial=("comments unavailable",),
        reasons=(Reason(kind=ReasonKind.BLOCKED, detail="review required"),),
    )
    payload = make_sweep(
        populated,
        make_pr(2),
        partial=("one repository unavailable",),
    ).to_dict(full=True)

    assert set(payload) == {
        "schema_version",
        "fetched_at",
        "scope",
        "viewer",
        "partial",
        "prs",
    }
    pr_keys = {
        "key",
        "repo",
        "number",
        "title",
        "url",
        "author",
        "draft",
        "base",
        "updated_at",
        "mergeable",
        "merge_state",
        "review_decision",
        "viewer_review",
        "review_requested_from",
        "head_oid",
        "approved_by",
        "changes_requested_by",
        "ci",
        "owed",
        "bot_findings",
        "partial",
        "reasons",
    }
    full, minimal = payload["prs"]
    assert set(full) == pr_keys
    assert set(minimal) == pr_keys
    assert set(full["ci"]) == {"state", "failed", "pending", "unknown", "checks"}
    assert set(minimal["ci"]) == {"state", "failed", "pending", "unknown", "checks"}
    assert set(full["ci"]["checks"][0]) == {
        "name",
        "state",
        "workflow",
        "run_id",
        "at",
    }
    assert set(full["viewer_review"]) == {"state", "submitted_at", "head_oid"}
    assert set(full["owed"][0]) == {
        "surface",
        "by",
        "at",
        "reason",
        "thread_id",
        "path",
        "outdated",
        "excerpt",
        "body",
    }
    assert set(full["bot_findings"][0]) == {
        "bot",
        "surface",
        "state",
        "at",
        "thread_id",
        "path",
        "resolved",
        "outdated",
        "minimized",
        "minimized_reason",
        "verdict",
        "superseded_by",
        "excerpt",
        "body",
        "open",
    }
    assert set(full["reasons"][0]) == {"kind", "detail"}

    assert {state.value for state in CiState} == {
        "pass",
        "fail",
        "pending",
        "unknown",
        "none",
    }
    assert {surface.value for surface in Surface} == {"thread", "review", "comment"}
    assert {state.value for state in BotState} == {
        "open_thread",
        "blocking",
        "stale",
        "stale_clean",
        "unknown",
    }
    assert {kind.value for kind in ReasonKind} == {
        "blocked",
        "reply",
        "botfix",
        "stale_verdict",
        "merge",
        "not_ready",
        "conflict",
        "ci",
        "partial",
        "running",
    }
    assert full["ci"]["state"] == "fail"
    assert full["owed"][0]["surface"] == "thread"
    assert full["bot_findings"][0]["state"] == "blocking"
    assert full["reasons"][0]["kind"] == "blocked"

    timestamp = "2026-09-25T08:00:00Z"
    assert payload["fetched_at"] == timestamp
    assert full["updated_at"] == timestamp
    assert full["viewer_review"]["submitted_at"] == timestamp
    assert full["ci"]["checks"][0]["at"] == timestamp
    assert full["owed"][0]["at"] == timestamp
    assert full["bot_findings"][0]["at"] == timestamp
    assert json.loads(json.dumps(payload)) == payload


def test_wheel_installs_package_and_documented_root_exports(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(dist),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))


    subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--offline",
            "--no-project",
            "--with",
            str(wheel),
            "python",
            "-I",
            "-c",
            """
import importlib
from prstate import (
    BotFinding,
    BotState,
    Check,
    Ci,
    CiState,
    Owed,
    PullRequest,
    ReasonKind,
    Surface,
    Sweep,
    ViewerReview,
    classify,
    fetch,
)

for module in ("classify", "cli", "gh", "model", "query", "render"):
    importlib.import_module(f"prstate.{module}")
""",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
