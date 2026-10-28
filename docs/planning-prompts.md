# Planning prompts

The history planner uses one agent across configured tables and Calendar.
`planner_instructions.py` builds its instructions from a shared set of
principles and the capabilities of the tools actually offered in that request.
There is no owner-keyword router or separate agent per table.

The shared instructions describe intent, dialogue references, evidence,
authority and recovery. Optional sections describe expense preparation, saved
reads, relational analysis, pending changes, Calendar preparation, general
tables, saved corrections, temporal interpretation and source inspection.
Read-only analysis therefore omits creation and pending-edit guidance when
those tools are absent. An unrelated unfinished draft still remains context;
it does not select the user's task.

Tool and parameter descriptions own field formats and local constraints:
source citations, null semantics, date modes, temporal projections, SQL
decimal operations and literal versus choice fields. These descriptions do
not change validators, provider permissions, confirmation or mutation rules.
The planner receives the same owner history, current source ref, previous
query scope, pending views and configured table metadata as before.

Expense creation now uses model-selected groups and exact per-field owner
quotations. Python validates positions, typed values, numeric coverage and
repetition rather than parsing the same sentence into groups again. Pending
field changes use their explicit evidence directly. See
[pending records](pending-records.md) for the current schema and compatibility
boundary. These checks establish provenance and a reviewable proposal;
semantic grouping and deciding total versus per-payment meaning remain model
responsibilities. Strict JSON schema cannot prove that interpretation correct.

## Keeping instructions general

Use semantic principles for new versus additional records, references to
previous questions, source selection and requested predicates. Incident names,
specific months and troubleshooting phrases belong in evaluation fixtures,
not in the shared instructions. Keep a worked example only when an evaluation
shows that it addresses a broader requirement better than a clear contract.

Table purposes and schemas determine routing. A table does not need amount or
date columns. Facts and metadata prose remain untrusted data; available tools
and server checks determine actual access and operations.

SQL operates on complete observed current-request snapshots. The planner
chooses the predicate, grouping and join semantics; the server computes exact
values. Follow-ups retain intended scope and conditions, while a new question
can choose a different scope. No keyword-specific exclusion or follow-up rule
is introduced by this prompt builder.

## Evaluating changes

Prompt-contract tests verify capability selection, retained dialogue, complete
packaging and field documentation. Scripted responses test code paths, not
language understanding.

Use the [inference validation process](inference-validation.md) to compare
actual model decisions on the same synthetic scenarios and fresh state. Keep
source archives, corpus/oracle fingerprints and all failed attempts. Cover
changed names and phrasing, multi-turn references, genuine ambiguity,
cross-source analysis, nonfinancial tables and unrelated pending work.
The [planner development corpus](../tests/fixtures/planner_semantics.json)
records twelve such scenarios, synthetic setup data and semantic expectations.
Accept equivalent SQL aliases, complete groupings and supported result
envelopes. A missing-period tool rejection before a source read can deliver
the same valid clarification as a direct question. Keep oracle corrections
separate from changes to model behavior and regrade received evidence offline.
Compare factual results and selected arguments before comparing tokens or
latency. A smaller prompt alone does not establish better generalization.

The grounded reply composer is a separate stage. Changing planner instructions
does not replace its output schema or weaken its factual checks. Development
evaluation does not publish source or deploy production.
