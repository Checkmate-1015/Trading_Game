"""
Checks for the futures trading game engine.  Run: python3 tests.py
Each check prints PASS or FAIL; the script exits with an error if any check fails.
"""
import math
import os
import random
import sys
import tempfile

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
                lhs = b.realised[t] + b.pos[t] * (g.mark() - b.avg[t]) - b.fee_paid[t]
                worst_split = max(worst_split, abs(lhs - (b.cash[t] + b.pos[t] * g.mark())))
        g.next_round()
    worst_sum = max(worst_sum, abs(total(g)))
    volume += sum(x["q"] for x in g.book.tape)
    settle_gap = max(settle_gap, abs(g.settle - (G.S0 + G.CARRY_PER_ROUND * G.ROUNDS + sum(g.shocks))))
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
check("Realised Plus Unrealised Less Fees Equals The Account Result", worst_split < 1e-6, f"{worst_split}")
check("Settlement Equals Start Plus Carry Plus All Shocks", settle_gap < 1e-6, f"{settle_gap}")

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
check("Futures Still Trade Overnight", g.order(0, "B", g.fair() * (1 + G.HOUSE_FEE) + 1, 1)["ok"] and g.book.pos[0] == 1)
f2 = g.fair()
g.next_round()
check("Index Gaps At The Open By The Overnight News", abs(g.spot - s1 - g.shocks[1] - G.CARRY_PER_ROUND) < 1e-9)
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
g.pause()
check("No Orders While Paused", not g.order(0, "S", g.fair(), 1)["ok"])

# 4. Fees, the house, contract size.
g = new_game(11, 1)
g.order(0, "B", g.fair() * 1.01, 2)
ask = G.to_tick(g.fair() * (1 + G.HOUSE_FEE))
check("House Offer Sits At Fair Plus 0.25%", g.book.pos[0] == 2 and abs(-g.book.cash[0] - 2 * (ask + G.FUT_FEE_PTS)) < 1e-9)
check("Fees Are Tracked", abs(g.book.fees - 2 * G.FUT_FEE_PTS) < 1e-9)
check("One Contract Is Worth USD 5,000, USD 1 A Point", G.MULTIPLIER * G.S0 == 5_000 and G.MULTIPLIER == 1)
check("Starting Capital Is USD 100,000", G.START_CAPITAL == 100_000 and new_game(1, 1).equity(0) == 100_000)
g = new_game(12, 1)
g.order(0, "S", g.fair() + 5, 2)                 # rests inside the house quotes
g.spot += 40                                     # news: the offer is now stale, below the house bid
g.house_quote()
check("The House Never Takes A Member's Order", g.book.pos[G.HOUSE] == 0 and g.book.open_qty(0, "S") == 2)

# 5. Capital and buying power.
g = new_game(13, 1)
check("A New Account Can Hold The Full Position Limit", g.set_limit(0) == G.POS_LIMIT)
g.book.cash[0] -= (G.START_CAPITAL - 7.5 * G.MARGIN_PER_CONTRACT) / G.MULTIPLIER   # capital left for seven
check("Losses Cut Buying Power", g.set_limit(0) == 7)
r = g.order(0, "B", g.fair() - 30, 10)
check("An Order Is Cut To Buying Power", g.book.open_qty(0, "B") == 7 and "Buying Power" in r["msg"])
g.book.cancel_all(0)
g.book.pos[0] = -9
check("A Member Can Always Reduce A Position", g.book.room(0, "B") >= 9 and g.book.room(0, "S") <= 0)

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
check("Contract Value Sets The Dollars Per Point", G.MULTIPLIER == 4 and abs(G.CARRY_PER_ROUND * G.ROUNDS - G.TOTAL_CARRY) < 1e-9)
g.start()
check("Setup Is Locked While A Game Runs", not G.admin_action({"action": "setup", "values": {"days": 2}})["ok"] and G.ROUNDS == 6)
check("Clue Holders Can Change Mid-Game", G.admin_action({"action": "holders", "value": 2})["ok"] and G.CLUE_HOLDERS == 2)
for _ in range(6):
    g.next_round()
check("A Three-Day Game Settles After Six Sessions", g.status == "settled" and [m["r"] for m in g.mtm] == ["Day 1", "Day 2", "Day 3", "Final"]
      and abs(g.settle - (G.S0 + G.TOTAL_CARRY + sum(g.shocks))) < 1e-6)
G.GAME.reset()
bad = [{"days": 0}, {"days": 11}, {"seconds": 5}, {"capital": "lots"}, {"limit": 2.5}, {"margin": float("nan")}, {"holders": True}]
check("Out-Of-Range Setup Is Refused", all(not G.admin_action({"action": "setup", "values": v})["ok"] for v in bad) and G.ROUNDS == 6)
check("Zero Margin Means Only The Position Limit Binds", G.admin_action({"action": "setup", "values": {"margin": 0}})["ok"]
      and G.GAME.set_limit(0) == G.POS_LIMIT)
check("Defaults Restore", G.apply_settings(default) == "" and G.settings() == default and G.ROUNDS == 10)

# 9. Crash recovery.
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
