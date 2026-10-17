# Provisional category development results

The [v2 synthetic development corpus](../../tests/fixtures/provisional_categories_development_v2.json)
passed40/40 turns across nine scenarios and two reset episodes each. It covers
query-only inclusion, separately reviewed permanent mapping, mistaken aliases,
ambiguous meanings, owner clarification, stale catalogs, repeated confirmation
and invalid input data. All Google, LINE and state services were simulated.
No actual private historical names were inference inputs or fixture data.

The [original strict v1 corpus](../../tests/fixtures/provisional_categories_development.json)
remains unchanged. Its runs scored13/34,6/7 and31/34 turns. The partial second run
also exposed a runner dependency error after a missing review. Those results
remain failed and all received usage is counted. Initial prompts encouraged
overly conservative abstention; one query dropped the requested category filter.
Clarifications preserved that filter and distinguished missing Database mapping
from semantic ambiguity.

One v1 positive name began with `Unknown`; conservative abstention on it was a
valid safe response but failed that strict oracle. V2 uses an unambiguous
synthetic positive label and adds an explicit owner-clarification episode. It is
a new development cohort, not a retrospective pass of v1 or an unseen holdout.
Classification behavior still varies; suggestions require owner review and
unclear names must remain excluded until resolved.

The40-turn cohort used frozen archive
`3753ecea3eb9e7a72ef5f0515a086959b5cb7ebd69c8064fa6e4965afb498ed9`.
The combined archive
`5942602ae0e7cbe556ca6437cda4e8a05f6272243bbd8f7e1db4f32e1866bb98`
then clarified the unique saved-record handoff. The label implementation,
integration files and label-only model instructions are identical; that
invariance was checked offline. With every new tool enabled together, two saved
edit scenarios passed8/8 turns on the combined archive. The earlier combined
run passed6/8 because it asked an unnecessary extra record-selection question;
it performed no unconfirmed write. These are small development checks.

All category runs used109 received Luna calls, estimated USD0.016351375 and
conservative all-input-cache-write ceiling USD0.082821500. Both combined saved
regression runs used16 calls, estimated USD0.003603925 and ceiling USD0.014205250.
Unknown request holds and automatic retries were zero. Pricing is the recorded
snapshot, not an invoice. [Structured results](results.json) retain failures,
corpus/archive hashes and all run costs.

The full offline suite and source/wheel/signed startup checks pass. Separate
processes verified eight same-image OFF-bridge cases for saved expense/Calendar
work and category reviews, query-only receipts and uncertain mapping actions,
with raw originals preserved and no additional simulated mutation. Actual
traffic rollback and live permissions were not exercised. Mermaid structure and
document links are checked; no browser-rendered diagram claim is made.

Read the [feature, consent and writer-policy limits](../../docs/provisional-categories.md)
before proposing a release. Flags remain disabled by default; source publication
does not change a running service or map real owner records.
