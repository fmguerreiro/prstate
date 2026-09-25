"""The command line: flags in, a sweep out, nothing written back to GitHub.

Skills shell out to this rather than import the library (DESIGN §8), so anything the
library can express the flags must be able to produce.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from prstate import gh, render
from prstate.model import CiState, PullRequest, Sweep

DEFAULT_AUTHOR = "@me"


def build_parser() -> argparse.ArgumentParser:
    """Built here rather than inline in main() so the tests drive the real flags."""
    parser = argparse.ArgumentParser(
        prog="prstate", description="Normalized GitHub pull-request review state."
    )

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--repo", help="restrict to one owner/name")
    scope.add_argument(
        "--org",
        help="restrict to one owner, user or organisation "
        "(default: the working directory's repo owner)",
    )
    scope.add_argument(
        "--all-orgs",
        action="store_true",
        help="every owner you have PRs under, not just the current one",
    )

    parser.add_argument("--author", help="PR author login (default: @me)")
    parser.add_argument(
        "--any-author",
        action="store_true",
        help="every open PR regardless of who wrote it",
    )
    parser.add_argument(
        "--review-requested",
        action="store_true",
        help="PRs where your review is requested",
    )
    parser.add_argument("--reviewed-by", help="PRs this login has already reviewed")
    parser.add_argument(
        "--pr",
        type=int,
        metavar="N",
        help="one named PR, skipping discovery; requires --repo",
    )

    parser.add_argument("--owed", action="store_true", help="only PRs you owe a reply")
    parser.add_argument(
        "--bot-findings", action="store_true", help="only PRs with an open bot finding"
    )
    parser.add_argument("--ci", choices=[state.value for state in CiState])
    parser.add_argument(
        "--include-drafts", action="store_true", help="drafts are excluded by default"
    )

    parser.add_argument("--json", action="store_true", help="emit the raw contract")
    parser.add_argument(
        "--full", action="store_true", help="whole bodies instead of excerpts"
    )
    parser.add_argument("--limit", type=int, default=200)
    return parser


def _reject_conflicts(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The exclusions argparse cannot state. Read off the parsed values rather than
    argv so an abbreviated flag is caught too: argparse expands --auth to --author."""
    named = [
        flag
        for flag, given in (
            ("--author", args.author is not None),
            ("--any-author", args.any_author),
            ("--review-requested", args.review_requested),
            ("--reviewed-by", args.reviewed_by is not None),
        )
        if given
    ]
    if args.pr is None:
        if "--author" in named and "--any-author" in named:
            parser.error("--any-author and --author contradict each other")
        return
    if not args.repo:
        parser.error("--pr needs --repo to say which repository the number is in")
    if named:
        # --pr names its PR outright, so anything that would search for a different
        # set of PRs contradicts it rather than narrowing it.
        parser.error(f"--pr names one PR; {', '.join(named)} searches for others")


def _keep(pr: PullRequest, args: argparse.Namespace) -> bool:
    if pr.draft and not args.include_drafts:
        return False
    if args.owed and not pr.owed:
        return False
    if args.bot_findings and not any(f.open for f in pr.bot_findings):
        return False
    if args.ci and pr.ci.state != args.ci:
        return False
    return True


def _filter(sweep: Sweep, args: argparse.Namespace) -> Sweep:
    """Post-filters narrow the classified sweep, never discovery: a PR is dropped
    for what the read said about it, which discovery cannot know."""
    kept = tuple(pr for pr in sweep.prs if _keep(pr, args))
    if len(kept) == len(sweep.prs):
        return sweep
    return dataclasses.replace(sweep, prs=kept)


def _author(args: argparse.Namespace) -> str | None:
    """@me is the default only when nothing else says which PRs are wanted: a
    reviewer sweep unioned with your own authored PRs is not a reviewer sweep."""
    if args.any_author:
        return None
    if args.author is not None:
        return args.author
    if args.review_requested or args.reviewed_by:
        return None
    return DEFAULT_AUTHOR


def _sweep(args: argparse.Namespace) -> Sweep:
    if args.pr is not None:
        # repo goes along with refs so the scope label names it; without it
        # resolve_scope spends a subprocess on the working directory's owner.
        return gh.fetch(refs=[(args.repo, args.pr)], repo=args.repo, limit=args.limit)
    return gh.fetch(
        author=_author(args),
        reviewer="@me" if args.review_requested else None,
        reviewed_by=args.reviewed_by,
        owner=args.org,
        repo=args.repo,
        all_owners=args.all_orgs,
        limit=args.limit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _reject_conflicts(parser, args)
    except SystemExit as bad_flags:  # argparse's own exit, --help included
        return int(bad_flags.code or 0)

    try:
        sweep = _sweep(args)
    except gh.GhError as err:
        print(f"prstate: {err}", file=sys.stderr)
        return 1

    # A sweep that found PRs and read none of them is not a thin result, it is no
    # result; reporting it as an empty sweep reads as "nothing needs you".
    if not sweep.prs and sweep.partial:
        print("prstate: no PR could be read; not reporting a partial result",
              file=sys.stderr)
        return 1

    sweep = _filter(sweep, args)
    if args.json:
        json.dump(sweep.to_dict(full=args.full), sys.stdout, indent=1)
        print()
    else:
        print(render.render(sweep, sweep.fetched_at, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
