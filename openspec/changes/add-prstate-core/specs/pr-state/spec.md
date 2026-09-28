## Purpose

Normalized, read-only state for a set of open pull requests, on three axes: the CI
verdict for the head commit, the human signals the viewer owes a reply to, and the
automated-reviewer findings still open. Consumers read one versioned document instead of
each re-deriving the same queries and rules.

## ADDED Requirements

### Requirement: Reviewer feedback is read as the union of three comment surfaces

The system SHALL collect reviewer feedback from all three surfaces GitHub exposes —
inline review threads, review bodies, and pull-request issue comments — and SHALL label
every reported signal with the surface it came from. Reading a subset of surfaces is not
a partial read but a wrong one: it reports silence where a finding is open.

#### Scenario: A finding on a review thread is reported when the issue comments are clean

- **WHEN** a pull request has no issue comments and no review bodies, and an automated
  reviewer has an unresolved inline review thread
- **THEN** that finding is reported, attributed to the thread surface

#### Scenario: A review body and the thread comments submitted with it both survive

- **WHEN** a reviewer submits a review with a body and two inline thread comments
- **THEN** the review body and both thread comments are reported as distinct signals,
  the body as the summary and the thread comments as the findings

#### Scenario: The newest comment in a long thread decides who spoke last

- **WHEN** a review thread holds more comments than a single page returns, and the newest
  comment is the viewer's reply
- **THEN** the thread is treated as answered by the viewer
- **AND** the decision is attributed to the newest comment's timestamp, not the thread
  opener's

### Requirement: The CI verdict collapses to the latest run per check

The head commit's check rollup retains every historical run, so a check that failed and
was re-run green still carries the old failure. The system SHALL report at most one entry
per `(workflow, check name)` pair, keeping the entry from the highest workflow run id
within that pair. Ordering SHALL NOT use finish time, and a run id from a different
workflow SHALL NOT supersede anything, since run ids are monotonic per repository rather
than per workflow.

#### Scenario: A re-run that went green supersedes the earlier failure

- **WHEN** a check appears twice on the head commit, failing under run id 100 and passing
  under run id 200 of the same workflow
- **THEN** the check is reported once, as passing
- **AND** the CI verdict is not `fail` on account of that check

#### Scenario: A re-run that went red supersedes the earlier pass

- **WHEN** the same check passes under the older run id and fails under the newer one
- **THEN** the check is reported once, as failing, and the CI verdict is `fail`

#### Scenario: The higher run id wins even when it finished first

- **WHEN** the newer workflow run's check finished ten seconds before the older run's
  check of the same name
- **THEN** the newer run's result is the one reported

#### Scenario: The same check name in two workflows is two checks

- **WHEN** two different workflows each define a check named `test`, one passing and one
  failing
- **THEN** both are reported and the CI verdict accounts for both

#### Scenario: Checks without workflow provenance do not supersede each other

- **WHEN** two checks of different names arrive with no workflow attribution, one green
  and one red
- **THEN** both survive, and neither is treated as a re-run of the other

#### Scenario: A tie resolves to the worse state

- **WHEN** two entries share a workflow, a check name, a run id and a timestamp, one
  failing and one passing
- **THEN** the failing entry is the one reported, whichever order they arrived in

### Requirement: The CI headline never reports an unreadable state as green

The system SHALL report the CI verdict as exactly one of `pass`, `fail`, `pending`,
`unknown` or `none`, chosen in that order of severity: any failed check yields `fail`,
otherwise any pending check yields `pending`, otherwise any unrecognised state yields
`unknown`, otherwise `pass` when checks exist and `none` when none do. Recognised check
states are an allowlist; an unfamiliar state SHALL rank worse than green. The names of
failing, pending and unrecognised checks SHALL be reported alongside the headline.

#### Scenario: An unrecognised conclusion is unknown, not a pass

- **WHEN** a check reports a conclusion the allowlists do not recognise, and every other
  check has passed
- **THEN** the CI verdict is `unknown` and the check is named in the unrecognised list

#### Scenario: A pending headline does not swallow an unrecognised check

- **WHEN** one check is queued and another reports an unrecognised conclusion
- **THEN** the CI verdict is `pending`
- **AND** the unrecognised check is still named in the unrecognised list

#### Scenario: A pull request with no checks is distinguished from a passing one

- **WHEN** no checks ran at all on the head commit
- **THEN** the CI verdict is `none`, not `pass`

#### Scenario: Pending checks are named, not counted

- **WHEN** two workflows each have a queued check named `test`
- **THEN** the pending list holds one entry per pending check, so a consumer can name the
  check to wait on, and the number of pending checks equals the length of that list

### Requirement: Owed signals are human asks with no later viewer activity

An unresolved conversation and an unanswered one are different: the resolve button is
routinely left untouched long after a conversation ends. The system SHALL report a human
signal as owed only when the viewer has no qualifying later activity, where later means
a strictly greater timestamp. Qualifying viewer activity SHALL be evaluated per surface:
a review body is answered by any later viewer activity including a push, while an issue
comment is answered only by a later viewer issue comment and a thread only by a later
viewer comment in that thread.

#### Scenario: A comment the viewer answered earlier is not owed

- **WHEN** a reviewer leaves an issue comment and the viewer replies with a later issue
  comment, and neither participant resolves anything
- **THEN** nothing is owed on that comment

#### Scenario: A review the viewer replied to is not owed

- **WHEN** a reviewer submits a review requesting changes and the viewer submits a review
  comment afterwards on the same day
- **THEN** nothing is owed on that review

#### Scenario: An earlier reply does not answer a later ask

- **WHEN** the viewer's reply predates the reviewer's comment
- **THEN** the comment is owed

#### Scenario: A push does not answer an issue comment

- **WHEN** a reviewer asks a question in an issue comment and the viewer's only later
  activity is a push to the branch
- **THEN** the comment is still owed

#### Scenario: A push does answer a review body

- **WHEN** a reviewer submits a review body requesting changes and the viewer's only
  later activity is a push to the branch
- **THEN** the review body is not owed

#### Scenario: Push time is the early bound of the push interval

- **WHEN** the head commit was authored before a review was submitted but landed on the
  branch after it
- **THEN** the push is not credited with answering that review, because the recorded push
  time is the commit's own timestamp

#### Scenario: A thread whose last human word is the reviewer's is owed

- **WHEN** an unresolved review thread ends with a reviewer's comment
- **THEN** it is owed, attributed to that reviewer and that comment's timestamp

#### Scenario: A thread whose last word is the viewer's is not owed

- **WHEN** an unresolved review thread ends with the viewer's own comment
- **THEN** nothing is owed: resolving it is the reviewer's move, not the viewer's

#### Scenario: A trailing automated comment does not close a human ask

- **WHEN** an unresolved thread holds a reviewer's question followed only by an automated
  reviewer's comment
- **THEN** the reviewer's question is still owed

#### Scenario: A thread authored only by automated reviewers owes nothing

- **WHEN** every comment in an unresolved thread was written by an automated reviewer
- **THEN** nothing is owed on that thread as a human signal

#### Scenario: GitHub's actor type identifies bots without a bot-shaped login

- **WHEN** GitHub reports a comment author as actor type `Bot` even though its login has
  no known bot suffix or allowlist entry
- **THEN** the author is treated as an automated reviewer

#### Scenario: A resolved thread owes nothing

- **WHEN** a review thread is marked resolved
- **THEN** nothing is owed on it

#### Scenario: An outdated comment is still owed

- **WHEN** a reviewer's unanswered comment sits on a line that has since moved, so GitHub
  marks it outdated
- **THEN** it is still owed, and the outdated status is reported as a field a consumer may
  filter on

#### Scenario: A hidden comment is not owed

- **WHEN** a reviewer's unanswered comment has been minimized, so GitHub hides it
- **THEN** nothing is owed on it, whatever the minimize reason

#### Scenario: A signal from a deleted account is reported, not dropped

- **WHEN** an unanswered comment's author no longer exists
- **THEN** the signal is still owed, attributed to a placeholder author name

#### Scenario: The same rule answers "what do I owe as a reviewer"

- **WHEN** the viewer is a requested reviewer on someone else's pull request and the
  author has left an unanswered question for them
- **THEN** it is owed to the viewer, computed by the same rule that serves an author

### Requirement: Automated-reviewer findings resolve supersession before reporting

The system SHALL report every automated-reviewer finding with a state drawn from
`open_thread`, `blocking`, `stale`, `stale_clean` and `unknown`, and SHALL mark whether
the finding is still open. Supersession SHALL be decided first by GitHub's own minimize
state — a finding minimized as outdated or resolved is superseded — and only then by
keeping the newest still-live summary per automated-reviewer login. A superseded finding
SHALL still be reported, carrying the identifier of the summary that replaced it, so that
a consumer can see why it was retired.

#### Scenario: A minimized-as-outdated summary is not open

- **WHEN** an automated reviewer's summary has been minimized with reason `OUTDATED`
- **THEN** the finding is reported with its state but marked not open, and it does not
  make the pull request actionable

#### Scenario: Minimize state beats newest-per-login

- **WHEN** one automated reviewer posts two concurrent comment lanes and minimizes the
  newer one as outdated
- **THEN** the older, live lane stays open and still blocks

#### Scenario: A minimize reason that is not supersession does not retire a finding

- **WHEN** a finding is minimized with reason `SPAM`
- **THEN** it is still open

#### Scenario: A superseded finding names its replacement

- **WHEN** an automated reviewer posts a newer summary that replaces its own earlier one
- **THEN** the earlier finding is reported as not open and carries the newer summary's
  identifier

#### Scenario: A current clean verdict is not reported at all

- **WHEN** an automated reviewer's current summary parses to zero findings and no push
  has happened since
- **THEN** no finding is reported for it

#### Scenario: A clean verdict that predates a push is distinct from a blocking one

- **WHEN** an automated reviewer's summary parses to zero findings and the head commit
  landed after it
- **THEN** the finding is reported as `stale_clean`
- **AND** the pull request is not placed in the actionable bucket on account of it

#### Scenario: A blocking verdict that predates a push stays blocking

- **WHEN** an automated reviewer's summary parses to two findings and the head commit
  landed after it
- **THEN** the finding is reported as `stale`, still blocking but flagged as unconfirmed
  against the head commit

#### Scenario: An unparseable verdict is unknown, never clean

- **WHEN** a summary is shaped like a verdict but its finding count cannot be read
- **THEN** the finding is reported as `unknown` and still blocks

#### Scenario: A viewer reply on the same surface suppresses a summary

- **WHEN** the viewer comments on the same surface after an automated reviewer's summary
- **THEN** that summary is not reported

#### Scenario: An unresolved thread opened by an automated reviewer survives a push

- **WHEN** an automated reviewer opened an unresolved review thread and the head commit
  landed afterwards
- **THEN** the finding is reported as `open_thread`, because ownership follows the opener
  and only the opener resolving it closes it

#### Scenario: Thread findings and summary findings arrive in one list

- **WHEN** a pull request has both an unresolved thread opened by an automated reviewer
  and a blocking summary from one
- **THEN** both are reported in the same list, distinguished by state and surface, in
  timestamp order

### Requirement: An incomplete read is declared, never inferred away

Every read that could not be completed SHALL be reported as a first-class list of
reasons, per pull request and for the sweep as a whole. An incompleteness reason SHALL
mean "this could not be determined", never "this was determined to be bad". A pull
request with a non-empty incompleteness list SHALL NOT be recommended for merge.

#### Scenario: A truncated check rollup is declared, not silently accepted

- **WHEN** GitHub reports more check contexts than were read, after pagination gave up
- **THEN** the pull request carries an incompleteness reason naming the rollup
- **AND** it is not recommended for merge even if every check that was read passed

#### Scenario: A missing total count is treated as truncation, not as zero

- **WHEN** check contexts come back with no total count reported
- **THEN** the pull request carries an incompleteness reason, rather than the read being
  taken as complete

#### Scenario: A second page of check results is read rather than declared incomplete

- **WHEN** a pull request has more check contexts than one page holds and the following
  pages are available
- **THEN** every context is read and the pull request carries no rollup incompleteness
  reason

#### Scenario: Every comment in a long thread is read

- **WHEN** a review thread has more comments than the two end reads reveal and every
  continuation page is available
- **THEN** the system reads every thread comment and carries no incompleteness reason
  for that thread

#### Scenario: A viewer reply in the middle answers the earlier human signal

- **WHEN** a human opens a thread, the viewer replies, and an automated reviewer posts
  the newest comment
- **AND** every thread-comment page is available
- **THEN** the earlier human signal is not reported as owed

#### Scenario: An unread thread middle blocks a verdict instead of guessing one

- **WHEN** an unresolved thread's newest comment is from an automated reviewer
- **AND** a continuation page fails or exceeds the bounded page count
- **THEN** nothing is reported as owed for that thread
- **AND** the pull request carries an incompleteness reason naming it

#### Scenario: An uncomputed merge state is incompleteness, not a blocker

- **WHEN** GitHub has still not computed the merge state after the system re-asks
- **THEN** the pull request carries an incompleteness reason and is not recommended for
  merge, and is not reported as conflicting

### Requirement: Actionability is computed once, not by every consumer

The system SHALL assign each pull request a prioritized list of reasons it needs
attention, including the mechanical merge recommendation, so that consumers read a
verdict rather than re-deriving one. A pull request SHALL be recommended for merge only
when it is approved, not a draft, owes nothing, has no open automated-reviewer finding of
any state, has no requested changes outstanding, is mergeable, and its CI verdict is
`pass`.

#### Scenario: An approved, green, answered pull request is recommended for merge

- **WHEN** a pull request is approved, not a draft, has a `pass` CI verdict, owes nothing,
  has no open automated finding and reports a clean merge state
- **THEN** it is recommended for merge

#### Scenario: An open automated thread keeps an otherwise-ready pull request out of merge

- **WHEN** the same pull request additionally has an unresolved thread opened by an
  automated reviewer
- **THEN** it is not recommended for merge, and the finding is named as the reason

#### Scenario: A stale clean verdict does not make a pull request actionable

- **WHEN** an otherwise-ready pull request's only automated finding is a clean verdict
  that predates a push
- **THEN** it is not placed in the actionable bucket, and its reason states that a
  re-review is pending rather than that a fix is needed

#### Scenario: A pull request appears in exactly one bucket

- **WHEN** a pull request qualifies for two reasons at once
- **THEN** it is listed once, under its highest-priority reason, with the other reason
  carried alongside

### Requirement: The JSON document is the versioned contract

The system SHALL emit a machine-readable document carrying `schema_version`, the sweep
scope, the resolved viewer login, the fetch timestamp, the sweep-level incompleteness
list, and one entry per pull request with its identity, review decision, CI block, owed
list, automated-finding list, reasons and per-pull-request incompleteness list. The
vocabularies of the CI verdict and the automated-finding state SHALL be part of the
contract. Adding a key SHALL NOT change `schema_version`; removing or retyping one SHALL.

#### Scenario: Every contract key is present on a minimal pull request

- **WHEN** a pull request has no checks, no owed signals and no automated findings
- **THEN** the document still carries every contract key, with empty collections rather
  than absent ones, and `schema_version` set

#### Scenario: States use only the published vocabularies

- **WHEN** any sweep is emitted
- **THEN** every CI verdict is one of `pass`, `fail`, `pending`, `unknown`, `none`, and
  every automated-finding state is one of `open_thread`, `blocking`, `stale`,
  `stale_clean`, `unknown`

#### Scenario: Timestamps are unambiguous

- **WHEN** the document carries any timestamp
- **THEN** it is UTC ISO-8601 with a trailing `Z`, matching the form GitHub returns

#### Scenario: Comment bodies are trimmed by default and available in full on request

- **WHEN** a pull request carries a six-kilobyte automated-reviewer comment
- **THEN** the default document carries only a short excerpt and no full body key
- **AND** the full-body option adds a body key capped at the transport trim length

### Requirement: The system is read-only against GitHub

The system SHALL never construct a GitHub command or request that could change state: it
SHALL NOT resolve a thread, post a comment, submit a review, approve, merge, close or
edit anything. Command construction SHALL pass through a single gate that accepts only
read subcommands and read endpoints and rejects any argument containing a GraphQL
mutation. The gate SHALL be enforced by a test that walks the entire source tree, not by
convention.

#### Scenario: A write subcommand is refused

- **WHEN** a command line is built for a subcommand outside the read allowlist, such as
  deleting a repository or merging a pull request
- **THEN** construction fails with an error and no process is spawned

#### Scenario: A write flag is refused

- **WHEN** a command line sets an HTTP method or attaches a field to an API call other
  than the GraphQL query itself
- **THEN** construction fails with an error

#### Scenario: A mutation is refused

- **WHEN** any argument contains a GraphQL `mutation` keyword
- **THEN** construction fails with an error

#### Scenario: The source tree contains no command outside the gate

- **WHEN** the source tree is scanned for GitHub command invocations
- **THEN** every one of them goes through the gate

### Requirement: Viewer and scope are resolved, not assumed

The viewer is the login the owed and answered rules are evaluated against and SHALL
default to the authenticated account rather than any hardcoded name, since for a
review-requested sweep the viewer is not the pull request's author. Scope SHALL resolve
by precedence: an explicit repository, then all owners, then an explicit owner, then the
owner of the repository in the working directory. Naming an owner explicitly SHALL NOT
cost a lookup of the working directory.

#### Scenario: The viewer defaults to the authenticated account

- **WHEN** no viewer is supplied
- **THEN** the sweep resolves and reports the authenticated login, and the owed rules run
  against it

#### Scenario: An explicit owner skips the working-directory lookup

- **WHEN** an owner is named explicitly
- **THEN** the working directory's repository owner is not looked up

#### Scenario: Conflicting scope selections are rejected

- **WHEN** a repository and an all-owners sweep are requested together
- **THEN** the invocation fails with a usage error

#### Scenario: Two reviewer filters produce one deduplicated sweep

- **WHEN** both review-requested and reviewed-by filters are supplied and a pull request
  matches both
- **THEN** it appears once in the sweep

### Requirement: Selection filters apply to classified state, not to discovery

Filters that depend on classification SHALL be applied after the sweep is classified, so
that a filter never changes what was read. Drafts SHALL be excluded unless explicitly
included. The exit status SHALL be zero for a completed sweep whatever it found, and
non-zero only when discovery succeeded but no pull request could be read at all.

#### Scenario: The owed filter keeps only pull requests with an owed signal

- **WHEN** an owed-only sweep runs over a set containing one pull request that owes
  nothing and one that owes a reply
- **THEN** only the second is reported, and the sweep's scope, viewer and incompleteness
  list are unchanged

#### Scenario: The CI filter matches the normalized verdict

- **WHEN** a sweep is filtered to failing CI
- **THEN** only pull requests whose normalized verdict is `fail` are reported, including
  one whose raw rollup still holds a superseded failure only if the collapsed verdict is
  still `fail`

#### Scenario: Drafts are excluded unless asked for

- **WHEN** a sweep runs over a set containing a draft
- **THEN** the draft is absent by default and present when drafts are explicitly included

#### Scenario: A sweep that found nothing is still a success

- **WHEN** discovery returns no open pull requests
- **THEN** an empty sweep is emitted and the exit status is zero

#### Scenario: A sweep that could read nothing is a failure

- **WHEN** discovery finds pull requests but none of them can be read
- **THEN** the exit status is non-zero rather than a confident empty report
