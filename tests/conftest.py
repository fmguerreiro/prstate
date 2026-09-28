"""Payload builders, in the exact shape the GraphQL query returns.

Builders rather than recorded JSON: the payloads are synthesized, not captured,
and a builder keeps the fields the query grew — node ids, isMinimized,
minimizedReason, pageInfo — in one place instead of in every literal.

`pushedDate` is deliberately absent everywhere: GitHub returns null for it on
every commit now, so a fixture supplying it would test a path production never
takes.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
BASE_PR = json.loads((FIXTURES / "base_pr.json").read_text())

_ids = itertools.count(1)

# Distinguishes "the caller said null" from "the caller said nothing", which is
# the whole point of the absent-totalCount rules.
AUTO = object()


def pr(**overrides) -> dict:
    payload = json.loads(json.dumps(BASE_PR))
    payload.update(overrides)
    return payload


def comment(who, at, body="x", minimized=False, reason=None, id=None,
            actor_type="User") -> dict:
    """An issue comment or a review-thread comment; `who=None` is a deleted account."""
    return {
        "id": id or f"C{next(_ids)}",
        "author": (
            None if who is None else {"__typename": actor_type, "login": who}
        ),
        "createdAt": at,
        "body": body,
        "isMinimized": minimized,
        "minimizedReason": reason,
    }


def review(who, state, body="", at="2026-01-03T00:00:00Z",
           minimized=False, reason=None, id=None, actor_type="User") -> dict:
    return {
        "id": id or f"R{next(_ids)}",
        "author": (
            None if who is None else {"__typename": actor_type, "login": who}
        ),
        "state": state,
        "submittedAt": at,
        "body": body,
        "isMinimized": minimized,
        "minimizedReason": reason,
    }


def thread(*comments, resolved=False, outdated=False, path="a.ts",
           total=AUTO, id=None) -> dict:
    """A review thread: opener and last as two separate connections.

    `total` is the server's comment count, which exceeds the two fetched ends on
    a long thread — the unread middle every partial rule turns on.
    """
    nodes = list(comments)
    return {
        "id": id or f"T{next(_ids)}",
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "opener": {"nodes": nodes[:1]},
        "recent": {
            "totalCount": len(nodes) if total is AUTO else total,
            "nodes": nodes[-1:],
        },
    }


def check(name, state, run=None, started=None, completed=None, workflow="wf") -> dict:
    return {
        "__typename": "CheckRun", "name": name, "conclusion": state,
        "status": "COMPLETED", "startedAt": started, "completedAt": completed,
        "checkSuite": {"workflowRun": {
            "databaseId": run,
            "workflow": None if workflow is None else {"name": workflow},
        }},
    }


def status(context, state, created=None) -> dict:
    """A StatusContext: an external provider's check, with no workflow provenance."""
    return {"__typename": "StatusContext", "context": context, "state": state,
            "createdAt": created}


def commits(*checks, committed="2026-01-02T00:00:00Z", total=AUTO,
            rollup=True) -> dict:
    """`commits(last: 1)` with its rollup. `rollup=False` is a PR that ran nothing."""
    commit = {"committedDate": committed, "statusCheckRollup": None}
    if rollup:
        commit["statusCheckRollup"] = {"contexts": {
            "totalCount": len(checks) if total is AUTO else total,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": list(checks),
        }}
    return {"nodes": [{"commit": commit}]}


def approved_pr(**overrides) -> dict:
    """The shape every bucket test starts from: approved, green, nothing owed."""
    base = dict(
        reviewDecision="APPROVED",
        reviews={"nodes": [review("them", "APPROVED")]},
        commits=commits(check("ci", "SUCCESS", run=1, completed="2026-01-02T01:05:00Z")),
    )
    base.update(overrides)
    return pr(**base)
