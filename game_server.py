"""
Traders@UST Futures Trading Game: game server.

Run:  python3 game_server.py [port] [--lan] [--fresh]
      Members open  http://<this computer>:7848           (sign up, then trade)
      Projector     http://localhost:7848/overview        (price chart and news)
      Game master   http://localhost:7848/monitor         (everything; this computer only)

One market. Members trade one index future with each other through a limit order book.
There are no bots and no trading fees. A house market maker quotes around fair value, about
2.5 points either side, and leans its prices with the order flow it absorbs: when members keep
buying from it, it raises its prices, so informed trading shows up in the futures before the news
reaches the spot index. Each session a few members, drawn at random, see that session's news early.
All levels are GAME PARAMETERS, not market data.
"""
import base64
import hmac
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
import urllib.request
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------- Game Parameters ----------------
SEATS = 60                   # most members who can sign up
ROUNDS = 10                  # sessions: five trading days, each a cash session then an overnight session
ROUND_SECONDS = 120          # length of one session: 10 sessions of 2 minutes is 20 minutes of trading
S0 = 5000.0                  # starting spot index (fictional)
SHOCK_MIN, SHOCK_MAX = 12.0, 40.0   # news moves the index 12 to 40 points, up or down at random
NOISE_SD = 4.0               # random noise (Brownian motion): about 4 points a session, far smaller than any news
NOISE_STEP = 2.0             # seconds between noise steps
CONTRACT_VALUE = 5_000       # USD value of one contract at the starting index, so USD 1 a point
MULTIPLIER = CONTRACT_VALUE / S0
POS_LIMIT = 10               # contracts, long or short, per member
START_CAPITAL = 100_000      # USD each member starts with: the base of the scoreboard (there is no margin)
CLUE_HOLDERS = 3             # members who see each session's news early (game master can change it)
TICK = 0.25
HOUSE = SEATS                # the house account sits after the last member seat
HOUSE_SPREAD = 0.0005        # house bid = centre x (1 - 0.05%), offer = centre x (1 + 0.05%): about 2.5 points
HOUSE_SIZE = 5               # contracts the house shows at each of its price levels
HOUSE_LEVELS = 4             # price levels the house shows on each side
HOUSE_LEAN = 0.5             # points the house moves its prices per contract of net buying (or selling) it absorbs
LEAN_CAP = 75.0              # the most the house leans away from fair value, in points
HOUSE_LIMIT = 250
REFRESH_SECONDS = 2.0        # how often the house re-quotes and the chart takes a point
MAX_ORDER_QTY = 20
PRICE_BAND = 0.10            # orders more than 10% from fair value are refused
BOOK_LEVELS = 8              # price levels shown each side of the order book
CANDLES_PER_SESSION = 12     # chart candles in one session: 10-second candles when a session is 2 minutes
TAPE_KEEP = 200              # recent trades kept for the screens (counts and volume are kept separately)


# ---------------- Game Setup (changed by the game master before a game starts) ----------------
SETUP = {   # key: (label, lowest, highest)
    "days": ("Trading Days", 1, 10),
    "seconds": ("Seconds Per Session", 10, 900),
    "holders": ("Clue Holders Each Session", 0, 10),
    "capital": ("Starting Capital", 1_000, 10_000_000),
    "contractValue": ("Contract Value", 100, 1_000_000),
    "limit": ("Position Limit", 1, 100),
}


def settings():
    return {"days": ROUNDS // 2, "seconds": ROUND_SECONDS, "holders": CLUE_HOLDERS, "capital": START_CAPITAL,
            "contractValue": CONTRACT_VALUE, "limit": POS_LIMIT}


def params():
    """Rules every page may show: nothing private."""
    return {"limit": POS_LIMIT, "mult": MULTIPLIER, "contractValue": CONTRACT_VALUE, "spread": HOUSE_SPREAD,
            "spreadPts": S0 * HOUSE_SPREAD, "houseSize": HOUSE_SIZE, "lean": HOUSE_LEAN, "capital": START_CAPITAL,
            "clueHolders": CLUE_HOLDERS, "roundSeconds": ROUND_SECONDS, "days": ROUNDS // 2, "shockMin": SHOCK_MIN,
            "shockMax": SHOCK_MAX, "noiseSd": NOISE_SD,
            "start": S0}


def apply_settings(new):
    """Check and apply setup values. Returns an error message, or an empty string when applied."""
    global ROUNDS, ROUND_SECONDS, CLUE_HOLDERS, START_CAPITAL, CONTRACT_VALUE, POS_LIMIT, MULTIPLIER
    s = settings()
    for key, (label, lo, hi) in SETUP.items():
        if key in new:
            v = new[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                    or v != int(v) or not lo <= v <= hi:
                return f"{label} Must Be A Whole Number From {lo:,} To {hi:,}."
            s[key] = int(v)
    ROUNDS, ROUND_SECONDS, CLUE_HOLDERS = s["days"] * 2, s["seconds"], s["holders"]
    START_CAPITAL, CONTRACT_VALUE, POS_LIMIT = s["capital"], s["contractValue"], s["limit"]
    MULTIPLIER = CONTRACT_VALUE / S0
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
    """Limit order book: price-time priority, trade at the resting price, pre-trade limits, no fees."""

    def __init__(self, limits):
        self.bids, self.asks = [], []          # [price, id, account, qty]
        self.limits = limits
        n = len(limits)
        self.pos = [0] * n
        self.cash = [0.0] * n                  # index points
        self.avg = [0.0] * n                   # average entry price of the open position
        self.realised = [0.0] * n              # profit locked in by closing trades, index points
        self.seq = 0
        self.trades = 0                        # fills so far
        self.volume = 0                        # contracts traded so far
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

    def submit(self, t, side, price, qty, rnd=0, post_only=False, now=False):
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
            self.last = best[0]
            self.last_round = rnd
            self.trades += 1
            self.volume += fill
            self.tape.append({"p": best[0], "q": fill, "b": buyer, "s": seller, "r": rnd, "taker": t, "t": time.time()})
            if len(self.tape) > TAPE_KEEP:
                del self.tape[:len(self.tape) - TAPE_KEEP]
            qty -= fill
            filled += fill
            best[3] -= fill
            if best[3] == 0:
                book.pop(i)
        if qty > 0 and now:                    # a 'now' order fills what it can and the rest is dropped
            return {"filled": filled, "rested": 0, "cut": cut, "dropped": qty}
        if qty > 0:
            self.seq += 1
            (self.bids if side == "B" else self.asks).append([price, self.seq, t, qty])
        return {"filled": filled, "rested": qty, "cut": cut}


class Game:
    def __init__(self):
        self.humans = {}                        # seat -> token of the member in it
        self.names = {}                         # seat -> name (kept after a member leaves)
        self.intro = set()                      # seats that have finished the how-to-play introduction
        self.rng = random.Random()
        self.t = time.time()                    # the game clock: moved forward by advance_to()
        self.reset()

    def reset(self):
        """New game: fresh news, flat accounts. Members who signed up keep their seats."""
        self.shocks = [self.rng.choice((-1, 1)) * self.rng.uniform(SHOCK_MIN, SHOCK_MAX) for _ in range(ROUNDS)]
        self.noise_total = 0.0                  # all the random noise so far (it adds to the news)
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
        self.pos_at_news = {}                   # session index -> every account's position when the news hit
        self.ends = []                          # every account's profit or loss at the end of each session
        self.next_refresh = math.inf            # next noise step; set when the game starts
        self.new_phase()
        self.house_quote()
        self.candles = []                       # [open, high, low, close, volume, taker-buy volume, spot, fair, session, slot]
        self.new_candle()

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
        """What the future is worth on public news alone: spot plus overnight news spot has not printed yet."""
        return self.spot + self.pending

    # ---------- the house market maker ----------
    # The house does not know the news early, but it watches the orders it fills. When members keep
    # buying from it, it guesses they know something and raises its prices; when they keep selling,
    # it lowers them. So clue holders who trade push the futures away from spot before the news is
    # public, and everyone can see it. When the news comes out, and at each new session, the house
    # forgets the flow and centres on fair value again.
    def new_phase(self):
        """Start counting order flow afresh: at a new session and when the news goes public."""
        self.flow_base = self.book.pos[HOUSE]   # house position when this phase began
        self.tape_mark = self.book.trades       # trades before this point are older than the latest news
        self.flow = {"buy": 0, "sell": 0}       # contracts members bought (took offers) or sold (hit bids)

    def lean(self):
        """How far the house has moved its prices this phase: up when members bought from it."""
        return max(-LEAN_CAP, min(LEAN_CAP, HOUSE_LEAN * (self.flow_base - self.book.pos[HOUSE])))

    def house_quote(self):
        # The house only posts quotes. It never takes a member's order, even a stale one after news.
        # It shows a few price levels a side, each one further away by the lean of one level's size,
        # so a big order moves the price as it fills, just as if it had come in as several small ones.
        self.book.cancel_all(HOUSE)
        if self.status == "settled":
            return
        centre, step = self.fair() + self.lean(), HOUSE_LEAN * HOUSE_SIZE
        for k in range(HOUSE_LEVELS):
            self.book.submit(HOUSE, "B", to_tick(centre * (1 - HOUSE_SPREAD) - k * step), HOUSE_SIZE,
                             self.round + 1, post_only=True)
            self.book.submit(HOUSE, "S", to_tick(centre * (1 + HOUSE_SPREAD) + k * step), HOUSE_SIZE,
                             self.round + 1, post_only=True)

    def fut_price(self):
        """The futures mark price: the middle of the best bid and offer. It carries the house's lean on order
        flow and never goes stale the way an old trade does while the noise moves fair value."""
        b = self.book
        if b.bids and b.asks:
            return (max(o[0] for o in b.bids) + min(o[0] for o in b.asks)) / 2
        if b.trades > self.tape_mark:
            return b.last
        return self.fair()

    def drift(self, dt):
        """Random noise between the news (Brownian motion). Cash hours move spot; overnight moves only the futures,
        and spot catches up at the open, just like overnight news."""
        step = self.rng.gauss(0, NOISE_SD * math.sqrt(dt / ROUND_SECONDS))
        if self.round % 2 == 0:
            self.spot += step
        else:
            self.pending += step
        self.noise_total += step

    def signal(self):
        """Public hint that someone may know the news: how far the futures sit from fair value, and who is taking."""
        price = self.fut_price()
        return {"price": price, "gap": price - self.fair(), "buy": self.flow["buy"], "sell": self.flow["sell"],
                "preNews": not self.news_out and self.status != "settled"}

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
            self.deadline = self.t + self.remaining
            self.next_refresh = self.t + NOISE_STEP
            self.draw_clues()

    def pause(self):
        if self.status == "open":
            self.remaining = max(self.deadline - self.t, 0)
            self.status = "paused"

    def release_news(self):
        """This session's shock goes public, midway through the session."""
        if self.status == "settled" or self.news_out:
            return
        shock = self.shocks[self.round]
        hint = self.signal()                    # what the futures said before anyone public knew
        self.pos_at_news[self.round] = list(self.book.pos)   # each player's bet on this news
        self.book.bids, self.book.asks = [], []  # news cancels every waiting order, so no one is caught at an old price
        self.news_out = True
        if self.round % 2 == 0:
            self.spot += shock                  # cash hours: the index moves at once
            shown = self.spot
        else:
            self.pending += shock               # overnight: only the future can react
            shown = None
        self.news.append({"r": self.round + 1, "day": self.day(), "session": self.session(),
                          "shock": shock, "spot": shown, "i": len(self.candles) - 1,
                          "lead": hint["gap"], "flowBuy": hint["buy"], "flowSell": hint["sell"]})
        self.new_phase()
        self.house_quote()
        self.candle_mark()

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
        if self.round < ROUNDS - 1:             # the last session is scored at the final settlement
            self.ends.append([self.pnl(t) for t in range(SEATS + 1)])
        self.spot += self.pending               # after a night, the index gaps at the open
        self.pending = 0.0
        self.round += 1
        self.news_out = False
        if self.round >= ROUNDS:                # final settlement at the opening print
            self.status = "settled"
            self.settle = self.spot
            self.book.bids, self.book.asks = [], []
            c = self.candles[-1]                # the last candle closes at the settlement price
            c[1], c[2], c[3], c[6], c[7] = max(c[1], self.settle), min(c[2], self.settle), self.settle, self.settle, self.settle
            self.mtm.append({"r": "Final", "px": self.settle,
                             "eq": [self.pnl(t) for t in range(SEATS + 1)]})
            self.ends.append([self.pnl(t) for t in range(SEATS + 1)])
            self.zero_sum_check("Final Settlement")
        else:
            self.remaining = ROUND_SECONDS
            self.deadline = self.t + ROUND_SECONDS
            self.new_phase()
            self.house_quote()
            self.candle_mark()
            if self.status == "open":
                self.draw_clues()

    def advance(self):
        """Game master skip: release the news if it is not out yet, otherwise end the session."""
        if not self.news_out:
            self.release_news()
        else:
            self.next_round()

    def advance_to(self, now):
        """Run the game clock forward to `now`, one event at a time in time order: noise steps, the news at
        halfway and session ends. Nothing here depends on when it is called, so the same stored game always
        plays out the same way. That lets any server (or any Vercel request) catch the game up."""
        while self.status == "open":
            news_at = math.inf if self.news_out else self.deadline - ROUND_SECONDS / 2
            nxt = min(self.deadline, news_at, self.next_refresh)
            if nxt > now:
                break
            self.t = nxt
            if nxt == self.deadline:
                self.next_round()
            elif nxt == news_at:
                self.release_news()
            else:
                self.next_refresh = nxt + NOISE_STEP
                self.drift(NOISE_STEP)
                self.house_quote()
                self.candle_mark()
        self.t = max(self.t, now)
        self.candle_mark()

    def tick(self):
        self.advance_to(time.time())

    # ---------- chart candles ----------
    def slot(self):
        """Which candle of this session the game clock is in. It stops while the game is paused."""
        elapsed = ROUND_SECONDS - self.secs()
        return max(0, min(int(elapsed / (ROUND_SECONDS / CANDLES_PER_SESSION)), CANDLES_PER_SESSION - 1))

    def new_candle(self):
        p = self.fut_price()
        self.candles.append([p, p, p, p, 0, 0, self.spot, self.fair(), self.round, 0])

    def candle_mark(self, price=None, qty=0, buy=False):
        """Move the current candle to the futures price now (or a trade price) and add any traded volume.
        Every slot of the session gets a candle, even one with no events, so the chart never depends on
        when someone happened to look."""
        if self.status == "settled":
            return
        r, s = self.round, self.slot()
        c = self.candles[-1]
        while (c[8], c[9]) != (r, s):
            nxt = c[9] + 1 if c[8] == r else 0
            if c[8] == r and nxt > s:
                break                           # the clock never runs backwards, but stay safe
            p = self.fut_price()
            self.candles.append([p, p, p, p, 0, 0, self.spot, self.fair(), r, nxt])
            c = self.candles[-1]
        p = self.fut_price() if price is None else price
        c[1], c[2], c[3] = max(c[1], p), min(c[2], p), p
        c[4] += qty
        c[5] += qty if buy else 0
        c[6], c[7] = self.spot, self.fair()

    def secs(self):
        if self.status == "open":
            return max(self.deadline - self.t, 0)
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
            self.intro.discard(seat)

    def display(self, t):
        if t == HOUSE:
            return "House"
        if t in self.humans:
            return self.names.get(t, "Member")
        return f"{self.names[t]} (Left)" if t in self.names else f"Seat {t + 1}"

    # ---------- money ----------
    def mark(self):
        """Accounts are valued at the futures price now: a trade from before the latest news is out of date."""
        return self.settle if self.settle is not None else self.fut_price()

    def pnl(self, t):
        return (self.book.cash[t] + self.book.pos[t] * self.mark()) * MULTIPLIER

    def equity(self, t):
        return START_CAPITAL + self.pnl(t)

    def order(self, seat, side, price, qty, now=False):
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
        self.book.limits[seat] = POS_LIMIT
        n0 = self.book.trades
        r = self.book.submit(seat, side, price, qty, self.round + 1, now=now)
        for x in self.book.tape[len(self.book.tape) - (self.book.trades - n0):]:   # this order's fills
            x["pre"] = not self.news_out
            self.candle_mark(x["p"], x["q"], side == "B")
        self.flow["buy" if side == "B" else "sell"] += r["filled"]
        if r["filled"]:
            self.house_quote()                  # the house leans on the flow at once
        parts = []
        if r["filled"]:
            parts.append(f"{'Bought' if side == 'B' else 'Sold'} {r['filled']}")
        if r["rested"]:
            parts.append(f"Waiting To {'Buy' if side == 'B' else 'Sell'} {r['rested']} At {price:,.2f}")
        if r.get("dropped"):
            parts.append(f"{r['dropped']} Not Filled: No One Was {'Selling' if side == 'B' else 'Buying'} At Your Price")
        if r["cut"]:
            parts.append(f"Cut {r['cut']} By The Position Limit")
        if not r["filled"] and not r["rested"] and not r.get("dropped"):
            parts.append("Your Position And Waiting Orders Already Use The Limit")
        return {"ok": True, "msg": ". ".join(parts) + "."}

    # ---------- what each player learns ----------
    def report(self, t):
        """One row per finished session: did you have a clue, how big was your bet when the news hit, what did it make."""
        rows = []
        for r, end in enumerate(self.ends):
            before = self.ends[r - 1][t] if r else 0.0
            pos = self.pos_at_news.get(r, [0] * (SEATS + 1))[t]
            rows.append({"r": r + 1, "day": r // 2 + 1, "session": self.session(r), "shock": self.shocks[r],
                         "clue": t in self.holders.get(r, []), "pos": pos, "pnl": end[t] - before,
                         "lead": self.news[r]["lead"] if r < len(self.news) else 0.0})
        return rows

    def lessons(self):
        """Class results for the debrief: does knowing more, and betting bigger on it, pay?"""
        clue, plain, follow, against = [], [], [], []
        for r, end in enumerate(self.ends):
            pos_now = self.pos_at_news.get(r)
            if pos_now is None:
                continue
            lead = self.news[r]["lead"]
            for t in self.names:
                if t >= SEATS or (t not in self.humans and self.clean(t)):
                    continue
                pnl = end[t] - (self.ends[r - 1][t] if r else 0.0)
                pos = pos_now[t]
                if t in self.holders.get(r, []):
                    clue.append((abs(pos), pnl))
                else:
                    plain.append((abs(pos), pnl))
                    if pos and abs(lead) >= 1:
                        (follow if (pos > 0) == (lead > 0) else against).append(pnl)
        avg = lambda xs: sum(xs) / len(xs) if xs else None
        called = [n for n in self.news if abs(n["lead"]) >= 1]
        right = [n for n in called if (n["lead"] > 0) == (n["shock"] > 0)]
        return {"sessions": len(self.ends),
                "clueSize": avg([a for a, _ in clue]), "cluePnl": avg([p for _, p in clue]), "clueN": len(clue),
                "plainSize": avg([a for a, _ in plain]), "plainPnl": avg([p for _, p in plain]), "plainN": len(plain),
                "followPnl": avg(follow), "followN": len(follow), "againstPnl": avg(against), "againstN": len(against),
                "hinted": len(called), "hintedRight": len(right)}

    # ---------- views ----------
    def clock(self):
        secs = self.secs()
        return {"status": self.status, "gameId": self.game_id, "day": self.day(), "days": ROUNDS // 2,
                "round": self.round, "rounds": ROUNDS, "session": self.session(),
                "cashOpen": self.cash_open(), "newsOut": self.news_out, "secondsLeft": secs,
                "newsIn": max(secs - ROUND_SECONDS / 2, 0), "spot": self.spot, "fair": self.fair(),
                "pending": self.pending, "indicatedOpen": self.spot + self.pending, "last": self.book.last,
                "settle": self.settle, "candles": self.candles, "candlesPerSession": CANDLES_PER_SESSION,
                "candleSeconds": ROUND_SECONDS / CANDLES_PER_SESSION, "news": self.news, "signal": self.signal(),
                "volume": self.book.volume, "people": len(self.humans), "seats": SEATS,
                "joinUrl": JOIN_URL, "params": params()}

    def overview(self):
        """What the projector page shows: prices and public news only, nothing private."""
        out = self.clock()
        out["lessons"] = self.lessons() if self.status == "settled" else None
        out["introDone"] = len(self.intro & set(self.humans))
        out["top5"] = [{"name": r["name"], "pnl": r["pnl"]} for r in self.top(5)]   # the members' page shows this too
        return out

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
                         "openBuy": b.open_qty(t, "B"), "openSell": b.open_qty(t, "S"),
                         "clues": self.clue_count.get(t, 0), "clueNow": t in now, "intro": t in self.intro})
        return rows

    def state(self, seat):
        b = self.book
        b.limits[seat] = POS_LIMIT
        mark, pos = self.mark(), b.pos[seat]
        holders = self.holders.get(self.round, [])
        clue = None
        if seat in holders and self.status != "settled":
            clue = {"id": self.round, "value": self.shocks[self.round], "public": self.news_out,
                    "holders": len(holders)}
        me = {
            "seat": seat, "pos": pos, "avg": b.avg[seat], "mark": mark, "pnl": self.pnl(seat),
            "capital": START_CAPITAL, "equity": self.equity(seat),
            "realisedUsd": b.realised[seat] * MULTIPLIER, "unrealisedUsd": pos * (mark - b.avg[seat]) * MULTIPLIER,
            "roomBuy": max(b.room(seat, "B"), 0), "roomSell": max(b.room(seat, "S"), 0),
            "orders": [{"id": o[1], "side": "Buy", "p": o[0], "q": o[3]} for o in b.bids if o[2] == seat]
                    + [{"id": o[1], "side": "Sell", "p": o[0], "q": o[3]} for o in b.asks if o[2] == seat],
            "clue": clue, "cluesSoFar": self.clue_count.get(seat, 0),
            "myFair": self.fair() + (clue["value"] if clue and not clue["public"] else 0),
            "report": self.report(seat), "introDone": seat in self.intro,
        }
        out = self.clock()
        out.update({
            "joined": True, "name": self.names.get(seat, "Member"), "me": me,
            "book": {"asks": self.levels(b.asks, seat, False), "bids": self.levels(b.bids, seat, True)},
            "tape": [{"p": x["p"], "q": x["q"], "r": x["r"], "took": "Buy" if x["taker"] == x["b"] else "Sell",
                      "pre": x.get("pre", False), "t": x.get("t", 0),
                      "you": "Bought" if x["b"] == seat else "Sold" if x["s"] == seat else "",
                      "house": HOUSE in (x["b"], x["s"])} for x in b.tape[-40:]][::-1],
            "top5": self.top(5, seat), "board": None,
        })
        if self.status == "settled":            # final standings are public once the game is over
            rows = self.accounts()
            out.update(board=rows, sumPnl=sum(r["pnl"] for r in rows), sumPos=sum(r["pos"] for r in rows),
                       lessons=self.lessons())
        return out

    def monitor(self):
        """Everything, for the game master only: all accounts, the full book and tape with names, hidden news."""
        b = self.book
        rows = self.accounts()
        out = self.clock()
        out.update({
            "accounts": rows, "sumPnl": sum(r["pnl"] for r in rows), "sumPos": sum(r["pos"] for r in rows),
            "mark": self.mark(), "lean": self.lean(), "lessons": self.lessons(),
            "introDone": len(self.intro & set(self.humans)),
            "book": {"asks": sorted(({"p": o[0], "q": o[3], "who": self.display(o[2])} for o in b.asks),
                                    key=lambda o: o["p"]),
                     "bids": sorted(({"p": o[0], "q": o[3], "who": self.display(o[2])} for o in b.bids),
                                    key=lambda o: -o["p"])},
            "tape": [{"p": x["p"], "q": x["q"], "r": x["r"], "buyer": self.display(x["b"]),
                      "seller": self.display(x["s"]), "taker": self.display(x["taker"]), "pre": x.get("pre", False)}
                     for x in b.tape[-80:]][::-1],
            "schedule": [{"r": r + 1, "day": r // 2 + 1, "session": self.session(r), "shock": self.shocks[r],
                          "state": "Public" if r < self.round or (r == self.round and self.news_out) else
                                   "This Session" if r == self.round else "Later",
                          "holders": [self.display(t) for t in self.holders.get(r, [])]} for r in range(ROUNDS)],
            "finalSpot": S0 + sum(self.shocks) + self.noise_total, "noise": self.noise_total, "checks": self.checks,
            "settings": dict(settings(), mult=MULTIPLIER, spread=HOUSE_SPREAD, lean=HOUSE_LEAN,
                             limits={k: [lo, hi] for k, (_, lo, hi) in SETUP.items()}),
        })
        return out


GAME = Game()
LOCK = threading.RLock()
ADMIN_KEY = os.environ.get("GAME_ADMIN_KEY", "")   # optional key for a game master on another computer
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "game_state.pkl")
STATE_VERSION = 11           # raise when the shape of Game changes, so an old save is ignored


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
    game.t = time.time()
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


# ---------------- Where the game lives ----------------
# On one computer the game sits in memory and the loop above moves its clock. On Vercel every request can land on
# a different short-lived server, so the game is kept in Redis instead: a request loads it, runs its clock forward
# to now with advance_to() (the same stored game always plays out the same way), and a request that changes the
# game saves it back while holding a lock, so two changes can never overwrite each other.
class StoreError(Exception):
    """Redis could not be reached, or the game was too busy to change in time."""


class RestRedis:
    """Redis over HTTPS, the way Vercel's Upstash Redis integration offers it. Needs no extra packages."""

    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def cmd(self, *args):
        req = urllib.request.Request(self.url, data=json.dumps([str(a) for a in args]).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                out = json.loads(r.read())
        except Exception as e:
            raise StoreError(f"Redis request failed: {e!r}") from None
        if isinstance(out, dict) and out.get("error"):
            raise StoreError(f"Redis error: {out['error']}")
        return out.get("result") if isinstance(out, dict) else out


class TcpRedis:
    """Redis over its own protocol (a redis:// or rediss:// address). Uses the 'redis' package."""

    def __init__(self, url):
        import redis                            # only needed on a host that gives a redis:// address
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=8)

    def cmd(self, *args):
        try:
            return self.r.execute_command(*args)
        except Exception as e:
            raise StoreError(f"Redis request failed: {e!r}") from None


def find_redis(env):
    """The Redis connection Vercel gave this project, whatever prefix its variables were given."""
    for k, url in sorted(env.items()):
        if k.endswith("REST_API_URL") and url.startswith("http"):
            token = env.get(k[:-len("URL")] + "TOKEN")
            if token:
                return RestRedis(url, token)
    for k in ("UPSTASH_REDIS_REST_URL",):
        if env.get(k) and env.get("UPSTASH_REDIS_REST_TOKEN"):
            return RestRedis(env[k], env["UPSTASH_REDIS_REST_TOKEN"])
    for k, url in sorted(env.items()):
        if (k.endswith("REDIS_URL") or k.endswith("KV_URL")) and url.startswith(("redis://", "rediss://")):
            return TcpRedis(url)
    return None


# Redis is used on Vercel (it sets VERCEL=1) or when GAME_STORE=redis; anywhere else the game stays in memory.
USE_REDIS = bool(os.environ.get("VERCEL")) or os.environ.get("GAME_STORE") == "redis"
REDIS = find_redis(os.environ) if USE_REDIS else None
PREFIX = os.environ.get("GAME_REDIS_PREFIX", "futures-game")
K_STATE, K_VER, K_LOCK, K_KEY = (f"{PREFIX}:{n}" for n in ("state", "version", "lock", "adminkey"))
SAVE_EVERY = 6.0             # a read saves the caught-up clock when the stored one is this many seconds old
UNLOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
CACHE = {"ver": None, "text": None}          # the last game text this server instance read, and its version
wall_clock = time.time       # the time requests run the game to (tests replace it to fast-forward a game)


def pack(game):
    return base64.b64encode(zlib.compress(pickle.dumps((STATE_VERSION, settings(), game)), 6)).decode()


def unpack(text):
    try:
        version, setup, game = pickle.loads(zlib.decompress(base64.b64decode(text)))
    except Exception:
        return None
    if version != STATE_VERSION or len(game.book.pos) != SEATS + 1 or apply_settings(setup):
        return None
    return game


def redis_read():
    """The stored game text. A tiny version number is checked first, so a busy server skips the big download."""
    ver = REDIS.cmd("GET", K_VER)
    if ver is not None and ver == CACHE["ver"]:
        return CACHE["text"]
    ver, text = REDIS.cmd("MGET", K_VER, K_STATE)
    CACHE["ver"], CACHE["text"] = ver, text
    return text


def redis_write(game):
    text = pack(game)
    REDIS.cmd("SET", K_STATE, text)
    ver = REDIS.cmd("INCR", K_VER)
    CACHE["ver"], CACHE["text"] = str(ver), text


@contextmanager
def redis_lock(wait):
    """Only one request changes the game at a time. Yields False when wait is off and someone else holds it."""
    token = base64.b16encode(os.urandom(8)).decode()
    give_up = time.time() + (6.0 if wait else 0.0)
    while REDIS.cmd("SET", K_LOCK, token, "NX", "PX", "8000") not in (True, "OK"):
        if time.time() >= give_up:
            if wait:
                raise StoreError("The game is busy. Try again.")
            yield False
            return
        time.sleep(0.04)
    try:
        yield True
    finally:
        try:
            REDIS.cmd("EVAL", UNLOCK, 1, K_LOCK, token)
        except StoreError:
            pass                                # the lock expires by itself after a few seconds


@contextmanager
def game_session(change):
    """The game for one request, with its clock run forward to now. Changes are kept when the request ends."""
    if not USE_REDIS:
        with LOCK:
            GAME.advance_to(time.time())
            yield GAME
        return
    if REDIS is None:
        raise StoreError("No Redis database is connected to this Vercel project.")
    if not change:
        text = redis_read()
        game = unpack(text) if text else None
        if game is not None:
            stored_t = game.t
            game.advance_to(wall_clock())
            yield game
            if game.status == "open" and game.t - stored_t > SAVE_EVERY:   # keep the stored clock fresh
                with redis_lock(wait=False) as got:
                    if got and REDIS.cmd("GET", K_STATE) == text:
                        redis_write(game)
            return
    with redis_lock(wait=True):                 # a change, or the very first game
        CACHE["ver"] = None
        text = redis_read()
        game = (unpack(text) if text else None) or Game()
        game.advance_to(wall_clock())
        yield game
        redis_write(game)


def claimed_key():
    return REDIS.cmd("GET", K_KEY) if REDIS is not None else None


def admin_action(d, game=None):
    game = game or GAME
    a = d.get("action")
    if a in ("start", "pause", "reset"):
        getattr(game, a)()
    elif a == "next":
        game.advance()
    elif a == "free":
        for seat in list(game.humans):
            game.leave(seat)
    elif a == "kick":
        game.leave(int(d.get("seat", -1)))
    elif a == "holders":                        # can change during a game; applies from the next session
        err = apply_settings({"holders": d.get("value")})
        if err:
            return {"ok": False, "msg": err}
    elif a == "setup":                          # everything else only between games
        if game.status != "waiting":
            return {"ok": False, "msg": "Setup Can Only Change Before A Game Starts. Press New Game First."}
        values = d.get("values")
        err = apply_settings(values) if isinstance(values, dict) else "Bad Request."
        if err:
            return {"ok": False, "msg": err}
        game.reset()                            # members keep their seats
    else:
        return {"ok": False, "msg": "Unknown Action."}
    return {"ok": True, "msg": ""}


PAGES = {"/": "index.html", "/overview": "overview.html", "/monitor": "monitor.html"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def is_admin(self):
        """The computer running the game is the game master. Anyone else needs the key: GAME_ADMIN_KEY if it is
        set, otherwise (on Vercel) the first key typed on the monitor page becomes the key."""
        forwarded = any(h in self.headers for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded",
                                                    "X-Real-Ip", "Cf-Connecting-Ip"))
        if self.client_address[0] in ("127.0.0.1", "::1") and not forwarded and not USE_REDIS:
            return True
        key = self.headers.get("X-Admin-Key") or ""
        if not key:
            return False
        if ADMIN_KEY:
            return hmac.compare_digest(key, ADMIN_KEY)
        if REDIS is None:
            return False
        saved = claimed_key()
        if saved is None and len(key) >= 4:
            REDIS.cmd("SET", K_KEY, key, "NX")
            saved = claimed_key()
        return saved is not None and hmac.compare_digest(key, saved)

    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        """The endpoint asked for. On Vercel every endpoint is sent to one function, which may see either the
        original address or /api/index?route=<name>."""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.rstrip("/") in ("/api/index", "/api") and q.get("route"):
            return "/api/" + q["route"][0], q
        return u.path, q

    def do_GET(self):
        path, q = self.route()
        if path in PAGES:
            with open(os.path.join(HERE, PAGES[path]), "rb") as f:
                return self.send(f.read(), "text/html; charset=utf-8")
        try:
            if path == "/api/state":
                admin = self.is_admin()
                with game_session(change=False) as game:
                    seat = game.seat_of(q.get("token", [None])[0])
                    if seat is None:
                        out = {"joined": False, "people": len(game.humans), "seats": SEATS, "status": game.status,
                               "params": params()}
                    else:
                        out = game.state(seat)
                out["isAdmin"] = admin
                return self.send(out)
            if path == "/api/overview":
                with game_session(change=False) as game:
                    out = game.overview()
                return self.send(out)
            if path == "/api/monitor":
                if not self.is_admin():
                    claimable = USE_REDIS and not ADMIN_KEY and REDIS is not None and claimed_key() is None
                    return self.send({"ok": False, "msg": "Game Master Only.", "claimable": claimable}, code=403)
                with game_session(change=False) as game:
                    out = game.monitor()
                return self.send(out)
        except StoreError as e:
            print("store error:", self.path, e, file=sys.stderr)
            return self.send({"ok": False, "msg": str(e)}, code=503)
        self.send({"error": "Not Found"}, code=404)

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(d, dict):
                raise ValueError
        except Exception:
            return self.send({"ok": False, "msg": "Bad Request."}, code=400)
        try:
            res = self.handle_post(d)
        except StoreError as e:
            print("store error:", self.path, e, file=sys.stderr)
            return self.send({"ok": False, "msg": str(e)}, code=503)
        except Exception as e:                  # a bad request must never take the server down
            print("request error:", self.path, repr(e), file=sys.stderr)
            return self.send({"ok": False, "msg": "That Request Could Not Be Read."}, code=400)
        self.send(res, code=res.pop("code", 200))

    def handle_post(self, d):
        path = self.route()[0]
        token = d.get("token")
        if path == "/api/admin":
            if not self.is_admin():
                return {"ok": False, "msg": "Game Master Only.", "code": 403}
            with game_session(change=True) as game:
                return admin_action(d, game)
        if path not in ("/api/join", "/api/intro", "/api/leave", "/api/order", "/api/cancel"):
            return {"ok": False, "msg": "Not Found.", "code": 404}
        with game_session(change=True) as game:
            if path == "/api/join":
                return game.join(token, d.get("name"))
            seat = game.seat_of(token)
            if seat is None:
                return {"ok": False, "msg": "Sign Up First."}
            if path == "/api/intro":            # the member finished (or skipped) the how-to-play introduction
                game.intro.add(seat)
                return {"ok": True, "msg": ""}
            if path == "/api/leave":
                game.leave(seat)
                return {"ok": True, "msg": ""}
            if path == "/api/order":
                price, qty = d.get("price"), d.get("qty")
                if isinstance(price, bool) or isinstance(qty, bool) or not isinstance(price, (int, float)) \
                        or not isinstance(qty, int):
                    return {"ok": False, "msg": "Enter A Price And A Quantity."}
                return game.order(seat, d.get("side"), float(price), qty, now=d.get("now") is True)
            ok = game.book.cancel(seat, int(d.get("id", -1)))
            return {"ok": ok, "msg": "Order Cancelled." if ok else "Order Not Found."}


if __name__ == "__main__":
    # On a hosting service (Render sets RENDER_EXTERNAL_URL and PORT) the game listens on the given port and
    # shows the public link; the monitor then needs GAME_ADMIN_KEY, because no visitor is on this computer.
    HOSTED = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else int(os.environ.get("PORT", 7848))
    host = "0.0.0.0" if "--lan" in sys.argv or HOSTED else "127.0.0.1"   # --lan lets others on the same network join
    if USE_REDIS:                               # the game lives in Redis: no local save file, no local clock loop
        print("Game state is kept in Redis." if REDIS else "Warning: no Redis connection found.")
    else:
        if "--fresh" not in sys.argv and load():
            print("Restored the last saved game (a running game comes back paused). Use --fresh to ignore it.")
        threading.Thread(target=loop, daemon=True).start()
    if HOSTED:
        JOIN_URL = HOSTED
        print(f"Hosted online. Players join:        {JOIN_URL}")
        print(f"Projector view:                    {JOIN_URL}/overview")
        print(f"Game master:                       {JOIN_URL}/monitor  (enter the GAME_ADMIN_KEY)")
        if not ADMIN_KEY:
            print("Warning: GAME_ADMIN_KEY is not set, so no one can open the game master monitor.")
    else:
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
