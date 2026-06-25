"""
Royal Spin — Telegram Mini App
Backend: Flask + serverless-wsgi
DB: PostgreSQL
"""

import os
import hmac
import hashlib
import json
import time
import random
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from functools import wraps

from flask import Flask, request, jsonify, Response
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# ============ CONFIG ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bothost_db_e7be6fc4ab15:dNEy8t5wXfBCOaZlrwKQ4T3VDsC7oiHP_J_BdDAM2UI@node1.pghost.ru:15810/bothost_db_e7be6fc4ab15",
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("royal-spin")

app = Flask(__name__)

# ============ DB POOL ============
db_pool = None


def init_db_pool():
    """Initialize the connection pool. Lazy because cold-start."""
    global db_pool
    if db_pool is not None:
        return db_pool
    try:
        db_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
            connect_timeout=5,
        )
        log.info("DB pool initialized")
        init_schema()
    except Exception as e:
        log.error(f"DB pool init failed: {e}")
        db_pool = None
    return db_pool


def get_conn():
    p = init_db_pool()
    if p is None:
        raise RuntimeError("DB unavailable")
    return p.getconn()


def put_conn(conn):
    if db_pool is not None and conn is not None:
        db_pool.putconn(conn)


def init_schema():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id      BIGINT PRIMARY KEY,
                    username     TEXT,
                    first_name   TEXT,
                    last_name    TEXT,
                    photo_url    TEXT,
                    balance      INTEGER NOT NULL DEFAULT 0,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    games_won    INTEGER NOT NULL DEFAULT 0,
                    created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    last_seen    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    amount      INTEGER NOT NULL,
                    game_type   TEXT NOT NULL,
                    win         BOOLEAN NOT NULL,
                    detail      TEXT,
                    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at DESC);
                """
            )
            conn.commit()
    finally:
        put_conn(conn)


# ============ TELEGRAM INIT DATA VALIDATION ============
def validate_init_data(init_data: str) -> dict | None:
    """
    Validate the raw initData string from Telegram WebApp.
    Returns the user dict if valid, else None.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        # Build data-check-string
        items = sorted(parsed.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in items)
        # Secret key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        # Check auth_date freshness (24h)
        auth_date = int(parsed.get("auth_date", "0"))
        if abs(time.time() - auth_date) > 86400:
            return None
        user_json = parsed.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        log.warning(f"initData validation error: {e}")
        return None


# ============ AUTH DECORATOR ============
def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Accept initData via header or JSON body
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        if not init_data and request.is_json:
            init_data = (request.get_json(silent=True) or {}).get("initData", "")
        user = validate_init_data(init_data)
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        request.tg_user = user
        return fn(*args, **kwargs)

    return wrapper


# ============ USER HELPERS ============
def upsert_user(tg_user: dict) -> dict:
    """Insert user on first visit with 5 stars bonus, otherwise update profile."""
    user_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name")
    last_name = tg_user.get("last_name")
    photo_url = tg_user.get("photo_url")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name, photo_url, balance)
                    VALUES (%s, %s, %s, %s, %s, 5)
                    RETURNING *
                    """,
                    (user_id, username, first_name, last_name, photo_url),
                )
                created = cur.fetchone()
                # Log welcome bonus
                cur.execute(
                    """
                    INSERT INTO transactions (user_id, amount, game_type, win, detail)
                    VALUES (%s, 5, 'welcome', TRUE, 'Welcome bonus')
                    """,
                    (user_id,),
                )
                conn.commit()
                return {"user": dict(created), "is_new": True}
            else:
                cur.execute(
                    """
                    UPDATE users
                    SET username = %s,
                        first_name = %s,
                        last_name = %s,
                        photo_url = %s,
                        last_seen = NOW()
                    WHERE user_id = %s
                    RETURNING *
                    """,
                    (username, first_name, last_name, photo_url, user_id),
                )
                updated = cur.fetchone()
                conn.commit()
                return {"user": dict(updated), "is_new": False}
    finally:
        put_conn(conn)


def get_balance(user_id: int) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        put_conn(conn)


def record_game(user_id: int, game_type: str, delta: int, win: bool, detail: str) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("user not found")
            new_balance = int(row["balance"]) + delta
            if new_balance < 0:
                raise ValueError("insufficient balance")
            cur.execute(
                """
                UPDATE users
                SET balance = %s,
                    games_played = games_played + 1,
                    games_won = games_won + %s
                WHERE user_id = %s
                RETURNING balance, games_played, games_won
                """,
                (new_balance, 1 if win else 0, user_id),
            )
            stats = cur.fetchone()
            cur.execute(
                """
                INSERT INTO transactions (user_id, amount, game_type, win, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, delta, game_type, win, detail),
            )
            conn.commit()
            return dict(stats)
    finally:
        put_conn(conn)


# ============ GAME LOGIC ============
def play_dice(stake: int):
    """Dice: 2 dice. sum 7-11 => x2, sum 12 => x5, else lose."""
    d1 = random.randint(1, 6)
    d2 = random.randint(1, 6)
    total = d1 + d2
    if total == 12:
        payout = stake * 5
        return {"dice": [d1, d2], "total": total, "win": True, "payout": payout, "mult": 5}
    if total >= 7:
        payout = stake * 2
        return {"dice": [d1, d2], "total": total, "win": True, "payout": payout, "mult": 2}
    return {"dice": [d1, d2], "total": total, "win": False, "payout": 0, "mult": 0}


def play_football(stake: int):
    """Football: penalty kick. 50% chance to score => x2."""
    scored = random.random() < 0.5
    if scored:
        return {"scored": True, "win": True, "payout": stake * 2, "mult": 2,
                "position": random.choice(["top-left", "top-right", "bottom-left", "bottom-right", "center"])}
    return {"scored": False, "win": False, "payout": 0, "mult": 0, "position": "saved"}


def play_basketball(stake: int):
    """Basketball: 3-point contest. 40% => x3, 25% => x1.5 (consolation), else miss."""
    r = random.random()
    if r < 0.40:
        return {"made": True, "win": True, "payout": stake * 3, "mult": 3, "kind": "swish"}
    if r < 0.65:
        return {"made": True, "win": True, "payout": int(stake * 1.5), "mult": 1.5, "kind": "rim"}
    return {"made": False, "win": False, "payout": 0, "mult": 0, "kind": "miss"}


# ============ API ROUTES ============
@app.route("/health", methods=["GET"])
def health():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        put_conn(conn)
        return jsonify({"ok": True, "db": "up"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


@app.route("/api/auth", methods=["POST"])
@require_auth
def api_auth():
    """Verify initData, upsert user, return profile + is_new flag."""
    tg_user = request.tg_user
    result = upsert_user(tg_user)
    u = result["user"]
    return jsonify({
        "ok": True,
        "is_new": result["is_new"],
        "user": {
            "id": u["user_id"],
            "username": u["username"],
            "first_name": u["first_name"],
            "last_name": u["last_name"],
            "photo_url": u["photo_url"],
            "balance": u["balance"],
            "games_played": u["games_played"],
            "games_won": u["games_won"],
        },
    })


@app.route("/api/user", methods=["GET"])
@require_auth
def api_user():
    uid = int(request.tg_user["id"])
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not found"}), 404
            return jsonify({
                "ok": True,
                "user": {
                    "id": row["user_id"],
                    "username": row["username"],
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "photo_url": row["photo_url"],
                    "balance": row["balance"],
                    "games_played": row["games_played"],
                    "games_won": row["games_won"],
                },
            })
    finally:
        put_conn(conn)


def _play(game_type, play_fn, payout_mult_attr):
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake = int(body.get("stake", 1))
    if stake < 1 or stake > 100:
        return jsonify({"ok": False, "error": "stake must be 1..100"}), 400

    current = get_balance(uid)
    if current < stake:
        return jsonify({"ok": False, "error": "insufficient_balance", "balance": current}), 400

    try:
        result = play_fn(stake)
    except Exception as e:
        log.exception(e)
        return jsonify({"ok": False, "error": "game_error"}), 500

    payout = int(result["payout"])
    delta = payout - stake  # negative on loss, positive on win
    try:
        stats = record_game(uid, game_type, delta, result["win"], json.dumps(result, default=str))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e), "balance": current}), 400

    return jsonify({
        "ok": True,
        "result": result,
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


@app.route("/api/game/dice", methods=["POST"])
@require_auth
def api_dice():
    return _play("dice", play_dice, "mult")


@app.route("/api/game/football", methods=["POST"])
@require_auth
def api_football():
    return _play("football", play_football, "mult")


@app.route("/api/game/basketball", methods=["POST"])
@require_auth
def api_basketball():
    return _play("basketball", play_basketball, "mult")


# ============ FRONTEND ============
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
<meta name="theme-color" content="#0b0612" />
<title>Royal Spin</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #0b0612;
    --bg2: #1a0f2e;
    --gold: #f7c948;
    --gold2: #ffb627;
    --purple: #8b5cf6;
    --pink: #ec4899;
    --green: #10b981;
    --red: #ef4444;
    --text: #f5f3ff;
    --muted: #a89dc9;
    --card: rgba(255,255,255,0.05);
    --border: rgba(247,201,72,0.25);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0; height: 100%; overflow-x: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    background: radial-gradient(ellipse at top, #2d1b5e 0%, var(--bg) 60%) fixed;
  }
  .app { min-height: 100%; padding: 16px 16px 32px; max-width: 520px; margin: 0 auto; }

  /* HEADER */
  .header {
    display: flex; align-items: center; gap: 12px;
    padding: 14px; border-radius: 18px;
    background: linear-gradient(135deg, rgba(247,201,72,0.12), rgba(139,92,246,0.12));
    border: 1px solid var(--border);
    box-shadow: 0 8px 32px rgba(247,201,72,0.08);
    margin-bottom: 18px;
  }
  .avatar {
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, var(--gold), var(--purple));
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; font-weight: 700; color: #0b0612;
    border: 2px solid var(--gold);
    overflow: hidden; flex-shrink: 0;
  }
  .avatar img { width: 100%; height: 100%; object-fit: cover; }
  .user-info { flex: 1; min-width: 0; }
  .user-name { font-weight: 700; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .user-handle { font-size: 12px; color: var(--muted); }
  .balance-box {
    text-align: right; padding: 6px 12px; border-radius: 12px;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #0b0612;
  }
  .balance-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .balance-val { font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 4px; }
  .star { display: inline-block; width: 16px; height: 16px; }

  /* TITLE */
  .title {
    text-align: center; margin: 8px 0 18px;
  }
  .title h1 {
    margin: 0; font-size: 28px; font-weight: 900;
    background: linear-gradient(90deg, var(--gold), var(--pink), var(--purple));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    letter-spacing: 1px;
  }
  .title p { margin: 4px 0 0; color: var(--muted); font-size: 13px; }

  /* GAME GRID */
  .games { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
  .game-card {
    position: relative; padding: 14px 8px; border-radius: 18px;
    background: var(--card); border: 1px solid var(--border);
    text-align: center; cursor: pointer;
    transition: transform 0.15s, box-shadow 0.15s;
    overflow: hidden;
  }
  .game-card::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(135deg, transparent, rgba(247,201,72,0.08));
    opacity: 0; transition: opacity 0.2s;
  }
  .game-card:active { transform: scale(0.96); }
  .game-card:hover::before { opacity: 1; }
  .game-icon {
    font-size: 38px; margin-bottom: 6px; display: block;
    filter: drop-shadow(0 4px 8px rgba(247,201,72,0.3));
  }
  .game-name { font-weight: 700; font-size: 13px; }
  .game-sub { font-size: 10px; color: var(--muted); margin-top: 2px; }

  /* MODAL */
  .modal-back {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: none; align-items: flex-end; justify-content: center;
    z-index: 100; backdrop-filter: blur(8px);
  }
  .modal-back.show { display: flex; animation: fadeIn 0.2s; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  .modal {
    width: 100%; max-width: 520px; max-height: 92vh;
    background: linear-gradient(180deg, var(--bg2), var(--bg));
    border-radius: 24px 24px 0 0; padding: 20px;
    border-top: 1px solid var(--border);
    animation: slideUp 0.25s cubic-bezier(.2,.8,.3,1);
    overflow-y: auto;
  }
  @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
  .modal h2 {
    margin: 0 0 4px; text-align: center;
    font-size: 22px;
    background: linear-gradient(90deg, var(--gold), var(--pink));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .modal-sub { text-align: center; color: var(--muted); font-size: 13px; margin-bottom: 16px; }
  .close-btn {
    position: absolute; top: 16px; right: 16px;
    width: 32px; height: 32px; border-radius: 50%;
    background: rgba(255,255,255,0.08); border: none; color: var(--text);
    font-size: 18px; cursor: pointer;
  }
  .stake-row {
    display: flex; gap: 8px; margin-bottom: 16px;
    justify-content: center;
  }
  .stake-btn {
    flex: 1; max-width: 80px; padding: 10px 0;
    border-radius: 12px; border: 1px solid var(--border);
    background: var(--card); color: var(--text); font-weight: 700;
    cursor: pointer; transition: 0.15s;
  }
  .stake-btn.active {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #0b0612; border-color: var(--gold);
  }
  .play-btn {
    width: 100%; padding: 16px; border: none; border-radius: 14px;
    background: linear-gradient(135deg, var(--purple), var(--pink));
    color: white; font-size: 17px; font-weight: 800; cursor: pointer;
    box-shadow: 0 8px 24px rgba(139,92,246,0.4);
    transition: 0.15s;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .play-btn:active { transform: scale(0.97); }
  .play-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* DICE */
  .dice-arena { display: flex; gap: 16px; justify-content: center; margin: 24px 0 12px; perspective: 600px; }
  .dice {
    width: 72px; height: 72px; position: relative;
    transform-style: preserve-3d;
    transition: transform 1.5s cubic-bezier(.4,1.6,.5,1);
  }
  .dice.rolling { animation: diceRoll 1.5s linear; }
  @keyframes diceRoll {
    0%   { transform: rotateX(0deg)   rotateY(0deg)   rotateZ(0deg); }
    25%  { transform: rotateX(360deg) rotateY(180deg) rotateZ(90deg); }
    50%  { transform: rotateX(720deg) rotateY(360deg) rotateZ(180deg); }
    75%  { transform: rotateX(1080deg) rotateY(540deg) rotateZ(270deg); }
    100% { transform: rotateX(1440deg) rotateY(720deg) rotateZ(360deg); }
  }
  .dice-face {
    position: absolute; inset: 0;
    background: linear-gradient(135deg, #fff, #e5e5f7);
    border-radius: 12px; border: 2px solid var(--gold);
    display: grid; padding: 8px;
    box-shadow: inset 0 0 12px rgba(247,201,72,0.3), 0 6px 16px rgba(0,0,0,0.3);
  }
  .dice-face.front  { transform: translateZ(36px); }
  .dice-face.back   { transform: rotateY(180deg) translateZ(36px); }
  .dice-face.right  { transform: rotateY(90deg)  translateZ(36px); }
  .dice-face.left   { transform: rotateY(-90deg) translateZ(36px); }
  .dice-face.top    { transform: rotateX(90deg)  translateZ(36px); }
  .dice-face.bottom { transform: rotateX(-90deg) translateZ(36px); }
  .dot { width: 14px; height: 14px; border-radius: 50%; background: #0b0612; align-self: center; justify-self: center; }
  .f1 { display: grid; place-items: center; }
  .f2 { display: grid; grid-template-columns: 1fr 1fr; align-items: center; justify-items: center; }
  .f3 { display: grid; grid-template-columns: 1fr 1fr 1fr; align-items: center; justify-items: center; }
  .f4 { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 8px; padding: 12px; }
  .f5 { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 4px; padding: 8px; align-items: center; justify-items: center; }
  .f6 { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr 1fr; gap: 4px; padding: 8px; align-items: center; justify-items: center; }

  /* FOOTBALL */
  .fb-field {
    position: relative; height: 220px;
    background: linear-gradient(180deg, #2d5a3d 0%, #1f4530 100%);
    border-radius: 16px; margin: 16px 0;
    overflow: hidden;
    border: 2px solid rgba(255,255,255,0.15);
  }
  .fb-field::before {
    content: ""; position: absolute; inset: 10px;
    border: 2px solid rgba(255,255,255,0.3); border-radius: 8px;
  }
  .fb-goal {
    position: absolute; left: 50%; transform: translateX(-50%);
    bottom: 10px; width: 160px; height: 50px;
    border: 3px solid #fff; border-bottom: none;
    background: rgba(255,255,255,0.05);
  }
  .fb-keeper {
    position: absolute; left: 50%; bottom: 15px; transform: translateX(-50%);
    width: 36px; height: 50px;
    transition: left 0.4s cubic-bezier(.4,1.6,.5,1), bottom 0.4s;
    font-size: 32px; text-align: center;
  }
  .fb-ball {
    position: absolute; left: 50%; bottom: 6px; transform: translateX(-50%);
    width: 28px; height: 28px; font-size: 26px; line-height: 28px; text-align: center;
    transition: all 0.8s cubic-bezier(.4,.1,.4,1);
  }
  .fb-ball.shoot { animation: ballShoot 0.9s forwards; }
  @keyframes ballShoot {
    0%   { left: 50%; bottom: 6px;  transform: translateX(-50%) scale(1); }
    60%  { bottom: 70%; left: var(--bx, 50%); transform: translateX(-50%) scale(1.1); }
    100% { bottom: 80%; left: var(--bx, 50%); transform: translateX(-50%) scale(1.3); }
  }

  /* BASKETBALL */
  .bb-court {
    position: relative; height: 240px;
    background: linear-gradient(180deg, #b8860b 0%, #8b6508 100%);
    border-radius: 16px; margin: 16px 0;
    overflow: hidden;
    border: 2px solid rgba(0,0,0,0.3);
  }
  .bb-court::before {
    content: ""; position: absolute; bottom: 0; left: 0; right: 0; height: 40px;
    background: linear-gradient(180deg, #d4a017, #b8860b);
  }
  .bb-hoop {
    position: absolute; top: 30px; right: 30px;
    width: 70px; height: 50px;
  }
  .bb-backboard {
    position: absolute; top: 0; right: 0;
    width: 50px; height: 40px;
    background: rgba(255,255,255,0.85);
    border: 2px solid #333;
  }
  .bb-rim {
    position: absolute; bottom: 0; left: 0;
    width: 40px; height: 8px; border-radius: 50%;
    background: #ff4500; border: 2px solid #cc3700;
  }
  .bb-net {
    position: absolute; bottom: -16px; left: 4px;
    width: 32px; height: 20px;
    background: repeating-linear-gradient(45deg, transparent 0 3px, rgba(255,255,255,0.6) 3px 5px),
                repeating-linear-gradient(-45deg, transparent 0 3px, rgba(255,255,255,0.6) 3px 5px);
  }
  .bb-ball {
    position: absolute; bottom: 8px; left: 20px;
    width: 36px; height: 36px; font-size: 32px; line-height: 36px; text-align: center;
    transition: all 0.9s cubic-bezier(.3,.1,.4,1);
  }
  .bb-ball.shoot { animation: bbShoot 0.9s forwards; }
  @keyframes bbShoot {
    0%   { left: 20px;  bottom: 8px;  transform: rotate(0deg); }
    40%  { left: 100px; bottom: 200px; transform: rotate(180deg) scale(1.1); }
    65%  { left: 180px; bottom: 130px; transform: rotate(360deg) scale(1); }
    85%  { left: calc(100% - 80px); bottom: 60px; transform: rotate(540deg) scale(0.9); }
    100% { left: calc(100% - 70px); bottom: 50px; transform: rotate(720deg) scale(0.85); }
  }

  /* RESULT TOAST */
  .toast {
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
    padding: 14px 22px; border-radius: 14px;
    font-weight: 800; font-size: 15px; z-index: 200;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    animation: toastIn 0.3s, toastOut 0.3s 2.2s forwards;
    text-align: center; min-width: 220px;
  }
  .toast.win { background: linear-gradient(135deg, var(--gold), var(--gold2)); color: #0b0612; }
  .toast.lose { background: linear-gradient(135deg, #555, #222); color: white; }
  @keyframes toastIn { from { opacity: 0; transform: translateX(-50%) translateY(-20px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }
  @keyframes toastOut { to { opacity: 0; transform: translateX(-50%) translateY(-20px); } }

  .result-text {
    text-align: center; font-size: 18px; font-weight: 800;
    margin: 12px 0; min-height: 24px;
  }
  .result-text.win { color: var(--gold); }
  .result-text.lose { color: var(--muted); }

  .loading {
    position: fixed; inset: 0; background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
    flex-direction: column; gap: 14px;
  }
  .spinner {
    width: 48px; height: 48px; border-radius: 50%;
    border: 4px solid rgba(247,201,72,0.2);
    border-top-color: var(--gold);
    animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .stats {
    margin-top: 16px; padding: 12px; border-radius: 14px;
    background: var(--card); border: 1px solid var(--border);
    display: flex; justify-content: space-around; text-align: center;
  }
  .stat-val { font-size: 18px; font-weight: 800; color: var(--gold); }
  .stat-lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; }
</style>
</head>
<body>

<div class="loading" id="loading">
  <div class="spinner"></div>
  <div style="color:var(--muted);font-size:13px;">Loading Royal Spin…</div>
</div>

<div class="app" id="app" style="display:none;">

  <div class="header">
    <div class="avatar" id="avatar">?</div>
    <div class="user-info">
      <div class="user-name" id="userName">Player</div>
      <div class="user-handle" id="userHandle">@username</div>
    </div>
    <div class="balance-box">
      <div class="balance-label">BALANCE</div>
      <div class="balance-val">
        <svg class="star" viewBox="0 0 24 24" fill="#0b0612"><path d="M12 2l2.9 6.9L22 10l-5.5 4.7L18 22l-6-3.7L6 22l1.5-7.3L2 10l7.1-1.1L12 2z"/></svg>
        <span id="balanceVal">0</span>
      </div>
    </div>
  </div>

  <div class="title">
    <h1>👑 ROYAL SPIN 👑</h1>
    <p>Pick a game. Risk a star. Win the crown.</p>
  </div>

  <div class="games">
    <div class="game-card" onclick="openGame('dice')">
      <span class="game-icon">🎲</span>
      <div class="game-name">Dice</div>
      <div class="game-sub">x2 / x5</div>
    </div>
    <div class="game-card" onclick="openGame('football')">
      <span class="game-icon">⚽</span>
      <div class="game-name">Football</div>
      <div class="game-sub">Penalty x2</div>
    </div>
    <div class="game-card" onclick="openGame('basketball')">
      <span class="game-icon">🏀</span>
      <div class="game-name">Basketball</div>
      <div class="game-sub">3-Point x3</div>
    </div>
  </div>

  <div class="stats">
    <div><div class="stat-val" id="statGames">0</div><div class="stat-lbl">Games</div></div>
    <div><div class="stat-val" id="statWon">0</div><div class="stat-lbl">Wins</div></div>
    <div><div class="stat-val" id="statRate">0%</div><div class="stat-lbl">Win rate</div></div>
  </div>

  <div style="text-align:center;color:var(--muted);font-size:11px;margin-top:24px;">
    Telegram Stars only · 18+ · Play responsibly
  </div>
</div>

<!-- DICE MODAL -->
<div class="modal-back" id="modal-dice" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎲 Dice Roll</h2>
    <div class="modal-sub">Sum 7–11 → x2 · Sum 12 → x5</div>
    <div class="dice-arena">
      <div class="dice" id="dice1"><div class="dice-face front f1"><div class="dot"></div></div><div class="dice-face back f6"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face right f3"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face left f4"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face top f2"><div class="dot"></div><div class="dot"></div></div><div class="dice-face bottom f5"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>
      <div class="dice" id="dice2"><div class="dice-face front f1"><div class="dot"></div></div><div class="dice-face back f6"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face right f3"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face left f4"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face top f2"><div class="dot"></div><div class="dot"></div></div><div class="dice-face bottom f5"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>
    </div>
    <div class="result-text" id="diceResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn active" onclick="setStake(5,this)">⭐ 5</button>
    </div>
    <button class="play-btn" id="diceBtn" onclick="playDice()">ROLL DICE</button>
  </div>
</div>

<!-- FOOTBALL MODAL -->
<div class="modal-back" id="modal-football" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>⚽ Penalty Shootout</h2>
    <div class="modal-sub">Score the goal → x2 your stake</div>
    <div class="fb-field">
      <div class="fb-goal"></div>
      <div class="fb-keeper" id="fbKeeper">🧤</div>
      <div class="fb-ball" id="fbBall">⚽</div>
    </div>
    <div class="result-text" id="fbResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn active" onclick="setStake(5,this)">⭐ 5</button>
    </div>
    <button class="play-btn" id="fbBtn" onclick="playFootball()">SHOOT</button>
  </div>
</div>

<!-- BASKETBALL MODAL -->
<div class="modal-back" id="modal-basketball" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🏀 3-Pointer</h2>
    <div class="modal-sub">Swish → x3 · Rim → x1.5</div>
    <div class="bb-court">
      <div class="bb-hoop">
        <div class="bb-backboard"></div>
        <div class="bb-rim"></div>
        <div class="bb-net"></div>
      </div>
      <div class="bb-ball" id="bbBall">🏀</div>
    </div>
    <div class="result-text" id="bbResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn active" onclick="setStake(5,this)">⭐ 5</button>
    </div>
    <button class="play-btn" id="bbBtn" onclick="playBasketball()">SHOOT</button>
  </div>
</div>

<script>
  const tg = window.Telegram ? window.Telegram.WebApp : null;
  let currentStake = 5;
  let userData = null;

  if (tg) {
    tg.ready();
    tg.expand();
    tg.setHeaderColor('#0b0612');
    tg.setBackgroundColor('#0b0612');
  }

  function setStake(v, el) {
    currentStake = v;
    document.querySelectorAll('.stake-btn').forEach(b => b.classList.remove('active'));
    el.classList.add('active');
  }

  function applyUser(u, isNew) {
    userData = u;
    const name = u.first_name || u.username || 'Player';
    document.getElementById('userName').textContent = name;
    document.getElementById('userHandle').textContent = u.username ? '@' + u.username : 'id:' + u.id;
    const av = document.getElementById('avatar');
    if (u.photo_url) {
      av.innerHTML = '<img src="' + u.photo_url + '" alt="" onerror="this.remove()">';
    } else {
      av.textContent = (name[0] || '?').toUpperCase();
    }
    document.getElementById('balanceVal').textContent = u.balance;
    document.getElementById('statGames').textContent = u.games_played;
    document.getElementById('statWon').textContent = u.games_won;
    const rate = u.games_played > 0 ? Math.round((u.games_won / u.games_played) * 100) : 0;
    document.getElementById('statRate').textContent = rate + '%';
    if (isNew && tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  }

  async function api(path, body) {
    const initData = tg ? tg.initData : '';
    const resp = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': initData
      },
      body: body ? JSON.stringify({ initData, ...body }) : undefined
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || 'http_' + resp.status);
    return data;
  }

  async function bootstrap() {
    try {
      const initData = tg ? tg.initData : '';
      if (!initData) {
        // Dev fallback: try without auth (will 401). Show a friendly hint.
        showDevMode();
        return;
      }
      const r = await api('/api/auth', {});
      applyUser(r.user, r.is_new);
      if (r.is_new && tg && tg.showAlert) {
        tg.showAlert('Welcome to Royal Spin! 🎁 You received 5 stars as a gift.');
      }
      document.getElementById('loading').style.display = 'none';
      document.getElementById('app').style.display = '';
    } catch (e) {
      console.error(e);
      showDevMode();
    }
  }

  function showDevMode() {
    // Allows opening the app outside Telegram for design preview
    document.getElementById('loading').style.display = 'none';
    document.getElementById('app').style.display = '';
    applyUser({id: 0, first_name: 'Guest', username: 'preview', photo_url: null, balance: 5, games_played: 0, games_won: 0}, true);
    if (tg && tg.showAlert) {
      tg.showAlert('Open this Mini App from inside Telegram to play with real stars.');
    }
  }

  function openGame(name) {
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    document.getElementById('modal-' + name).classList.add('show');
  }
  function closeModal() {
    document.querySelectorAll('.modal-back').forEach(m => m.classList.remove('show'));
  }

  function toast(text, win) {
    const t = document.createElement('div');
    t.className = 'toast ' + (win ? 'win' : 'lose');
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2700);
  }

  function setBalance(b) {
    userData.balance = b;
    document.getElementById('balanceVal').textContent = b;
  }

  function updateStats(played, won) {
    userData.games_played = played;
    userData.games_won = won;
    document.getElementById('statGames').textContent = played;
    document.getElementById('statWon').textContent = won;
    const rate = played > 0 ? Math.round((won / played) * 100) : 0;
    document.getElementById('statRate').textContent = rate + '%';
  }

  // ===== DICE =====
  function faceRot(n) {
    // rotation for showing face n on top of a 3D dice
    const map = {
      1: {x: 0,    y: 0},
      2: {x: -90,  y: 0},
      3: {x: 0,    y: -90},
      4: {x: 0,    y: 90},
      5: {x: 90,   y: 0},
      6: {x: 180,  y: 0},
    };
    const r = map[n];
    return `rotateX(${r.x}deg) rotateY(${r.y}deg)`;
  }
  async function playDice() {
    const btn = document.getElementById('diceBtn');
    btn.disabled = true;
    const d1 = document.getElementById('dice1');
    const d2 = document.getElementById('dice2');
    d1.classList.add('rolling'); d2.classList.add('rolling');
    d1.style.transform = ''; d2.style.transform = '';
    document.getElementById('diceResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/dice', { stake: currentStake });
      setTimeout(() => {
        d1.classList.remove('rolling'); d2.classList.remove('rolling');
        d1.style.transform = faceRot(r.result.dice[0]);
        d2.style.transform = faceRot(r.result.dice[1]);
        const txt = document.getElementById('diceResult');
        if (r.result.win) {
          txt.className = 'result-text win';
          txt.textContent = `Sum ${r.result.total} — WIN x${r.result.mult} (+${r.result.payout - currentStake}⭐)`;
          toast(`🎉 +${r.result.payout - currentStake} stars`, true);
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
        } else {
          txt.className = 'result-text lose';
          txt.textContent = `Sum ${r.result.total} — LOSE (-${currentStake}⭐)`;
          toast(`💀 -${currentStake} stars`, false);
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
        }
        setBalance(r.balance);
        updateStats(r.games_played, r.games_won);
      }, 1500);
    } catch (e) {
      toast('Error: ' + e.message, false);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 1700);
    }
  }

  // ===== FOOTBALL =====
  async function playFootball() {
    const btn = document.getElementById('fbBtn');
    btn.disabled = true;
    const ball = document.getElementById('fbBall');
    const keeper = document.getElementById('fbKeeper');
    ball.classList.remove('shoot');
    ball.style.left = '50%'; ball.style.bottom = '6px'; ball.style.transform = 'translateX(-50%)';
    keeper.style.left = '50%'; keeper.style.bottom = '15px';
    document.getElementById('fbResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/football', { stake: currentStake });
      const pos = r.result.position;
      // animate keeper jump first
      setTimeout(() => {
        if (pos === 'top-left')     { keeper.style.left = '25%'; keeper.style.bottom = '40px'; }
        else if (pos === 'top-right'){ keeper.style.left = '75%'; keeper.style.bottom = '40px'; }
        else if (pos === 'bottom-left'){ keeper.style.left = '35%'; keeper.style.bottom = '15px'; }
        else if (pos === 'bottom-right'){ keeper.style.left = '65%'; keeper.style.bottom = '15px'; }
      }, 100);
      setTimeout(() => {
        // ball shoot
        let bx = '50%';
        if (pos === 'top-left')      bx = '25%';
        else if (pos === 'top-right') bx = '75%';
        else if (pos === 'bottom-left')  bx = '35%';
        else if (pos === 'bottom-right') bx = '65%';
        ball.style.setProperty('--bx', bx);
        ball.classList.add('shoot');
        const txt = document.getElementById('fbResult');
        if (r.result.win) {
          setTimeout(() => {
            txt.className = 'result-text win';
            txt.textContent = `GOAL! ⚽ WIN x2 (+${r.result.payout - currentStake}⭐)`;
            toast(`🎉 +${r.result.payout - currentStake} stars`, true);
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
          }, 600);
        } else {
          setTimeout(() => {
            txt.className = 'result-text lose';
            txt.textContent = `SAVED 🧤 LOSE (-${currentStake}⭐)`;
            toast(`💀 -${currentStake} stars`, false);
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
          }, 600);
        }
        setBalance(r.balance);
        updateStats(r.games_played, r.games_won);
      }, 350);
    } catch (e) {
      toast('Error: ' + e.message, false);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 1600);
    }
  }

  // ===== BASKETBALL =====
  async function playBasketball() {
    const btn = document.getElementById('bbBtn');
    btn.disabled = true;
    const ball = document.getElementById('bbBall');
    ball.classList.remove('shoot');
    ball.style.left = '20px'; ball.style.bottom = '8px'; ball.style.transform = '';
    document.getElementById('bbResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/basketball', { stake: currentStake });
      setTimeout(() => {
        ball.classList.add('shoot');
        const txt = document.getElementById('bbResult');
        if (r.result.win) {
          setTimeout(() => {
            const kindTxt = r.result.kind === 'swish' ? 'SWISH! 🏀' : 'RIM IN!';
            txt.className = 'result-text win';
            txt.textContent = `${kindTxt} WIN x${r.result.mult} (+${r.result.payout - currentStake}⭐)`;
            toast(`🎉 +${r.result.payout - currentStake} stars`, true);
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
          }, 850);
        } else {
          setTimeout(() => {
            txt.className = 'result-text lose';
            txt.textContent = `MISSED 😢 (-${currentStake}⭐)`;
            toast(`💀 -${currentStake} stars`, false);
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
          }, 850);
        }
        setBalance(r.balance);
        updateStats(r.games_played, r.games_won);
      }, 100);
    } catch (e) {
      toast('Error: ' + e.message, false);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 1700);
    }
  }

  bootstrap();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ============ SERVERLESS HANDLER ============
try:
    from serverless_wsgi import handle_request

    def handler(event, context):
        """AWS Lambda / Yandex Cloud / similar serverless entry point."""
        return handle_request(app, event, context)
except ImportError:
    handler = None


# Standard WSGI for gunicorn / Vercel / Render
application = app


if __name__ == "__main__":
    init_db_pool()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
