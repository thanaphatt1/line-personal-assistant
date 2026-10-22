# Relational analysis from observed data

`query_relations` is the raw-history planner's main composable analysis layer,
using read-only SQLite over observed records. Typed source tools and legacy
operations remain useful for simple direct reads; broader questions
can compose SELECT, CTEs, joins, EXISTS, CASE, HAVING, UNION, subqueries and
window functions without adding a handler for each question or table.

1. The planner reads necessary configured sources with
   `continue_analysis=true`. Expense reads use `view=list` for raw rows.
2. `relations_catalog` exposes current-request query references, SQL table
   names, observed columns/types/units, row counts, scope and unavailable fields.
   Additional tables derive their schema from registry metadata.
3. The planner proposes SQL over selected references. The server loads their
   complete matched raw records into an isolated in-memory database and runs
   the query. Display limits do not restrict calculations.
4. The computed result can answer directly, or become another current-request
   relation for further analysis. Model prose cannot replace computed results.

For example, grouping payments in a CTE and applying
`SUM(total) OVER (ORDER BY date)` gives daily running totals. Joining people
with income by a declared reference column requires no people/income-specific
query function. Conditional activity spending can use EXISTS with a payment
date between each observed event's Bangkok `start_date` and `last_date`;
several overlapping events then do not multiply the same payment. This proves
spending on those days, not that the event caused the expense.

Relations contain the raw rows selected by their source read, even if that
read displayed grouped summaries. Missing rows or unselected columns require
a new read. Incomplete or ambiguous source selections cannot establish totals.
Calendar date spans are clipped to the period actually read. Truncated event
details cannot determine membership through SQL filters. Expense records have
snapshot-only identity; equal purchases must not be deduplicated by value.
SQL has ordinary bag/join semantics: the model must choose the intended
join, overlapping-read and coverage semantics. Execution verifies sources
and arithmetic, not whether a proposed SQL statement expresses the owner intent.

Decimal columns are text with numeric `COLLATE decimal`. SUM/decimal_sum,
including window sums, use decimal arithmetic. Derived decimal aliases need
explicit `COLLATE decimal` when sorting. `decimal_add`, `decimal_sub`,
`decimal_mul`, `decimal_cmp` and `decimal_div(a,b,places)` avoid implicit
binary64 arithmetic; division rounds half-even to an explicit number of
places. Fractional constants in decimal helpers are quoted strings. AVG and
TOTAL are unavailable; an average is `decimal_div(SUM(amount),COUNT(amount),2)`.
Floating output is rejected. Native SQL casts/functions/arithmetic still have
SQLite semantics, so decimal helpers are required inside filters and derived
expressions too; this is not a static SQL type checker. SQLite documents the
[floating point precision limits](https://www.sqlite.org/floatingpoint.html).

Every invocation uses a new in-memory connection. Extensions are disabled.
The [authorizer](https://www.sqlite.org/c3ref/set_authorizer.html) permits only
reads of selected materialized tables, SELECT/recursive plans and supported
scalar/aggregate/window functions. Provider identifiers, record versions,
filesystem paths and credentials are never SQL bindings. No SQL can change a
provider, create a review, confirm, grant access or escape the read-only loop.
Mutation workflows retain their separate review and consent requirements.

Resource bounds on VM work/time, expression size and result bytes return a
failure instead of a partial total. A failed query can be repaired against
the actual catalog/schemas using the existing progress-based repair loop.
Unchanged diagnostics stop. An unresolved SQL failure cannot silently fall
back to reporting the original, different read result. Query references expire
when the owner turn ends. No new provider, dependency, access or collection is
required; the registry still controls which sources are available.
