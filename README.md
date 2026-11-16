# Futures Trading Game

A classroom trading game. Members trade one index future with each other through a limit order book, while a spot index moves on scheduled news. It runs on one computer with Python and nothing else to install.

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
| Projector View | http://localhost:7848/overview | Price chart and public news, safe to show the room |
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

1. Players open the member page and sign up. Names must be unique.
2. The game master sets the game up on the monitor page and presses Start Game.
3. The game runs as trading days. Each day has a cash session (the stock market is open and the spot index prints live) and an overnight session (the index is frozen at its close; only the future trades).
4. One news shock goes public midway through every session. Cash-session news moves the index at once. Overnight news moves only the future, and the index gaps to it at the next open.
5. At the start of each session a few players, drawn at random, see that session's shock early as a pop-up on their own screen. Players with the fewest clues so far are drawn first.
6. At the end every open position settles in cash at the final spot index.

## Rules In Short

- **Order book:** a trade happens when a bid meets an offer, at the resting order's price. Best price first, then earliest order.
- **Zero-sum:** every buy is someone's sell, so one player's profit is another's loss. Members plus the house always sum to zero.
- **Session check:** at the end of every session the game adds up every account's profit or loss and confirms the total is zero and that long and short positions net to zero. The monitor page lists each check as Pass or Fail.
- **House market maker:** always quotes fair value less and plus 0.25%. It only posts quotes and never takes a player's order.
- **Fair futures:** spot + overnight news not yet in spot + cost of carry to expiry.
- **Position limit and margin:** each player has a contract limit, and each contract ties up margin from their capital.
- **Daily settlement:** accounts are marked to market at each cash close.

## Game Setup

The game master can change these on the monitor page before a game starts:

| Setting | Default |
|---|---|
| Trading Days | 5 |
| Seconds Per Session | 45 |
| Clue Holders Each Session | 3 |
| Starting Capital (USD) | 100,000 |
| Contract Value (USD) | 5,000 (USD 1 a point) |
| Margin Per Contract (USD) | 1,000 |
| Position Limit (Contracts) | 10 |

## Files

| File | What It Does |
|---|---|
| `game_server.py` | The whole game: order book, clock, news, clues, accounts and the web server |
| `index.html` | Member page |
| `overview.html` | Projector view |
| `monitor.html` | Game master monitor |
| `tests.py` | Automated checks of the engine |

## Tests

```bash
python3 tests.py
```

It prints PASS or FAIL for each check: zero-sum over random games, the end-of-session check, position limits, trading hours, sign-up, clue rotation, setup and crash recovery.

## Good To Know

- The game saves itself every few seconds to `game_state.pkl`. After a crash or restart it comes back paused with seats and positions intact.
- There are no passwords. Names are not checked against anything.
- State lives on one computer. It is not built for the public internet.
