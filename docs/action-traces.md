# Turn and action traces

The hosted raw-history assistant emits bounded JSON lines to stdout. Cloud Run
can retain these in its existing Cloud Logging destination; no new database,
credentials, background job or business-state migration is required. Local
mock assistants are silent unless a test explicitly supplies `action_trace_sink`.

Filter logs using `jsonPayload.event="assistant_action_trace"` and the revision
and timestamp of the reported conversation. Records share `turn_ref`, a hash
of the LINE webhook event ID. `attempt_ref` distinguishes delivery attempts;
`sequence` orders their records. A duplicate delivery has the same turn ref
but a new attempt ref. It must not repeat a completed business effect.

Each attempt can show:

- Planner, rolling-history-summary and reply-realization model requests and
  outcomes, with cached attempts distinguished. Provider request and response
  bodies are excluded.
- Tool name and call ref; hashed draft/entry/source refs, draft version, supplied
  field names, entry count and name-resolution step. Actual names, amounts,
  dates, notes, table values and physical provider coordinates are excluded.
- Tool outcome, machine error code, known invalid/missing field paths, next
  required category field, diagnosed failure kind/next action and bounded
  before/after draft/action summaries.
- Action checkpoints (`prepared`, `executing`, `effect_returned`,
  `action_completed`), a hashed action key, kind and status. A checkpoint does
  not independently prove an external effect; the existing action proof and
  business receipt remain authoritative.
- Final server result status, or an interruption marker without exception
  messages, stack traces, owner IDs, credentials or confirmation nonces.

The route marker distinguishes the raw-history workflow from a retained legacy
fallback. Legacy fallback traces include the route and action checkpoints;
their old planner's internal proposal steps are outside this instrumentation.

At most 96 ordinary records and one terminal record are emitted per attempt.
The terminal record reports dropped records and observed sink failures. A
missing terminal record can mean abrupt process termination. Logging failures
do not retry, authorize or block a business operation. Retention, availability,
access and export follow the existing logging destination's settings; these
are diagnostic logs, not a transactional audit ledger or an unlimited wire
request census. They do not store model chain of thought.

Raw speaker history and model accounting already exist, but consumed proposal
caches are cleared after the response receipt. These traces preserve the
operational steps without copying raw conversations into another store. They
do not retroactively recover tool arguments from an older deployed version.

## Name clarification

When matching an expense finds no candidate, the assistant reports that result
instead of asking the owner to know the contents of Database. A reply such as
“if absent, add it” can continue the already delivered name question using its
owned draft, version and row. An omitted model row ref can be resolved only
from one explicit name or one still-valid delivered question. Ambiguity,
expired/undelivered questions and changed versions remain blocked.

The conditional exception applies only to name preparation, using a complete
current catalog check and original certified expense facts. It does not relax
the general guard against hypothetical/negated financial facts. Repeating
“add as new” resumes the current category question without resetting the draft.
The model receives `question_focus` metadata for that delivered row. If it
proposes beginning a sibling while the owner has not named another row, the
server re-presents the pending category question without mutating either row.
Categories are still chosen from supplied metadata and final writes still
require the existing delivered review and fresh owner confirmation.

## Actionable failure replies

Failed expense preparation supplies structured `validation_feedback` to the
reply model: actual server error codes, the failed step, invalid/required
fields and a classified next action. A rejected inferred category is an
assistant proposal error. It is not evidence that the owner mistyped a name,
amount or date, or that access to Database failed.

For a verified owned enrollment target, feedback includes retained draft rows
and current category/subcategory choices. The reply asks only that target's
missing choice, without selecting it or altering the draft. The delivered
question retains its draft/version/row focus so a short answer can continue
after a restart. Final review and fresh confirmation are still required.

When reply realization is enabled, a known enrollment failure permits one
short LLM paraphrase of the diagnosed cause. The model receives the actual
failure and current question, rather than only a generic error template.
Server status and next question are inserted unchanged. Known draft entries
remain model context; failure replies do not recite them or add stock
reassurance about keeping data or not resending the original message. The
parser checks reason codes and rejects obvious invented values, success,
owner blame and unrelated access/network diagnoses. These checks are not a
general proof of free-text meaning. Invalid prose, timeout or quota failure
uses the same actionable server explanation as fallback. Unknown failures
keep an explicit unknown-cause explanation; the model cannot infer their
cause from a missing diagnosis.

Typed argument errors expose only field paths, without raw invalid values or
exception prose. Detailed feedback remains in the existing conversation
receipt; operational logs retain only codes, failure kind, next action and
hashed refs, not the names, amounts or explanation text. This adds no log
reader to the bot, collection, credential, state migration or production
configuration change.
