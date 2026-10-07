# Hourly contracts mirroring BTC 15-minute trades

Run October 5, 2026 on recorded data from September 24 through the completed 19:00–19:15 ET window October 5. Live code, settings, databases, services and existing studies were not changed. Only this research directory was created.

## Question and method

At each recorded BTC entry, use the hourly expiry containing that 15-minute window, select the archived hourly strike closest to the BTC 15-minute target, and buy YES for UP or NO for DOWN. Select the strike without looking at its outcome or exit price. Re-select on each new 15-minute trade, potentially using the same hourly expiry four times. Entry is at the original signal/trade time, not automatically at the quarter-hour opening. Exit at the last archived bid strictly before the 15-minute close; never use the hourly settlement payout, including the fourth quarter.

Use a constant $25 purchase budget (whole contracts, fees additional) for both instruments to isolate the proposed substitution. The BTC comparator buys at the recorded entry price and holds to its recorded settlement result. It is NOT actual account P&L: historical stakes varied and some trades cashed out. Daily targets and the new after-loss size boosts are not simulated. The actual-taken cohort preserves historical entry selection, including historical filters and pauses; it is not a retrospective application of today's rules to all dates. The all-signals cohort includes signals the live system skipped.

The fee assumption is the repository's 7% × contracts × price × (1-price), rounded up to $0.0001, on each purchase and sale; BTC settlement has no exit fee. This is a model assumption, not independently verified hourly fill charges.

Quotes must be within 90 seconds of each boundary. Entry uses the latest snapshot at/before the signal or last recorded order attempt. The sensitivity case uses the first snapshot at/after that timestamp, on the SAME previously selected strike. Exits can therefore be up to 90 seconds early; this is an approximate boundary-exit study, not proof of a fill exactly at the requested time. Missing data is excluded and counted, never imputed. Hourly strike selection is limited to the archived ladder.

## Results: trades the live system actually took

542 settled filled BTC windows since September 28; 499 matched comparisons across eight days. Excluded: 41 missing timely exit snapshots, one missing timely entry snapshot, one invalid price. Mean absolute hourly-to-BTC strike gap $27.66.

| $25 normalized comparison | Net P&L | Return on cumulative entry cost including entry fees |
|---|---:|---:|
| BTC 15m held to settlement | -$180.64 | -1.44% |
| Hourly, latest available entry quote | -$343.36 | -2.72% |
| Hourly, next recorded entry quote | -$932.70 | -7.39% |

These returns are per cumulative dollars deployed, not daily account returns. They do not represent the production account's profits or losses.

| Quarter within the hour | Trades | Hourly latest quote | Hourly next quote | BTC held |
|---|---:|---:|---:|---:|
| :00–:15 | 124 | -$267.17 | -$343.72 | -$38.52 |
| :15–:30 | 128 | -$75.95 | -$155.46 | +$74.42 |
| :30–:45 | 123 | -$53.38 | -$219.53 | -$66.80 |
| :45–:00 | 124 | +$53.13 | -$213.98 | -$149.74 |

An additional 1 cent worse on each hourly entry/exit changes the latest-quote total to -$745.32. This is sensitivity analysis, not an estimate of actual slippage.

## Broader recorded signals, September 24–October 5

1,108 settled same-side signals, 891 matched windows. Latest-entry hourly total +$293.63 (+1.30%) versus BTC -$318.71 (-1.43%); next-entry hourly total -$2,011.15 (-8.92%). Excluded 217 windows: 39 missing entry snapshots, 177 missing exits, one invalid price.

The apparent hourly gain is dominated by one October 3 quote: KXBTCD-26OCT0317-T84749.99, buy 2 cents and sell 97 cents, producing +$1,183.24 under unlimited quote-size assumptions. Removing just this observation leaves approximately -$889.61. Its executability is unproven; it is not evidence that 1,250 contracts were available at 2 cents. It disappears as an advantage in the next-poll comparison. Do not interpret the broad-sample headline as a demonstrated edge.

## Conclusion and limits

The archive does not support improved returns from repeatedly replacing the 15-minute trade with the nearest hourly strike and selling at the quarter-hour boundary. The first three quarters lose in the matched live-trade sample; the fourth quarter's apparent gain is not robust to entry timing.

Hourly /markets snapshots can lag executable order books. No historical depth/fill reconstruction was performed, and zero-bid exits are marked at zero rather than assumed filled. This study establishes neither executable profits nor precise realizable losses. An hourly order-book forward recorder would be needed to evaluate exact boundary exits and size availability; none was installed or enabled.

Artifacts: `study.py` reproduces the read-only analysis; `results.json` includes daily totals, quarters, halves and exclusions; four CSV files contain each matched trade. Runtime assertions check expiry alignment and entry-before-exit ordering. The existing test suite was not modified or run.
