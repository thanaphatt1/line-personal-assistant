# Historical category development results

This is a predefined synthetic development matrix, not a held-out accuracy score.
The corpus is [`historical_categories_development.json`](../../tests/fixtures/historical_categories_development.json);
additional period cases are [`historical_categories_periods_development.json`](../../tests/fixtures/historical_categories_periods_development.json).
All Google, LINE and business-state effects are mocked. Raw model/usage/approval
records remain private.

The first fresh run assessed28/28turns before a mock-provider tab-ID translation
error stopped the remaining regression sequence. It used32received Luna requests;
this incomplete run is retained and is not relabelled as a full pass.

After fixing only the mock, the complete14-scenario/31-turn sequence passed using
32recorded Luna decisions and one new request for the previously unrun Calendar
case. No paid calls were repeated merely to increase the count.

A subsequent server-only correction revalidates the original source when a
confirmed but not-yet-attempted Action resumes after a crash. Model instructions,
tool schemas, context, grouping, proposals and rendered reviews are unchanged;
that invariance was checked. The final exact artifact then passed31/31turns by
offline replay of all33recorded decisions. This is replay-assisted controller
validation, not fresh inference on31new turns. Two additional fresh period
scenarios on the final artifact passed3/3turns using4requests.

The final runtime archive is
`8eb3b9a665751e0e2591a3054b178d884f9acc90f8e9c494c1cc2fbdb198d4e6`.
It passed3,080offline tests (including54new historical cases), source/wheel/signed
startup with zero external connections and13separate-process compatible OFF-bridge
cases with zero new mutations and raw originals preserved. Test counts overlap;
do not add the54subset to3,080.

All37unique received model requests, including the incomplete run, total an
estimatedUSD0.011249650; conservative all-input-cache-write ceilingUSD0.041598625,
unknown holds0, automatic retries0. These are estimates, not an invoice. Existing
frozen v1 benchmark files were not changed. No real historical scan/mapping write,
production canary, image build, deployment or actual traffic rollback occurred in
this development. Owner review, the external-writer operating constraint and a
separately approved release remain necessary.

See [`results.json`](results.json) and the [feature contract](../../docs/historical-categories.md).
