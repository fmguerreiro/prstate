"""Shared data contract with stable JSON field order."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum

SCHEMA_VERSION = 1
DELETED = "(deleted account)"
# Minimizing as spam or off-topic hides a comment; it does not mean a newer one
# replaced it. Only these two reasons are supersession.
SUPERSEDING_MINIMIZE_REASONS = frozenset({"OUTDATED", "RESOLVED"})


def iso(value: datetime | None) -> str | None:
    """UTC ISO-8601 with a Z suffix, matching what GitHub returns."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CiState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    UNKNOWN = "unknown"
    NONE = "none"


class Surface(StrEnum):
    THREAD = "thread"
    REVIEW = "review"
    COMMENT = "comment"


class BotState(StrEnum):
    OPEN_THREAD = "open_thread"
    BLOCKING = "blocking"
    STALE = "stale"
    STALE_CLEAN = "stale_clean"
    UNKNOWN = "unknown"


class ReasonKind(StrEnum):
    BLOCKED = "blocked"
    REPLY = "reply"
    BOTFIX = "botfix"
    STALE_VERDICT = "stale_verdict"
    MERGE = "merge"
    NOT_READY = "not_ready"
    CONFLICT = "conflict"
    CI = "ci"
    PARTIAL = "partial"
    RUNNING = "running"


def _value(item, *, full: bool):
    if isinstance(item, datetime):
        return iso(item)
    if isinstance(item, StrEnum):
        return item.value
    if isinstance(item, tuple):
        return [_value(x, full=full) for x in item]
    if hasattr(item, "to_dict"):
        return item.to_dict(full=full)
    return item


def _as_dict(record, *, full: bool, skip: tuple[str, ...] = ()) -> dict:
    return {
        f.name: _value(getattr(record, f.name), full=full)
        for f in fields(record)
        if f.name not in skip
    }


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    state: str | None
    workflow: str | None
    run_id: int | None
    at: datetime | None

    def to_dict(self, *, full: bool = False) -> dict:
        return _as_dict(self, full=full)


@dataclass(frozen=True, slots=True)
class Ci:
    state: CiState
    failed: tuple[str, ...]
    pending: tuple[str, ...]
    unknown: tuple[str, ...]
    checks: tuple[Check, ...]

    def to_dict(self, *, full: bool = False) -> dict:
        return _as_dict(self, full=full)


@dataclass(frozen=True, slots=True)
class Owed:
    surface: Surface
    by: str
    at: datetime
    reason: str
    thread_id: str | None
    path: str | None
    outdated: bool
    excerpt: str
    body: str

    def to_dict(self, *, full: bool = False) -> dict:
        return _as_dict(self, full=full, skip=() if full else ("body",))


@dataclass(frozen=True, slots=True)
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
    minimized_reason: str | None
    verdict: int | None
    superseded_by: str | None
    excerpt: str
    body: str

    @property
    def open(self) -> bool:
        """Still live: no newer summary replaced it, GitHub did not retire it."""
        return not self.superseded_by and not (
            self.minimized and self.minimized_reason in SUPERSEDING_MINIMIZE_REASONS
        )

    def to_dict(self, *, full: bool = False) -> dict:
        out = _as_dict(self, full=full, skip=() if full else ("body",))
        # After superseded_by, which is what it is derived from.
        body = out.pop("body", None)
        excerpt = out.pop("excerpt")
        out["excerpt"] = excerpt
        if body is not None:
            out["body"] = body
        out["open"] = self.open
        return out


@dataclass(frozen=True, slots=True)
class ViewerReview:
    """The viewer's own latest review. Absent from approved_by/changes_requested_by,
    which list everyone except the viewer, so it has nowhere else to live."""

    state: str                   # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    submitted_at: datetime
    head_oid: str | None         # the commit reviewed, when GitHub reports it

    def to_dict(self, *, full: bool = False) -> dict:
        return _as_dict(self, full=full)


@dataclass(frozen=True, slots=True)
class Reason:
    kind: ReasonKind
    detail: str

    def to_dict(self, *, full: bool = False) -> dict:
        return _as_dict(self, full=full)


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    draft: bool
    base: str
    updated_at: datetime
    mergeable: str | None
    merge_state: str | None
    review_decision: str | None
    viewer_review: ViewerReview | None
    review_requested_from: tuple[str, ...]
    head_oid: str | None
    approved_by: tuple[str, ...]
    changes_requested_by: tuple[str, ...]
    ci: Ci
    owed: tuple[Owed, ...]
    bot_findings: tuple[BotFinding, ...]
    partial: tuple[str, ...]
    reasons: tuple[Reason, ...]

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    def to_dict(self, *, full: bool = False) -> dict:
        return {"key": self.key, **_as_dict(self, full=full)}


@dataclass(frozen=True, slots=True)
class Sweep:
    scope: str
    viewer: str
    fetched_at: datetime
    prs: tuple[PullRequest, ...]
    partial: tuple[str, ...]

    def to_dict(self, *, full: bool = False) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "fetched_at": iso(self.fetched_at),
            "scope": self.scope,
            "viewer": self.viewer,
            "partial": list(self.partial),
            "prs": [pr.to_dict(full=full) for pr in self.prs],
        }
