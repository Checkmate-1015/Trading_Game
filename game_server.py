"""
Traders@UST Futures Trading Game: game server.

Run:  python3 game_server.py [port] [--lan] [--fresh]
      Members open  http://<this computer>:7848           (sign up, then trade)
      Projector     http://localhost:7848/overview        (price chart and news)
      Game master   http://localhost:7848/monitor         (everything; this computer only)

One market. Members trade one index future with each other through a limit order book.
There are no bots. A house market maker always quotes fair value less and plus 0.25%.
Each session a few members, drawn at random, see that session's news early.
All levels are GAME PARAMETERS, not market data.
"""
import json
import math
import os
import pickle
import random
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------- Game Parameters ----------------
SEATS = 60                   # most members who can sign up
ROUNDS = 10                  # sessions: five trading days, each a cash session then an overnight session
ROUND_SECONDS = 45           # length of one session
S0 = 5000.0                  # starting spot index (fictional)
SHOCK_SD = 25.0              # index points per news shock
RATE, DIV_YIELD, GAME_YEARS = 0.045, 0.013, 0.25
TOTAL_CARRY = S0 * (RATE - DIV_YIELD) * GAME_YEARS                # 40 points over the whole game
CARRY_PER_ROUND = TOTAL_CARRY / ROUNDS                            # 4.0 points a session
CONTRACT_VALUE = 5_000       # USD value of one contract at the starting index, so USD 1 a point
MULTIPLIER = CONTRACT_VALUE / S0
POS_LIMIT = 10               # contracts, long or short, per member
START_CAPITAL = 100_000      # USD each member starts with
MARGIN_PER_CONTRACT = 1_000  # USD of capital tied up per contract held
CLUE_HOLDERS = 3             # members who see each session's news early (game master can change it)
TICK = 0.25
HOUSE = SEATS                # the house account sits after the last member seat
HOUSE_FEE = 0.0025           # house bid = fair x (1 - 0.25%), offer = fair x (1 + 0.25%)
HOUSE_SIZE = 10
HOUSE_LIMIT = 250
FUT_FEE_PTS = 0.05           # exchange fee per contract per side, in index points
REFRESH_SECONDS = 2.0        # how often the house re-quotes and the chart takes a point
MAX_ORDER_QTY = 20
PRICE_BAND = 0.10            # orders more than 10% from fair value are refused
BOOK_LEVELS = 5              # price levels shown each side of the order book


# ---------------- Game Setup (changed by the game master before a game starts) ----------------
SETUP = {   # key: (label, lowest, highest)
    "days": ("Trading Days", 1, 10),
    "seconds": ("Seconds Per Session", 10, 900),
    "holders": ("Clue Holders Each Session", 0, 10),
    "capital": ("Starting Capital", 1_000, 10_000_000),
    "contractValue": ("Contract Value", 100, 1_000_000),
    "margin": ("Margin Per Contract", 0, 1_000_000),
    "limit": ("Position Limit", 1, 100),
}


def settings():
    return {"days": ROUNDS // 2, "seconds": ROUND_SECONDS, "holders": CLUE_HOLDERS, "capital": START_CAPITAL,
            "contractValue": CONTRACT_VALUE, "margin": MARGIN_PER_CONTRACT, "limit": POS_LIMIT}


def apply_settings(new):
    """Check and apply setup values. Returns an error message, or an empty string when applied."""
    global ROUNDS, ROUND_SECONDS, CLUE_HOLDERS, START_CAPITAL, CONTRACT_VALUE, MARGIN_PER_CONTRACT
    global POS_LIMIT, MULTIPLIER, CARRY_PER_ROUND
    s = settings()
    for key, (label, lo, hi) in SETUP.items():
        if key in new:
            v = new[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                    or v != int(v) or not lo <= v <= hi:
                return f"{label} Must Be A Whole Number From {lo:,} To {hi:,}."
            s[key] = int(v)
    ROUNDS, ROUND_SECONDS, CLUE_HOLDERS = s["days"] * 2, s["seconds"], s["holders"]
    START_CAPITAL, CONTRACT_VALUE, MARGIN_PER_CONTRACT, POS_LIMIT = s["capital"], s["contractValue"], s["margin"], s["limit"]
    MULTIPLIER = CONTRACT_VALUE / S0
    CARRY_PER_ROUND = TOTAL_CARRY / ROUNDS
    return ""


JOIN_URL = ""                # address players on the same network use; set at start-up with --lan


def lan_addresses():
    """This computer's addresses on the local network, most likely first. A VPN adds extra ones."""
    found = []

    def add(ip):
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    for nic in ("en0", "en1"):                  # macOS: the Wi-Fi or Ethernet address, not a VPN tunnel
        try:
            out = subprocess.run(["ipconfig", "getifaddr", nic], capture_output=True, text=True, timeout=2)
            add(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass
    try:                                        # the address used for outside traffic (no data is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            add(sock.getsockname()[0])
    except OSError:
        pass
    return found


def to_tick(p):
    return round(p / TICK) * TICK


class Book:
    """Limit order book: price-time priority, trade at the resting price, pre-trade limits."""

    def __init__(self, limits):
        self.bids, self.asks = [], []          # [price, id, account, qty]
        self.limits = limits
        n = len(limits)
        self.pos = [0] * n
        self.cash = [0.0] * n                  # index points
        self.fee_paid = [0.0] * n              # index points
        self.avg = [0.0] * n                   # average entry price of the open position
        self.realised = [0.0] * n              # profit locked in by closing trades, index points
        self.fees = 0.0                        # fee income of the house, index points
        self.seq = 0
        self.last = None
        self.last_round = None
        self.tape = []

    def cancel_all(self, t):
        self.bids = [o for o in self.bids if o[2] != t]
        self.asks = [o for o in self.asks if o[2] != t]

    def cancel(self, t, oid):
        n = len(self.bids) + len(self.asks)
        self.bids = [o for o in self.bids if not (o[2] == t and o[1] == oid)]
        self.asks = [o for o in self.asks if not (o[2] == t and o[1] == oid)]
        return n != len(self.bids) + len(self.asks)

    def open_qty(self, t, side):
        return sum(o[3] for o in (self.bids if side == "B" else self.asks) if o[2] == t)

    def room(self, t, side):
        """Contracts this account may still add on one side: limit, less position, less resting orders."""
        if side == "B":
            return self.limits[t] - self.pos[t] - self.open_qty(t, "B")
        return self.limits[t] + self.pos[t] - self.open_qty(t, "S")

    def track(self, t, dq, p):
        """Keep the average entry price and realised profit for one fill (call before pos changes)."""
        pos = self.pos[t]
        if pos == 0 or (pos > 0) == (dq > 0):
            self.avg[t] = (self.avg[t] * abs(pos) + p * abs(dq)) / (abs(pos) + abs(dq))
        else:
            closed = min(abs(dq), abs(pos))
            self.realised[t] += closed * (p - self.avg[t]) * (1 if pos > 0 else -1)
            if abs(dq) > abs(pos):
                self.avg[t] = p
            elif abs(dq) == abs(pos):
                self.avg[t] = 0.0

    def submit(self, t, side, price, qty, rnd=0, post_only=False):
        cut = max(qty - max(self.room(t, side), 0), 0)
        qty -= cut
        filled = 0
        if qty <= 0:
            return {"filled": 0, "rested": 0, "cut": cut}
        book = self.asks if side == "B" else self.bids
        book.sort(key=lambda o: (o[0], o[1]) if side == "B" else (-o[0], o[1]))
        if post_only and book and (book[0][0] <= price if side == "B" else book[0][0] >= price):
            return {"filled": 0, "rested": 0, "cut": cut}      # a passive quote never takes
        i = 0
        while qty > 0 and i < len(book):
            best = book[i]
            if not (best[0] <= price if side == "B" else best[0] >= price):
                break
            if best[2] == t:                   # never trade with yourself
                i += 1
                continue
            fill = min(qty, best[3])
            buyer, seller = (t, best[2]) if side == "B" else (best[2], t)
            self.track(buyer, fill, best[0])
            self.track(seller, -fill, best[0])
            self.pos[buyer] += fill
            self.pos[seller] -= fill
            self.cash[buyer] -= fill * best[0]
            self.cash[seller] += fill * best[0]
            for party in (buyer, seller):      # exchange fee, paid to the house
                if party != HOUSE:
                    self.cash[party] -= FUT_FEE_PTS * fill
                    self.fee_paid[party] += FUT_FEE_PTS * fill
                    self.cash[HOUSE] += FUT_FEE_PTS * fill
                    self.fees += FUT_FEE_PTS * fill
            self.last = best[0]
            self.last_round = rnd
            self.tape.append({"p": best[0], "q": fill, "b": buyer, "s": seller, "r": rnd, "taker": t})
            qty -= fill
            filled += fill
            best[3] -= fill
            if best[3] == 0:
                book.pop(i)
        if qty > 0:
            self.seq += 1
            (self.bids if side == "B" else self.asks).append([price, self.seq, t, qty])
        return {"filled": filled, "rested": qty, "cut": cut}


class Game:
    def __init__(self):
        self.humans = {}                        # seat -> token of the member in it
        self.names = {}                         # seat -> name (kept after a member leaves)
        self.rng = random.Random()
        self.reset()

    def reset(self):
        """New game: fresh news, flat accounts. Members who signed up keep their seats."""
        self.shocks = [self.rng.gauss(0, SHOCK_SD) for _ in range(ROUNDS)]
        self.game_id = self.rng.randrange(10 ** 9)    # lets a browser tell one game from the next
        self.book = Book([POS_LIMIT] * SEATS + [HOUSE_LIMIT])
        self.names = {t: n for t, n in self.names.items() if t in self.humans}
        self.spot = S0
        self.mtm = []                           # daily settlement records
        self.round = 0                          # session index: even = cash session, odd = overnight
        self.pending = 0.0                      # overnight news the closed cash index has not printed yet
        self.news_out = False                   # has this session's shock gone public
        self.status = "waiting"
        self.remaining = ROUND_SECONDS
        self.deadline = None
        self.settle = None
        self.news = []
        self.holders = {}                       # session index -> seats that see its news early
        self.clue_count = {}                    # seat -> clues received this game
        self.checks = []                        # end-of-session proof that profits sum to zero
        self.hist = [[self.spot, None, 0]]      # spot, last futures trade, session index
        self.next_refresh = 0
        self.next_hist = 0
        self.house_quote()

    # ---------- trading hours ----------
    # Cash session = the stock market is open and the index prints live.
    # Overnight session = the stock market is closed and the index is frozen at its close,
    # while the future keeps trading. Overnight news reaches the index only at the next open.
    def day(self):
        return min(self.round, ROUNDS - 1) // 2 + 1

    def cash_open(self):
        return self.round % 2 == 0 and self.status != "settled"

    def session(self, r=None):
        return "Cash Session" if (self.round if r is None else r) % 2 == 0 else "Overnight Session"

    def fair(self):
        return self.spot + self.pending + CARRY_PER_ROUND * (ROUNDS - self.round)

    def house_quote(self):
        # The house only posts quotes. It never takes a member's order, even a stale one after news.
        self.book.cancel_all(HOUSE)
        f = self.fair()
        self.book.submit(HOUSE, "B", to_tick(f * (1 - HOUSE_FEE)), HOUSE_SIZE, self.round + 1, post_only=True)
        self.book.submit(HOUSE, "S", to_tick(f * (1 + HOUSE_FEE)), HOUSE_SIZE, self.round + 1, post_only=True)

    # ---------- private clues ----------
    def draw_clues(self):
        """Pick who sees this session's news early: random, favouring members with the fewest clues so far."""
        if self.status == "settled" or self.round in self.holders:
            return
        people = list(self.humans)
        self.rng.shuffle(people)
        people.sort(key=lambda t: self.clue_count.get(t, 0))
        chosen = people[:max(CLUE_HOLDERS, 0)]
        self.holders[self.round] = chosen
        for t in chosen:
            self.clue_count[t] = self.clue_count.get(t, 0) + 1

    # ---------- clock ----------
    def start(self):
        if self.status in ("waiting", "paused"):
            self.status = "open"
            self.deadline = time.time() + self.remaining
            self.draw_clues()

    def pause(self):
        if self.status == "open":
            self.remaining = max(self.deadline - time.time(), 0)
            self.status = "paused"

    def release_news(self):
        """This session's shock goes public, midway through the session."""
        if self.status == "settled" or self.news_out:
            return
        shock = self.shocks[self.round]
        self.news_out = True
        if self.round % 2 == 0:
            self.spot += shock                  # cash hours: the index moves at once
            shown = self.spot
        else:
            self.pending += shock               # overnight: only the future can react
            shown = None
        self.news.append({"r": self.round + 1, "day": self.day(), "session": self.session(),
                          "shock": shock, "spot": shown, "i": len(self.hist) - 1})
        self.house_quote()

    def zero_sum_check(self, label):
        """Every buy is someone's sell, so members' profit plus the house's must be zero. Record the proof."""
        members = sum(self.pnl(t) for t in range(SEATS))
        house = self.pnl(HOUSE)
        positions = sum(self.book.pos)
        self.checks.append({"label": label, "members": members, "house": house, "total": members + house,
                            "positions": positions, "ok": abs(members + house) < 0.005 and positions == 0})

    def next_round(self):
        """End the session: the cash close (daily settlement) or the next morning's open."""
        if self.status == "settled":
            return
        self.release_news()
        self.zero_sum_check(f"Day {self.day()}, {self.session()}")
        if self.round % 2 == 0:                 # cash close: daily settlement, mark to market
            self.mtm.append({"r": f"Day {self.day()}", "px": self.mark(),
                             "eq": [self.pnl(t) for t in range(SEATS + 1)]})
        self.spot += CARRY_PER_ROUND + self.pending     # after a night, the index gaps at the open
        self.pending = 0.0
        self.round += 1
        self.news_out = False
        if self.round >= ROUNDS:                # final settlement at the opening print
            self.status = "settled"
            self.settle = self.spot
            self.book.bids, self.book.asks = [], []
            self.hist.append([self.spot, self.book.last, ROUNDS - 1])
            self.mtm.append({"r": "Final", "px": self.settle,
                             "eq": [self.pnl(t) for t in range(SEATS + 1)]})
            self.zero_sum_check("Final Settlement")
        else:
            self.remaining = ROUND_SECONDS
            self.deadline = time.time() + ROUND_SECONDS
            self.house_quote()
            if self.status == "open":
                self.draw_clues()

    def advance(self):
        """Game master skip: release the news if it is not out yet, otherwise end the session."""
        if not self.news_out:
            self.release_news()
        else:
            self.next_round()

    def tick(self):
        if self.status != "open":
            return
        now = time.time()
        if now >= self.deadline:
            self.next_round()
            return
        if not self.news_out and now >= self.deadline - ROUND_SECONDS / 2:
            self.release_news()
        if now >= self.next_refresh:
            self.next_refresh = now + REFRESH_SECONDS
            self.house_quote()
        if now >= self.next_hist:               # about 450 chart points a game, however long it runs
            self.next_hist = now + max(REFRESH_SECONDS, ROUNDS * ROUND_SECONDS / 450)
            self.hist.append([self.spot, self.book.last, self.round])

    def secs(self):
        if self.status == "open":
            return max(self.deadline - time.time(), 0)
        return self.remaining if self.status in ("waiting", "paused") else 0

    # ---------- members ----------
    def seat_of(self, token):
        if token:
            for seat, tok in self.humans.items():
                if tok == token:
                    return seat
        return None

    def clean(self, t):
        b = self.book
        return b.pos[t] == 0 and b.cash[t] == 0 and not b.open_qty(t, "B") and not b.open_qty(t, "S")

    def join(self, token, name):
        name = " ".join(str(name or "").split())[:20]
        if not token or not name:
            return {"ok": False, "msg": "Enter Your Name."}
        if self.seat_of(token) is not None:
            return {"ok": True, "msg": "You Are Already Signed Up."}
        if any(self.names.get(t, "").casefold() == name.casefold() for t in self.humans):
            return {"ok": False, "msg": "That Name Is Taken. Add An Initial Or A Number."}
        free = [t for t in range(SEATS) if t not in self.humans and self.clean(t)]
        if not free:
            return {"ok": False, "msg": "The Game Is Full."}
        self.humans[free[0]], self.names[free[0]] = token, name     # seat number = order of sign-up
        return {"ok": True, "msg": ""}

    def leave(self, seat):
        """The member's orders are pulled. Any open position stays on the books under their name."""
        if seat in self.humans:
            self.book.cancel_all(seat)
            del self.humans[seat]

    def display(self, t):
        if t == HOUSE:
            return "House"
        if t in self.humans:
            return self.names.get(t, "Member")
        return f"{self.names[t]} (Left)" if t in self.names else f"Seat {t + 1}"

    # ---------- money ----------
    def mark(self):
        if self.settle is not None:
            return self.settle
        b = self.book
        if b.last is not None and b.last_round == self.round + 1:
            return b.last                       # a trade this session sets the mark
        if b.bids and b.asks:                   # otherwise the mid of the best bid and offer
            return (max(o[0] for o in b.bids) + min(o[0] for o in b.asks)) / 2
        return self.fair()

    def pnl(self, t):
        return (self.book.cash[t] + self.book.pos[t] * self.mark()) * MULTIPLIER

    def equity(self, t):
        return START_CAPITAL + self.pnl(t)

    def set_limit(self, t):
        """Buying power: capital caps the contracts a member can hold, on top of the position limit."""
        by_capital = int(self.equity(t) // MARGIN_PER_CONTRACT) if MARGIN_PER_CONTRACT > 0 else POS_LIMIT
        self.book.limits[t] = max(min(POS_LIMIT, by_capital), 0)
        return self.book.limits[t]

    def order(self, seat, side, price, qty):
        if self.status != "open":
            return {"ok": False, "msg": "The Market Is Not Open."}
        if seat not in self.humans:
            return {"ok": False, "msg": "Sign Up First."}
        if side not in ("B", "S") or not isinstance(qty, int) or not (1 <= qty <= MAX_ORDER_QTY):
            return {"ok": False, "msg": f"Quantity Must Be 1 To {MAX_ORDER_QTY} Contracts."}
        if not isinstance(price, (int, float)) or not math.isfinite(price):
            return {"ok": False, "msg": "Enter A Price And A Quantity."}
        price = to_tick(price)
        if abs(price / self.fair() - 1) > PRICE_BAND:
            return {"ok": False, "msg": "Price Is More Than 10% From Fair Value."}
        eff = self.set_limit(seat)
        r = self.book.submit(seat, side, price, qty, self.round + 1)
        parts = []
        if r["filled"]:
            parts.append(f"Filled {r['filled']}")
        if r["rested"]:
            parts.append(f"Resting {r['rested']} At {price:,.2f}")
        if r["cut"]:
            parts.append(f"Cut {r['cut']} By " + ("The Position Limit" if eff >= POS_LIMIT else "Your Buying Power"))
        if not r["filled"] and not r["rested"]:
            parts.append("Your Position And Resting Orders Already Use The Limit")
        return {"ok": True, "msg": ". ".join(parts) + "."}

    # ---------- views ----------
    def clock(self):
        secs = self.secs()
        return {"status": self.status, "gameId": self.game_id, "day": self.day(), "days": ROUNDS // 2,
                "session": self.session(),
                "cashOpen": self.cash_open(), "newsOut": self.news_out, "secondsLeft": secs,
                "newsIn": max(secs - ROUND_SECONDS / 2, 0), "spot": self.spot, "fair": self.fair(),
                "indicatedOpen": self.spot + self.pending + CARRY_PER_ROUND,
                "carryLeft": CARRY_PER_ROUND * (ROUNDS - self.round), "last": self.book.last,
                "settle": self.settle, "hist": self.hist, "news": self.news,
                "volume": sum(x["q"] for x in self.book.tape), "people": len(self.humans), "seats": SEATS,
                "joinUrl": JOIN_URL}

    def overview(self):
        """What the projector page shows: prices and public news only, nothing private."""
        return self.clock()

    def levels(self, orders, seat, reverse):
        agg = {}
        for p, _, t, q in orders:
            lv = agg.setdefault(p, {"p": p, "q": 0, "you": 0, "house": 0})
            lv["q"] += q
            if t == seat:
                lv["you"] += q
            if t == HOUSE:
                lv["house"] += q
        return sorted(agg.values(), key=lambda lv: lv["p"], reverse=reverse)[:BOOK_LEVELS]

    def top(self, n, seat=None):
        rows = [{"name": self.display(t), "seat": t, "pnl": self.pnl(t), "you": t == seat} for t in self.humans]
        return sorted(rows, key=lambda r: -r["pnl"])[:n]

    def accounts(self):
        """Every account that matters: seated members, members who left with a position, and the house."""
        b, mark, rows = self.book, self.mark(), []
        now = self.holders.get(self.round, [])
        for t in list(range(SEATS)) + [HOUSE]:
            if t != HOUSE and t not in self.humans and self.clean(t):
                continue
            rows.append({"seat": t, "name": self.display(t), "here": t in self.humans or t == HOUSE,
                         "pos": b.pos[t], "avg": b.avg[t], "pnl": self.pnl(t),
                         "equity": None if t == HOUSE else self.equity(t),
                         "realised": b.realised[t] * MULTIPLIER,
                         "unrealised": b.pos[t] * (mark - b.avg[t]) * MULTIPLIER,
                         "fees": b.fee_paid[t] * MULTIPLIER,
                         "openBuy": b.open_qty(t, "B"), "openSell": b.open_qty(t, "S"),
                         "clues": self.clue_count.get(t, 0), "clueNow": t in now})
        return rows

    def state(self, seat):
        b = self.book
        eff = self.set_limit(seat)
        mark, pos = self.mark(), b.pos[seat]
        holders = self.holders.get(self.round, [])
        clue = None
        if seat in holders and self.status != "settled":
            clue = {"id": self.round, "value": self.shocks[self.round], "public": self.news_out,
                    "holders": len(holders)}
        margin_used = abs(pos) * MARGIN_PER_CONTRACT
        me = {
            "seat": seat, "pos": pos, "avg": b.avg[seat], "mark": mark, "pnl": self.pnl(seat),
            "capital": START_CAPITAL, "equity": self.equity(seat),
            "realisedUsd": b.realised[seat] * MULTIPLIER, "unrealisedUsd": pos * (mark - b.avg[seat]) * MULTIPLIER,
            "feesUsd": b.fee_paid[seat] * MULTIPLIER, "marginUsed": margin_used,
            "freeCapital": self.equity(seat) - margin_used, "effLimit": eff,
            "marginCall": self.equity(seat) < margin_used,
            "roomBuy": max(b.room(seat, "B"), 0), "roomSell": max(b.room(seat, "S"), 0),
            "orders": [{"id": o[1], "side": "Buy", "p": o[0], "q": o[3]} for o in b.bids if o[2] == seat]
                    + [{"id": o[1], "side": "Sell", "p": o[0], "q": o[3]} for o in b.asks if o[2] == seat],
            "clue": clue, "cluesSoFar": self.clue_count.get(seat, 0),
            "myFair": self.fair() + (clue["value"] if clue and not clue["public"] else 0),
            "mtm": [{"r": m["r"], "px": m["px"], "eq": m["eq"][seat]} for m in self.mtm],
        }
        out = self.clock()
        out.update({
            "joined": True, "name": self.names.get(seat, "Member"), "me": me,
            "params": {"limit": POS_LIMIT, "mult": MULTIPLIER, "contractValue": CONTRACT_VALUE, "fee": HOUSE_FEE,
                       "houseSize": HOUSE_SIZE, "marginPer": MARGIN_PER_CONTRACT, "capital": START_CAPITAL,
                       "futFeeUsd": FUT_FEE_PTS * MULTIPLIER, "clueHolders": CLUE_HOLDERS,
                       "roundSeconds": ROUND_SECONDS},
            "book": {"asks": self.levels(b.asks, seat, False), "bids": self.levels(b.bids, seat, True)},
            "tape": [{"p": x["p"], "q": x["q"], "r": x["r"],
                      "you": "Bought" if x["b"] == seat else "Sold" if x["s"] == seat else "",
                      "house": HOUSE in (x["b"], x["s"])} for x in b.tape[-30:]][::-1],
            "top5": self.top(5, seat), "board": None,
        })
        if self.status == "settled":            # final standings are public once the game is over
            rows = self.accounts()
            out.update(board=rows, sumPnl=sum(r["pnl"] for r in rows), sumPos=sum(r["pos"] for r in rows))
        return out

    def monitor(self):
        """Everything, for the game master only: all accounts, the full book and tape with names, hidden news."""
        b = self.book
        rows = self.accounts()
        out = self.clock()
        out.update({
            "accounts": rows, "sumPnl": sum(r["pnl"] for r in rows), "sumPos": sum(r["pos"] for r in rows),
            "feesUsd": b.fees * MULTIPLIER, "mark": self.mark(),
            "book": {"asks": sorted(({"p": o[0], "q": o[3], "who": self.display(o[2])} for o in b.asks),
                                    key=lambda o: o["p"]),
                     "bids": sorted(({"p": o[0], "q": o[3], "who": self.display(o[2])} for o in b.bids),
                                    key=lambda o: -o["p"])},
            "tape": [{"p": x["p"], "q": x["q"], "r": x["r"], "buyer": self.display(x["b"]),
                      "seller": self.display(x["s"]), "taker": self.display(x["taker"])}
                     for x in b.tape[-80:]][::-1],
            "schedule": [{"r": r + 1, "day": r // 2 + 1, "session": self.session(r), "shock": self.shocks[r],
                          "state": "Public" if r < self.round or (r == self.round and self.news_out) else
                                   "This Session" if r == self.round else "Later",
                          "holders": [self.display(t) for t in self.holders.get(r, [])]} for r in range(ROUNDS)],
            "finalSpot": S0 + CARRY_PER_ROUND * ROUNDS + sum(self.shocks), "checks": self.checks,
            "settings": dict(settings(), mult=MULTIPLIER, houseFee=HOUSE_FEE,
                             limits={k: [lo, hi] for k, (_, lo, hi) in SETUP.items()}),
        })
        return out


GAME = Game()
LOCK = threading.RLock()
ADMIN_KEY = os.environ.get("GAME_ADMIN_KEY", "")   # optional key for a game master on another computer
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "game_state.pkl")
STATE_VERSION = 8            # raise when the shape of Game changes, so an old save is ignored


def save():
    with LOCK:
        blob = pickle.dumps((STATE_VERSION, time.time(), settings(), GAME))
    with open(STATE_FILE + ".tmp", "wb") as f:
        f.write(blob)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def load():
    """Bring back the last saved game after a crash or restart. A running game comes back paused."""
    global GAME
    try:
        with open(STATE_FILE, "rb") as f:
            version, saved_at, setup, game = pickle.load(f)
    except Exception:
        return False
    if version != STATE_VERSION or len(game.book.pos) != SEATS + 1 or apply_settings(setup):
        return False
    if game.status == "open":
        game.remaining = max(game.deadline - saved_at, 0)
        game.status = "paused"
    GAME = game
    return True


def loop():
    last_save = 0
    while True:
        try:
            with LOCK:
                GAME.tick()
            if time.time() - last_save > 3:
                last_save = time.time()
                save()
        except Exception as e:                  # one bad tick must not stop the game
            print("loop error:", repr(e), file=sys.stderr)
        time.sleep(0.25)


def admin_action(d):
    a = d.get("action")
    if a in ("start", "pause", "reset"):
        getattr(GAME, a)()
    elif a == "next":
        GAME.advance()
    elif a == "free":
        for seat in list(GAME.humans):
            GAME.leave(seat)
    elif a == "kick":
        GAME.leave(int(d.get("seat", -1)))
    elif a == "holders":                        # can change during a game; applies from the next session
        err = apply_settings({"holders": d.get("value")})
        if err:
            return {"ok": False, "msg": err}
    elif a == "setup":                          # everything else only between games
        if GAME.status != "waiting":
            return {"ok": False, "msg": "Setup Can Only Change Before A Game Starts. Press New Game First."}
        values = d.get("values")
        err = apply_settings(values) if isinstance(values, dict) else "Bad Request."
        if err:
            return {"ok": False, "msg": err}
        GAME.reset()                            # members keep their seats
    else:
        return {"ok": False, "msg": "Unknown Action."}
    return {"ok": True, "msg": ""}


PAGES = {"/": "index.html", "/overview": "overview.html", "/monitor": "monitor.html"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def is_admin(self):
        # A tunnel or proxy makes every visitor look local, so a forwarded request is never the game master.
        forwarded = any(h in self.headers for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded",
                                                    "X-Real-Ip", "Cf-Connecting-Ip"))
        local = self.client_address[0] in ("127.0.0.1", "::1") and not forwarded
        return local or (ADMIN_KEY != "" and self.headers.get("X-Admin-Key") == ADMIN_KEY)

    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in PAGES:
            with open(os.path.join(HERE, PAGES[u.path]), "rb") as f:
                return self.send(f.read(), "text/html; charset=utf-8")
        with LOCK:
            if u.path == "/api/state":
                seat = GAME.seat_of(q.get("token", [None])[0])
                if seat is None:
                    out = {"joined": False, "people": len(GAME.humans), "seats": SEATS, "status": GAME.status}
                else:
                    out = GAME.state(seat)
                out["isAdmin"] = self.is_admin()
                return self.send(out)
            if u.path == "/api/overview":
                return self.send(GAME.overview())
            if u.path == "/api/monitor":
                if not self.is_admin():
                    return self.send({"ok": False, "msg": "Game Master Only."}, code=403)
                return self.send(GAME.monitor())
        self.send({"error": "Not Found"}, code=404)

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(d, dict):
                raise ValueError
        except Exception:
            return self.send({"ok": False, "msg": "Bad Request."}, code=400)
        try:
            with LOCK:
                res = self.handle_post(d)
        except Exception as e:                  # a bad request must never take the server down
            print("request error:", self.path, repr(e), file=sys.stderr)
            return self.send({"ok": False, "msg": "That Request Could Not Be Read."}, code=400)
        self.send(res, code=res.pop("code", 200))

    def handle_post(self, d):
        token = d.get("token")
        if self.path == "/api/admin":
            if not self.is_admin():
                return {"ok": False, "msg": "Game Master Only.", "code": 403}
            return admin_action(d)
        if self.path == "/api/join":
            return GAME.join(token, d.get("name"))
        seat = GAME.seat_of(token)
        if seat is None:
            return {"ok": False, "msg": "Sign Up First."}
        if self.path == "/api/leave":
            GAME.leave(seat)
            return {"ok": True, "msg": ""}
        if self.path == "/api/order":
            price, qty = d.get("price"), d.get("qty")
            if isinstance(price, bool) or isinstance(qty, bool) or not isinstance(price, (int, float)) \
                    or not isinstance(qty, int):
                return {"ok": False, "msg": "Enter A Price And A Quantity."}
            return GAME.order(seat, d.get("side"), float(price), qty)
        if self.path == "/api/cancel":
            ok = GAME.book.cancel(seat, int(d.get("id", -1)))
            return {"ok": ok, "msg": "Order Cancelled." if ok else "Order Not Found."}
        return {"ok": False, "msg": "Not Found.", "code": 404}


if __name__ == "__main__":
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else 7848
    host = "0.0.0.0" if "--lan" in sys.argv else "127.0.0.1"   # --lan lets others on the same network join
    if "--fresh" not in sys.argv and load():
        print("Restored the last saved game (a running game comes back paused). Use --fresh to ignore it.")
    threading.Thread(target=loop, daemon=True).start()
    print(f"Game master (this computer only):  http://localhost:{port}/monitor")
    print(f"Projector view:                    http://localhost:{port}/overview")
    if host == "0.0.0.0":
        ips = lan_addresses()
        JOIN_URL = f"http://{ips[0] if ips else '<this computer address>'}:{port}"
        print(f"Players on the same network join:  {JOIN_URL}")
        for ip in ips[1:]:
            print(f"  (if that does not open, try     http://{ip}:{port})")
    else:
        print(f"Players: only this computer can open http://localhost:{port}. "
              "Start with --lan to let other computers join.")
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((host, port), Handler).serve_forever()
