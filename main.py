# SPDX-License-Identifier: MIT
"""Capitulation dip-buyer for the make.fun BTC race (agent-fun/bars-replay/v1).

Cash by default. The bot watches a trailing 240-bar mean of closes and buys
only a capitulation: the close at least 2.2% under that mean AND a bounce
trigger (the lowest close of the last 15 bars is at least 2 bars old and the
3-bar return is no worse than -0.1%), so it buys the first stabilisation,
never the freefall. Position size is 100% of equity. Exits: a trailing
snap-back exit (once the unrealised gain reaches +2.0% it arms, and exits
when the close falls 1.0% from its post-entry peak), a reference exit (close
back at the mean less 0.5%) armed only once the trade is 60 bars old, a -3%
catastrophe stop, and a 240-bar time stop. Cooldown is
30 bars after an exit, 60 after a stop. A one-shot pressure valve takes a
40% dip-buy after bar 1100 if nothing has traded yet. All signal comparisons
are exact integer arithmetic; the bot is a pure function of the bars it is
sent (no clock, no environment, no randomness), so two runs agree byte for
byte.

Protocol: newline-delimited JSON on stdin/stdout. hello once, then one
batch per bar (bar, fills of the previous batch's orders, account), then
done. One reply per batch, echoing its seq. Orders decided on a batch fill
at the next bar's open. Amounts are integer strings at their scales.
"""

import json
import sys

REF_LEN = 240            # trailing closes in the reference mean
ENTRY_D_PCT_X1000 = 22   # capitulation dislocation: 2.2% under the mean
BOUNCE_SPAN = 15         # bars scanned for the recent low
BOUNCE_MIN_AGE = 2       # that low must be at least this many bars old
RET3_FLOOR_X1000 = 999   # 3-bar return >= -0.1%
SIZE_BPS = 10000         # position size: 100% of equity
TRAIL_ARM_X100 = 102     # trailing exit arms at +2.0% over the entry price
TRAIL_KEEP_X100 = 99     # ...and exits when close <= 99% of the peak close
STOP_X100 = 97           # catastrophe stop at -3.0% under it
REF_EXIT_AGE = 60        # reference exit arms this many bars after entry
REF_EXIT_NUM = 199       # reference exit level: mean * 199/200 (mean - 0.5%)
REF_EXIT_DEN = 200
TIME_STOP_AGE = 240      # bars after entry
COOLDOWN_BARS = 30
COOLDOWN_STOP_BARS = 60
VALVE_BAR = 1100         # pressure valve: no earlier than this scored bar
VALVE_SIZE_BPS = 4000    # 40% of equity
VALVE_D_PCT_X100 = 1     # valve dip: 1.0% under the mean
MAX_SLIPPAGE_BPS = 10000


class Bot:
    def __init__(self, hello):
        self.symbol = hello["symbols"][0]["symbol"]
        self.warmup = int(hello.get("warmup_bars", 0))
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.closes = []            # every close seen, warm-up included
        self.seq = -1
        self.order_n = 0
        # Position state, reconciled from fills and the account each batch.
        self.entry_price = None     # fill price of the open position
        self.entry_bar = None       # scored bar index whose open filled it
        self.peak_close = None      # highest close since the entry fill
        self.trail_armed = False    # unrealised gain has reached +2.0%
        self.cooldown_until = 0     # scored bar index; no entries before it
        self.entries_made = 0
        self.valve_used = False
        self.awaiting = None        # ("buy"|"sell", was_stop) decided last batch

    # -- helpers ---------------------------------------------------------
    def _cid(self):
        self.order_n += 1
        return "o%d" % self.order_n

    def _ref_sum(self):
        if len(self.closes) < REF_LEN:
            return None
        return sum(self.closes[-REF_LEN:])

    def _bounce(self):
        """True when the recent low has stopped falling (see module doc)."""
        if len(self.closes) < BOUNCE_SPAN + 3:
            return False
        window = self.closes[-BOUNCE_SPAN:]
        low = min(window)
        # Age of the most recent occurrence of that low, in bars.
        age = 0
        for k in range(len(window) - 1, -1, -1):
            if window[k] == low:
                age = len(window) - 1 - k
                break
        if age < BOUNCE_MIN_AGE:
            return False
        # 3-bar return >= -0.1%: 1000 * c[-1] >= 999 * c[-4].
        return 1000 * self.closes[-1] >= RET3_FLOOR_X1000 * self.closes[-4]

    def _dislocated(self, ref_sum, pct_num, pct_den):
        """(mean - close) / mean >= pct_num / pct_den, exactly."""
        close = self.closes[-1]
        return (ref_sum - REF_LEN * close) * pct_den >= pct_num * ref_sum

    # -- per-batch decision ----------------------------------------------
    def on_batch(self, batch):
        self.seq = int(batch["seq"])
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == self.symbol:
                bar = b
        if bar is None:
            return []
        self.closes.append(int(bar["c"]))
        account = batch["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        qty = 0
        for p in account["positions"]:
            if p["symbol"] == self.symbol:
                qty = int(p["qty"])

        # Reconcile the fills of the orders decided on the previous batch.
        for f in batch["fills"]:
            if f["symbol"] != self.symbol or f["status"] != "filled":
                continue
            if f["side"] == "buy" and self.awaiting and self.awaiting[0] == "buy":
                self.entry_price = int(f["price"])
                self.entry_bar = self.seq - self.warmup
                self.peak_close = self.closes[-1]
                self.trail_armed = False
                self.entries_made += 1
                self.awaiting = None
            elif f["side"] == "sell" and self.awaiting and self.awaiting[0] == "sell":
                self.entry_price = None
                self.entry_bar = None
                self.peak_close = None
                self.trail_armed = False
                self.awaiting = None
        if self.awaiting is not None and batch["fills"]:
            # A decided order came back rejected; nothing is outstanding.
            self.awaiting = None

        if self.seq < self.warmup:
            return []
        i = self.seq - self.warmup  # scored bar index of this batch

        if qty > 0 and self.entry_price is not None:
            close = self.closes[-1]
            age = i - self.entry_bar
            if self.peak_close is None or close > self.peak_close:
                self.peak_close = close
            if 100 * close >= TRAIL_ARM_X100 * self.entry_price:
                self.trail_armed = True
            reason = None
            if 100 * close <= STOP_X100 * self.entry_price:
                reason = "stop"
            elif self.trail_armed and 100 * close <= TRAIL_KEEP_X100 * self.peak_close:
                reason = "trail"
            elif age >= TIME_STOP_AGE:
                reason = "time"
            elif age >= REF_EXIT_AGE:
                ref_sum = self._ref_sum()
                if ref_sum is not None and (
                    REF_EXIT_DEN * REF_LEN * close >= REF_EXIT_NUM * ref_sum
                ):
                    reason = "ref"
            if reason is None:
                return []
            cd = COOLDOWN_STOP_BARS if reason == "stop" else COOLDOWN_BARS
            self.cooldown_until = i + 1 + cd
            self.awaiting = ("sell", reason == "stop")
            return [{
                "side": "sell",
                "client_id": self._cid(),
                "symbol": self.symbol,
                "qty": str(qty),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]

        if qty == 0 and i >= self.cooldown_until:
            ref_sum = self._ref_sum()
            if ref_sum is not None and self._bounce():
                if self._dislocated(ref_sum, ENTRY_D_PCT_X1000, 1000):
                    return [self._buy(equity, cash, SIZE_BPS)]
                if (
                    not self.valve_used
                    and self.entries_made == 0
                    and i >= VALVE_BAR
                    and self._dislocated(ref_sum, VALVE_D_PCT_X100, 100)
                ):
                    self.valve_used = True
                    return [self._buy(equity, cash, VALVE_SIZE_BPS)]
        return []

    def _buy(self, equity, cash, size_bps):
        notional = equity * size_bps // 10000
        if notional > cash:
            notional = cash
        self.awaiting = ("buy", False)
        return {
            "side": "buy",
            "client_id": self._cid(),
            "symbol": self.symbol,
            "notional_quote_units": str(notional),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(msg)
            reply = {"type": "reply", "seq": int(msg["seq"]), "orders": orders}
            out.write(json.dumps(reply) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
