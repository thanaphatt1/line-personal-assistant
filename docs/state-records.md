# Durable workflow evidence

Hosted history can separate accumulated workflow evidence from the owner
snapshot with `PILOT_STATE_RECORDS_ENABLED=true`. The default is false. It
requires production history state and the original-message journal. Business
tables, Calendar events and the raw-message journal keep their existing schemas.

The owner document keeps the lease, fence, active reviews, bounded conversation
context, aliases and retained workspace context. Evidence is read by exact key,
indexed active-operation queries or the current conversation's references.
The runtime does not download every completed model decision on each message.
Model-record limits still count all unexpired records using a server aggregation.

## Storage contract

Under `assistant_pilot_states/<owner_hash>/evidence_sets/v1`, records use
`groups/<group>/records/<key_hash>`. These are app-owned documents. Do not edit
them or use them as business collections.

| Group | Contents |
| --- | --- |
| `events` | Event claim, result receipt, reply delivery and workbook binding in one bundle |
| `actions` | One durable business action and its result/proofs |
| `models` | One model request reservation, status and recorded decision |
| `budgets` | One month of model accounting |

Each document has `payload` (compressed JSON), `auth_mac` (HMAC bound to its
document path), `active` (boolean), `retain_until` (timestamp) and `record_ref`
(receipt lookup string). The payload identifies format, group, key, workbook
and typed values. Compression does not encrypt data. Existing service identity
and secret material are used; no new credentials, indexes or TTL policies are
installed by this feature.

Changed records and the owner snapshot commit atomically with the owner fence
and each document's version precondition. An unknown acknowledgement is checked
against the exact owner commit nonce and fields before proceeding. Historical
redelivery reads the same evidence; it cannot silently repeat a business effect
or a model request with an unresolved outcome.

Limits remain per atomic checkpoint and per record: 500 writes/900,000 request
bytes, 200,000 encoded bytes per evidence record and 8,000,000 expanded bytes.
The owner's existing encoded snapshot limit remains unchanged. Large active
reviews can still reach a bound; accumulating completed evidence no longer
increases the owner snapshot. A working mapping's iteration covers materialized
context and unfinished actions, rather than the entire historical collection.

## Migration and recovery

Migration runs under the existing exclusive owner lease before a business turn.
The original snapshot is first detached into an authenticated temporary backup.
Bounded chunks copy records while atomically advancing a durable cursor. A
restart resumes that cursor. The final commit publishes the ready marker and
deletes the backup. It never discards records to make the snapshot fit.

The packaged operator `python scripts/migrate_state_records.py` previews the
existing layout without writes. `--apply` performs or resumes migration and
requires the feature flag. `--verify` restores the migrated state and verifies
that the owner document did not change during the read. All modes reject
business, model and LINE I/O. Run it with the reviewed image and the service's
existing identity, pinned secrets and configuration. An active owner request
can cause a busy result; preserve the state and retry only a verified safe
attempt. Never replay a real confirmation as a deployment check.

Deploy compatible code before migrating. Core version 2 is intentionally
rejected by older runtimes. Compatible code continues reading, writing and
resuming migrated state even when the flag is false. Recovery must keep that
code; turning the flag off does not reconstruct the old snapshot or reverse
effects. Retain a compatible revision and never restore an old snapshot over
new state. A same-image flag-off revision cannot remove a code regression.

## Retention

Existing logical expiration rules still apply. Unresolved business actions,
model requests and uncertain reply deliveries remain retained. Completed
records can be conditionally deleted in small indexed pages during owner
activity, at most once every 15 minutes. Live action dependencies and retained
Calendar batch parent proofs are protected. Cleanup cursors cycle so protected
expired rows do not prevent later rows from being considered. Cleanup is
bounded and can defer after a failure; it is not a scheduled retention SLA.
The original-message journal and configured log retention are unchanged.

Synthetic tests cover cold restarts, interrupted migration, lost acknowledgements,
duplicate confirmations, multiple workspaces, Calendar batch proofs, both expense
backends, receipt buttons, aliases, growing evidence, expiration and tampering.
The official Firestore emulator also checks the REST transactions and queries.
