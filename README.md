<div align="center">
  <a href="https://github.com/fmguerreiro/prstate">
    <img src="icon.webp" alt="prstate" width="96" height="96" />
  </a>
  <h1>prstate</h1>
  <p><em>Normalized GitHub pull-request review state, as a library and a CLI.</em></p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-4c1.svg" alt="MIT License" /></a>
</div>

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

Example output below uses made-up PRs. Human-readable grouping may change; use
`--json` for the stable fields.

Default sweep (`prstate`):

```text
4 of 6 open PRs in owner acme need you.

== Waiting on your reply (1) ==
  acme/api#418  Add idempotency keys to payment retries
    ci=pass mergeable=MERGEABLE/CLEAN review=REVIEW_REQUIRED base=main idle=2d
    - 1 unanswered from maria
      > maria on payments/retry.py, 1d ago: Should this key include the merchant id?

== Unresolved bot findings (blocks the merge until current re-review clears them) (1) ==
  acme/web#92  Move billing settings to the new form
    ci=pass mergeable=MERGEABLE/CLEAN review=REVIEW_REQUIRED base=main idle=1d
    - 1 unresolved bot finding(s) from coderabbitai — fix valid findings in code and obtain a current re-review
      ~ coderabbitai (review), 0d ago, 2 blocking: Actionable comments posted: 2. Validate the retry limit before scheduling.

== Approved, green, GitHub will take the merge (1) ==
  acme/web#95  Fix timezone label on invoices
    ci=pass mergeable=MERGEABLE/CLEAN review=APPROVED base=main idle=0d
    - approved by lee

== CI red (1) ==
  acme/api#421  Upgrade database driver
    ci=fail mergeable=MERGEABLE/CLEAN review=APPROVED base=main idle=0d
    - failing: test-integration
      checks: test-integration

2 PRs need nothing (waiting on reviewers or already answered). Every PR whose read was inconclusive is flagged above, not counted here.
```

Waiting on your reply (`prstate --owed`):

```text
1 of 1 open PRs in owner acme need you.

== Waiting on your reply (1) ==
  acme/api#418  Add idempotency keys to payment retries
    ci=pass mergeable=MERGEABLE/CLEAN review=REVIEW_REQUIRED base=main idle=2d
    - 1 unanswered from maria
      > maria on payments/retry.py, 1d ago: Should this key include the merchant id?

0 PRs need nothing (waiting on reviewers or already answered). Every PR whose read was inconclusive is flagged above, not counted here.
```

Review requested (`prstate --review-requested`):

```text
1 of 3 open PRs in owner acme need you.

== Checks still running (1) ==
  acme/web#97  Cache dashboard summaries
    ci=pending mergeable=MERGEABLE/CLEAN review=REVIEW_REQUIRED base=main idle=0d
    - 1 check(s) still running

2 PRs need nothing (waiting on reviewers or already answered). Every PR whose read was inconclusive is flagged above, not counted here.
```

This filter selects PRs requesting your review, but the text report groups only PRs
with classified action reasons. For every selected PR, including unflagged ones, use
`prstate --review-requested --json`.

All owners, machine-readable (`prstate --all-orgs --json`), projected here with `jq`
to keep the example short:

```bash
prstate --all-orgs --json | jq '{schema_version, scope, prs: [.prs[] | {key, ci: .ci.state, owed: (.owed | length)}]}'
```

```json
{
  "schema_version": 1,
  "scope": "every owner",
  "prs": [
    {
      "key": "acme/api#421",
      "ci": "fail",
      "owed": 0
    }
  ]
}
```

`--json` is the stable contract; `schema_version` tracks it.

### Python

```python
import prstate

sweep = prstate.fetch(owner="acme", author="@me")
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

## License

MIT. See [`LICENSE`](LICENSE).
