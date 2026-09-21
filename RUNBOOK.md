# Runbook

Operating the live BTC 15-minute trading service.

## Stop trading, right now

Send **`/auto off`** in Telegram. It takes effect within one poll (~10s) and
works even between market windows. Nothing further is ordered.

It is unconditional: it does not depend on Kalshi being reachable, on the local
model running, or on the service being healthy in any other respect.

To stop the service entirely instead:

```bash
taskkill //F //PID $(cat runtime/service.pid)
```

An open position is unaffected either way — it lives at Kalshi and settles at
expiry whether or not anything here is running.

## The three processes

| What | Command | Needed for |
|---|---|---|
| Trading service | `.venv/Scripts/pythonw.exe scripts/run_service.py` | everything |
| Book recorder | `.venv/Scripts/pythonw.exe -m btc15_signal.recorder --every 10` | LLM commentary, microstructure research |
| LLM brain | `D:\Kalshi\llm\llama-server.exe -m D:\Kalshi\llm\models\granite-4.2-3b-Q4_K_M.gguf --host 127.0.0.1 --port 8080 -c 4096 -t 6 --no-warmup` | commentary only |

Only the first is required to trade. The recorder and the brain are additive:
if either is down, trading is bit-for-bit unchanged and the only difference is
that no commentary arrives.

Start them detached, or they die with the shell that spawned them:

```powershell
Start-Process -FilePath ".venv\Scripts\pythonw.exe" -ArgumentList 'scripts\run_service.py' -WindowStyle Hidden
```

**Each service shows up as two PIDs.** The venv `pythonw.exe` is a launcher stub
that re-execs the base interpreter, so `wmic` lists a parent and a child for one
service. `runtime/service.pid` holds the real one. This is not a duplicate.

**Never delete `runtime/service.lock`.** It is the single-instance guard; a
second service would place duplicate orders. If the service will not start, it
is usually because one is already running — check `runtime/service.pid` first.

### Keeping it up overnight

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList 'scripts\watchdog.py' -WindowStyle Hidden
```

It restarts the service if it dies, messages Telegram when it does, and gives up
after 6 restarts in an hour rather than hiding a real fault behind a loop. It is
safe to loop because no trading limit lives in memory — see below.

## Telegram commands

| Command | Effect |
|---|---|
| `/auto` | status: on/off, size, limits, what is currently blocking |
| `/auto on` / `/auto off` | the kill switch; `off` always works |
| `/autosize 1` | dollars per automated order |
| `/size 1` | dollars per manual (button) order |
| `/status` | execution readiness and the entry window |
| `/ledger` | every real trade with its running balance |
| `/intel` | what each gate turned down, in edge per contract |
| `/sessions` | live record by session, beside the measured figure |
| `/id` | your Telegram user id (the only unauthenticated command) |

## What stops it losing money

All of these are read from the database on **every** decision, never held in
memory, so a crash and restart cannot reset one that has already been breached.

| Guard | Default | Where |
|---|---|---|
| Daily loss floor | −$10.00 | `auto_daily_loss_limit` |
| Trades per day | 96 | `auto_max_trades_per_day` |
| Trades per hour | 6 | `auto_max_trades_per_hour` |
| Seconds between orders | 120 | `auto_min_seconds_between` |
| Open positions | 1 at a time | hard-coded |
| Order size | $1.00 | `/autosize` |
| Entry attempts per window | 3 | `auto_retry_limit` |
| Price drift before abandoning a retry | $0.08 | `auto_retry_max_drift` |
| Slippage allowance on an entry | $0.01 | `entry_slippage` |

**Alerting fires once per window; trading is evaluated on EVERY poll.** These
were one gate, and it made 67% of qualifying windows unreachable (see
FINDINGS.md §3). If you ever merge them again you will lose two thirds of the
opportunity and it will look like the strategy got worse.

A retry needs ALL of: previous attempt terminally `unfilled`, no open position,
the whole rule still matching at the new price, edge still positive after the
fee, drift within `auto_retry_max_drift`, and `attempt < auto_retry_limit`.

**Two switches must both be on to trade unattended:** `enabled: true` in
`strategy.json`, and `/auto on`. Turning off either one stops automation.

Orders are immediate-or-cancel, so nothing ever rests on the book unfilled.
Exits are reduce-only, so a stale count can never open a position the other way.

## Reading the money

Three different numbers appear, and they are not interchangeable. Conflating
them is what once reported -$3.91 on a night whose real loss was $0.85.

```
🎯 82% win rate · 9W-2L · 11 signals      <- were the calls right? (all signals)
▰▰▰▰▰▰▰▰▱▱ paper -0.06 on 1 contract      <- hypothetical, NOT money
💸 Live: -0.85 · 1 trade (0W-1L)          <- the account. This is the real one.
```

* **Signals / paper** covers every settled signal, including the great majority
  nobody ever placed an order for, priced at a stated basis. It measures the
  strategy, not the account.
* **Live** comes from `Store.realised_record`: the count actually filled, the
  price actually paid, the fee actually charged. A position sold early is scored
  at what it sold for, not at who eventually won.

One function, `store.position_pnl`, computes every realised figure. The Telegram
header, the settlement report, the daily loss floor and the dashboard all call
it, so they cannot disagree about the same trade. They previously each had their
own copy and did. **If you add a fifth surface, call that function.**

`status` matters: `filled`, `protected` and `unprotected` all mean contracts are
held (`HELD_STATUSES` in store.py). `unprotected` means the entry filled but the
take-profit did not - the most important one not to lose track of. A partial
early exit stays `filled`, because contracts are still at risk.

A money figure marked **"Priced at the posted limit"** means the fill was never
read back from Kalshi; the real cost was that or lower. Check the Kalshi ticket.

## Reading the log

`runtime/service.log`. One line per window says what automation decided:

```
auto[KXBTC15M-26SEP202315-15 UP@0.87 625s]: eligible
auto[KXBTC15M-26SEP202330-30 DOWN@0.91 500s]: rule: distance(1.2x vol vs >=3.0)
auto: declined KXBTC15M-... - 1 position(s) already open
auto-exit[KXBTC15M-... UP]: exited - Sold 1 at 41%
```

A night with no trades should still show one `auto[...]` line per window. **If
those lines stop, the service is not running** — that is the distinction the
line exists to make, since "no trades" and "crashed" otherwise look identical.

Errors appear as `cycle error: ...`. A `429 Too Many Requests` is Kalshi rate
limiting; the loop retries on the next poll and loses ~10 seconds.

## The research archive

`observations` in `btc15.db` holds one row per poll for the whole of every
window - before the entry scan opens, through it, past it, and down to
settlement - whether or not the rule liked the setup and whether or not anything
was alerted. That is deliberate: a strategy we do not already have will not be
found in the minutes we already trade.

Each row carries the market and book state, the regime, our own position and
unrealised P&L at that moment, the rule's verdict with every failed gate, and
four identifiers:

| Id | Meaning |
|---|---|
| `session_id` | one service run. Changes on restart **on purpose** - it distinguishes "quiet because nothing happened" from "quiet because the process died" |
| `market_id` | the Kalshi ticker |
| `signal_id` | one opportunity. **Derived** from the ticker, so a restart mid-window does not split a path in two |
| `proposal_id` | the order, once one exists |

Read it through **`Store.research_rows()`**, never with a raw SELECT.

### The legacy boundary — do not infer across it

Rows written before the linkage columns existed carry a NULL `signal_id`. They
are real observations and worth keeping, but no lifecycle can be reconstructed
from them, and **they must never be pooled with linked rows** - that would
compare reconstructable paths against loose rows from the same period.

`observation_coverage()` reports the boundary explicitly:

```
linked_rows      21      <- usable for lifecycle work
legacy_rows      31      <- describable only
research_from_ms ...     <- first fully linked observation
```

`research_rows()` excludes legacy rows, and excludes unsettled rows unless asked
otherwise. `lifecycle_by_signal()` cannot return a legacy row at all. Any
conclusion drawn about a period earlier than `research_from_ms` is about loose
observations, not about lifecycles.

## The intelligence layer

`decision.py` computes **every** comparison, ratio, threshold and verdict, and
hands the model finished plain-English phrases. The model writes prose and
nothing else.

This is not stylistic. Given raw depths the model stated that 53,883 was deeper
than 59,779 - both figures real, the comparison false, and the number guard
cannot catch that because nothing was invented. Given the nested fact dict it
recited field names (`spread_bps is 0.0`) and stated one fact twice in two
dialects as though they disagreed. **If the model can see two numbers side by
side it will eventually compare them, and eventually get one wrong.** Never
supply the pair; supply the conclusion.

Guards on the reply, in order: scaffolding stripped (`Sentence 1:` labels),
truncated replies dropped, and any number absent from the facts rejects the
whole reply.

Commentary is once per window, fire-and-forget, and can never gate or delay a
trade.

## The hourly ladder (KXBTCD) — shadow only

A second instrument, recording since 2026-09-21. **It does not trade and there
is no code path by which it can.** `hourly_trading_enabled` is read by nothing;
it exists so that turning hourly trading on has to be a deliberate code change,
not a config flag someone flips at 2am.

### What it is

`KXBTCD` is **not daily** — the D is misleading. Its events are hourly:
`KXBTCD-26SEP2112` opens 15:00 UTC and closes 16:00 UTC. One event is a ladder
of ~188 "or above" thresholds $100 apart, all settling on the same BRTI print.
Typically ~24 of the 188 are quotable at once; the rest sit pinned at 0.00/0.01
or 0.99/1.00 and are the exchange saying the outcome is already decided.

`KXBTC` is the *same* expiry expressed as ~186 mutually exclusive $100
brackets. Different instrument. Not handled.

Liquidity is far better than the 15-minute market: the rung nearest spot showed
93,083 contracts of volume against a few hundred on a typical 15-minute
contract.

### Where it lives

| Thing | Where |
|---|---|
| chain parse + integrity | `src/btc15_signal/hourly.py` |
| archive | `src/btc15_signal/hourly_store.py` |
| shadow recorder | `src/btc15_signal/hourly_shadow.py` |
| database | `runtime/hourly.db` — **separate file on purpose** |
| history fetcher | `scripts/fetch_hourly.py` |

The database is separate from `btc15.db` so a research experiment can never
hold a write lock on, or corrupt, the database that knows what money is at
risk. Nothing on the 15-minute trading path imports any of these modules.

The recorder is called before the 15-minute market lookup, because that block
`continue`s between windows and would otherwise stop the archive for the length
of every gap. Both its entry points swallow their own exceptions: a failed
research write must never interrupt settlement reporting or order placement.

### Reading the archive

```powershell
.venv\Scripts\python.exe -c "import sqlite3; d=sqlite3.connect('runtime/hourly.db'); d.row_factory=sqlite3.Row; print(dict(d.execute('SELECT COUNT(*) n, COUNT(DISTINCT chain_id) chains, SUM(integrity_ok) clean FROM hourly_chains').fetchone()))"
```

`hourly_chains` is one row per poll, `hourly_strikes` one row per rung per poll
(only rungs within `hourly_archive_window` of spot — `n_strikes` vs
`n_archived` on the chain row says how many were left out, so the trimming is
never silent), `hourly_settlements` one row per event.

**Every row carries `mode='shadow'`.** If hourly ever trades, live rows must
stay distinguishable from shadow rows forever after. This is the same boundary
lesson as the legacy null `signal_id` rows — do not infer across it.

### Two traps already paid for

**`updated_time` is not a quote clock.** All 188 rungs carry one of three
timestamps stamped at chain open, and they never move. Measured: 13 rungs
changed their quotes inside 20 seconds while not one timestamp changed. Using
it for freshness marks *every* snapshot stale. Staleness is measured across
polls instead — how long since any quotable rung's quote last moved — which is
why `check_integrity` takes `quotes_age_s` from the caller rather than working
it out itself.

**Do not pick the strike with the best edge.** The ladder offers ~24 quotable,
heavily correlated estimates at once, and taking the maximum over them selects
wherever the model is most wrong, not wherever the edge is most real. This is
the grid-search failure in FINDINGS.md §7 with a new face: the best of 11,365
searched rules scored below the *median* best rule on shuffled data. Strike
selection must be a pre-registered rule measured in advance — "the rung nearest
N volatility units from spot", say — and never an argmax over the live chain.

## Known limits

- **`bid_imbalance` is hard-coded to 0.0** in backtests. It has never been
  validated and nothing should be concluded from it.
- **The Kalshi `orderbook_fp` book-to-quote mapping is unresolved.** Observed
  offsets range 1–10¢ and flip sign between runs. Depth-derived features are
  blocked until this is settled from recorded data.
- **GPU offload does not work on this machine.** The Vulkan llama.cpp build
  access-violates on the Radeon HD 7800 at any `-ngl`, and crashes on inference
  even at `-ngl 0`. Use the CPU build in `D:\Kalshi\llm\`.
- **The sample is far too small to prove anything.** The measured edge is about
  2c per $1 trade; the dashboard prints how many trades would actually be needed
  to detect it. A losing night is entirely consistent with the strategy working.

## Security

`KALSHI_PRIVATE_KEY_PATH` points at a plaintext RSA private key outside the
repo. It is not encrypted and not permission-restricted. Anyone with read access
to that file can trade this account. Rotating it and tightening its ACL is
outstanding.
