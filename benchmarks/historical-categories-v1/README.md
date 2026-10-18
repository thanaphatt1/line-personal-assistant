# Historical category development results

These are predefined synthetic development cases, not held-out accuracy scores.
All Google, LINE and business-state effects are mocked. Raw model/usage/approval
records remain private. Existing frozen `benchmarks/v1` files are unchanged.

The final runtime archive is
`f42e75969310c0784e37bdf846681af37a458371255a8d87be49dd8b27c05bb0`.
It passed3,080offline tests (including54historical cases), source/wheel/signed
startup with zero external connections and13separate-process compatible OFF-bridge
cases with zero new mutations and raw originals preserved. Counts overlap.

## Fresh inference and the pagination correction

A fresh16-scenario/43-turn matrix on the preceding `8eb3b9a6` archive passed42/43
using45new requests and zero replay. All expense/Calendar regressions and both mixed
conversations passed. The failure was previous-page navigation: the second model
step answered in text instead of proposing categories, leaving an unclassified
page. No write occurred. The failure and costs are retained.

The final source explicitly selects `propose_historical_categories` for the
remaining step after a historical scan/navigation. The transport accepts that
choice only for an unclassified pending historical page with the existing exact
owner-bound reservation. This uses the existing two-call budget.

A compact fresh recheck on this corrected exact archive passed15/15turns using
18new requests and zero replay. It covers:

| Coverage | Fresh on corrected final archive |
| --- | --- |
| All-history scan, next/previous pages | Yes |
| Correction plus partial selection, separate permanent consent | Yes |
| Partial query-only consent, natural cancel | Yes |
| Historical query -> expense total -> saved expense edit -> all-day Calendar reschedule in one conversation | Yes |
| New expense/Calendar creation, stale source, ambiguity and invalid input | Fresh on preceding archive; final offline regressions pass |
| Explicit month/Buddhist year and old date range |3fresh turns on preceding archive; final offline regressions pass |

Exact confirmation controls make zero model calls. The preceding interpretation
and reviews above use fresh inference; the model cannot authorize writes.
The final recheck is15fresh turns, not a claim that all43larger-matrix turns or
period cases were rerun on the corrected archive. Both new corpora are checked in:
[`full fresh matrix`](../../tests/fixtures/historical_categories_fresh_final_development.json)
and [`corrected-source recheck`](../../tests/fixtures/historical_categories_final_recheck_development.json).

## Earlier evidence and cost

The original fresh run on `30c467f3` assessed28/28turns with32requests, then stopped
due to a mock-provider expense append tab-ID translation error. After fixing only
the mock,14scenarios/31turns passed using32recorded decisions plus1new Calendar
request. A later `8eb3b9a6` controller replay passed31/31with33recorded decisions
and0paid calls. Two fresh period scenarios on that archive passed3/3with4requests.
These replay results remain separate from fresh inference. They do not prove a
held-out/general classification rate or human style quality.

All100unique received requests, including incomplete/failed runs, estimate
USD0.028124510; conservative all-input-cache-write ceilingUSD0.113901250, unknown
holds0 and automatic retries0. The existing100-request local phase guard is now
reached. These estimates are not an invoice; earlier development scopes are separate.

No real historical scan/mapping write, production canary, image build, deployment
or actual traffic rollback occurred in this development. Owner review, the external
writer operating constraint and a separately approved release remain necessary.

See [`results.json`](results.json) and the [feature contract](../../docs/historical-categories.md).
