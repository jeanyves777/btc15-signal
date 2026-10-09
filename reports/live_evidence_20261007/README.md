# Actual live strategy and recorded evidence for cloud testing

## Code identity

Production BTC process PID 18388 started October 5, 2026 at approximately 14:30 New York. Its startup log reports source fingerprint **100c3efebc28**. The desktop package files produced the SAME fingerprint at export. Source snapshot commit: **ec876943f20cdd1888e1580d42a8b8f7baf28d0e**. This cloud branch already contains that source; the difference under src is the added pair_recovery_backtest module. The startup commit label cb19a35/DIRTY predates the snapshot commit; use source content, not that old commit label, to identify deployment. Fingerprints include file bytes, so Git line-ending conversion may change them on Linux.

## Inputs and scope

- `settings.sanitized.json`: Settings resolved from the production .env, with credentials, chat IDs, account labels and local paths excluded. This is NOT the class defaults. The file's modification time precedes process startup. This supports configuration continuity but does not directly inspect the process's in-memory settings or inherited environment overrides.
- Each instrument directory contains the full available signal/observation, prediction/outcome, execution/fill, all-signal lifecycle, recovery, capital, and learning evidence from that instrument's database. It is not limited to 14 days. `manifest.json` lists precisely which tables exist and their counts; missing tables/history were not fabricated.
- `settings.csv.gz` contains numeric database switches and state. These may override .env values. Text notification state and chat records are not exported.
- `daily_profit/profit_days.csv.gz` contains opening capital, daily targets, stake, realized profit and cap state for primary and mirrors.
- `mirror.jsonl.gz` preserves available mirror dispatch/fill/exit history. It is not a complete mirror broker ledger or a reconstruction of missing mirror fills.
- `hourly/` contains the recorded ladder snapshots and settlements. `settlement_reference/` contains recorded reference observations/features, feed gaps and reconciliation; raw second_bars are not included.
- `cache_*/markets.csv.gz` preserves available market metadata/results without turning unresolved outcomes into wins or losses.

Each SQLite database was opened mode=ro with query_only enabled, and exported in one read transaction. Different database snapshots are not atomic with one another: read the per-database snapshot timestamps. NULL is represented by the literal `\N` in CSV. Schema definitions are in each directory's schema.json. Each payload has SHA-256 in the manifest.

## Faithful testing requirements

Read the actual implementation in `main.py`, `daily_profit.py`, `mirror.py`, `execution.py`, `store.py`, and `tests/test_new_setup_1005.py` before reproducing it. Also read FINDINGS sections 152–167.

The current setup is BTC-only: primary $25 base, $30 for two taken trades after a known loss, stop at its 8% daily target; no primary $10 phase. Wife/George have their own day stakes and targets, and +$1 during the shared after-loss window. Affoue has $6/$8/$3 sizing and continues after the primary stops. Use the exported settings and real functions rather than these prose labels as the executable specification. Cash-out is disabled in the current setup. The 93-cent chase, 5-bps cushion, after-two-loss trend skip, copied-row accounting, midnight boundaries and fee treatment all matter.

Keep TWO distinct questions separate:

1. Historical reproduction: replay the recorded actual fills, sizes, exits and broker outcomes under the settings that existed THEN. Settings changed during the archive; today's settings are not historical settings for every date.
2. Current-rule counterfactual: apply today's actual code/settings to recorded signals with information available at each decision time. Clearly label this modeled, never actual account P&L.

Match signal -> direction -> ticker/window -> submission/retry -> broker fill -> exit/settlement. Preserve all statuses: paused, skipped, unfilled, copied, filled. A copied trade is not primary money. Unknown/unmatched outcomes remain unknown. Fill history can be incomplete: do not invent fill timestamps, contract counts or missing fills. Recorded market quotes may lag the book and do not prove size availability. Hourly hypothetical fills must remain quote-based/unverified. Use broker-reported net P&L as recorded; do not deduct fees twice or reconstruct broker truth from incomplete fills.

## Safe execution

This bundle contains no credentials or production databases. Use offline test copies. Do not run the service entry point, restore production credentials, connect to the trading API, or automatically deploy the cloud experiment. The live-settings snapshot describes production; it is not a safe service-launch configuration. Nothing in this export changes or restarts production.

Read a table with Python's gzip + csv modules. Use the manifest to report coverage and exclusions before giving performance results. The older reports/pair_recovery_inputs export is a reduced convenience format with documented limitations; this bundle is the richer evidence source.
