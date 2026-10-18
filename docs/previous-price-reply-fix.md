# Preserve catalog and previous-price questions in replies

With response realization enabled, an unresolved expense could lose the actual
catalog choices and ask only for a generic Database name. A matching item could
also lose its offered saved amount and date. The tool results were correct, but
the fact projection omitted those questions. Earlier price tests mostly had
realization disabled, so they did not cover the deployed composition.

The reply now keeps the complete server-built catalog question or price question
as required facts. A price review retains both the historical price date and the
independent date of the new expense. Optional wording failures retain the same
authoritative question through the existing fallback.

For a synthetic catalog containing `Spotify service`, `ค่า Spotify` asks whether
that is the intended name. One visible name can be selected with an affirmative
reply; several real plans require the owner's explicit name choice. Matching a
vendor prefix supplies suggestions only. It never selects a plan or copies a
saved price automatically. Explicit amounts still take precedence.

Old pending drafts remain intact. A choice omitted from the delivered message
cannot authorize a name or price selection. Reading the pending draft displays
a fresh question before the owner chooses. Price selection prepares a review;
the separate final confirmation performs the save. The visibility check reads
the delivered text or Flex body, excluding alternative text and action metadata.

The regression suite in `tests/test_previous_price_replies.py` enables response
realization and uses invented labels, amounts and dates. Its development corpus
also covers ordinary expenses, Calendar interruptions and independent historical
classification reviews. These are synthetic development checks, not live user
data or a claim of general model accuracy.

This correction requires a separate approved source-bound release. Keep raw
history, state, usage counters and the current draft when changing revisions.
Stage a new candidate and a compatible price-OFF recovery from the same image;
all other existing features remain enabled. Do not restore snapshots over newer
data or assume an older image can read all new receipts safely. Switching to the
price-OFF revision does not undo a saved financial or Calendar operation.
