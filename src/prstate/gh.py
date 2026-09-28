"""The only module that shells out. Every network read in prstate lands here.

Read-only is enforced, not promised: `argv` is the one place a gh command line is
built and it allowlists what may be run. `tests/test_gh.py` walks the package AST to
prove no other module imports `subprocess`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time

# The function, not the module: the package re-exports `classify` as a function,
# so `from prstate import classify` (or `import prstate.classify as x`) binds that
# function here instead of the module. `from prstate.classify import ...` resolves
# through sys.modules and gets the real submodule.
from prstate.classify import classify as _classify_pr
from prstate import query
from prstate.model import Sweep

READ_ONLY_SUBCOMMANDS = {"search", "api", "auth", "repo"}

READ_ONLY_ENDPOINTS = {"graphql", "user"}
READ_ONLY_REPO_VERBS = {"view"}
QUERY_FLAGS = {"-f", "--raw-field"}
WRITE_FLAGS = {"-X", "--method", "-F", "--field", "--input"} | QUERY_FLAGS
MUTATION_RE = re.compile(r"\bmutation\b")

# gh reports a secondary rate limit and a bad gateway on stderr with the request
# already spent, so the retry decision can only be read out of the text. The primary
# hourly limit is deliberately absent: it resets in minutes, not in a backoff, so
# retrying it burns a second request and still reports nothing.
RETRY_RE = re.compile(r"secondary rate limit|submitted too quickly|abuse detection"
                      r"|HTTP 50[23]", re.IGNORECASE)
RETRY_WAIT = 4.0

DISCOVERY_FIELDS = "url,repository,number,title,isDraft,createdAt,updatedAt"
UNKNOWN_MERGE = "GitHub never computed the merge state"


class GhError(RuntimeError):
    """A gh invocation that failed, or one that was refused before it ran."""


def _flag_name(token: str) -> str:
    """The flag a token names, ignoring any attached value.

    `--field=x`, `-Fname=x`, and their split forms name the same write flags.
    """
    name = token.split("=", 1)[0]
    return name[:2] if name.startswith("-") and not name.startswith("--") else name


def _reject_flags(forbidden: set[str], parts: tuple[str, ...]) -> None:
    for token in parts:
        if _flag_name(token) in forbidden:
            raise GhError(f"gh call carries the write flag {_flag_name(token)}: {list(parts)}")


def argv(*parts: str) -> list[str]:
    """The one place a gh command line is built. Raises GhError on anything else.

    A denylist of `gh pr merge` and friends is unbounded and the wrong shape: nearly
    every call here is `gh api graphql`, so a write arrives as a mutation in the query
    string or as a write method on a REST path, neither of which a subcommand denylist
    sees. `gh pr` is off the allowlist entirely (D5) — prstate never runs it, and
    `merge|close|edit|review|comment` are all writes with no verb check to catch them.
    """
    if not parts:
        raise GhError("an empty gh call cannot be checked")
    for token in parts:
        if MUTATION_RE.search(token):
            raise GhError(f"a GraphQL mutation is present: {token[:60]}")
    if parts[0] not in READ_ONLY_SUBCOMMANDS:
        raise GhError(f"gh call is not on the read-only allowlist: {list(parts)}")
    if parts[0] == "api":
        endpoint = parts[1] if len(parts) > 1 else ""
        if endpoint not in READ_ONLY_ENDPOINTS:
            raise GhError(f"gh api endpoint is not on the read-only allowlist: {list(parts)}")
        # -f/--raw-field carry the query on graphql and are write fields anywhere
        # else, so the forbidden set is named per endpoint. Named forbidden, not
        # allowed: this is the set that must be absent, and a reader who inverts
        # that ships a write.
        _reject_flags(WRITE_FLAGS - QUERY_FLAGS if endpoint == "graphql" else WRITE_FLAGS, parts)
    elif parts[0] == "auth":
        if parts != ("auth", "status"):
            raise GhError(f"gh auth call is not on the read-only allowlist: {list(parts)}")
    elif parts[0] == "repo":
        verb = parts[1] if len(parts) > 1 else ""
        if verb not in READ_ONLY_REPO_VERBS:
            raise GhError(f"gh repo verb is not on the read-only allowlist: {list(parts)}")
        _reject_flags(WRITE_FLAGS, parts)
    else:
        _reject_flags(WRITE_FLAGS, parts)
    return ["gh", *parts]


def _parse(stdout: str) -> dict:
    try:
        return json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


def run(*parts: str) -> str:
    """stdout of a checked gh call. Raises GhError, never SystemExit: this is a
    library, and a caller holding a Sweep should not be exited out from under."""
    cmd = argv(*parts)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(f"command failed: {' '.join(cmd[:3])}...\n{result.stderr.strip()}")
    return result.stdout


def run_graphql(query_text: str) -> tuple[dict, str]:
    """(parsed body, stderr). A nonzero exit does NOT mean an empty response.

    gh exits 1 on a PARTIAL error (one archived repo, one revoked SAML grant) while
    still printing every alias it did resolve, so discarding the batch threw away up
    to BATCH-1 readable PRs for a reason already answered in the payload. Parse stdout
    regardless, and let the per-alias null be what counts as unread.
    """
    cmd = argv("api", "graphql", "-f", f"query={query_text}")
    body: dict = {}
    stderr = ""
    for attempt in range(2):
        result = subprocess.run(cmd, capture_output=True, text=True)
        stderr = result.stderr or ""
        body = _parse(result.stdout)
        if result.returncode == 0 or body.get("data"):
            return body, stderr
        # One retry, then give up and let the caller mark those PRs partial rather
        # than half-report a sweep whose gaps nobody named.
        if attempt == 0 and RETRY_RE.search(stderr):
            time.sleep(RETRY_WAIT)
            continue
        break
    return body, stderr


def viewer_login() -> str:
    return run("api", "user", "--jq", ".login").strip()


def current_owner() -> str:
    cmd = argv("repo", "view", "--json", "owner")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # A rate limit or a network blip fails here too, so the real stderr rides
        # along: the hint alone would send the reader hunting a missing repo.
        raise GhError("could not read an owner from the working directory, so there is "
                      "nothing to scope to; pass --org <owner>, --repo <owner/name>, or "
                      f"--all-orgs\n{result.stderr.strip()}")
    return json.loads(result.stdout)["owner"]["login"]


def resolve_scope(*, repo: str | None = None, owner: str | None = None,
                  all_owners: bool = False, owner_lookup=current_owner) -> tuple[list[str], str]:
    """The search flags that narrow discovery, and the label naming what they swept.

    The label says "owner", not "org": gh reports a user and an organisation through
    the same field, so calling every owner an org asserts an account type nothing here
    looked up. Conflicting flags never reach this function; argparse rejects them.
    """
    if repo:
        return [f"--repo={repo}"], f"repo {repo}"
    if all_owners:
        return [], "every owner"
    # An explicit owner must not spend a subprocess resolving what it already names.
    resolved = owner or owner_lookup()
    return [f"--owner={resolved}"], f"owner {resolved}"


def discover(*, author: str | None = None, reviewer: str | None = None,
             reviewed_by: str | None = None, scope_flags: list[str] | None = None,
             limit: int = 200) -> list[dict]:
    """Open PRs matching each filter, unioned and deduped by (repo, number).

    One search per filter, because gh ANDs them into one query otherwise and
    babysit-reviews wants --review-requested OR --reviewed-by in a single sweep.
    `gh search prs --json` lacks mergeStateStatus/reviewDecision (cli/cli#13239), so
    this yields identity only and the detail comes from GraphQL.
    """
    filters = [flag for flag in (f"--author={author}" if author else None,
                                 f"--review-requested={reviewer}" if reviewer else None,
                                 f"--reviewed-by={reviewed_by}" if reviewed_by else None)
               if flag]
    found: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for one in filters or [None]:
        parts = ["search", "prs", "--state=open", f"--limit={limit}",
                 f"--json={DISCOVERY_FIELDS}", *(scope_flags or [])]
        if one:
            parts.append(one)
        for row in _parse_rows(run(*parts)):
            key = ((row.get("repository") or {}).get("nameWithOwner"), row.get("number"))
            if not key[0] or key[1] is None or key in seen:
                continue
            seen.add(key)
            found.append(row)
    return found


def _parse_rows(stdout: str) -> list[dict]:
    rows = json.loads(stdout) if stdout.strip() else []
    return rows if isinstance(rows, list) else []


def _body_nodes(node: dict):
    for key in ("reviews", "comments"):
        yield from (node.get(key) or {}).get("nodes") or []
    for thread in (node.get("reviewThreads") or {}).get("nodes") or []:
        for side in ("opener", "activity", "recent"):
            yield from (thread.get(side) or {}).get("nodes") or []


def _trim_nodes(items) -> None:
    for item in items:
        body = item.get("body")
        if isinstance(body, str) and len(body) > query.BODY_CHARS:
            item["body"] = body[:query.BODY_CHARS]


def _trim_bodies(node: dict) -> None:
    """Cut every fetched body to BODY_CHARS in this one place, right after parsing,
    so no rule downstream has to remember which surface it came from."""
    _trim_nodes(_body_nodes(node))


def fetch_details(refs: list[tuple[str, int]]) -> tuple[dict[str, dict], list[str]]:
    """Raw GraphQL PR payloads keyed "owner/name#number", plus sweep-level partials.

    Callable on a single ref, which is how --pr N reaches a PR discovery never saw.
    """
    payloads: dict[str, dict] = {}
    partial: list[str] = []
    if not refs:
        return payloads, partial
    for chunk in query.batches(list(refs)):
        body, stderr = run_graphql(query.pr_query(chunk))
        data = body.get("data") or {}
        if not data:
            named = ", ".join(f"{repo}#{number}" for repo, number in chunk)
            detail = stderr.strip()[:200] or _first_error(body)
            partial.append(f"a batch of {len(chunk)} PRs could not be read ({named})"
                           + (f": {detail}" if detail else ""))
            continue
        for idx, (repo, number) in enumerate(chunk):
            node = (data.get(f"p{idx}") or {}).get("pullRequest")
            if not node:
                partial.append(f"no data for {repo}#{number}")
                continue
            node["_repo"] = repo
            node["_partial"] = []
            node["_thread_activity_complete"] = True
            _trim_bodies(node)
            payloads[f"{repo}#{node.get('number', number)}"] = node
    return payloads, partial


def _first_error(body: dict) -> str:
    errors = body.get("errors") or []
    return str((errors[0] or {}).get("message", ""))[:200] if errors else ""


def _rollup_contexts(node: dict | None) -> dict | None:
    if not node:
        return None
    commits = (node.get("commits") or {}).get("nodes") or []
    if not commits:
        return None
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") or {}
    return rollup.get("contexts")


def _has_next(contexts: dict | None) -> bool:
    return bool(contexts and (contexts.get("pageInfo") or {}).get("hasNextPage"))


def _stop_paging(contexts: dict | None) -> None:
    if contexts is not None:
        contexts["pageInfo"] = dict(contexts.get("pageInfo") or {}, hasNextPage=False)


def _paginate_rollups(payloads: dict[str, dict]) -> None:
    """Follow the rollup's own cursor instead of declaring >100 contexts unreadable.

    The review threads' comments page the same way in _paginate_thread_activity. The
    remaining connections still declare partial rather than paginate: a PR with more
    than 100 review threads is pathological and the flag is honest. A rollup with
    more than 100 contexts is a normal monorepo.
    """
    for _ in range(query.MAX_ROLLUP_PAGES):
        pending = [node for node in payloads.values()
                   if _has_next(_rollup_contexts(node))]
        if not pending:
            return
        for chunk in query.batches(pending):
            cursors = [(node["_repo"], node["number"],
                        _rollup_contexts(node)["pageInfo"]["endCursor"]) for node in chunk]
            body, _stderr = run_graphql(query.rollup_query(cursors))
            data = body.get("data") or {}
            for idx, node in enumerate(chunk):
                more = _rollup_contexts((data.get(f"r{idx}") or {}).get("pullRequest"))
                contexts = _rollup_contexts(node)
                if not more:
                    node["_partial"].append("the check rollup could not be paged")
                    _stop_paging(contexts)
                    continue
                contexts["nodes"] = (contexts.get("nodes") or []) + (more.get("nodes") or [])
                contexts["pageInfo"] = more.get("pageInfo") or {"hasNextPage": False}
    for node in payloads.values():
        if _has_next(_rollup_contexts(node)):
            node["_partial"].append("the check rollup had more pages than prstate read")


def _threads(node: dict) -> list[dict]:
    return (node.get("reviewThreads") or {}).get("nodes") or []


def _comment_key(comment: dict):
    return comment.get("id") or ((comment.get("author") or {}).get("login"),
                                 comment.get("createdAt"))


def _known_comments(thread: dict) -> list[dict]:
    """The thread's comments already in hand, deduplicated: on a short thread the
    opener and the newest comment are the same one."""
    seen: set = set()
    out = []
    for side in ("opener", "activity", "recent"):
        for comment in (thread.get(side) or {}).get("nodes") or []:
            key = _comment_key(comment)
            if key in seen:
                continue
            seen.add(key)
            out.append(comment)
    return out


def _wants_page(thread: dict) -> bool:
    activity = thread.get("activity")
    if activity is not None:
        return bool((activity.get("pageInfo") or {}).get("hasNextPage"))
    total = (thread.get("recent") or {}).get("totalCount")
    # An absent count is not a zero, and it is not a gap either: classify already
    # reports a thread that refused to count itself.
    return isinstance(total, int) and total > len(_known_comments(thread))


def _stop_thread(thread: dict) -> None:
    activity = thread.setdefault("activity", {"nodes": []})
    activity["pageInfo"] = dict(activity.get("pageInfo") or {}, hasNextPage=False)


def _thread_gap(node: dict, thread: dict, what: str) -> None:
    node["_thread_activity_complete"] = False
    reason = f"the comments of a thread on {thread.get('path')} {what}"
    if reason not in node["_partial"]:
        node["_partial"].append(reason)


def _read_thread_page(chunk: list[tuple[dict, dict]]) -> None:
    body, _stderr = run_graphql(query.thread_query(
        [(thread["id"], ((thread.get("activity") or {}).get("pageInfo") or {}).get("endCursor"))
         for _node, thread in chunk]))
    data = body.get("data") or {}
    for idx, (node, thread) in enumerate(chunk):
        more = ((data.get(f"t{idx}") or {}).get("comments")) or {}
        page = more.get("pageInfo") or {}
        nodes = more.get("nodes")
        # A missing successor cursor stops pagination; retrying would repeat the same page.
        if not isinstance(nodes, list) or (page.get("hasNextPage") and not page.get("endCursor")):
            _stop_thread(thread)
            _thread_gap(node, thread, "could not be read")
            continue
        activity = thread.setdefault("activity", {"nodes": []})
        _trim_nodes(nodes)
        activity["nodes"] = (activity.get("nodes") or []) + nodes
        activity["pageInfo"] = page or {"hasNextPage": False}


def _paginate_thread_activity(payloads: dict[str, dict]) -> None:
    """Viewer replies between thread endpoints change whether human signals are owed."""
    for node in payloads.values():
        node.setdefault("_thread_activity_complete", True)
    for _ in range(query.MAX_THREAD_PAGES):
        pending = []
        for node in payloads.values():
            for thread in _threads(node):
                if not _wants_page(thread):
                    continue
                if not thread.get("id"):
                    _stop_thread(thread)
                    _thread_gap(node, thread, "have no thread id to page from")
                    continue
                pending.append((node, thread))
        if not pending:
            return
        for chunk in query.batches(pending):
            _read_thread_page(chunk)
    for node in payloads.values():
        for thread in _threads(node):
            if _wants_page(thread):
                node["_thread_activity_complete"] = False
                _thread_gap(node, thread, "ran to more pages than prstate read")


def repoll_mergeable(payloads: dict[str, dict], attempts: int = 3, wait: float = 4.0) -> None:
    """Ask again for the PRs whose merge state GitHub had not computed yet.

    `mergeable` is computed lazily: the first read returns UNKNOWN and schedules the
    background job. Without a second look a fresh PR is permanently UNKNOWN, real
    conflicts go unreported, and most of a daily sweep lands in partial (D6).
    """
    for _ in range(attempts):
        pending = [node for node in payloads.values() if node.get("mergeable") == "UNKNOWN"]
        if not pending:
            break
        time.sleep(wait)
        for chunk in query.batches(pending):
            body, _stderr = run_graphql(
                query.merge_query([(node["_repo"], node["number"]) for node in chunk]))
            data = body.get("data") or {}
            for idx, node in enumerate(chunk):
                fresh = (data.get(f"m{idx}") or {}).get("pullRequest")
                if fresh and fresh.get("mergeable") != "UNKNOWN":
                    node["mergeable"] = fresh["mergeable"]
                    node["mergeStateStatus"] = fresh.get("mergeStateStatus")
    for node in payloads.values():
        # classify.partial_reasons says the same thing from mergeable == UNKNOWN, so
        # this only records that the repoll was tried and still came back unknown.
        if node.get("mergeable") == "UNKNOWN" and UNKNOWN_MERGE not in node["_partial"]:
            node["_partial"].append(UNKNOWN_MERGE)


def _apply_viewer_review(node: dict, viewer: str, token_login: str | None) -> None:
    """Make `viewerLatestReview` mean the viewer's review, not the token holder's.

    GitHub resolves `viewerLatestReview` against the credential, while prstate's
    `viewer` is a free-form argument. They answer the same question only while they
    name the same login; the moment they differ the field reports a stranger's review
    as yours, so rescan the reviews connection instead.
    """
    if token_login is not None and viewer == token_login:
        return
    mine = [review for review in (node.get("reviews") or {}).get("nodes") or []
            if ((review.get("author") or {}).get("login")) == viewer and review.get("submittedAt")]
    node["viewerLatestReview"] = max(mine, key=lambda r: r["submittedAt"]) if mine else None


def fetch(*, author: str | None = "@me", reviewer: str | None = None,
          reviewed_by: str | None = None, owner: str | None = None,
          repo: str | None = None, all_owners: bool = False,
          viewer: str | None = None, limit: int = 200,
          refs: list[tuple[str, int]] | None = None) -> Sweep:
    """One normalized read of every open PR matching the filters.

    `refs` names PRs outright and skips discovery, so a single PR gets the same read
    quality as a sweep rather than a second, thinner code path.
    """
    run("auth", "status")
    token_login = viewer_login()
    viewer = viewer or token_login
    scope_flags, scope = resolve_scope(repo=repo, owner=owner, all_owners=all_owners)
    if refs is None:
        found = discover(author=author, reviewer=reviewer, reviewed_by=reviewed_by,
                         scope_flags=scope_flags, limit=limit)
        refs = [(row["repository"]["nameWithOwner"], row["number"]) for row in found]
    payloads, partial = fetch_details(refs)
    _paginate_rollups(payloads)
    _paginate_thread_activity(payloads)
    repoll_mergeable(payloads)
    now = dt.datetime.now(dt.timezone.utc)
    prs = []
    for key in sorted(payloads):
        node = payloads[key]
        _apply_viewer_review(node, viewer, token_login)
        prs.append(_classify_pr(node, viewer, now))
    missing = len(refs) - len(payloads)
    if missing > 0:
        # A shortfall belongs in the report, not in a warning the reader sees after
        # the confident count has already landed.
        partial.append(f"{missing} of {len(refs)} PRs could not be read")
    return Sweep(scope=scope, viewer=viewer, fetched_at=now, prs=tuple(prs),
                 partial=tuple(partial))
