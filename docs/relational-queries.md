# Relational analysis from observed data

`query_relations` is the raw-history planner's main composable analysis layer,
using read-only SQLite over observed records. Typed source tools and legacy
operations remain available; expense planner tools always retrieve a complete
source before selecting the final answer. Broader questions
can compose SELECT, CTEs, joins, EXISTS, CASE, HAVING, UNION, subqueries and
window functions without adding a handler for each question or table.

Filtering uses SQL, including a single named item or category: read the full
requested period with `filter=null`, `view=list`, `continue_analysis=true`, then
express the conditions in `WHERE`. Resolve the requested label using catalog
meanings and categorical choices in source metadata. Unresolved closed-choice
labels need clarification rather than guessed predicates and empty-total claims.
The retained typed filter supports positive equality for compatible direct
callers. No separate exclusion action or
list of natural-language keyword handlers is needed. The model chooses SQL
predicates such as `!=`, `NOT IN`, `NOT LIKE`, `AND`, `OR` and subqueries.
For example, excluding a known device purchase can use
`WHERE lower(item) != lower('Tablet Aurora')`. A total and its payment count
must use the same filtered relation; a displayed list can use window totals
over that relation before display truncation. Empty `SUM` is NULL in SQLite;
`COALESCE(SUM(amount),'0')` can express a verified empty total.

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
raw `item` plus `canonical_item`, `category` and `subcategory` from verified
current catalog identity/mappings. A mapping column missing for any input row
appears in `unavailable_fields`: the authorizer rejects reading that column,
including a negative predicate that would otherwise silently drop NULL rows.
Raw item/date/amount predicates remain available without guessing categories.
The model can repair its query or disclose missing mappings; a failed mapped
query cannot be replaced by an unconditional total.

Expense records have
snapshot-only identity; equal purchases must not be deduplicated by value.
SQL has ordinary bag/join semantics: the model must choose the intended
join, overlapping-read and coverage semantics. Execution verifies sources
and arithmetic, not whether a proposed SQL statement expresses the owner intent.

Result units come from source column metadata and conservative expression
lineage through aliases, CTEs, aggregates, windows and compatible branches.
Counts and ratios do not inherit currency units. Mixed or unresolved units stay
unknown. SQLGlot inspects a copy of the executed query for metadata only; SQLite
remains the executor and authorization boundary. An unsupported metadata
expression does not change the SQL result. Names such as `total`, `amount` or
`total_baht` cannot create a unit. Currency display removes zero satang but keeps
the full precision of computed values; other units and untyped text stay exact.
Mandatory source provenance, such as an owner-confirmed activity label used to
select dates, follows used relations through derived SQL results and remains
required in both composed answers and the core fallback.

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

A delivered SQL result based on one unambiguous source scope retains that
period, executed logical SQL, result columns/units and row count as follow-up
context. Source fetch controls (such as reading a full unfiltered list) stay
inside the analysis inputs and do not describe the delivered answer view.
Derived results
carry the original source scope too. Several distinct periods/sources do not
invent a single previous period. On a new turn, records and query references
are never reused: the planner must read fresh data and rebuild the intended
conditions against its new relation names. A new general question clears old
conditions. A complaint about selecting an excluded item is a correction to
the earlier intent, not consent to select only that item.

During a read-analysis continuation, the planner must choose a read tool or
`finish_query_analysis`. Its `clarify` operation asks one unresolved condition
question without promoting supporting input reads into a completed answer.
The question carries no business numbers or write authority. Ordinary final
reports still render verified result facts rather than model-generated totals.
A text-only response in this required-tool phase is a protocol failure; it
cannot cause supporting inputs to be automatically reported as the answer.
Source snapshots marked `analysis_input_only` require a SQL projection or
calculation before `report`; premature selection returns schema feedback to
the planner. The model still chooses all predicates and result columns.
Planner SQL calls continue into a review of the executed selection, output
shape and unresolved requested conditions before reporting. A failed review
returns to the same read-only repair loop. This adds a planner call for SQL
answers; it is model-based intent checking, not a proof that arbitrary SQL
captures every natural-language request.

Resource bounds on VM work/time, expression size and result bytes return a
failure instead of a partial total. A failed query can be repaired against
the actual catalog/schemas using the existing progress-based repair loop.
Unchanged diagnostics stop. An unresolved SQL failure cannot silently fall
back to reporting the original, different read result. Query references expire
when the owner turn ends. No new provider, access or collection is
required; the registry still controls which sources are available.
