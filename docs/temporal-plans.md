# Semantic temporal plans and tool recovery

The raw-history planner can use `resolve_temporal` when literal date/time
parsing is insufficient. It supplies original owner evidence and a typed plan;
the server performs the arithmetic. No phrase-specific router or new provider
access is involved.

| Plan | Inputs | Server behavior |
| --- | --- | --- |
| `components` | Complete ISO date and/or Bangkok HH:MM | Validate the model's interpretation of owner wording |
| `offset` | Verified basis plus signed days, months, years, minutes | Compute the resulting date/time |
| `weekday` | Weekday and next, previous or inclusive direction | Compute relative to the original owner timestamp or selected basis |
| `duration` | Hours and minutes | Compute exact whole minutes |

The planner asks for clarification when wording, clock, date or target is
ambiguous. Only Bangkok is supported. Calendar dates must exist and fall in
1900–2100. Undefined month arithmetic, such as January31 plus one month, is
rejected rather than clamped. Existing appointment duration/mode restrictions
and the prohibition on future expense dates remain.

`temporal_context` offers owned pending Calendar start/end values, complete
current-request Calendar reads and offered saved-edit candidates. Metadata or
a guessed event title does not supply a saved-event basis. The default basis
is the cited owner's original timestamp; later turns do not move “tomorrow.”
Calendar query analysis remains read-only and can answer temporal questions
from its observed events. Saved-event rescheduling uses the offered candidates
from `find_saved_records` and its separate reviewed mutation flow.

A resolution returns a current-turn `temporal_ref` and typed projections.
Pending field patches cite the reference, normalized value and original owner
span; a literal subspan or containing span from the same message is accepted
while the full resolution proof stays authoritative. Exact retained fields
are omitted before field-evidence validation and keep their existing proofs;
draft/version/record/field and duplicate checks still apply. A wholly unchanged
patch returns an unchanged answer without creating a review or consent.
Calendar creation uses `temporal_refs` bindings, ordinary table date
fields accept `temporal_ref`, and expense dates can use the offered `date_ref`.
Saved Calendar rescheduling accepts the same reference bindings and retains
its original conditional ETag write protocol. Offset date/time projections
must describe the same instant across midnight.

For a Calendar request with shared date components, the planner resolves each
intended date separately and binds its reference in `prepare_calendar_events`.
It leaves the paired field null or supplies the returned projection/original
span, rather than concatenating fragments into a fabricated quote. Distinct
verified dates can establish separate occurrences in a shared source clause.
One literal title and its original clock range remain common to those events;
unrepresented substantive clauses and explicit extra slots still block review.
Each occurrence retains original text coordinates and a sealed temporal proof,
including after cold restore. This creates proposals only, never appointments.

References cannot be invented, reused on a later owner turn or used after a
pending basis changes. A sealed pending draft retains a self-contained
calculation proof and original fact provenance; cold restore validates the
calculation again. These are observed snapshots, not formulas that silently
move another appointment when its basis changes later. A new confirmation
of the delivered review is still required for every external effect.

Tools declare `recovery.kind` as `assistant_arguments`, `owner_input`,
`unsupported` or `unavailable`. Argument failures include field diagnostics,
required inputs and an optional repair-tool scope. The planner receives the
actual offered schemas. A new validator/tool can declare recovery without
adding its name or error code to a global repair allowlist.

Calendar source errors identify the failing field. If argument repair stops,
the fallback describes an internal preparation failure instead of claiming the
owner omitted details that the validator did not mark as missing.

Repair has no fixed two-attempt ceiling. It stops on repeated plans or
unchanged normalized diagnostics. Rotating invented fields/targets does not
count as progress. Successful helper calls restore the original planning
scope. Dedicated read-analysis/review-refresh flows retain their own
continuations. Owner-input, unsupported and unavailable failures do not
trigger argument repair; provider requests and unknown mutations are never
automatically retried. Existing time, context and accounting gates still apply.
