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

Received assistant replies that claim an operation without a verified result
receive structured feedback in the same turn, using the original owner facts
and offered tools. An unchanged invalid reply stops; provider failures and
unknown operations are not retried. Read-analysis feedback lists actual input
and reportable result references without selecting one for the model. A new
successful query result permits another selection repair; repeating the same
failure against unchanged evidence still stops.

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
