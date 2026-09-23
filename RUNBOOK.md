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

### Deploying a change, and getting back

**The restart budget is spent silently.** The watchdog counts restarts in
memory, not in a file, so the count cannot be read back — assume every restart
in the last hour counts against 6. Batch the work and deploy ONCE; a live
trading service is not somewhere to iterate.

Before touching anything:

```powershell
# 1. Where we are now - this is the rollback point.
git rev-parse HEAD
# 2. The watchdog MUST be alive, or a kill leaves nothing running.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*watchdog.py*' }
# 3. Flat is the moment to do it: no open position, nothing resting.
```

Deploy by killing the service **tree** and letting the watchdog bring it back.
Kill the tree, not the child: killing only the child can leave the venv stub
holding the lock, and the relaunch then returns immediately against a lock held
by nothing, which reads as success while old code keeps trading.

```powershell
taskkill /PID <parent-pid> /T /F    # the watchdog relaunches within ~5s
```

Then verify by **timestamp and revision**, never by grepping the log for a
started line — a stale line from the previous restart matches and reports
success. The service prints its revision on startup:

```
BTC15 signal started; revision <sha> (main, clean) ...
```

Check that line's timestamp is from this restart, that `runtime/service.pid`
matches a process whose `CreationDate` is after the kill, and that exactly one
service tree exists.

**Rolling back** is the same move against the old revision. Nothing here
migrates: `btc15.db`, `runtime/` and `.env` are untouched by a checkout and are
all gitignored.

```powershell
git checkout <rollback-sha>
taskkill /PID <parent-pid> /T /F
```

If a deploy check fails and the cause is not obvious, `/auto off` in Telegram
stops trading within one poll and does not depend on the service being healthy.
That is the safe place to stand while working out what happened.

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
| `/learning` | frozen candidates, what each would change, and how many may touch an order (currently 0) |
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

## Continuous learning — running, and mostly activating nothing

The learning loop is **part of the service**, not a script. It refits the
intelligence policy on Kalshi-native features, evaluates the new fit against
the running one, and activates only what clears a bar that was fixed in
advance. Nothing about it needs a person, and nothing about it can place,
resize or block an order without one.

### What it does, and when

Driven off the service poll, immediately after the settlement sweep:

| trigger | when |
|---|---|
| `bootstrap` | no valid policy is active — nothing else matters |
| `settlements` | `learning_min_new_settlements` (24) new settled markets |
| `interval` | `learning_interval_ms` (6h) regardless |

The due-check is throttled to `learning_check_ms` (5 min) and the fit itself
runs in a worker thread with its own read-only connection, so neither the poll
loop nor an order ever waits on it.

### Reading it

    /learning                              in Telegram
    python scripts/learning_report.py      state + end-to-end traces
    python scripts/learning_report.py --window <window_open>   one market

`/learning` reports **four states, and they are four different claims**:

| state | means |
|---|---|
| Running | the loop is scheduled and alive |
| Updating | a fit is in progress right now |
| Adjusting confidence | at least one arm re-rates the displayed confidence |
| Authorised to affect execution | an arm may actually change an order |

A system can be running, updating and adjusting confidence while being
authorised to change nothing. **That is the normal state**, and reading those
four as one word is how "the learning loop is live" gets heard as "an
adjustment is trading".

### The tables — three records, deliberately not one

| table | answers |
|---|---|
| `learning_runs` | when training ran, and **what data it used** |
| `policy_activations` | when a policy **became active**, and what it replaced |
| `policy_withdrawals` | when an active arm was **taken back**, and what condemned it |
| `learning_state` | watermark, next due, last error — survives restarts |

They disagree on purpose. Most runs fit a policy that is never activated; that
is the loop working, not failing.

### The bar, which is not negotiable downward

    confidence    n >= 120, >= 2 days, day-clustered interval clear of zero
    execution     all of the above, PLUS validate n >= 40, train/validate sign
                  agreement, and a validation interval WIDENED by
                  sqrt(candidates examined) still clear of zero, and forward
                  evidence that does not contradict it

Confidence is not held to the multiplicity widening because it is not
*chosen*: a delta is computed for every eligible cell and which one is
consulted is decided by where the market puts the next signal. Execution IS
chosen — the policy keeps whichever cell points hardest — so it is corrected
for having been picked. Each run reports how many confidence arms would have
survived the stronger bar anyway.

### Two switches for execution, and neither is in the code

An execution change needs **evidence** (a promoted arm, from the bar above)
AND **authority** (`intelligence_mode` + `intelligence_authorised`, both set by
a person). `intelligence_policy.authorise()` applies them as separate gates and
records both outcomes: `final_action` is what took effect, `evidence_action` is
what the policy would have done with permission. Nothing in the code raises the
mode.

**Confidence rides on `intelligence_enabled`, not on the mode.** It moves a
label and is arithmetically incapable of admitting, refusing or resizing
anything; requiring the execution switch to see a calibration would mean
granting the power to trade on one in order to read it.

**No mode may ever change size.** `intel_mode.may_change_size` returns False
for every mode and a test asserts it for each.

### Failure, and what it does not touch

A failed run leaves the active policy exactly where it was, writes the error to
`learning_state`, and backs off exponentially from `learning_retry_ms` up to
the normal interval — it never becomes a hot loop and never becomes a
permanent stop. `/learning` shows the failure and says the last valid Kalshi
policy stayed active.

On startup the runner closes out any run a killed process left `running`, and
if the artefact on disk cannot act it **restores a valid rollback immediately**
rather than waiting for the next scheduled fit.

### Artefacts

    runtime/intelligence_policy.json              the live artefact
    runtime/intelligence_policy.rollback.json     the last valid one
    runtime/policies/<version>.json               every version ever live
    runtime/intelligence_candidates.json          the forward-evaluation set
    runtime/learning_report.txt                   the regenerated report

Activation is an `os.replace` over a fsynced temporary file, so a crash
mid-write cannot leave a truncated artefact live — `Policy.load` answers a
malformed file with an empty policy, which is an artefact that refuses every
decision, deployed by a power cut.

### The trap this replaced

Before 2026-09-23 the deployed artefact was Binance-trained and correctly
refused by the retirement guard, so **928 of 1,097 live decisions returned
neutral for the same reason** while everything downstream looked healthy. A
state report can read fine while every decision is a refusal. That is why
`scripts/learning_report.py` follows real decisions end to end — signal,
baseline, intelligence, final, execution, settlement, evaluation — rather than
only printing counters.

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

## The settlement reference (BRTI) — shadow only

### Why it exists

The bot decides on Binance spot. The contract settles on **the average of sixty
CF Benchmarks BRTI prices in the final minute**, and its strike is the same
statistic at the window open — confirmed from Kalshi's own `rules_primary`, not
assumed. FINDINGS 41 measures the gap: a median **6.2 bps feed basis**, and
**0.7 bps** from the 60-second averaging. Almost all of it is the feed.

### What it does

Records, and nothing else. No entry, exit or sizing path reads any of it, and
there is deliberately no config flag that changes that — promoting it has to be
a code change someone makes on purpose.

- Every `reference_poll_seconds` (5s), one row per source: raw price, event and
  receipt timestamps, age, staleness, signed distance to the strike, that
  source's own trailing 60-second mean with its sample count.
- Every `reference_reconcile_seconds` (5 min), one row per newly settled market
  comparing computed averages against Kalshi's official `expiration_value`.
- It runs **behind** the trading path and swallows every error, the same
  contract as the hourly ladder. A dead feed cannot delay a fill.

### Where it lives

    runtime/settlement_reference.db      its own file, its own locks
      reference_observations             per poll, per source
      settlement_reconciliation          per settled market, with the split
      feed_gaps                          every missing/stale run, with reasons
      second_bars                        cached 1s bars, so reruns are free

Schema is versioned with `PRAGMA user_version`; every insert names its columns,
because a positional insert is what put 93 shadow rows into permanent
quarantine.

### Turning the real feed on

CF Benchmarks gates index values behind an entitlement. Without a key the
recorder writes a `missing` row every poll naming the reason, and the basis
column stays NULL — **it never substitutes Coinbase, Kraken or Binance for the
reference**. The official 60-second averages still arrive from Kalshi either
way, so the reconciliation keeps working.

    CFB_API_KEY=...        # in .env; CFB_INDEX_ID defaults to BRTI

### Reading it

    python scripts/reconcile_settlement.py --limit 400    # backfill + decompose
    python scripts/reconcile_settlement.py --report-only

`computed_brti_error_bps` is the gate on everything downstream. **The HOLD/EXIT
shadow model does not start until the recorder reproduces official settlements
from its own BRTI ticks.** Today that column is empty because the feed is not
entitled, so the model has not been started. That is the intended state, not a
bug.

### The one number to watch

A walk-forward trailing-20 basis correction cuts outcome disagreement from
19.4% to **4.0%** (FINDINGS 41). It is stored as evidence and read by nothing.
Do not wire it into a decision without measuring it as a decision first.

## The recovery add-on: reading its lifecycle

A recovery add is a SECOND contract, resting 2c below the base fill, placed
only while the BRTI evidence still holds. It is one extra contract, never a
doubling - see [sizing](FINDINGS.md) section 45.

`recovery_adds` holds one row per attempt. The states:

| state | meaning |
|---|---|
| `RECOVERY ADD SKIPPED` | never placed - the conditions did not hold |
| `RECOVERY ADD DEFERRED` | **not a verdict.** The crossing could not be established yet; the next poll asks again |
| `RECOVERY ADD PENDING` | resting at the broker, **not a position** |
| `RECOVERY ADD EXECUTED` | filled; `filled_count`, `fill_price`, `fee_paid` are the broker's |
| `RECOVERY ADD CANCELLED` | pulled before filling |

**`placed_ms` is what says whether an order ever existed**, not the state
string. NULL means nothing was sent, so there is no resting price to quote -
`limit_price` on such a row is the price it WOULD have rested at. Reading the
state string instead is how a recap came to say "Recovery add: pending -
rested at 82c" over an add that was never placed (FINDINGS 51).

**DEFERRED is the only state the runner will re-enter.** Every other state is
terminal for that position, which is what keeps one add per position. A
deferred row is swept to SKIPPED by `close_stale_deferred_adds` once it can no
longer be answered — the window has closed, or the base position is gone — so
a DEFERRED row you see in the table is one still being asked.

**`order_id IS NULL` on a PENDING row means the placement response was lost**,
not that nothing was sent. `placed_ms` says we tried. Such a row is resolved by
asking Kalshi which order carries our `client_order_id`:

```bash
.venv/Scripts/python.exe -c "
import sqlite3
db=sqlite3.connect('file:btc15.db?mode=ro',uri=True); db.row_factory=sqlite3.Row
q=('SELECT ticker, client_order_id, placed_ms FROM recovery_adds '
   \"WHERE state='RECOVERY ADD PENDING' AND order_id IS NULL\")
for r in db.execute(q): print(dict(r))
"
```

Anything printed is an order that may be resting at Kalshi unwatched. The
service resolves these itself on the next poll and at startup
(`resolve_order_id`); if one persists, the order listing could not be read.
**Never mark such a row cancelled by hand** — that is exactly what created the
orphan this replaced.

### What the add-on actually earned

`settled`/`realised_pnl` are closed from `/portfolio/settlements` by
`settle_filled_adds`, on the broker's record and never on a clock. This is the
add's OWN leg, kept apart from the account: `daily_ledger` already holds the
whole position and the deficit is already credited from it, so nothing here
moves money.

```bash
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from btc15_signal.config import Settings
from btc15_signal.store import Store
print(Store(Settings().database_path).add_pnl_summary())
"
```

`pnl` of exactly 0 with `filled` above 0 means the settlement step is not
running — that was the state until 2026-09-23, when it had never run at all.

**A cancel can lose the race to a fill.** The runner therefore re-reads the
order before cancelling and banks a fill if it finds one. Until 2026-09-23
that re-read used `/portfolio/events/orders/{id}`, which returns **404 for
every order** - so it never banked anything and 7 of 7 filled adds were
recorded as CANCELLED. The live read is:

```bash
GET /portfolio/orders/{order_id}
```

If an add's `cancel_reason` contains "order not found (already filled,
expired or cancelled)", **the order probably filled**. Check the fills:

```bash
.venv/Scripts/python.exe -c "
import sqlite3
db=sqlite3.connect('file:btc15.db?mode=ro',uri=True); db.row_factory=sqlite3.Row
q = ('SELECT a.ticker, a.state, a.order_id, f.count, f.no_price '
     'FROM recovery_adds a JOIN fills f ON f.order_id = a.order_id '
     'WHERE COALESCE(a.filled_count,0)=0')
for r in db.execute(q):
    print(dict(r))
"
```

An INNER join, deliberately: a LEFT join also lists adds that were
correctly cancelled and never filled, which is a false positive an
operator would chase. **No output means nothing is mis-recorded.**

Any row it does print is a mis-recorded add. Repair it - idempotent, never
invents a fill, never touches the ledger, and charges the lifetime add budget
the way a normal fill does:

```bash
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from btc15_signal.config import Settings
from btc15_signal.store import Store
st=Store(Settings().database_path)
import sqlite3
ws=[r[0] for r in st.db.execute('SELECT DISTINCT window_open_ms FROM recovery_adds')]
print(sum(st.reconcile_recovery_adds(w) for w in ws), 'repaired')
"
```

## Reading a money message

One command prints everything a market's message should reconcile
against - local records, every broker order and fill by ID, and the
settlement:

```bash
.venv/Scripts/python.exe scripts/trace_market.py KXBTC15M-26SEP231615-15
```

**Check it against itself.** Entry price, quantity and exit must produce the
stated P&L. On 2026-09-23 a recap read "Bought DOWN at 85c / Cost $1.72 /
Profit +$0.45" - and 2 x (99.7c - 85c) is 29.4c, so it could not. The
position was 2 @ 85c plus a recovery add of 1 @ 83c, with 2 sold early and 1
run to settlement.

A recap now shows every leg:

```
📦 Base: 2 @ 85¢
🔧 Recovery add: 1 @ 83¢
💵 Total cost $2.55 for 3 contracts (incl. $0.02 fees)
💵 Sold before expiry at 99.7¢ · 2 of 3 · 1 ran to settlement
💰 Combined realised Profit $0.45 (net of fees, all legs)
```

A pending or cancelled add is printed **by name**. A reader who sees nothing
cannot tell an add that never happened from one the message forgot.

**`Today` is realised money only.** An open position is a separate line,
marked at the bid and labelled as not yet realised. If a recap's market has
not come back from the broker, the footer says the totals are as of the last
reconciliation rather than quietly excluding the trade above them.

**The ledger is the broker's.** It reads `/portfolio/settlements` and is never
rebuilt locally. When a message and the ledger disagree, the message is wrong.

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
