"""gh.py, offline. Nothing here is allowed to reach the network: `subprocess.run` is
stubbed in every test that would call it, and the read-only guard parses the package
rather than running it."""

from __future__ import annotations

import ast
import json
import pathlib
import types

import pytest

from prstate import gh, query


class Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def stub_gh(monkeypatch, handler):
    """Replace the one subprocess call site and record every argv it built."""
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False):
        calls.append(list(cmd))
        reply = handler(list(cmd))
        return reply if isinstance(reply, Proc) else Proc(stdout=reply)

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    monkeypatch.setattr(gh.time, "sleep", lambda *_: None)
    return calls


def graphql_reply(data, returncode=0, stderr=""):
    return Proc(stdout=json.dumps({"data": data}), stderr=stderr, returncode=returncode)


@pytest.mark.parametrize("parts", [
    ("pr", "merge", "1"),
    ("pr", "view", "1"),
    ("repo", "delete", "o/n"),
    ("api", "graphql", "-X", "POST", "-f", "query=query {x}"),
    ("api", "repos/o/n", "-X", "DELETE"),
    ("api", "repos/o/n/merges", "--method=POST"),
    ("api", "graphql", "-f", "query=mutation { addComment(input: {}) { clientMutationId } }"),
    ("search", "prs", "--field=x"),
    ("repo", "view", "--input", "-"),
])
def test_argv_refuses_writes(parts):
    with pytest.raises(gh.GhError):
        gh.argv(*parts)


@pytest.mark.parametrize("parts", [
    ("search", "prs", "--state=open", "--limit=200"),
    ("api", "graphql", "-f", "query=query { viewer { login } }"),
    ("api", "user", "--jq", ".login"),
    ("auth", "status"),
    ("repo", "view", "--json", "owner"),
])
def test_argv_allows_reads(parts):
    assert gh.argv(*parts) == ["gh", *parts]


def test_argv_allowlist_excludes_pr():
    # D5: gh pr has no verb check here, and merge|close|edit|review|comment are writes.
    assert "pr" not in gh.READ_ONLY_SUBCOMMANDS


def test_run_raises_gherror_not_systemexit(monkeypatch):
    stub_gh(monkeypatch, lambda cmd: Proc(stderr="gh: boom", returncode=1))
    with pytest.raises(gh.GhError) as caught:
        gh.run("auth", "status")
    assert "gh: boom" in str(caught.value)


def test_run_graphql_retries_once_on_secondary_rate_limit(monkeypatch):
    replies = [Proc(stderr="You have exceeded a secondary rate limit", returncode=1),
               graphql_reply({"p0": {"pullRequest": {"number": 7}}})]
    calls = stub_gh(monkeypatch, lambda cmd: replies.pop(0))
    body, _stderr = gh.run_graphql("query { x }")
    assert len(calls) == 2
    assert body["data"]["p0"]["pullRequest"]["number"] == 7


def test_run_graphql_gives_up_after_one_retry(monkeypatch):
    calls = stub_gh(monkeypatch, lambda cmd: Proc(stderr="HTTP 503", returncode=1))
    body, stderr = gh.run_graphql("query { x }")
    assert len(calls) == 2
    assert body == {} and "503" in stderr


def test_run_graphql_keeps_a_partial_payload(monkeypatch):
    # gh exits 1 on a partial error while still printing the aliases it resolved.
    stub_gh(monkeypatch, lambda cmd: graphql_reply(
        {"p0": None, "p1": {"pullRequest": {"number": 2}}},
        returncode=1, stderr="Could not resolve to a Repository"))
    body, _stderr = gh.run_graphql("query { x }")
    assert body["data"]["p1"]["pullRequest"]["number"] == 2


def boom_lookup():
    raise AssertionError("an explicit owner must not spend a subprocess")


def test_resolve_scope_precedence_and_labels():
    assert gh.resolve_scope(repo="o/n", owner="other", all_owners=True,
                            owner_lookup=boom_lookup) == (["--repo=o/n"], "repo o/n")
    assert gh.resolve_scope(repo=None, owner="other", all_owners=True,
                            owner_lookup=boom_lookup) == ([], "every owner")
    assert gh.resolve_scope(repo=None, owner="acme", all_owners=False,
                            owner_lookup=boom_lookup) == (["--owner=acme"], "owner acme")
    assert gh.resolve_scope(repo=None, owner=None, all_owners=False,
                            owner_lookup=lambda: "cwdowner") == (["--owner=cwdowner"],
                                                                 "owner cwdowner")


def test_resolve_scope_says_owner_not_org():
    # gh reports users and organisations through the same field, so "org" would
    # assert an account type nothing looked up.
    _flags, label = gh.resolve_scope(owner="acme", owner_lookup=boom_lookup)
    assert "org" not in label


def test_current_owner_keeps_the_real_stderr(monkeypatch):
    stub_gh(monkeypatch, lambda cmd: Proc(stderr="HTTP 403: rate limit", returncode=1))
    with pytest.raises(gh.GhError) as caught:
        gh.current_owner()
    assert "rate limit" in str(caught.value)


def discovery_flags(calls):
    return [[token for token in cmd if token.startswith("--author")
             or token.startswith("--review-requested") or token.startswith("--reviewed-by")]
            for cmd in calls]


def test_discover_sends_one_search_per_filter(monkeypatch):
    calls = stub_gh(monkeypatch, lambda cmd: "[]")
    gh.discover(author="@me", reviewer="@me", reviewed_by="fmguerreiro",
                scope_flags=["--owner=acme"], limit=50)
    assert discovery_flags(calls) == [["--author=@me"], ["--review-requested=@me"],
                                      ["--reviewed-by=fmguerreiro"]]
    for cmd in calls:
        assert cmd[:4] == ["gh", "search", "prs", "--state=open"]
        assert "--limit=50" in cmd and "--owner=acme" in cmd


def test_discover_without_an_author_sends_no_author_filter(monkeypatch):
    # B3: --any-author is the author=None the library always documented.
    calls = stub_gh(monkeypatch, lambda cmd: "[]")
    gh.discover(author=None, reviewer=None, reviewed_by=None, scope_flags=[], limit=200)
    assert len(calls) == 1
    assert discovery_flags(calls) == [[]]


def test_discover_dedupes_across_filters(monkeypatch):
    row = {"repository": {"nameWithOwner": "o/n"}, "number": 7}
    stub_gh(monkeypatch, lambda cmd: json.dumps([row]))
    found = gh.discover(reviewer="@me", reviewed_by="@me", scope_flags=[], limit=10)
    assert [(r["repository"]["nameWithOwner"], r["number"]) for r in found] == [("o/n", 7)]


def test_fetch_details_marks_a_dead_batch_partial_instead_of_raising(monkeypatch):
    stub_gh(monkeypatch, lambda cmd: Proc(stderr="HTTP 502", returncode=1))
    payloads, partial = gh.fetch_details([("o/n", 7), ("o/m", 8)])
    assert payloads == {}
    assert len(partial) == 1
    assert "o/n#7" in partial[0] and "o/m#8" in partial[0]


def test_fetch_details_keeps_the_readable_aliases_of_a_partial_batch(monkeypatch):
    stub_gh(monkeypatch, lambda cmd: graphql_reply(
        {"p0": None, "p1": {"pullRequest": {"number": 8}}},
        returncode=1, stderr="Could not resolve to a Repository"))
    payloads, partial = gh.fetch_details([("o/n", 7), ("o/m", 8)])
    assert list(payloads) == ["o/m#8"]
    assert payloads["o/m#8"]["_repo"] == "o/m"
    assert partial == ["no data for o/n#7"]


def test_fetch_details_trims_every_body_surface(monkeypatch):
    long = "x" * (query.BODY_CHARS + 500)
    node = {"number": 7,
            "reviews": {"nodes": [{"body": long}]},
            "comments": {"nodes": [{"body": long}]},
            "reviewThreads": {"nodes": [{"opener": {"nodes": [{"body": long}]},
                                        "recent": {"nodes": [{"body": long}]}}]}}
    stub_gh(monkeypatch, lambda cmd: graphql_reply({"p0": {"pullRequest": node}}))
    payloads, _partial = gh.fetch_details([("o/n", 7)])
    bodies = [item["body"] for item in gh._body_nodes(payloads["o/n#7"])]
    assert len(bodies) == 4
    assert {len(body) for body in bodies} == {query.BODY_CHARS}


def rollup_node(number=1, *, nodes, has_next, cursor="CUR"):
    contexts = {"totalCount": 999, "nodes": list(nodes),
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}
    return {"_repo": "o/n", "_partial": [], "number": number,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": contexts}}}]}}


def rollup_page(nodes, has_next, cursor="NEXT"):
    node = rollup_node(nodes=nodes, has_next=has_next, cursor=cursor)
    return {"r0": {"pullRequest": {"commits": node["commits"]}}}


def test_paginate_rollups_reads_the_second_page(monkeypatch):
    node = rollup_node(nodes=[{"name": "a"}], has_next=True)
    calls = stub_gh(monkeypatch,
                    lambda cmd: graphql_reply(rollup_page([{"name": "b"}], False)))
    gh._paginate_rollups({"o/n#1": node})
    contexts = gh._rollup_contexts(node)
    assert [c["name"] for c in contexts["nodes"]] == ["a", "b"]
    assert node["_partial"] == []
    assert len(calls) == 1


def test_paginate_rollups_sends_each_prs_own_cursor(monkeypatch):
    node = rollup_node(nodes=[], has_next=True, cursor="Y3Vyc29y")
    sent: list[str] = []

    def handler(cmd):
        sent.append(cmd[-1])
        return graphql_reply(rollup_page([], False))

    stub_gh(monkeypatch, handler)
    gh._paginate_rollups({"o/n#1": node})
    assert "Y3Vyc29y" in sent[0]


def test_paginate_rollups_stops_at_max_rollup_pages(monkeypatch):
    node = rollup_node(nodes=[{"name": "a"}], has_next=True)
    calls = stub_gh(monkeypatch,
                    lambda cmd: graphql_reply(rollup_page([{"name": "more"}], True)))
    gh._paginate_rollups({"o/n#1": node})
    assert len(calls) == query.MAX_ROLLUP_PAGES
    assert node["_partial"] == ["the check rollup had more pages than prstate read"]


def test_paginate_rollups_stops_asking_after_a_failed_page(monkeypatch):
    node = rollup_node(nodes=[{"name": "a"}], has_next=True)
    calls = stub_gh(monkeypatch, lambda cmd: graphql_reply({"r0": None}))
    gh._paginate_rollups({"o/n#1": node})
    assert len(calls) == 1
    assert node["_partial"] == ["the check rollup could not be paged"]


def merge_node(number=1, mergeable="UNKNOWN"):
    return {"_repo": "o/n", "_partial": [], "number": number, "mergeable": mergeable}


def test_repoll_mergeable_takes_the_second_answer(monkeypatch):
    node = merge_node()
    stub_gh(monkeypatch, lambda cmd: graphql_reply(
        {"m0": {"pullRequest": {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}}}))
    gh.repoll_mergeable({"o/n#1": node})
    assert node["mergeable"] == "CONFLICTING"
    assert node["mergeStateStatus"] == "DIRTY"
    assert node["_partial"] == []


def test_repoll_mergeable_names_a_state_github_never_computed_once(monkeypatch):
    node = merge_node()
    calls = stub_gh(monkeypatch, lambda cmd: graphql_reply(
        {"m0": {"pullRequest": {"mergeable": "UNKNOWN"}}}))
    gh.repoll_mergeable({"o/n#1": node}, attempts=3)
    assert len(calls) == 3
    assert node["_partial"] == [gh.UNKNOWN_MERGE]


def test_repoll_mergeable_skips_prs_github_already_answered(monkeypatch):
    node = merge_node(mergeable="MERGEABLE")
    calls = stub_gh(monkeypatch, lambda cmd: graphql_reply({}))
    gh.repoll_mergeable({"o/n#1": node})
    assert calls == []


def reviewed_node(*reviews):
    nodes = [{"author": {"login": login}, "state": state, "submittedAt": at}
             for login, state, at in reviews]
    return {"reviews": {"nodes": nodes},
            "viewerLatestReview": {"state": "APPROVED",
                                   "submittedAt": "2026-01-01T00:00:00Z"}}


def test_viewer_review_is_rescanned_when_the_viewer_is_not_the_token():
    # viewerLatestReview resolves against the credential, so it reports the token
    # holder's review as the viewer's the moment the two logins differ.
    node = reviewed_node(("alice", "COMMENTED", "2026-02-01T00:00:00Z"),
                         ("bob", "CHANGES_REQUESTED", "2026-03-01T00:00:00Z"),
                         ("bob", "APPROVED", "2026-04-01T00:00:00Z"))
    gh._apply_viewer_review(node, viewer="bob", token_login="alice")
    assert node["viewerLatestReview"]["state"] == "APPROVED"
    assert node["viewerLatestReview"]["submittedAt"] == "2026-04-01T00:00:00Z"


def test_viewer_review_is_none_when_the_viewer_never_reviewed():
    node = reviewed_node(("alice", "APPROVED", "2026-02-01T00:00:00Z"))
    gh._apply_viewer_review(node, viewer="carol", token_login="alice")
    assert node["viewerLatestReview"] is None


def test_viewer_review_is_left_alone_when_viewer_is_the_token():
    node = reviewed_node(("alice", "APPROVED", "2026-02-01T00:00:00Z"))
    gh._apply_viewer_review(node, viewer="alice", token_login="alice")
    assert node["viewerLatestReview"]["submittedAt"] == "2026-01-01T00:00:00Z"


def stub_fetch(monkeypatch, aliases):
    """gh auth status, gh api user, then one GraphQL reply carrying `aliases`."""
    def handler(cmd):
        if cmd[1] == "auth":
            return Proc()
        if cmd[1:3] == ["api", "user"]:
            return Proc(stdout="alice\n")
        return graphql_reply(aliases)

    seen = []
    # gh.py binds the function itself, so the stub replaces that binding rather
    # than an attribute on the classify module.
    monkeypatch.setattr(gh, "_classify_pr",
                        lambda node, viewer, now: seen.append((node, viewer)) or node)
    return stub_gh(monkeypatch, handler), seen


def test_fetch_with_refs_skips_discovery(monkeypatch):
    # B3: --pr N names a PR discovery never ran for, and still reads it in full.
    def no_discovery(**kwargs):
        raise AssertionError("discovery ran for an explicitly named PR")

    monkeypatch.setattr(gh, "discover", no_discovery)
    _calls, seen = stub_fetch(monkeypatch, {"p0": {"pullRequest": {"number": 7}}})
    sweep = gh.fetch(repo="o/n", refs=[("o/n", 7)])
    assert sweep.scope == "repo o/n"
    assert sweep.viewer == "alice"
    assert sweep.partial == ()
    assert [node["_repo"] for node, _viewer in seen] == ["o/n"]


def test_fetch_reports_the_shortfall_as_a_floor(monkeypatch):
    monkeypatch.setattr(gh, "discover", lambda **kwargs: [])
    stub_fetch(monkeypatch, {"p0": {"pullRequest": {"number": 7}}, "p1": None})
    sweep = gh.fetch(repo="o/n", refs=[("o/n", 7), ("o/n", 8)])
    assert "1 of 2 PRs could not be read" in sweep.partial
    assert len(sweep.prs) == 1


def test_fetch_of_nothing_is_an_empty_sweep_not_an_error(monkeypatch):
    monkeypatch.setattr(gh, "discover", lambda **kwargs: [])
    stub_fetch(monkeypatch, {})
    sweep = gh.fetch(owner="acme")
    assert sweep.prs == () and sweep.partial == ()
    assert sweep.scope == "owner acme"


def imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_gh_shells_out():
    """Read-only is a unit test, not a convention: one module may shell out.

    Parsed rather than grepped, so a `subprocess` inside a docstring or a comment
    does not fail and an aliased import does not pass.
    """
    package = pathlib.Path(gh.__file__).parent
    importers: list[str] = []
    unreadable: list[str] = []
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as err:
            unreadable.append(f"{path.name}: {err}")
            continue
        if "subprocess" in imported_modules(tree):
            importers.append(path.name)
    assert not unreadable, f"a module did not parse, so it went unchecked: {unreadable}"
    assert importers == ["gh.py"]
