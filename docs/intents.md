# Read intents and checked responses

The read workflow uses one `act` entry point with a tagged `request`:

- `read`: declare sources, aliases, SQL and original owner source references.
  The server fetches and computes using the existing typed readers and SQL
  executor. The planner does not choose the internal sequence or continuation
  flags. `sql=null` permits exploration first.
- `inspect`: discover configured source capabilities and observed coverage.
  Inspection never creates access or connects another service.
- `respond`: review an offered result against the owner conditions and compose
  its grounded answer in the same planner call. This intent is offered only
  when response realization is enabled and a reportable complete result fits
  the bounded response context.

For example, the first call can submit:

```json
{
  "request": {
    "intent": "read",
    "sources": [{
      "source": "expense",
      "alias": "e",
      "parameters": {
        "date_mode": "previous",
        "start_date": null,
        "end_date": null,
        "period_text": null,
        "relative_offset": null
      }
    }],
    "sql": "SELECT SUM(amount) AS total FROM e WHERE item='Rail'",
    "source_refs": ["an actual visible owner source ref"]
  }
}
```

The next request includes `read_responses`: current response references with
verified facts, record cells, units, source coverage and executed SQL evidence.
The planner can correct its read plan if those results do not answer the owner.
Otherwise it submits `intent=respond`, the actual `response_ref` and a document
over fact/cell placeholders. Result selection and presentation happen together;
there is no model-supplied self-review flag or duplicate owner-source argument.
No nested planning model or auxiliary reply-model call is introduced.

The server rebuilds the selected core result and binds its response reference to
the owner, active event, query result, rendered facts and execution evidence.
Previous-turn, foreign or changed response references fail. Documents cannot
copy amounts, invent facts, drop required cells, reorder ranked rows or change
units. The existing answer renderer and repeat/deadline/repair guards remain.
An invalid received document uses a complete placeholder-only fallback through
the same renderer, without another model call or source read.

`finish_query_analysis` also omits the self-review object from its offered
schema. Selecting a response/report is the planner's decision; true booleans
cannot prove semantic correctness. Legacy review/source fields remain readable
for compatibility, and an explicitly negative/unresolved legacy review is
still rejected. Neither a simplified schema nor a valid document proves that
the SQL matches every owner condition; independent outcome tests remain needed.

At most eight response candidates and 24,000 encoded context bytes are offered,
with recent candidates preferred. Large or nonreportable results retain the
existing finishing path. `finish_query_analysis` remains available for other
result combinations and clarification; its existing optional composer remains
a compatibility path. Generalizing read intents does not change write review,
confirmation, saved-record versioning, recovery or connection workflows.

For a complete result, the intended normal path is one read-planning call,
server execution, then one review-and-response call. Semantic Calendar
classification, discovery, uncertain selections and repairs can require more
work. Two calls are an available path, not an accuracy or latency guarantee.
