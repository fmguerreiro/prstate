## Why

Seven agent skills — `pr-comment-surfaces`, `reviews-needed`, `babysit-reviews`,
`babysit-all-prs`, `review-swarm`, `pr-review-fanout` and `standup` — each re-derive the
same pull-request state in prose: a GraphQL query, a `--jq` projection, a CI-collapse
paragraph, and a per-surface "what counts as answered" rule. Prose is not shared code, so
every skill carries its own copy and each copy drifts independently.

Four classes of bug are attributable to that duplication, each one observed:

1. **One comment surface read instead of three.** Reviewer feedback lives on review
   threads, review bodies and issue comments. `gh pr view --json comments` omits review
   comments entirely (cli/cli#11477), so a skill reading that one surface reports a clean
   PR while a finding sits open on another. A `claude[bot]` finding was missed on komb#208
   exactly this way.
2. **Stale `statusCheckRollup` verdicts.** The rollup on a head commit keeps every
   historical run, so a check that failed and was re-run green still carries the old
   `FAILURE` (cli/cli#4946). Collapsing on finish time rather than workflow run id gets it
   backwards: on komb-enterprise#530 the newer run's `lint` finished ten seconds before the
   older run's, so the older red won.
3. **"Unresolved" conflated with "unanswered".** GitHub's resolve button is left untouched
   long after a conversation ends. A sweep that treated unresolved as unanswered told the
   author to reply to a coral#48 comment he had answered three weeks earlier, and to a
   hl#3270 review he had replied to that morning.
4. **Bot summaries reaching no bucket.** Superseded, minimized and stale bot verdicts have
   no shared rule, so a bot summary on komb-enterprise#710 landed in no bucket at all —
   neither actionable nor dismissed. The same area regressed on 2026-08-24 when a clean
   verdict predating a push reused the `stale` state: six clean PRs were pulled into the
   actionable bucket and #711 was knocked out of the merge bucket.

Each bug is a rule that was correct in one skill and absent or wrong in another. One
normalizer with one contract removes the duplication and makes each rule fixable once.

## What Changes

- Introduce `prstate`: a zero-runtime-dependency Python 3.12 library plus a thin CLI that
  answers, for a set of open pull requests, **what is true right now, normalized** on three
  axes — CI verdict, what the viewer owes a human, what automated reviewers still have open.
- Fetch over the `gh` binary (`gh search prs`, `gh api graphql`), inheriting the user's
  existing keyring auth; no HTTP client, no token handling, no runtime dependencies.
- Classification is pure and offline-testable; network is confined to a single module.
- Emit a versioned `--json` document carrying `schema_version` as the interface the skills
  consume, plus a default bucketed text report for the two skills that print one today.
- Guarantee read-only operation mechanically: the one helper that builds a `gh` command
  line rejects any subcommand outside a read allowlist and any argument containing
  `mutation`, enforced by a test that walks the whole source tree.
- Port the rules from `~/.agents/skills/babysit-all-prs/triage.py` (965 lines) and its test
  suite (852 lines), with six deliberate behaviour changes recorded in design.md.
- Cut the seven skills over to `prstate --json`, deleting their duplicated query and
  classification prose. **Gated**: the cutover edits a different repository
  (`~/projects/dotfiles`) and starts only after live smoke tests pass, because a
  half-migrated skill is a broken skill the user runs daily.

Not in scope: writing to GitHub (never resolves a thread, posts, approves or merges), a
TUI, a merge queue, an LLM judgment engine, a cache or persistent store, and merged-PR
history — `prstate` reports open-PR state, not history.

## Capabilities

### New Capabilities

- `pr-state`: normalized, read-only pull-request state — CI verdict collapsed to the latest
  run per check, human signals the viewer owes a reply to across all three comment
  surfaces, automated-reviewer findings with supersession resolved, an explicit
  incompleteness signal, and a versioned JSON contract for consumers.

### Modified Capabilities

None. This is the project's first capability.

## Impact

- **New code**: `src/prstate/` (`model.py`, `query.py`, `gh.py`, `classify.py`,
  `render.py`, `cli.py`, `__init__.py`) and `tests/` with recorded GraphQL fixtures.
- **Packaging**: `prstate = "prstate.cli:main"` console script, hatchling, `requires-python
  >= 3.12`, `dependencies = []`, `pytest` the only dev dependency. Install via
  `uv tool install` / `pipx install` from the git URL; no PyPI publish yet.
- **External dependencies**: the `gh` binary, already required for auth. GitHub GraphQL
  usage is roughly ten requests per sweep, well under the 5000-point hourly budget.
- **Downstream, gated**: the seven skills in `~/projects/dotfiles`. `triage.py` and
  `test_triage.py` are deleted only after `babysit-all-prs` points at `prstate --json`.
- **Contract surface**: `schema_version`, every key of the `--json` document, the enum
  vocabularies of `ci.state` and `bot_findings[].state`, and the meaning of a non-empty
  `partial`. Added keys bump nothing; removed or retyped keys bump `schema_version`.
