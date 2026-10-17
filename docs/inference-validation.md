# Synthetic inference validation

The focused [mapping-resolution development regression](../benchmarks/mapping-resolution-v2/README.md) separately records the newer identity/partial-result change. It uses only synthetic data and does not replace or tune the historical frozen v1 holdout. Its 54 turns are repeated development examples, not 54 independent unseen cases.

Offline scripted tool outputs test the code contract, not the model's understanding of language. The synthetic corpus in [`tests/fixtures/query_inference.json`](../tests/fixtures/query_inference.json) defines separate real-inference scenarios, independent repetition counts and expected results. It contains no owner records or private event content.

A separately authorized runner must bind the exact reviewed runtime archive and use the signed history factory with injected Google, LINE and state mocks. Only the selected model request may leave the machine. Keep the existing model, request timeout, output limits, call budget and response validator; extending development timeouts would hide a production fallback. Never point an evaluation at a live workbook or calendar.

For every independent repetition, start fresh synthetic state and advance the declared Bangkok clock. Preserve both sides of the conversation across turns and cold factory restarts. Check:

- the chosen typed tool, explicit dates or calendar frame, catalog filter, total/list mode, complete result, amount and count;
- carry-over for a real follow-up and clearing filters for a new question;
- honest clarification for a missing/ambiguous period or unsupported filter, and distinct provider/schema/mapping failures;
- all-day exclusive ends, timed offsets, pagination and long-list disclosure;
- no provider write on a query or preview, no old consent consumed after a read;
- exact saved expense names, amounts and dates, and exact created event titles and boundaries after explicit confirmation;
- grounded Thai wording, factual fallback, and at most two model calls per owner turn.

Treat a correct factual fallback separately from a semantic or business-effect failure. Record its reason and wording success rate; do not label it a successful LLM realization. API schema rejection is a test failure, even when a safe fallback reaches the user.

Before transmitting, record the source fingerprint, corpus/oracle fingerprint and request bound. Retain every original failure, independent repetition and manual rerun. For received responses record unique response identity and usage; preserve conservative holds when usage is unavailable and stop that run rather than automatically retrying an uncertain request. Estimates are not invoices.

Credentials, response traces, usage/approval records and release receipts remain outside this public checkout. The offline CI workflow never calls a paid model or deploys the application. Passing development evaluation does not authorize a build or production release.
