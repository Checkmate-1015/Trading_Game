# Futures Trading Game

A classroom trading game for teenagers. Players trade one stock-index future against each other on a trading screen like a real exchange, while the spot index moves on scheduled news and a little random noise. It teaches how spot and futures markets work and how to size a bet when you have an information edge and when you don't. It runs on one computer with Python and nothing else to install.

Every price level in the game is a game parameter, not market data.

## Run It

You need Python 3.9 or newer and nothing else. Unzip the folder you were sent (or, on GitHub, press the green **Code** button, then **Download ZIP**, and unzip that). Keep all the files together in one folder. Open a terminal in that folder (Mac: right-click the folder, then **New Terminal at Folder**; Windows: type `cmd` in the folder's address bar) and run:

```bash
python3 game_server.py
```

On Windows the command is `python game_server.py`.

The game now runs **on your own computer**. `localhost` means "this computer", so every person who runs the code gets their own separate copy of the game at these addresses:

| Page | Address On The Computer Running It | Who It Is For |
|---|---|---|
| Member Page | http://localhost:7848 | Players sign up with a unique name and trade |
| Projector View | http://localhost:7848/overview | Join screen, price chart, news and class results, safe to show the room |
| Game Master Monitor | http://localhost:7848/monitor | Setup, controls and every account |

A `localhost` link never opens on someone else's computer. Sending a friend one of these links does nothing; they either run the code themselves or join your game as below.

## Let Other People Join Your Game

One computer is the host. Everyone else joins from their own laptop or phone on the **same Wi-Fi**.

1. On the host computer run:

   ```bash
   python3 game_server.py --lan
   ```

2. The terminal prints the address players use, for example `http://192.168.1.23:7848`. The monitor page and the projector view show it too.
3. Players type that address into their browser and sign up.
4. The game master page still only opens on the host computer.

If the host has a VPN on, the terminal lists more than one address; turn the VPN off or try the next address.

If players still cannot reach it, the network is blocking device-to-device traffic (many campus and office networks do). A phone hotspot that everyone joins usually works.

To let people join from anywhere on the internet, the game has to run on a computer with a public address (a hosting service or a tunnel). That is not set up here.

Other options:

- `python3 game_server.py 8000` uses another port.
- `python3 game_server.py --fresh` ignores the last saved game.

## How A Game Works

Built for players aged about 14 to 16. The whole lesson takes 30 to 35 minutes: about 4 minutes of intro, 20 minutes of trading and a short debrief.

1. Players open the member page and sign up. Names must be unique.
2. Each player's screen plays a short **animated intro** (8 slides: spot vs futures, long and short, day and night, news and clues, reading the market, bet sizing). It can be skipped and replayed with **How To Play**. The monitor and projector show how many players have finished it.
3. The game master presses Start Game. The game runs as 5 trading days. Each day has a cash session (the stock market is open and spot moves live) and an overnight session (spot is frozen; only the future trades). Each session lasts 2 minutes.
4. One news item goes public halfway through every session and moves the price 12 to 40 points. Cash-session news moves spot at once. Overnight news moves only the future, and spot gaps to it at the next open.
5. Between news items the price follows a random walk (Brownian motion) of only about ±4 points a session, so every news item is at least 3 times bigger than the noise. A clue is a real edge.
6. At the start of each session a few players, drawn at random, see that session's news early as a pop-up on their own screen. Players with the fewest clues so far are drawn first.
7. At the end every open position settles in cash at the final spot index. Each player gets a **report card** comparing their bet size and profit with and without a clue, and the projector shows the class results.

## The Player Screen

A trading terminal modelled on a real futures exchange:

- **Candlestick chart** of the futures with volume bars, the spot index (gold line) and fair value (dashed line) on top, overnight sessions shaded and every news item marked.
- **Order book** with cumulative depth, so big orders show up as buy and sell walls, and a **trades** list.
- **Order panel** with Market and Limit tabs, a size slider, Buy/Long and Sell/Short.
- **Bet Sizer**: with a clue it shows your edge on each extra contract, a suggested size and the expected profit. Without a clue it says you are guessing and suggests small sizes.
- **Market signal**: how far the futures sit from fair value, and how many contracts buyers and sellers have taken before the news.
- Tabs for positions (with Close At Market), open orders, your sessions, news (and whether the futures hinted at it first), the leaderboard and the rules. An account box shows balance and profit.

## Rules In Short

- **No trading fees.** You pay only the price you trade at.
- **Order book:** a trade happens when a bid meets an offer, at the resting order's price. Best price first, then earliest order. Market orders fill what they can straight away; limit orders wait.
- **News cancels waiting orders**, so no one is caught at an old price.
- **House market maker:** always quotes about 2.5 points either side of its price (0.05%), 5 contracts at each of 4 levels. It leans on order flow: every contract players buy from it raises its prices 0.5 points, and every contract they sell lowers them. It goes back to fair value when the news comes out and at each new session. So clue holders who trade push the futures ahead of spot, and everyone can see it. The house only posts quotes and never takes a player's order.
- **Fair value:** spot plus overnight news (and overnight noise) that spot has not printed yet. There is no cost of carry.
- **Position limit:** 10 contracts long or short. There is no margin.
- **Zero-sum:** every buy is someone's sell. At the end of every session the game checks that all profits add up to zero and that positions net to zero; the monitor lists each check as Pass or Fail.

## Game Setup

The game master can change these on the monitor page before a game starts. The monitor shows the resulting game length.

| Setting | Default |
|---|---|
| Trading Days | 5 |
| Seconds Per Session | 120 |
| Clue Holders Each Session | 3 |
| Starting Balance (USD) | 100,000 |
| Contract Value (USD) | 5,000 (USD 1 a point) |
| Position Limit (Contracts) | 10 |

News size (12 to 40 points), noise (±4 points a session), the house spread and lean are constants at the top of `game_server.py`.

## Files

| File | What It Does |
|---|---|
| `game_server.py` | The whole game: order book, clock, news, noise, clues, the house, accounts, report cards and the web server |
| `index.html` | Player page: trading terminal and animated intro |
| `overview.html` | Projector view: chart, news, top five, join screen and class results |
| `monitor.html` | Game master monitor |
| `tests.py` | Automated checks of the engine |

## Tests

```bash
python3 tests.py
```

It prints PASS or FAIL for each check: zero-sum over random games, the end-of-session check, no fees, the house leaning on order flow, news versus noise, candles, trading hours, position limits, sign-up, clue rotation, report cards, the intro count, setup and crash recovery.

## Good To Know

- The game saves itself every few seconds to `game_state.pkl`. After a crash or restart it comes back paused with seats and positions intact.
- There are no passwords. Names are not checked against anything.
- State lives on one computer. It is not built for the public internet.
