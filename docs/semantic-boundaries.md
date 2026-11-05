# Language interpretation and execution checks

The planner selects intent, appointments, fields, source quotations, shared
facts and targets. Current Calendar tools accept model-selected event members
with original owner quotations and normalized ISO dates, Bangkok clocks or
elapsed minutes. Line breaks, conjunctions and punctuation do not determine
the event count. The application does not rewrite titles, propagate shared
fields or select an ordinal a second time. Unresolved members and unsupported
requests must be declared in the proposal and remain unconfirmable.

Current expense preparation already uses model-selected groups and numeric
roles. Pending amount changes validate complete numeric tokens and their
quoted positions without reclassifying dates, counts or payment clauses.
Semantic Calendar patches use offered typed field metadata and the same
temporal evidence path as creation. They can declare exact, approximate or
uncertain wording, and resolving an earlier uncertainty to the same value
still records the new evidence. Unsupported Calendar requests fail before
publishing a draft, so a supported restatement can reuse the original facts.
Changing the event mode retires uncertainty about fields that mode does not
use: an all-day event needs no clock. Replacing an endpoint with a duration
also retires uncertainty about the removed endpoint. Date and event-mode
uncertainty still block confirmation, and simultaneous conflicting facts are
still shown as unresolved.

Generic tables use their declared columns and choices. Date values are model
interpretations paired with original owner wording, not restricted to a
natural-language date grammar. Record/reference selection accepts a model
choice among actually offered records without demanding the name or ID in
the newest sentence. An incomplete/truncated candidate set must still be
read sufficiently before selection, and the selected version is re-read.

The application retains authentication, configured source/table permissions,
actual offered IDs, current versions, exact quotation checks, numeric token
integrity, civil date/clock validity, interval/provider constraints, sealed
state, review delivery and fresh explicit confirmation. Tools prepare data;
they cannot commit a write or confirm themselves. Unknown write outcomes are
reconciled without blindly repeating the request.

## Language policy and typed checks

Read periods, Calendar conditions and follow-up selection descriptions are model
interpretations of owner context. They may normalize spelling, spacing or wording;
no substring match proves their meaning. `period_text` is optional context, while
typed date frames, offsets and anchors determine the actual bounded read. A
missing typed date or invalid interval still fails. Current owner references,
delivered candidate bindings and fresh source checks still apply. Explicit date
components in an activity selection are checked against the current owner message
and candidate dates, independently of the model's selection description. Activity
labels likewise accept normalized wording with the current owner reference.

The optional natural-read experiment writes an `answer` string directly from
computed results, without placeholder or literal-number validation. Its current
result reference proves evidence identity, not prose or numeric correctness.
The model may copy a wrong number or omit a qualifier; structural grounding is
not applicable in evaluation. See [natural text experiment](answer-composition.md#natural-text-experiment).

Default document-based read presentation can select supporting cells and omit unrelated
rows. Mandatory scope/provenance/coverage facts and bound values remain checked;
write review coverage stays complete. SQL computes analytical totals, shares and
comparisons before prose. Selecting a useful explanation remains the model's
job; a valid selection is not proof of relevance or a causal explanation.

Assistant prose has no word whitelist, status-word blacklist, question-word
filter or per-field phrase enum. Ordinary fact replies and diagnosed explanations
accept bounded free text while requiring their original fact IDs/reason codes.
On these fact/document paths, literal business numbers, URLs, markup, unsafe control characters, omitted facts
and invalid references still fail. These checks constrain data/transport, not
natural-language meaning. Words such as “saved” never update structured status,
create an action or replace a provider receipt. A misleading sentence can be
shown; semantic evaluation and owner review must assess that separately.

Source readers can declare an argument failure repairable. The unified executor
preserves that diagnosis and offers the native schema to the same planner when
all failed sources declare `assistant_arguments`. Mixed unavailable sources stop;
provider failures are not retried. Complete valid snapshots can be reused within
the turn. A new successful result permits another selection repair; repeated
plans or unchanged diagnostics still stop.

This policy removes language acceptance/rejection heuristics. It retains explicit
protocol controls (buttons, nonces and reference IDs), catalog identity, numeric
and calendar parsers, exact spans binding proposed writes to original owner
records, and compatibility readers for stored legacy proposals. Those mechanisms
check the data/protocol being executed; they do not certify arbitrary prose.

Quoted evidence and a valid schema do not prove that the model understood the
language correctly. Semantic omissions, a wrong date interpretation or the
wrong choice among real records remain possible. The complete proposed values
are shown before writing. Varied model evaluations and owner review complement
the mechanical checks; the server does not claim to prove semantic coverage.

The earlier Calendar parser remains for cached legacy tool calls and pending
records created with the old schema. It is not the current tool interface.
Pending expense dates without a `temporal_ref` retain literal-parser
compatibility; the temporal helper supplies semantic normalization otherwise.
Stored receipts are not rewritten. New semantic metadata requires a compatible
reader; recovery must use the same new image and preserve state, never silently
fall back to an older image, restore a snapshot or replay confirmation.
