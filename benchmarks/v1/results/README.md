# Luna role benchmark v1

This is an initial **synthetic held-out baseline**, not a universal accuracy claim. Model-facing files, labels and config were frozen at [c4b594c](https://github.com/thanaphatt1/line-personal-assistant/commit/c4b594cebf7b558e9e80de04988f2f76fe1fe983). Only `gpt-6-luna` was evaluated. The runtime archive is `a8299562e8be1776342d5e1f11ed258b18c3cdd3906507928785e4d3ce791581`.

The query set has **22 scenarios, three independent repetitions each, 81 total turns**. The critic set has **12 scenarios × three repetitions = 36 trials**. Synthetic oracle labels were predefined by Codex; no independent human adjudication or human naturalness score is claimed. Development failures were retained separately and were not mixed into held-out accuracy.

| Role / measure | Result |
| --- | --- |
| Planner: read-tool selection | 63/63 |
| Planner: argument acceptance / normalized dates | 54/54 each |
| Planner: normalized expense filter | 42/42 |
| Style: actual responses replayed through the frozen exact validator | 81/81, matching delivered mock LINE text |
| Style: naturalness / conciseness | Human ratings pending; [20-sample review pack](human-review-sample.json) |
| Critic: corrupt text rejected / raw valid text accepted | 24/24 / 12/12 |
| Critic: critical false accepts | 0/9 observed |
| Critic: valid-output rate | 36/36 |
| Critic: valid input fell back at the two-second gate deadline | 1/12 |

Raw critic precision and recall were 100% on this small set; their descriptive 95% Wilson interval is **86.2–100%**. Zero false accepts among nine critical trials has an upper interval bound near **29.9%**, so it does not prove zero risk. Repetitions within a scenario are correlated. The corpus literal-span baseline missed three corrupted examples; adding the critic rejected those. That baseline is weaker than production's strict slot renderer, so this is **not** evidence that the production renderer would have accepted those errors.

## End-to-end comparison

| Measure | Planner + style | Planner + style + development critic |
| --- | --- | --- |
| Semantic success | 81/81 (100%) | 80/81 (98.8%) |
| Descriptive 95% Wilson interval | 95.5–100% | 93.3–99.8% |
| Observed critical business-effect errors | 0 | 0 |
| Latency p50 / p95 | 3.33s / 4.70s | 4.33s / 5.80s |
| Model requests / received usage | 156 / 156 | 236 / 235 |
| Received-usage estimate | $0.01555 | $0.01672 plus $0.001658 unknown-request hold |
| Received estimate per successful turn | $0.000192 | $0.000209, excluding the hold |
| Automatic request retries | 0 | 0 |

The three-call cohort had one planner intent error, which the critic did not fix: the critic checks wording against the selected authoritative result, not whether the planner selected the owner's intended query. One incomplete provider style response used a trusted fallback and stopped its run; only unexecuted cases were continued, and its usage hold remains. The 80 completed critic gates in this cohort passed; no duplicate critic calls are included in the scored cohort.

The standalone critic's foreground latency p50/p95 was **1.28s / 2.01s**. Measured composed p95 stayed below the predefined 20s development threshold and the application's 30s RPC envelope; the code's LINE token cutoff is 55s. Google/LINE/state services were mocked, so these measurements do not establish a live delivery SLO. The production factory remains at two calls: the third critic request exists only in the explicitly enabled development adapter.

Actual cache rates differed between cohorts. The [machine-readable scorecard](scorecard.json) reports cache-read/cache-write tokens, unknown holds, per-role latency/cost and uncached-normalized estimates; do not interpret the small observed cost difference as a controlled causal estimate. Estimates are not invoices.

## Reproducibility and limits

[Protocol](../../PROTOCOL.md), [config and thresholds](../config.json), [freeze hashes](../freeze.json), [query labels](../evaluation/queries.json), and [critic labels](../evaluation/critic.json) are public. Operational requests, responses, credentials, approval records and cost receipts remain private.

Two execution-only adapter fixes are recorded in [amendment 1](../adapter-fix-01.json) and [amendment 2](../adapter-fix-02.json). The first setup failed before any model call. The second composed experiment incorrectly finalized a result twice and is excluded from model accuracy; its costs remain retained. No frozen model prompts, labels or model config were changed for either repair.

Observed automatic thresholds passed on the scored cohorts. **Human style review remains pending.** A same-model critic can share writer errors and is not proof of correctness; its incremental benefit over the existing exact renderer is not established by this small experiment. Keep the critic as a development prototype until its production reservation, timeout, receipt and once-per-event integration is separately reviewed. Passing this benchmark does not authorize a production build or deployment.

Future model choices require explicit model IDs, prices and authorization. Reuse v1 for historical comparison; if its failures inform prompt/config tuning, move that data to development and create a fresh hold-out version for final selection.
