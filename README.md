# prstate

Normalized GitHub pull-request review state, as a library and a CLI.

Three axes per PR:

- `ci` — `pass | fail | pending | unknown | none`, collapsed to the current run per
  check. A raw `statusCheckRollup` keeps every historical run on the head commit, so a
  check that failed and was re-run green still reports the old failure.
- `owed` — human signals with no later activity from you, per surface. Unresolved is not
  the same as unanswered.
- `bot_findings` — unresolved bot threads and PR-level bot summaries, superseded by
  GitHub's own `isMinimized`/`minimizedReason` first and newest-per-bot-login second.

All three read the three comment surfaces GitHub splits feedback across: review threads,
review bodies, and issue comments. `gh pr view --json comments` omits review comments
entirely, which is why this is raw GraphQL.

Read-only. It never posts, resolves, approves, or merges.

## Install

```bash
pipx install git+https://github.com/fmguerreiro/prstate
# or: uv tool install git+https://github.com/fmguerreiro/prstate
```

Requires `gh` on PATH and authenticated; prstate inherits that auth.

## Use

```bash
prstate                       # open PRs you authored, under the cwd repo's owner
prstate --owed                # only PRs where someone is waiting on you
prstate --review-requested    # PRs waiting on your review
prstate --all-orgs --json     # every owner, machine-readable
```

`--json` is the stable contract; `schema_version` tracks it.

### Python

```python
import prstate

sweep = prstate.fetch(owner="SakanaAIBusiness", author="@me")
for pr in sweep.prs:
    print(pr.ci, pr.owed, pr.bot_findings)
```

`Sweep.partial` and each `PullRequest.partial` name reads GitHub could not complete. Do
not treat a partial result as a clean pull request.

## Trust boundary

`prstate` treats GitHub titles, bodies, reviewer names, workflow names, and API errors as
untrusted. Human-readable output removes terminal control characters. Full bodies appear
only with `--full`; JSON output retains data but cannot execute terminal controls.

The process shells out only through one checked `gh` command builder. It permits pull
request search, read-only GraphQL and user reads, repository owner lookup, and
`gh auth status`. It rejects GraphQL mutations, write HTTP methods and fields, every
other auth command, and token display. It never reads or prints a token directly.

## Design

`docs/DESIGN.md`.
