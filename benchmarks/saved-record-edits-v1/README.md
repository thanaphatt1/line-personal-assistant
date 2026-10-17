# Saved-record edit development results

The frozen final runtime archive `0e374f6b1fa557cbaca5a143de9aab49b391de4e36aecbf98cf36703d91b9446`
passed 48/48 synthetic development turns: 12 scenarios, two independent reset
episodes each. This is development evidence; failures were used for corrections.
It is not a held-out accuracy estimate or a live integration result.

The [predefined synthetic corpus](../../tests/fixtures/saved_record_edits_development.json)
covers expense amount/name/date changes, ambiguous saved-record selection,
proposal amendment, legacy rows without IDs, timed/all-day rescheduling, guests
and unsupported deletion. Every successful change required a later owner
confirmation. Signed replay caused no additional simulated mutation.

The initial run passed32/48 turns; it exposed overly narrow money citations and
an inferred all-day endpoint that extended the requested duration. The second
passed46/48; a redundant unchanged date with no new citation prevented one
timed-review episode. Fixes accept a cited money expression with its unit,
validate literal date/clock evidence, calculate preserved duration on the server,
and retain already certified unchanged fields. Both failed runs remain counted.

All three runs used138 received Luna calls, no unknown requests and no automatic
retries. Estimated received usage was USD0.017573795; the conservative
all-input-cache-write ceiling was USD0.110124875. These use the recorded pricing
snapshot and are not an invoice. The [structured results](results.json) retain
every failed scenario/turn and the aggregate costs.

The final full offline suite passed2,993 tests, including65 new provider/signed
webhook regressions. Source/wheel and signed startup probes passed with zero
external connections, with both saved-edit flags OFF and ON. A separate-process
same-image OFF bridge preserved four synthetic pending/uncertain expense and
Calendar cases, retained raw originals and reconciled positive results without
issuing a mutation. No owner records were inference inputs or test fixtures.

Read the [feature and rollback limits](../../docs/saved-record-edits.md) before
considering a release. In particular, Sheets cannot enforce conditional writes
against outside collaborators; expense editing requires the reviewed
single-writer policy. Source publication does not enable either mutation flag.
