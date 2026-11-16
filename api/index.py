"""
Vercel entry point. Every /api/... request of the game is answered here by the same handler the local
server uses (see game_server.py). On Vercel the game lives in the project's Redis database, because each
request may run on a different short-lived server.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GAME_STORE", "redis")

from game_server import Handler  # noqa: E402


class handler(Handler):  # Vercel looks for a class named `handler`
    pass
