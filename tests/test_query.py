"""Behaviour of the query builder: page sizes reach the text, batches lose nothing."""

from __future__ import annotations

import re

import pytest

from prstate import query as q

# (regex capturing the page argument, name of the constant it must come from)
PAGED_CONNECTIONS = [
    (r"reviewThreads\(first: (\d+)\)", "THREAD_PAGE"),
    (r"(?<!recent: )comments\(last: (\d+)\)", "COMMENT_PAGE"),
    (r"reviews\(last: (\d+)\)", "REVIEW_PAGE"),
    (r"contexts\(first: (\d+)", "ROLLUP_PAGE"),
]


def block(text: str, marker: str) -> str:
    """The balanced-brace selection following the first match of `marker`."""
    start = re.search(marker, text)
    assert start, f"{marker} not in the query"
    open_at = text.index("{", start.end() - 1)
    depth = 0
    for pos in range(open_at, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at:pos + 1]
    raise AssertionError(f"unbalanced braces after {marker}")


@pytest.mark.parametrize("pattern,constant", PAGED_CONNECTIONS)
def test_page_sizes_come_from_the_named_constant(pattern, constant):
    text = q.pr_query([("o/n", 7)])
    assert re.findall(pattern, text) == [str(getattr(q, constant))]


@pytest.mark.parametrize("pattern,constant", PAGED_CONNECTIONS)
def test_changing_a_page_constant_changes_the_query(pattern, constant, monkeypatch):
    monkeypatch.setattr(q, constant, 3)
    text = q.pr_query([("o/n", 7)])
    assert re.findall(pattern, text) == ["3"]


def test_batches_split_without_loss_or_duplication():
    refs = [(f"o/r{n}", n) for n in range(25)]
    chunks = list(q.batches(refs))
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert [ref for chunk in chunks for ref in chunk] == refs


def test_each_batch_is_one_query_with_distinct_aliases():
    refs = [(f"o/r{n}", n) for n in range(25)]
    queries = [q.pr_query(chunk) for chunk in q.batches(refs)]
    assert len(queries) == 3
    seen = []
    for text, chunk in zip(queries, q.batches(refs)):
        aliases = re.findall(r"\b(p\d+): repository", text)
        assert len(set(aliases)) == len(aliases) == len(chunk)
        seen += re.findall(r"pullRequest\(number: (\d+)\)", text)
    assert seen == [str(n) for n in range(25)]


def test_a_single_pr_is_one_query_with_one_alias():
    chunks = list(q.batches([("o/n", 7)]))
    assert len(chunks) == 1
    text = q.pr_query(chunks[0])
    assert text.count("pullRequest(") == 1
    assert "p0: repository" in text and "p1:" not in text


SURFACES = [
    r"reviews\(last: \d+\)",
    r"reviewThreads\(first: \d+\)",
    r"(?<!recent: )comments\(last: \d+\)",
]


@pytest.mark.parametrize("surface", SURFACES)
def test_every_comment_surface_is_selected_with_its_minimize_state(surface):
    selection = block(q.pr_query([("o/n", 7)]), surface)
    assert "isMinimized" in selection
    assert "minimizedReason" in selection
    assert "id" in selection


def test_thread_comments_carry_minimize_state_on_both_ends():
    threads = block(q.pr_query([("o/n", 7)]), r"reviewThreads\(first: \d+\)")
    assert threads.count("isMinimized") == 2
    assert "opener: comments(first: 1)" in threads
    assert "recent: comments(last: 1)" in threads


def test_viewer_review_state_is_selected():
    text = q.pr_query([("o/n", 7)])
    review = block(text, r"viewerLatestReview")
    assert "state" in review and "submittedAt" in review and "oid" in review
    assert "headRefOid" in text


def test_review_requests_cover_users_and_teams():
    requests = block(q.pr_query([("o/n", 7)]), r"reviewRequests\(first: \d+\)")
    assert "... on User { login }" in requests
    assert "... on Team { slug }" in requests


def test_the_rollup_pages_and_reports_its_total():
    contexts = block(q.pr_query([("o/n", 7)]), r"contexts\(first: \d+\)")
    assert "hasNextPage" in contexts and "endCursor" in contexts
    assert "totalCount" in contexts


def test_both_queries_select_the_same_context_fields():
    def nodes(text):
        contexts = block(text, r"contexts\(first: \d+[^)]*\)")
        return " ".join(block(contexts, r"nodes\b").split())

    assert nodes(q.pr_query([("o/n", 7)])) == nodes(q.rollup_query([("o/n", 7, "c")]))


def test_rollup_query_carries_each_prs_own_cursor():
    text = q.rollup_query([("o/n", 7, "first"), ("o/m", 9, "second")])
    assert re.findall(r'after: "(\w+)"', text) == ["first", "second"]
    assert "r0: repository" in text and "r1: repository" in text


def test_merge_query_asks_only_for_the_merge_state():
    text = q.merge_query([("o/n", 7)])
    assert "m0: repository" in text
    assert "mergeable mergeStateStatus" in text
    assert "reviewThreads" not in text and "comments" not in text


@pytest.mark.parametrize("build", [q.pr_query, q.rollup_query, q.merge_query])
def test_an_empty_request_is_rejected_not_sent_as_an_empty_query(build):
    with pytest.raises(ValueError):
        build([])


@pytest.mark.parametrize("build,ref", [
    (q.pr_query, ("o/n", 7)),
    (q.rollup_query, ("o/n", 7, "c")),
    (q.merge_query, ("o/n", 7)),
])
def test_no_query_is_a_mutation_or_asks_for_pushed_date(build, ref):
    text = build([ref])
    assert "mutation" not in text
    assert text.lstrip().startswith("query {")
    assert "pushedDate" not in text


def test_a_repo_name_with_a_quote_is_escaped_not_injected():
    text = q.pr_query([('o/n"} evil {x', 7)])
    assert r'name: "n\"} evil {x"' in text
    assert 'name: "n"}' not in text
