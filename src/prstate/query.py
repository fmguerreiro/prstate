"""GraphQL text, page constants and batching. Pure: builds strings, no I/O.

Separate from gh.py so the page sizes are provably reachable in the emitted query
text without a subprocess in the way.
"""

from __future__ import annotations

import json
from typing import Iterator, Sequence, TypeVar

BATCH = 10
# Named once and interpolated into the queries below. Hardcoding them twice let a
# smaller page silently stop the truncation check firing.
THREAD_PAGE = 100
COMMENT_PAGE = 50
REVIEW_PAGE = 50
ROLLUP_PAGE = 100
# Give up and mark the PR partial rather than page forever.
MAX_ROLLUP_PAGES = 10
BODY_CHARS = 1500
EXCERPT_CHARS = 200

T = TypeVar("T")

# Spliced into both the first-page and the continuation query so the two selections
# cannot drift apart.
CONTEXT_NODES = """__typename
            ... on CheckRun { name conclusion status startedAt completedAt
              checkSuite { workflowRun { databaseId workflow { name } } } }
            ... on StatusContext { context state createdAt }"""

# pushedDate stays absent: GitHub returns null for it on every commit, so selecting
# it invites trusting a field that never arrives.
PR_FRAGMENT = """
  p%(idx)d: repository(owner: %(owner)s, name: %(name)s) {
    pullRequest(number: %(number)d) {
      number title url isDraft createdAt updatedAt
      mergeable mergeStateStatus reviewDecision baseRefName headRefName headRefOid
      author { login }
      reviewRequests(first: 20) { nodes { requestedReviewer {
        ... on User { login }
        ... on Team { slug }
      } } }
      viewerLatestReview { state submittedAt commit { oid } }
      commits(last: 1) { nodes { commit {
        committedDate
        statusCheckRollup { contexts(first: %(rollup_page)d) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            %(context_nodes)s
          }
        } }
      } } }
      reviews(last: %(review_page)d) { nodes {
        id author { login } state submittedAt body isMinimized minimizedReason
        commit { oid } } }
      reviewThreads(first: %(thread_page)d) { nodes {
        id isResolved isOutdated path
        opener: comments(first: 1) { nodes {
          id author { login } createdAt body isMinimized minimizedReason } }
        recent: comments(last: 1) { totalCount nodes {
          id author { login } createdAt body isMinimized minimizedReason } }
      } }
      comments(last: %(comment_page)d) { nodes {
        id author { login } createdAt body isMinimized minimizedReason } }
    }
  }
"""

ROLLUP_FRAGMENT = """
  r%(idx)d: repository(owner: %(owner)s, name: %(name)s) {
    pullRequest(number: %(number)d) { commits(last: 1) { nodes { commit {
      statusCheckRollup { contexts(first: %(rollup_page)d, after: %(cursor)s) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          %(context_nodes)s
        }
      } }
    } } } }
  }
"""

MERGE_FRAGMENT = """
  m%(idx)d: repository(owner: %(owner)s, name: %(name)s) {
    pullRequest(number: %(number)d) { mergeable mergeStateStatus }
  }
"""


def batches(items: Sequence[T], size: int = BATCH) -> Iterator[list[T]]:
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _vars(idx: int, repo: str, number: int) -> dict:
    owner, name = repo.split("/")
    # json.dumps is the GraphQL string escaping: a repo name carrying a quote has
    # to come back quoted, not injected.
    return {
        "idx": idx,
        "owner": json.dumps(owner),
        "name": json.dumps(name),
        "number": number,
        "thread_page": THREAD_PAGE,
        "comment_page": COMMENT_PAGE,
        "review_page": REVIEW_PAGE,
        "rollup_page": ROLLUP_PAGE,
        "context_nodes": CONTEXT_NODES,
    }


def _wrap(parts: Sequence[str]) -> str:
    return "query {" + "".join(parts) + "}"


def pr_query(refs: Sequence[tuple[str, int]]) -> str:
    if not refs:
        raise ValueError("pr_query needs at least one pull request")
    return _wrap([PR_FRAGMENT % _vars(idx, repo, number)
                  for idx, (repo, number) in enumerate(refs)])


def rollup_query(cursors: Sequence[tuple[str, int, str]]) -> str:
    if not cursors:
        raise ValueError("rollup_query needs at least one cursor")
    parts = []
    for idx, (repo, number, cursor) in enumerate(cursors):
        parts.append(ROLLUP_FRAGMENT % {**_vars(idx, repo, number),
                                        "cursor": json.dumps(cursor)})
    return _wrap(parts)


def merge_query(refs: Sequence[tuple[str, int]]) -> str:
    if not refs:
        raise ValueError("merge_query needs at least one pull request")
    return _wrap([MERGE_FRAGMENT % _vars(idx, repo, number)
                  for idx, (repo, number) in enumerate(refs)])
