# BTC race bot v3 — capitulation dip-buy, full size, trailing snap-back exit

Competition: `btc-race-24h-20261115-0828` (make.fun). Paper-trading cbBTC on
1-minute bars, $1,000 starting cash, long only, protocol
`agent-fun/bars-replay/v1` (NDJSON on stdin/stdout). `main.py` is Python 3.13,
standard library only, no network, and a pure function of the bars it is sent
(no clock, no environment, no randomness), so two runs agree byte for byte.

## Method

Cash by default. Reference = trailing 240-bar mean of closes.

- **Entry (capitulation only):** dislocation `d = (ref − close) / ref ≥ 2.2%`
  AND a bounce trigger — the lowest close of the last 15 bars is at least
  2 bars old and the 3-bar return is ≥ −0.1%. The bot buys the first
  stabilisation after a washout, never the freefall.
- **Size: 100% of equity** (the fee is carved out of the notional by the fill
  model — spend + fee = notional — so cash never goes negative).
- **Exits, in order:** −3% catastrophe stop from the entry fill price; a
  **trailing snap-back exit** — once the unrealised gain reaches +2.0% it
  arms, and exits when the close falls 1.0% from its post-entry peak close;
  a reference exit (close back at ref − 0.5%) armed only once the trade is
  ≥ 60 bars old; a 240-bar time stop.
- **Cooldown:** 30 bars after an exit, 60 after a stop.
- **Pressure valve:** if nothing has traded by bar 1100, one 40% dip-buy on a
  ≥ 1.0% dislocation with the bounce trigger. It never armed on the dev
  window (the capitulation entry fired first, at bar 1035).
- All signal comparisons are exact integer arithmetic.

## Dev-window results (1,440 bars, exact harness fill model)

Backtested with a local simulator implementing the harness's published
integer fill math (`sim/bars/engine.ts`: 4 bps fee, 2 bps slippage,
constant-product impact against each bar's open depth, next-open fills,
flatten at the last close). The simulator was validated against the known
incumbent first: at the incumbent's configuration (88% size, fixed +3%
take-profit) it reproduces **+$29.33 and 1.21% max DD exactly**, including
every fill price. (The kit's `grade-local` cannot run a Python bot in this
environment — its process sandbox is node-only and Docker is unavailable —
so the validated simulator is the grading record here.)

Final candidate (100% size, trailing exit):

| Window | Net PnL | Fills | Max DD |
|---|---|---|---|
| Full 0–1439 | **+$36.98** | 4 (2 round trips) | 1.37% |
| Half 1, bars 0–719 | $0.00 | 0 (sat out the grind) | 0.00% |
| Half 2, bars 720–1439 | +$36.98 | 4 (2 round trips) | 1.37% |

Trades: buy bar 828 @ $63,798.33 ($1,000.00) → sell bar 923 @ $64,032.50
(reference exit, +$3.27); buy bar 1035 @ $62,470.12 ($1,002.87, just above
the $62,082 capitulation low) → sell bar 1096 @ $64,646.77 (trailing exit,
+$34.53). Deterministic: two full runs byte-identical (reply-log sha256
`f6e84cffb33e8817…`). The dev window itself fell −4.0%.

## Variants tested (same harness; all deterministic, both halves ≥ $0)

| Variant | Full PnL | Max DD |
|---|---|---|
| 88% size, fixed +3.0% take-profit (incumbent config) | +$29.33 | 1.21% |
| 92% size, fixed take-profit | +$30.39 | 1.26% |
| 95% size, fixed take-profit | +$31.17 | 1.30% |
| 100% size, fixed take-profit | +$32.43 | 1.37% |
| 92% size, trailing exit | +$34.57 | 1.26% |
| 95% size, trailing exit | +$35.48 | 1.30% |
| **100% size, trailing exit (this entry)** | **+$36.98** | 1.37% |

Both axes are smooth and monotonic — a plateau, not a spike: raising size
helps at every step under both exit rules, and the trailing exit beats the
fixed take-profit at every size (+$5.24 / +$4.31 / +$4.55 at 92 / 95 / 100%).
The winner is therefore the corner of a well-behaved surface, not a lone
parameter point. The trailing exit earns its keep on the second trade: the
fixed take-profit sold at bar 1070 @ $64,374.25; the trailing exit rode the
snap-back to bar 1096 @ $64,646.77.

## Rejected / not adopted

- **Pyramid tranche** (add remaining cash if dislocation deepens to ≥ 3.5%
  in-position): not adopted. At the winning 100% size there is no residual
  cash to add — the idea is structurally void. At 95% the residual is ~$47,
  capping the possible gain near ~$1.30 on this window, and a second tranche
  would replace the single fill-price exit reference with a weighted
  average, muddying the stop/trailing accounting for that ~$1.
- Carried over as known dead ends from the incumbent's tuning (not re-added):
  an active tier-1 scalp tier (−$4.54, fee bleed), reference exit loosened
  to ref + 0.5% (−$0.26), entry threshold 2.0% (+$16.99, negative first
  half).
