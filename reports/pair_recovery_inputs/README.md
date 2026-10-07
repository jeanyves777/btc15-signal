# Local recorded inputs for pair-recovery research

Exported on 2026-10-07 from the desktop databases using the branch exporter at 1091657, with --days 14. Source databases opened mode=ro. Production checkout and trading services were not changed. These are data exports, not backtest results.

- BTC: 598 trade rows; 95,474 quote rows across 1,322 windows; 46 outcome rows.
- GOLD: 61,958 quote rows across 793 windows; 1,236 outcome rows.
- No depth was included. Observation asks are recorded quotes, not proven executable fills.
- KXBTC15M-26SEP241000-00 contains buys on BOTH sides. The exporter aggregates the buys and emits one side; do not use this row as an ordinary directional entry. Retained unaltered for audit.
- BTC outcomes are incomplete in these exporter sources. Missing outcomes are unknown, not losses or wins. Match by exact ticker and report exclusions.
- The exporter uses outcome_ms=min(close_ms,last realised event) and falls back to close_ms if no event exists. These are derived timing fields, not proof of when settlement became known or funds became spendable. Avoid using them as verified causal trigger times.
- Trade counts/costs are sums of the locally available broker-fill rows and can be incomplete where fill history is missing. Preserve these limitations when comparing strategies.

Files are the exporter output without manual row changes. No credentials, private keys, or raw databases are included.
