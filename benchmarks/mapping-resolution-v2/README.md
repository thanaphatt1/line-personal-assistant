# Mapping-resolution development regression

This focused **development** corpus validates the actionable-mapping change at [b763585](https://github.com/thanaphatt1/line-personal-assistant/commit/b7635853bbac3780d68edad62650387901194595), runtime archive `361a61d375705cdf7de853ca552c2e26d4a156187b6e2c4839f18d1b94d82f11`. It is not a new held-out accuracy estimate and does not replace the frozen [v1 benchmark](../v1/results/README.md). Its fixtures are synthetic; owner-derived investigation records were never included in a model request or public source.

[Corpus](../../tests/fixtures/query_mapping_development_v2.json), [machine-readable results](results.json), [query behavior](../../docs/read-queries.md), and [runtime diagram](../../docs/system-flow.md#4-stored-data-queries-llm-chooses-scope-code-reads-and-calculates).

The run used **14 unique scenarios ×3 separate fixture episodes =42 episodes**, containing **18 turn templates ×3 =54 turns**. All 54 turns passed the predefined development assertions. Repetitions share scenarios and multi-turn steps share state, so 54 is not a count of independent cases. Assertions verify scope/date/filter/view, known partial subtotal and unresolved names, immutable partial disclosure, no read-caused business write, exact confirmed expense/Calendar effects and at most two model requests per owner turn. No model judge supplies the labels or a human style score.

Coverage includes unique spacing identity, missing names, zero known subtotal, exact priority versus spacing collisions, typo nonidentity, blank category, clearing a failed category query's filter, unfiltered totals, provider failure, stored instruction injection, expense confirmation and mixed timed/all-day Calendar creation. Partial results retain `complete=false`, omit a complete top-level total, show unresolved items and cannot describe the known subtotal as the complete category total.

Only `gpt-6-luna` inference was real. Google/LINE/state services were mocked; no production expense/Calendar write, GCP build or deployment occurred. Production timing, independent naturalness and general unseen-language accuracy are not established. Source/wheel/signed startup checks passed with zero external startup connections, and 2,928 offline tests passed.

| Role | Requests / received usage | Received estimate |
| --- | --- | --- |
| Query planner | 48 / 48 | USD 0.005420955 |
| Response style | 54 / 54 | USD 0.004530785 |
| Total | 102 / 102 | **USD 0.009951740** |

Measured end-to-end p50/p95 was 3.11/4.24 seconds with mocked provider services. There were zero automatic retries and zero uncertain usage holds. The conservative received all-input-cache-write ceiling isUSD 0.039493125. These are usage estimates, not invoices. All operational requests, responses, credential handling, approvals and usage receipts remain private. The result JSON includes three explicitly synthetic delivered examples, not private live findings.

The corpus and oracles were recorded before inference. No source change occurred during the run. This development result is reported as such; if these failures or examples inform future tuning, a separate fresh frozen held-out split is needed for any final selection claim. The original v1 frozen files and their source archive remain unchanged. A source push, CI result or this validation does not authorize a production deployment.
