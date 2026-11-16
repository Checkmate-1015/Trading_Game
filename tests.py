"""
Checks for the futures trading game engine.  Run: python3 tests.py
Each check prints PASS or FAIL; the script exits with an error if any check fails.
"""
import math
import os
import random
import sys
import tempfile
import time

import game_server as G

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def new_game(seed, people=0):
    g = G.Game()
    g.rng = random.Random(seed)
    g.reset()
    for i in range(people):
        g.join(f"tok{i}", f"Member {i + 1}")
    g.start()
    g.deadline = 1e18
    return g


def total(g):
    return sum(g.pnl(t) for t in range(G.SEATS + 1))


# 1. Random play: 30 members, no bots, all ten sessions.
worst_sum = worst_pos = worst_split = settle_gap = 0
volume = 0
for seed in range(40):
    g = new_game(seed, 30)
    rng = random.Random(1000 + seed)
    for _ in range(G.ROUNDS):
        for step in range(8):
            for t in list(g.humans):
                view = g.fair() + (g.shocks[g.round] if t in g.holders.get(g.round, []) and not g.news_out else 0)
                if rng.random() < 0.5:
                    g.order(t, rng.choice("BS"), view + rng.uniform(-15, 15), rng.randint(1, 20))
                if rng.random() < 0.1:
                    g.book.cancel_all(t)
            g.house_quote()
            if step == 4:
                g.release_news()
            worst_sum = max(worst_sum, abs(total(g)))
            b = g.book
            for t in range(G.SEATS):
                worst_pos = max(worst_pos, b.pos[t] + b.open_qty(t, "B"), -b.pos[t] + b.open_qty(t, "S"))
                lhs = b.realised[t] + b.pos[t] * (g.mark() - b.avg[t])
                worst_split = max(worst_split, abs(lhs - (b.cash[t] + b.pos[t] * g.mark())))
        g.next_round()
    worst_sum = max(worst_sum, abs(total(g)))
    volume += sum(x["q"] for x in g.book.tape)
    settle_gap = max(settle_gap, abs(g.settle - (G.S0 + sum(g.shocks) + g.noise_total)))
    if seed == 0:
        check("Game Settles After Ten Sessions", g.status == "settled")
        check("Five Daily Settlements Plus Final", [m["r"] for m in g.mtm] == [f"Day {d}" for d in range(1, 6)] + ["Final"])
        check("Sum Of Positions Is Zero", sum(g.book.pos) == 0)
        check("Final Standings List 30 Members And The House", len(g.state(0)["board"]) == 31)
        check("A Zero-Sum Check Is Recorded At Every Session End", len(g.checks) == G.ROUNDS + 1
              and g.checks[0]["label"] == "Day 1, Cash Session" and g.checks[-1]["label"] == "Final Settlement")
        check("Every Session Check Passes", all(c["ok"] and abs(c["members"] + c["house"]) < 0.005 for c in g.checks))
        g.book.cash[0] += 1                      # corrupt one account: the check must catch it
        g.zero_sum_check("Tamper Test")
        check("The Check Fails When Money Appears From Nowhere", not g.checks[-1]["ok"])
check("Members Trade With Each Other Without Bots", volume > 40 * 100, f"{volume}")
check("Members Plus House Sum To Zero (40 Games)", worst_sum < 0.01, f"{worst_sum}")
check("Position Plus Resting Orders Never Pass The Limit", worst_pos <= G.POS_LIMIT, f"{worst_pos}")
check("Realised Plus Unrealised Equals The Account Result", worst_split < 1e-6, f"{worst_split}")
check("Settlement Equals Start Plus All News Plus Noise", settle_gap < 1e-6, f"{settle_gap}")

# 2. Trading hours.
g = new_game(7, 2)
check("Session 1 Is A Cash Session", g.cash_open() and g.session() == "Cash Session")
s0 = g.spot
g.release_news()
check("Cash News Moves The Index At Once", abs(g.spot - s0 - g.shocks[0]) < 1e-9)
f1 = g.fair()
g.next_round()
check("Fair Value Is Continuous Across The Close", abs(g.fair() - f1) < 1e-9)
check("Session 2 Is Overnight", not g.cash_open() and g.session() == "Overnight Session")
s1, f1 = g.spot, g.fair()
g.release_news()
check("Overnight News Leaves The Index Frozen", g.spot == s1)
check("Overnight News Moves Fair Futures", abs(g.fair() - f1 - g.shocks[1]) < 1e-9)
check("Futures Still Trade Overnight", g.order(0, "B", g.fair() * (1 + G.HOUSE_SPREAD) + 1, 1)["ok"] and g.book.pos[0] == 1)
f2 = g.fair()
g.next_round()
check("Index Gaps At The Open By The Overnight News", abs(g.spot - s1 - g.shocks[1]) < 1e-9)
check("Fair Value Is Continuous Across The Open", abs(g.fair() - f2) < 1e-9)
check("Zero-Sum Holds Through The Gap", abs(total(g)) < 0.01)

# 3. Order rules.
g = new_game(3, 2)
g.book.cancel_all(G.HOUSE)
g.order(0, "S", g.fair() + 5, 3)
g.order(0, "B", g.fair() + 6, 3)
check("A Member Never Trades With Their Own Order", g.book.pos[0] == 0 and not g.book.tape)
g.order(1, "B", g.fair() + 6, 2)
check("Two Members Trade At The Resting Price", g.book.pos[1] == 2 and g.book.pos[0] == -2 and g.book.last == G.to_tick(g.fair() + 5))
g = new_game(3, 1)
r = g.order(0, "B", g.fair() - 30, 20)
check("An Oversize Order Is Cut To The Limit", g.book.open_qty(0, "B") == G.POS_LIMIT and "Cut 10" in r["msg"])
r = g.order(0, "B", g.fair() - 31, 1)
check("Resting Orders Count Toward The Limit", g.book.open_qty(0, "B") == G.POS_LIMIT and "Cut 1" in r["msg"])
check("A Price Far From Fair Is Refused", not g.order(0, "B", 1, 1)["ok"])
check("A Non-Number Price Is Refused", not g.order(0, "B", float("nan"), 1)["ok"] and not g.order(0, "B", math.inf, 1)["ok"])
check("Zero, Huge Or Fractional Quantity Is Refused", not g.order(0, "B", g.fair(), 0)["ok"]
      and not g.order(0, "B", g.fair(), 999)["ok"] and not g.order(0, "B", g.fair(), 1.5)["ok"])
check("An Empty Seat Cannot Order", not g.order(5, "B", g.fair(), 1)["ok"])
g = new_game(3, 1)
r = g.order(0, "B", g.fair() + 6, 12, now=True)
check("A Buy-Now Order Fills What It Can And Drops The Rest", g.book.pos[0] == 10 and g.book.open_qty(0, "B") == 0
      and "Not Filled" not in r["msg"] and "Cut 2" in r["msg"])
g = new_game(3, 1)
r = g.order(0, "B", g.fair() + 3, 8, now=True)    # only the first house level, 5 contracts, is within 3 points
check("A Buy-Now Order Never Waits In The Book", g.book.pos[0] == 5 and g.book.open_qty(0, "B") == 0 and "3 Not Filled" in r["msg"])
g.pause()
check("No Orders While Paused", not g.order(0, "S", g.fair(), 1)["ok"])

# 4. No fees, the house, contract size.
g = new_game(11, 1)
g.order(0, "B", g.fair() * 1.01, 2)
ask = G.to_tick(g.fair() * (1 + G.HOUSE_SPREAD))
check("House Offer Sits At Fair Plus 0.05%, About 2.5 Points", g.book.pos[0] == 2 and abs(-g.book.cash[0] - 2 * ask) < 1e-9
      and abs(ask - g.fair() - 2.5) < 1e-9)
g = new_game(11, 2)
f0 = g.fair()                                    # trade inside the house quotes, so members meet each other
g.order(0, "S", f0 + 1, 3)
g.order(1, "B", f0 + 1, 3)
g.order(1, "S", f0 + 2, 3)
g.order(0, "B", f0 + 2, 3)
check("No Trading Fees: Cash Moves Only By Trade Prices", g.book.pos[0] == 0 and g.book.pos[1] == 0
      and abs(g.book.cash[0] - 3 * (1 - 2)) < 1e-9 and abs(g.book.cash[1] - 3 * (2 - 1)) < 1e-9
      and g.book.cash[G.HOUSE] == 0)
check("One Contract Is Worth USD 5,000, USD 1 A Point", G.MULTIPLIER * G.S0 == 5_000 and G.MULTIPLIER == 1)
check("Starting Capital Is USD 100,000", G.START_CAPITAL == 100_000 and new_game(1, 1).equity(0) == 100_000)
g = new_game(12, 1)
g.order(0, "S", g.fair() + 5, 2)                 # rests above the house offer
g.spot += 40                                     # news: the offer is now stale, below the house bid
g.house_quote()
check("The House Never Takes A Member's Order", g.book.pos[G.HOUSE] == 0 and g.book.open_qty(0, "S") == 2)

# 5. The house leans on order flow, so informed trading shows up in the futures before the news.
g = new_game(15, 4)
f0, spot0 = g.fair(), g.spot
best_ask = lambda: min(o[0] for o in g.book.asks)
best_bid = lambda: max(o[0] for o in g.book.bids)
a0 = best_ask()
g.order(0, "B", f0 + 40, 10)
check("Buying From The House Before News Raises Its Prices", best_ask() > a0 and abs(g.lean() - 10 * G.HOUSE_LEAN) < 1e-9)
check("A Big Order Pays More For Each Extra Contract", abs(-g.book.cash[0] - (5 * a0 + 5 * (a0 + 2.5))) < 1e-9)
sig = g.signal()
check("Futures Run Ahead Of Spot While Spot Stays Put", g.spot == spot0 and sig["gap"] >= 5 and sig["buy"] == 10 and sig["preNews"])
g.order(1, "S", f0 - 40, 10)
check("Selling To The House Lowers Its Prices Again", abs(g.lean()) < 1e-9)
g.order(2, "B", f0 + 60, 10)
g.order(3, "B", f0 + 60, 10)
lead = g.signal()["gap"]
g.release_news()
check("The News Record Keeps What The Futures Hinted", abs(g.news[-1]["lead"] - lead) < 1e-9 and g.news[-1]["flowBuy"] == 30)
check("The House Re-Centres On Fair Value When The News Comes Out", g.lean() == 0
      and abs((best_ask() + best_bid()) / 2 - g.fair()) <= G.TICK)
check("Accounts Are Valued At The New Price, Not A Trade From Before The News", abs(g.mark() - g.fair()) <= G.TICK)
g = new_game(17, 2)
g.order(0, "S", g.fair() + 1, 3)
g.order(1, "B", g.fair() - 1, 3)
g.release_news()
check("News Cancels Every Waiting Order", not g.book.open_qty(0, "S") and not g.book.open_qty(1, "B")
      and abs(g.fut_price() - g.fair()) <= G.TICK)
g = new_game(16, 2)
g.order(0, "B", g.fair() + 60, 10)
check("The Lean Is Capped", abs(g.lean()) <= G.LEAN_CAP)
g.next_round()
check("Each New Session Starts With No Lean", g.lean() == 0 and g.flow == {"buy": 0, "sell": 0})
g = new_game(13, 1)
check("A New Account Can Hold The Full Position Limit", g.book.room(0, "B") == G.POS_LIMIT)
g.book.pos[0] = -9
check("A Member Can Always Reduce A Position", g.book.room(0, "B") >= 9 and g.book.room(0, "S") <= 1)

# 6. Sign-up.
g = G.Game()
check("Sign-Up Works Before The Game Starts", g.join("a", "Alex")["ok"] and g.status == "waiting")
check("Seat Numbers Follow Sign-Up Order", g.join("b", "Sam")["ok"] and g.seat_of("a") == 0 and g.seat_of("b") == 1)
check("A Duplicate Name Is Refused", not g.join("c", "  alex ")["ok"])
check("A Blank Name Is Refused", not g.join("d", "   ")["ok"])
check("The Same Browser Keeps Its Seat", g.join("a", "Other Name")["ok"] and g.seat_of("a") == 0 and len(g.humans) == 2)
for i in range(G.SEATS):
    g.join(f"x{i}", f"Member {i}")
check("Sign-Up Stops When The Game Is Full", len(g.humans) == G.SEATS and not g.join("late", "Late")["ok"])
g = new_game(14, 3)
g.order(0, "B", g.fair() * 1.01, 2)
g.order(0, "S", g.fair() + 6, 1)
g.leave(0)
check("Leaving Pulls Orders And Keeps The Position On The Books", g.book.open_qty(0, "S") == 0 and g.book.pos[0] == 2
      and g.display(0) == "Member 1 (Left)" and abs(total(g)) < 0.01)
g.join("new", "Newcomer")
check("A New Member Never Inherits Someone's Position", g.seat_of("new") == 3 and g.book.pos[3] == 0)

# 7. Private clues.
g = new_game(5, 30)
check("Three Members Hold The First Clue", len(g.holders[0]) == G.CLUE_HOLDERS == 3)
st = g.state(g.holders[0][0])
check("A Holder Sees The Shock Before It Is Public", st["me"]["clue"] is not None
      and st["me"]["clue"]["value"] == g.shocks[0] and not st["me"]["clue"]["public"])
other = next(t for t in g.humans if t not in g.holders[0])
check("Everyone Else Sees No Clue", g.state(other)["me"]["clue"] is None)
check("The Projector Feed Carries Nothing Private", "shocks" not in str(g.overview().keys())
      and all(k not in g.overview() for k in ("holders", "schedule", "accounts", "me")))
for _ in range(G.ROUNDS - 1):
    g.next_round()
counts = [g.clue_count.get(t, 0) for t in g.humans]
check("Clues Rotate: 30 Clues Reach 30 Different Members", sum(counts) == 30 and max(counts) == 1)
g = new_game(6, 2)
check("Fewer Members Than Clues Still Works", len(g.holders[0]) == 2)

# 8. Game setup.
default = G.settings()
G.GAME = G.Game()
G.GAME.join("a", "Alex")
r = G.admin_action({"action": "setup", "values": {"days": 3, "seconds": 20, "holders": 5, "contractValue": 20_000}})
g = G.GAME
check("Setup Applies Before The Game Starts", r["ok"] and G.ROUNDS == 6 and G.ROUND_SECONDS == 20 and G.CLUE_HOLDERS == 5)
check("Setup Keeps Members Seated", g.seat_of("a") == 0 and len(g.shocks) == 6 and g.remaining == 20)
check("Contract Value Sets The Dollars Per Point", G.MULTIPLIER == 4)
g.start()
check("Setup Is Locked While A Game Runs", not G.admin_action({"action": "setup", "values": {"days": 2}})["ok"] and G.ROUNDS == 6)
check("Clue Holders Can Change Mid-Game", G.admin_action({"action": "holders", "value": 2})["ok"] and G.CLUE_HOLDERS == 2)
for _ in range(6):
    g.next_round()
check("A Three-Day Game Settles After Six Sessions", g.status == "settled" and [m["r"] for m in g.mtm] == ["Day 1", "Day 2", "Day 3", "Final"]
      and abs(g.settle - (G.S0 + sum(g.shocks) + g.noise_total)) < 1e-6)
G.GAME.reset()
bad = [{"days": 0}, {"days": 11}, {"seconds": 5}, {"capital": "lots"}, {"limit": 2.5}, {"limit": float("nan")}, {"holders": True}]
check("Out-Of-Range Setup Is Refused", all(not G.admin_action({"action": "setup", "values": v})["ok"] for v in bad) and G.ROUNDS == 6)
check("The Old Margin Setting Is Gone", "margin" not in G.settings())
check("Defaults Restore", G.apply_settings(default) == "" and G.settings() == default and G.ROUNDS == 10)
check("Default Game Is 20 Minutes Of Trading", G.ROUNDS * G.ROUND_SECONDS == 20 * 60)

# 8a. Random noise (Brownian motion) is small next to the news.
g = new_game(51, 1)
paths = []
for _ in range(400):
    s0 = g.spot
    for _ in range(int(G.ROUND_SECONDS / G.NOISE_STEP)):
        g.drift(G.NOISE_STEP)
    paths.append(g.spot - s0)
sd = (sum(x * x for x in paths) / len(paths)) ** 0.5
check("Noise Over A Session Is About Its Set Size", 0.85 * G.NOISE_SD < sd < 1.15 * G.NOISE_SD, f"{sd:.2f}")
shocks = [abs(x) for seed in range(50) for x in new_game(seed).shocks]
check("Every News Item Is At Least 3 Times The Noise Of A Whole Session",
      min(shocks) >= G.SHOCK_MIN >= 3 * G.NOISE_SD and max(shocks) <= G.SHOCK_MAX, f"{min(shocks):.1f}")
g = new_game(52, 1)
s0, p0 = g.spot, g.pending
g.drift(G.NOISE_STEP)
check("Noise Moves Spot In The Cash Session", g.spot != s0 and g.pending == p0)
g.next_round()
s1, f1 = g.spot, g.fair()
g.drift(G.NOISE_STEP)
check("Noise Overnight Moves Only The Futures", g.spot == s1 and g.fair() != f1)
g.next_round()
check("Spot Catches Up With Overnight Noise At The Open", abs(g.spot - (G.S0 + g.shocks[0] + g.shocks[1] + g.noise_total)) < 1e-9)

# 9a. Chart candles.
g = new_game(41, 3)
check("The Chart Starts With One Flat Candle", len(g.candles) == 1 and g.candles[0][0] == g.candles[0][3] == g.fair())
g.order(0, "B", g.fair() + 10, 8)
c = g.candles[-1]
check("A Trade Moves The Candle And Adds Volume", c[4] == 8 and c[5] == 8 and c[1] >= c[3] > c[0])
g.release_news()
check("News Is Marked On The Candle It Lands In", g.news[-1]["i"] == len(g.candles) - 1 and g.candles[-1][6] == g.spot)
g.next_round()
check("Each Session Starts A New Candle", g.candles[-1][8] == 1 and len(g.candles) == 2)
for _ in range(G.ROUNDS - 1):
    g.next_round()
check("The Last Candle Closes At The Settlement Price", g.candles[-1][3] == g.settle)

# 9. What players learn: the intro, the report card and the class lessons.
g = new_game(31, 6)
check("Nobody Has Finished The Intro At First", not g.intro and not g.state(0)["me"]["introDone"])
g.intro.add(0)
check("Finishing The Intro Shows On The Monitor", g.monitor()["introDone"] == 1 and g.state(0)["me"]["introDone"]
      and g.accounts()[0]["intro"])
g.reset()
check("The Intro Stays Done In A New Game", g.state(0)["me"]["introDone"])
g.leave(0)
check("Leaving Clears The Intro", 0 not in g.intro)
g = new_game(32, 6)
h = g.holders[0]
side = "B" if g.shocks[0] > 0 else "S"
for t in h:
    g.order(t, side, g.fair() + (60 if side == "B" else -60), 4)
plain = next(t for t in g.humans if t not in h)
g.order(plain, "S" if side == "B" else "B", g.fair() + (-60 if side == "B" else 60), 1)
g.next_round()
rep = g.report(h[0])
check("The Report Card Records The Clue And The Bet When The News Hit", len(rep) == 1 and rep[0]["clue"]
      and rep[0]["pos"] == (4 if side == "B" else -4) and abs(rep[0]["pnl"] - g.ends[0][h[0]]) < 1e-9)
check("A Player Without A Clue Sees Their Own Bet", not g.report(plain)[0]["clue"] and abs(g.report(plain)[0]["pos"]) == 1)
les = g.lessons()
check("Class Lessons Compare Clue Holders With Everyone Else", les["clueN"] == 3 and les["plainN"] == 3
      and les["clueSize"] == 4 and les["hinted"] == 1)
for _ in range(G.ROUNDS - 1):
    g.next_round()
check("Every Session Is On The Report Card", len(g.report(h[0])) == G.ROUNDS
      and abs(sum(r["pnl"] for r in g.report(h[0])) - g.pnl(h[0])) < 1e-6)
check("The Projector Gets Class Lessons Only After The Game", g.overview()["lessons"] is not None
      and new_game(33, 2).overview()["lessons"] is None)

# 9b. The clock: a stored game always plays out the same way, however often it is caught up.
import copy
import pickle
g = new_game(61, 6)
g.deadline = g.t + G.ROUND_SECONDS                # a real clock this time
g.next_refresh = g.t + G.NOISE_STEP
t0 = g.t
a, b = pickle.loads(pickle.dumps(g)), pickle.loads(pickle.dumps(g))
a.advance_to(t0 + 400)                          # one big jump
step = random.Random(7)
now = t0
while now < t0 + 400:                           # many small, uneven steps
    now = min(now + step.uniform(0.05, 9), t0 + 400)
    b.advance_to(now)
same = lambda x, y: (x.round, x.status, x.spot, x.pending, x.news, x.holders, x.ends, x.candles, x.book.bids, x.book.asks) \
    == (y.round, y.status, y.spot, y.pending, y.news, y.holders, y.ends, y.candles, y.book.bids, y.book.asks)
check("Catching Up In One Jump Or Many Steps Gives The Same Game", same(a, b) and a.round == 3)
a.advance_to(t0 + 10 * G.ROUND_SECONDS + 5)
check("A Game Left Alone Plays Out To The End", a.status == "settled" and len(a.ends) == G.ROUNDS
      and len(a.candles) == G.ROUNDS * G.CANDLES_PER_SESSION and a.checks[-1]["ok"])
g = new_game(62, 2)
g.book.cancel_all(G.HOUSE)
for k in range(150):                             # 150 one-contract trades between two members
    g.order(0, "S", g.fair() + 1, 1)
    g.order(1, "B", g.fair() + 1, 1)
    g.order(1, "S", g.fair() + 1, 1)
    g.order(0, "B", g.fair() + 1, 1)
check("Only Recent Trades Are Kept, But Volume Counts Them All", len(g.book.tape) == G.TAPE_KEEP
      and g.book.volume == g.book.trades == 300)

# 9c. Vercel mode: the game lives in Redis, and any short-lived server can answer any request.
import json as _json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


class FakeRedis:
    """Just enough of Redis for the game: GET, MGET, SET (NX, PX), INCR and the unlock script."""

    def __init__(self):
        self.d = {}
        self.calls = 0

    def cmd(self, *a):
        self.calls += 1
        c, a = a[0].upper(), [str(x) for x in a[1:]]
        if c == "GET":
            return self.d.get(a[0])
        if c == "MGET":
            return [self.d.get(k) for k in a]
        if c == "SET":
            if "NX" in [x.upper() for x in a[2:]] and a[0] in self.d:
                return None
            self.d[a[0]] = a[1]
            return "OK"
        if c == "INCR":
            self.d[a[0]] = str(int(self.d.get(a[0], "0")) + 1)
            return int(self.d[a[0]])
        if c == "EVAL":
            key, tok = a[2], a[3]
            if self.d.get(key) == tok:
                del self.d[key]
                return 1
            return 0
        raise ValueError(c)


G.apply_settings(default)
fake = FakeRedis()
G.USE_REDIS, G.REDIS, G.ADMIN_KEY = True, fake, ""
G.CACHE.update(ver=None, text=None)
T = [1_800_000_000.0]
G.wall_clock = lambda: T[0]
srv = ThreadingHTTPServer(("127.0.0.1", 0), G.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None, key=None, fresh=True):
    if fresh:                                   # a different Vercel instance: nothing remembered
        G.CACHE.update(ver=None, text=None)
    req = urllib.request.Request(URL + path, data=None if body is None else _json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Forwarded-For": "1.2.3.4",
                                          **({"X-Admin-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, _json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json.loads(e.read())


code, body = call("/api/monitor")
check("Online, The Monitor Is Locked And Offers To Set A Key", code == 403 and body["claimable"])
check("The First Key Typed Becomes The Game Master Key", call("/api/monitor", key="teach42")[0] == 200
      and call("/api/monitor", key="guess")[0] == 403 and call("/api/monitor")[1]["claimable"] is False)
check("A Player Cannot Start The Game", call("/api/admin", {"action": "start"})[0] == 403)
for i, n in enumerate(["Ava", "Ben", "Cy", "Di"]):
    call("/api/join", {"token": f"p{i}", "name": n})
call("/api/intro", {"token": "p0"})
st = call("/api/state?token=p0")[1]
check("Players Join And Are Remembered Between Servers", st["joined"] and st["people"] == 4 and st["me"]["introDone"])
check("The Game Master Starts The Game", call("/api/admin", {"action": "start"}, key="teach42")[1]["ok"])
T[0] += 20
holder = next(f"p{i}" for i in range(4) if call(f"/api/state?token=p{i}")[1]["me"]["clue"])
clue = call(f"/api/state?token={holder}")[1]["me"]["clue"]["value"]
side = "B" if clue > 0 else "S"
r = call("/api/order", {"token": holder, "side": side, "price": 5000 + (60 if side == "B" else -60), "qty": 6, "now": True})[1]
T[0] += 15
s1 = call(f"/api/state?token={holder}")[1]
s2 = call(f"/api/state?token={holder}", fresh=False)[1]
check("A Trade Is Saved And Every Server Sees It", r["ok"] and s1["me"]["pos"] == (6 if side == "B" else -6)
      and s1["signal"]["gap"] * clue > 0 and s1["candles"] == s2["candles"] and s1["spot"] == s2["spot"])
T[0] += 40                                       # past the news, which nobody requested at the moment it happened
s3 = call("/api/overview")[1]
check("News Comes Out On Time Even When Nobody Is Looking", len(s3["news"]) == 1 and abs(s3["news"][0]["shock"] - clue) < 1e-9)
T[0] += 9 * G.ROUND_SECONDS + 60                  # the rest of the game (75 s were used), with no requests
fin = call("/api/state?token=p0")[1]
mon = call("/api/monitor", key="teach42")[1]
check("A Whole Game Finishes In Redis, Zero-Sum, With Report Cards", fin["status"] == "settled"
      and len(fin["me"]["report"]) == G.ROUNDS and all(c["ok"] for c in mon["checks"]) and abs(mon["sumPnl"]) < 0.01)
before = fake.calls
for _ in range(5):
    call("/api/state?token=p1", fresh=False)
check("A Busy Server Reads Only A Tiny Version Number When Nothing Changed", fake.calls - before <= 6)
srv.shutdown()
G.USE_REDIS, G.REDIS, G.ADMIN_KEY, G.wall_clock = False, None, "", time.time

# 10. Crash recovery.
G.GAME = new_game(21, 4)
G.GAME.order(3, "B", G.GAME.fair() * 1.01, 2)
before = (G.GAME.book.pos[3], G.GAME.book.cash[3], G.GAME.spot, G.GAME.names[3])
G.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.pkl")
G.GAME.deadline = 2e9
G.save()
G.GAME = G.Game()
ok = G.load()
a = G.GAME
check("A Saved Game Is Restored", ok and (a.book.pos[3], a.book.cash[3], a.spot, a.names[3]) == before)
check("A Restored Running Game Comes Back Paused", ok and a.status == "paused" and a.seat_of("tok3") == 3)
G.apply_settings({"days": 2, "seconds": 30})
G.GAME = new_game(22, 2)
G.GAME.deadline = 2e9
G.save()
G.apply_settings(default)
check("The Saved Setup Comes Back With The Game", G.load() and G.ROUNDS == 4 and G.ROUND_SECONDS == 30 and len(G.GAME.shocks) == 4)
G.apply_settings(default)

print()
print("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
