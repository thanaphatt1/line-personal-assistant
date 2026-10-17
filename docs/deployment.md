# Deployment and rollback

The current runtime composition is `line_assistant.history_cloud_run:app_factory`. The production container definition is `deploy/history-production.Dockerfile`; the root Dockerfile retains the older factory for compatibility. A source checkout is not the immutable image currently serving an existing deployment.

## Prepare

1. Run the full offline suite and deterministic source packaging checks.
2. Select your own project, region, attached account, state database, reviewed Sheet schema, secondary calendar and single LINE owner. Configure nonsecret values from [configuration](configuration.md); provision version-pinned mounted secrets separately.
3. Keep the three integration secrets outside the repository and build context. Review least-privilege access and the LINE channel's webhook configuration separately. No setup command here changes IAM or creates live records.
4. Review an explicit build/release budget. The generic `deploy/cloudbuild.json` requires an operator-selected `_IMAGE`, reviewed tag and checksum substitution. Builder and Python base image digests are pinned; update them deliberately after reviewing and testing dependency changes.

The application is disabled by default. Do not enable a real transport using synthetic example targets or treat `PILOT_SHEET_SCHEMA_VERIFIED=true` as a substitute for inspecting the chosen workbook. No default owner, production project, spreadsheet or calendar is supplied.

## Build and stage

Bind the reviewed source checksum to an immutable source generation and build one immutable image. Verify successful input checksum checks, source provenance, pinned base/builder and resulting image digest. Resolve the placeholder image path and use your own explicitly chosen build service account; do not commit a private deployment manifest.

Create an OFF bridge and ON candidate from the same new image. Both retain history/state. The bridge disables Calendar and response realization; the candidate enables only approved features. Keep the prior runtime at 100% until both zero-traffic revisions pass:

- Ready, exact image/configuration and reviewed CPU/memory/concurrency/scaling bounds;
- signed startup with zero provider calls;
- `/health` success and invalid LINE signature rejection;
- targeted sanitized error checks;
- unchanged reviewed access/TTL policy and idle owner-lease metadata where required.

The history container runs `scripts/verify_history_signed_startup.py` before starting the real server. Its test transport rejects provider calls, its test events are empty/nonowner, and secret values are never printed. A startup pass does not prove a real owner Calendar or expense write.

Promote only after checking current configuration, state compatibility and the release budget again. Preserve durable receipts and uncertain effects. Do not use replayed owner confirmations as a deployment smoke test.

## Rollback

A pending all-day draft can already write the newer Calendar history format. Before exposing a candidate, verify a bridge image that reads both that format and retained timed state. Once the candidate can touch state, the old image may be incompatible even before a traffic promotion.

Rollback changes traffic to the compatible OFF bridge. It never imports an old snapshot, clears history, resets counters, retries an uncertain write or deletes events. Zero minimum instances means a bridge may require a cold start. Traffic rollback cannot retract an event already created in an external calendar.

This repository intentionally excludes deployment executors, production manifests, approval records, real account state and operational budgets. Keep those private and bind any release automation to the exact reviewed source, image, plan and authorization.
