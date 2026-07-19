import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import wraps
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(MAX_CONTENT_LENGTH=32 * 1024, JSON_SORT_KEYS=False)
application = app  # WSGI/serverless entry point

# Environment variables still take precedence, while these defaults make the
# supplied deployment self-contained. Rotate both credentials if this file is
# ever exposed outside the intended serverless deployment.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bothost_db_84ec903bbba4:kqgbIpjb75Y-3WogkF8-TR4byUe619W0-SAuKc6oEGI@node1.pghost.ru:15826/bothost_db_84ec903bbba4",
)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8871814741:AAEGXnpb6mlbaqDykBWr-VIsm1xDMqMCNaU")
ALLOW_DEV_AUTH = os.getenv("ALLOW_DEV_AUTH", "0") == "1"
DEV_TELEGRAM_USER_ID = int(os.getenv("DEV_TELEGRAM_USER_ID", "900000001"))
AUTH_MAX_AGE = int(os.getenv("TELEGRAM_AUTH_MAX_AGE", "86400"))
MAX_BET = Decimal(os.getenv("MAX_BET", "1000000"))
MONEY_STEP = Decimal("0.01")
RNG = secrets.SystemRandom()


DEFAULT_COEFFICIENTS = {
    ("dice", "1"): "0", ("dice", "2"): "0.3", ("dice", "3"): "0.5",
    ("dice", "4"): "1", ("dice", "5"): "1.5", ("dice", "6"): "3",
    ("dice", "bonus_3x6"): "5",
    ("basketball", "win"): "1.85", ("football", "win"): "1.7",
    ("roulette", "777"): "4", ("roulette", "fruit"): "2",
    ("roulette", "series_3x777"): "10",
    ("lootbox_2x2", "win"): "2", ("lootbox_3x3", "win"): "3",
    ("lootbox_6x5", "1_prize"): "1", ("lootbox_6x5", "2_prize"): "9",
    ("lootbox_6x5", "3_prize"): "30",
    ("darts", "bullseye"): "5", ("darts", "center"): "2",
    ("darts", "edge"): "1.5", ("darts", "miss"): "0",
    ("tictactoe", "win"): "2", ("tictactoe", "draw"): "1",
    ("minesweeper", "safe"): "1.5", ("minesweeper", "bomb"): "0",
    ("rps", "win"): "2", ("rps", "draw"): "1",
    ("coinflip", "win"): "1.95",
    ("blackjack", "win"): "2", ("blackjack", "push"): "1",
    ("blackjack", "blackjack"): "2.5",
    ("ladder", "step_1"): "1.40", ("ladder", "step_2"): "1.89",
    ("ladder", "step_3"): "2.38", ("ladder", "step_4"): "3.50",
    ("ladder", "step_5"): "7.00", ("ladder", "step_6"): "7.00",
    ("ladder", "step_7"): "7.00",
}

INSTANT_GAMES = {
    "dice", "basketball", "football", "roulette", "darts",
    "rps", "coinflip", "lootbox_2x2", "lootbox_3x3",
}
SESSION_GAMES = {"tictactoe", "minesweeper", "blackjack", "ladder", "lootbox_6x5"}
LADDER_MINES = [3, 4, 5, 6, 7, 7, 7]
LADDER_ROWS = 8

_schema_ready = False
_schema_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def qmoney(value):
    return Decimal(str(value)).quantize(MONEY_STEP, rounding=ROUND_DOWN)


def money(value):
    return format(qmoney(value), "f")


def parse_bet(value):
    try:
        bet = qmoney(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError("Введите корректную ставку")
    if bet < Decimal("1"):
        raise ApiError("Минимальная ставка — 1 ⭐")
    if bet > MAX_BET:
        raise ApiError(f"Максимальная ставка — {money(MAX_BET)} ⭐")
    return bet


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL, connect_timeout=7, application_name="telegram-miniapp")


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        balance DECIMAL(20,2) DEFAULT 0,
                        bonus_claimed BOOLEAN DEFAULT FALSE,
                        games_played INT DEFAULT 0,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN DEFAULT FALSE;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS games_played INT DEFAULT 0;
                    CREATE TABLE IF NOT EXISTS game_coefficients (
                        id SERIAL PRIMARY KEY,
                        game_name TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        coefficient DECIMAL DEFAULT 1,
                        UNIQUE(game_name, event_name)
                    );
                    CREATE TABLE IF NOT EXISTS miniapp_rounds (
                        id BIGSERIAL PRIMARY KEY,
                        request_id TEXT UNIQUE NOT NULL,
                        user_id BIGINT NOT NULL,
                        game TEXT NOT NULL,
                        bet DECIMAL(20,2) NOT NULL,
                        payout DECIMAL(20,2) NOT NULL DEFAULT 0,
                        result JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS miniapp_rounds_user_idx
                        ON miniapp_rounds(user_id, id DESC);
                    CREATE TABLE IF NOT EXISTS miniapp_sessions (
                        id TEXT PRIMARY KEY,
                        request_id TEXT UNIQUE NOT NULL,
                        user_id BIGINT NOT NULL,
                        game TEXT NOT NULL,
                        bet DECIMAL(20,2) NOT NULL,
                        state JSONB NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS miniapp_sessions_user_idx
                        ON miniapp_sessions(user_id, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS miniapp_pvp_rooms (
                        id TEXT PRIMARY KEY,
                        code TEXT UNIQUE NOT NULL,
                        player1_id BIGINT NOT NULL,
                        player2_id BIGINT,
                        bet DECIMAL(20,2) NOT NULL,
                        status TEXT NOT NULL DEFAULT 'waiting',
                        p1_roll INT,
                        p2_roll INT,
                        winner_id BIGINT,
                        result JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS miniapp_pvp_player_idx
                        ON miniapp_pvp_rooms(player1_id, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS miniapp_crash_rounds (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'betting',
                        betting_ends_at TIMESTAMPTZ NOT NULL,
                        crash_at TIMESTAMPTZ NOT NULL,
                        crash_multiplier DECIMAL(12,2) NOT NULL,
                        crashed_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS miniapp_crash_rounds_created_idx
                        ON miniapp_crash_rounds(created_at DESC);
                    CREATE TABLE IF NOT EXISTS miniapp_crash_bets (
                        id BIGSERIAL PRIMARY KEY,
                        round_id TEXT NOT NULL REFERENCES miniapp_crash_rounds(id),
                        user_id BIGINT NOT NULL,
                        amount DECIMAL(20,2) NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        cashout_multiplier DECIMAL(12,2),
                        payout DECIMAL(20,2) NOT NULL DEFAULT 0,
                        request_id TEXT UNIQUE NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(round_id, user_id)
                    );
                    CREATE INDEX IF NOT EXISTS miniapp_crash_bets_round_idx
                        ON miniapp_crash_bets(round_id, status);
                """)
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO game_coefficients(game_name, event_name, coefficient)
                       VALUES %s ON CONFLICT(game_name, event_name) DO NOTHING""",
                    [(game, event, Decimal(value)) for (game, event), value in DEFAULT_COEFFICIENTS.items()],
                )
            conn.commit()
        _schema_ready = True


@contextmanager
def transaction():
    ensure_schema()
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_telegram_init_data(raw):
    if not raw:
        if ALLOW_DEV_AUTH:
            return {"id": DEV_TELEGRAM_USER_ID, "first_name": "Dev", "username": "dev_user"}
        raise ApiError("Откройте приложение внутри Telegram", 401, "telegram_auth_required")
    if not BOT_TOKEN:
        raise ApiError("BOT_TOKEN не настроен на сервере", 503, "server_not_configured")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    supplied_hash = pairs.pop("hash", "")
    if not supplied_hash:
        raise ApiError("Некорректные данные Telegram", 401, "invalid_telegram_data")
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, supplied_hash):
        raise ApiError("Подпись Telegram не прошла проверку", 401, "invalid_telegram_signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        now = int(time.time())
        if auth_date > now + 60 or now - auth_date > AUTH_MAX_AGE:
            raise ApiError("Сессия Telegram устарела — откройте Mini App заново", 401, "telegram_data_expired")
        user = json.loads(pairs["user"])
        user["id"] = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("В данных Telegram отсутствует пользователь", 401, "invalid_telegram_user")
    return user


def telegram_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        raw = request.headers.get("X-Telegram-Init-Data", "")
        g.telegram_user = verify_telegram_init_data(raw)
        return fn(*args, **kwargs)
    return wrapped


def upsert_and_get_user(cur, tg_user, lock=False):
    username = tg_user.get("username") or None
    cur.execute("""
        INSERT INTO users(user_id, username) VALUES (%s, %s)
        ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username
    """, (tg_user["id"], username))
    cur.execute("""
        UPDATE users SET balance = balance + 5, bonus_claimed = TRUE
        WHERE user_id = %s AND COALESCE(bonus_claimed, FALSE) = FALSE
    """, (tg_user["id"],))
    suffix = " FOR UPDATE" if lock else ""
    cur.execute(
        "SELECT user_id, username, balance, games_played, created_at FROM users WHERE user_id = %s" + suffix,
        (tg_user["id"],),
    )
    return cur.fetchone()


def coefficient(cur, game, event):
    cur.execute(
        "SELECT coefficient FROM game_coefficients WHERE game_name=%s AND event_name=%s",
        (game, event),
    )
    row = cur.fetchone()
    if row:
        return Decimal(str(row["coefficient"]))
    return Decimal(DEFAULT_COEFFICIENTS.get((game, event), "1"))


def debit(cur, user_id, amount):
    cur.execute("""
        UPDATE users SET balance = balance - %s, games_played = games_played + 1
        WHERE user_id = %s AND balance >= %s RETURNING balance, games_played
    """, (amount, user_id, amount))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
        balance = cur.fetchone()["balance"]
        raise ApiError(f"Недостаточно средств. Баланс: {money(balance)} ⭐", 409, "insufficient_balance")
    return row


def credit(cur, user_id, amount):
    amount = qmoney(amount)
    if amount:
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, user_id))


def current_balance(cur, user_id):
    cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
    return qmoney(cur.fetchone()["balance"])


def valid_request_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value):
        raise ApiError("Некорректный request_id")
    return value


def add_round(cur, request_id, user_id, game, bet, payout, result):
    cur.execute("""
        INSERT INTO miniapp_rounds(request_id,user_id,game,bet,payout,result)
        VALUES(%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(request_id) DO NOTHING
    """, (request_id, user_id, game, qmoney(bet), qmoney(payout), json.dumps(result, ensure_ascii=False)))


def get_recent_game_results(cur, user_id, game, limit=2):
    cur.execute("""
        SELECT bet, result FROM miniapp_rounds
        WHERE user_id=%s AND game=%s ORDER BY id DESC LIMIT %s
    """, (user_id, game, limit))
    return cur.fetchall()


def public_profile(row, tg_user):
    display_name = " ".join(x for x in [tg_user.get("first_name"), tg_user.get("last_name")] if x).strip()
    return {
        "id": row["user_id"],
        "username": row.get("username"),
        "name": display_name or row.get("username") or f"Игрок {row['user_id']}",
        "photo_url": tg_user.get("photo_url"),
        "balance": money(row["balance"]),
        "games_played": int(row.get("games_played") or 0),
    }


@app.errorhandler(ApiError)
def handle_api_error(exc):
    return jsonify(ok=False, error=exc.message, code=exc.code), exc.status


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("Unhandled error")
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Временная ошибка сервера", code="server_error"), 500
    return Response("Internal Server Error", status=500)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "public, max-age=300"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'"
    )
    return response


@app.get("/health")
def health():
    return jsonify(ok=True, service="telegram-miniapp")


@app.get("/api/me")
@telegram_required
def api_me():
    with transaction() as (_, cur):
        row = upsert_and_get_user(cur, g.telegram_user)
        profile = public_profile(row, g.telegram_user)
    return jsonify(ok=True, user=profile)


@app.get("/api/balance")
@telegram_required
def api_balance():
    """Small read-only balance endpoint for a lightweight Telegram client."""
    with transaction() as (_, cur):
        row = upsert_and_get_user(cur, g.telegram_user)
    return jsonify(ok=True, balance=money(row["balance"]), games_played=int(row.get("games_played") or 0))


@app.get("/api/history")
@telegram_required
def api_history():
    limit = min(max(int(request.args.get("limit", 20)), 1), 50)
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user)
        cur.execute("""
            SELECT game, bet, payout, result, created_at FROM miniapp_rounds
            WHERE user_id=%s ORDER BY id DESC LIMIT %s
        """, (g.telegram_user["id"], limit))
        rows = cur.fetchall()
    items = [{
        "game": row["game"], "bet": money(row["bet"]), "payout": money(row["payout"]),
        "result": row["result"], "created_at": row["created_at"].isoformat(),
    } for row in rows]
    return jsonify(ok=True, items=items)


def instant_outcome(cur, user_id, game, bet, payload):
    payout = Decimal("0")
    result = {"game": game, "won": False}

    if game == "dice":
        value = RNG.randint(1, 6)
        mult = coefficient(cur, "dice", str(value))
        payout = qmoney(bet * mult)
        result.update(value=value, emoji="🎲", title=f"Выпало {value}", multiplier=money(mult))
        previous = get_recent_game_results(cur, user_id, "dice", 2)
        if value == 6 and len(previous) == 2 and all(int(x["result"].get("value", 0)) == 6 for x in previous):
            avg_bet = (bet + sum((Decimal(str(x["bet"])) for x in previous), Decimal("0"))) / 3
            bonus = qmoney(avg_bet * coefficient(cur, "dice", "bonus_3x6"))
            payout += bonus
            result["bonus"] = money(bonus)
            result["title"] = "Три шестёрки подряд!"

    elif game == "basketball":
        value = RNG.randint(1, 5)
        won = value in (4, 5)
        mult = coefficient(cur, game, "win") if won else Decimal("0")
        payout = qmoney(bet * mult)
        result.update(value=value, emoji="🏀", title="Попадание!" if won else "Мимо", multiplier=money(mult))

    elif game == "football":
        value = RNG.randint(1, 5)
        won = value in (3, 4, 5)
        mult = coefficient(cur, game, "win") if won else Decimal("0")
        payout = qmoney(bet * mult)
        result.update(value=value, emoji="⚽", title="Гол!" if won else "Мимо", multiplier=money(mult))

    elif game == "roulette":
        value = RNG.randint(1, 64)
        if value == 64:
            mult, title, symbol = coefficient(cur, game, "777"), "777!", "7️⃣7️⃣7️⃣"
        elif value in (1, 22, 43):
            mult, title, symbol = coefficient(cur, game, "fruit"), {1: "BAR", 22: "Вишня", 43: "Лимон"}[value], "🍒"
        else:
            mult, title, symbol = Decimal("0"), "Мимо", "🎰"
        payout = qmoney(bet * mult)
        result.update(value=value, emoji="🎰", symbol=symbol, title=title, multiplier=money(mult))
        previous = get_recent_game_results(cur, user_id, "roulette", 2)
        if value == 64 and len(previous) == 2 and all(int(x["result"].get("value", 0)) == 64 for x in previous):
            avg_bet = (bet + sum((Decimal(str(x["bet"])) for x in previous), Decimal("0"))) / 3
            bonus = qmoney(avg_bet * coefficient(cur, game, "series_3x777"))
            payout += bonus
            result["bonus"] = money(bonus)
            result["title"] = "Серия 3×777!"

    elif game == "darts":
        value = RNG.randint(1, 6)
        if value == 6:
            event, title = "bullseye", "Яблочко!"
        elif value == 5:
            event, title = "center", "Центр!"
        elif value in (2, 3, 4):
            event, title = "edge", "Попадание!"
        else:
            event, title = "miss", "Мимо"
        mult = coefficient(cur, game, event)
        payout = qmoney(bet * mult)
        result.update(value=value, emoji="🎯", title=title, multiplier=money(mult))

    elif game == "rps":
        choice = str(payload.get("choice", ""))
        if choice not in ("rock", "scissors", "paper"):
            raise ApiError("Выберите камень, ножницы или бумагу")
        bot = RNG.choice(["rock", "scissors", "paper"])
        wins = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        if choice == bot:
            mult, title = coefficient(cur, game, "draw"), "Ничья"
        elif wins[choice] == bot:
            mult, title = coefficient(cur, game, "win"), "Победа!"
        else:
            mult, title = Decimal("0"), "Бот победил"
        payout = qmoney(bet * mult)
        result.update(choice=choice, bot=bot, emoji="✊", title=title, multiplier=money(mult))

    elif game == "coinflip":
        choice = str(payload.get("choice", ""))
        if choice not in ("heads", "tails"):
            raise ApiError("Выберите орла или решку")
        landed = RNG.choice(["heads", "tails"])
        mult = coefficient(cur, game, "win") if choice == landed else Decimal("0")
        payout = qmoney(bet * mult)
        result.update(choice=choice, landed=landed, emoji="🪙", title="Угадали!" if choice == landed else "Не угадали", multiplier=money(mult))

    elif game in ("lootbox_2x2", "lootbox_3x3"):
        size = 4 if game.endswith("2x2") else 9
        try:
            chosen = int(payload.get("choice", RNG.randrange(size)))
        except (TypeError, ValueError):
            raise ApiError("Выберите бокс")
        if chosen < 0 or chosen >= size:
            raise ApiError("Такого бокса нет")
        winning = RNG.randrange(size)
        mult = coefficient(cur, game, "win") if chosen == winning else Decimal("0")
        payout = qmoney(bet * mult)
        result.update(choice=chosen, winning=winning, emoji="🎁", title="Приз найден!" if chosen == winning else "Пусто", multiplier=money(mult))

    else:
        raise ApiError("Неизвестная игра")

    result["won"] = payout > 0
    result["payout"] = money(payout)
    result["bet"] = money(bet)
    return qmoney(payout), result


@app.post("/api/play")
@telegram_required
def api_play():
    payload = request.get_json(silent=True) or {}
    game = str(payload.get("game", ""))
    if game not in INSTANT_GAMES:
        raise ApiError("Эта игра запускается другим способом")
    bet = parse_bet(payload.get("bet"))
    request_id = valid_request_id(payload.get("request_id"))
    user_id = g.telegram_user["id"]

    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT result FROM miniapp_rounds WHERE request_id=%s AND user_id=%s", (request_id, user_id))
        duplicate = cur.fetchone()
        if duplicate:
            result = duplicate["result"]
            result["balance"] = money(current_balance(cur, user_id))
            return jsonify(ok=True, result=result, duplicate=True)
        debit(cur, user_id, bet)
        payout, result = instant_outcome(cur, user_id, game, bet, payload)
        credit(cur, user_id, payout)
        result["balance"] = money(current_balance(cur, user_id))
        add_round(cur, request_id, user_id, game, bet, payout, result)
    return jsonify(ok=True, result=result)


# ----- Interactive games ----------------------------------------------------

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def make_deck():
    deck = [rank + suit for suit in SUITS for rank in RANKS]
    RNG.shuffle(deck)
    return deck


def card_rank(card):
    return card[:-1]


def hand_value(hand):
    total, aces = 0, 0
    for card in hand:
        rank = card_rank(card)
        if rank == "A":
            total, aces = total + 11, aces + 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total, aces = total - 10, aces - 1
    return total


def ttt_winner(board):
    for a, b, c in ((0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)):
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None


def ttt_score(board, maximizing, depth=0):
    winner = ttt_winner(board)
    if winner == "O": return 10 - depth
    if winner == "X": return depth - 10
    if winner == "draw": return 0
    scores = []
    for i, value in enumerate(board):
        if not value:
            board[i] = "O" if maximizing else "X"
            scores.append(ttt_score(board, not maximizing, depth + 1))
            board[i] = ""
    return max(scores) if maximizing else min(scores)


def ttt_bot_move(board):
    best_score, candidates = -999, []
    for i, value in enumerate(board):
        if not value:
            board[i] = "O"
            score = ttt_score(board, False)
            board[i] = ""
            if score > best_score:
                best_score, candidates = score, [i]
            elif score == best_score:
                candidates.append(i)
    return RNG.choice(candidates)


def ladder_multiplier(cur, steps):
    value = Decimal("1")
    for index in range(steps):
        value = qmoney(value * coefficient(cur, "ladder", f"step_{index + 1}"))
    return value


def settle_blackjack(cur, state):
    while hand_value(state["dealer"]) < 17:
        state["dealer"].append(state["deck"].pop())
    player_value, dealer_value = hand_value(state["player"]), hand_value(state["dealer"])
    bet = Decimal(state["bet_total"])
    if player_value > 21:
        payout, label = Decimal("0"), "Перебор"
    elif dealer_value > 21 or player_value > dealer_value:
        payout, label = qmoney(bet * coefficient(cur, "blackjack", "win")), "Победа!"
    elif player_value == dealer_value:
        payout, label = qmoney(bet * coefficient(cur, "blackjack", "push")), "Ничья"
    else:
        payout, label = Decimal("0"), "Дилер победил"
    state.update(result=label, payout=money(payout), finished=True)
    return qmoney(payout)


def new_session_state(cur, game, bet, options):
    if game == "tictactoe":
        board = [""] * 9
        if RNG.choice([True, False]):
            board[ttt_bot_move(board)] = "O"
        return {"board": board, "finished": False}
    if game == "minesweeper":
        size = int(options.get("size", 5))
        if size not in (5, 7):
            raise ApiError("Размер поля должен быть 5×5 или 7×7")
        bombs, target = (6, 6) if size == 5 else (12, 12)
        return {"size": size, "bombs": RNG.sample(range(size * size), bombs), "revealed": [], "target": target, "finished": False}
    if game == "blackjack":
        deck = make_deck()
        state = {"deck": deck, "player": [deck.pop(), deck.pop()], "dealer": [deck.pop(), deck.pop()],
                 "bet_total": money(bet), "doubled": False, "finished": False}
        return state
    if game == "ladder":
        grid = []
        for count in LADDER_MINES:
            column = [True] * count + [False] * (LADDER_ROWS - count)
            RNG.shuffle(column)
            grid.append(column)
        return {"grid": grid, "revealed": [], "step": 0, "finished": False}
    if game == "lootbox_6x5":
        return {"winning": RNG.sample(range(30), 3), "opened": [], "attempts": 3, "hits": 0, "finished": False}
    raise ApiError("Неизвестная игра")


def session_public(cur, row, balance):
    game, state, status = row["game"], dict(row["state"]), row["status"]
    view = {
        "id": row["id"], "game": game, "bet": money(row["bet"]), "status": status,
        "finished": status == "finished", "balance": money(balance),
        "result": state.get("result"), "payout": state.get("payout", "0.00"),
    }
    if game == "tictactoe":
        view.update(board=state["board"])
    elif game == "minesweeper":
        view.update(size=state["size"], revealed=state["revealed"], target=state["target"])
        if status == "finished": view["bombs"] = state["bombs"]
    elif game == "blackjack":
        dealer = state["dealer"] if status == "finished" else [state["dealer"][0], "🂠"]
        view.update(player=state["player"], dealer=dealer, player_value=hand_value(state["player"]),
                    dealer_value=hand_value(state["dealer"]) if status == "finished" else None,
                    can_double=status == "active" and len(state["player"]) == 2 and not state["doubled"])
    elif game == "ladder":
        view.update(step=state["step"], revealed=state["revealed"], mines=LADDER_MINES,
                    multiplier=money(ladder_multiplier(cur, state["step"])))
        if status == "finished": view["mine"] = state.get("mine")
    elif game == "lootbox_6x5":
        view.update(opened=state["opened"], attempts=state["attempts"], hits=state["hits"])
        if status == "finished": view["winning"] = state["winning"]
    return view


def finish_session(cur, row, state, payout, label):
    user_id, game = row["user_id"], row["game"]
    state.update(finished=True, result=label, payout=money(payout))
    credit(cur, user_id, payout)
    cur.execute("""
        UPDATE miniapp_sessions SET state=%s::jsonb,status='finished',updated_at=NOW(),bet=%s WHERE id=%s
    """, (json.dumps(state, ensure_ascii=False), Decimal(state.get("bet_total", row["bet"])), row["id"]))
    add_round(cur, f"session:{row['id']}", user_id, game, Decimal(state.get("bet_total", row["bet"])), payout,
              {"title": label, "payout": money(payout), "game": game})


@app.post("/api/session/start")
@telegram_required
def api_session_start():
    payload = request.get_json(silent=True) or {}
    game = str(payload.get("game", ""))
    if game not in SESSION_GAMES:
        raise ApiError("Неизвестная интерактивная игра")
    bet = Decimal("3") if game == "lootbox_6x5" else parse_bet(payload.get("bet"))
    request_id = valid_request_id(payload.get("request_id"))
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT * FROM miniapp_sessions WHERE request_id=%s AND user_id=%s", (request_id, user_id))
        existing = cur.fetchone()
        if existing:
            return jsonify(ok=True, session=session_public(cur, existing, current_balance(cur, user_id)), duplicate=True)
        debit(cur, user_id, bet)
        state = new_session_state(cur, game, bet, payload.get("options") or {})
        session_id = uuid.uuid4().hex
        status = "active"
        payout = Decimal("0")
        if game == "blackjack" and hand_value(state["player"]) == 21:
            dealer_value = hand_value(state["dealer"])
            event, label = ("push", "У обоих блэкджек — ничья") if dealer_value == 21 else ("blackjack", "BLACKJACK!")
            payout = qmoney(bet * coefficient(cur, game, event))
            state.update(finished=True, result=label, payout=money(payout))
            credit(cur, user_id, payout)
            status = "finished"
        cur.execute("""
            INSERT INTO miniapp_sessions(id,request_id,user_id,game,bet,state,status)
            VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
        """, (session_id, request_id, user_id, game, bet, json.dumps(state, ensure_ascii=False), status))
        row = cur.fetchone()
        if status == "finished":
            add_round(cur, f"session:{session_id}", user_id, game, bet, payout,
                      {"title": state["result"], "payout": money(payout), "game": game})
        view = session_public(cur, row, current_balance(cur, user_id))
    return jsonify(ok=True, session=view)


@app.post("/api/session/<session_id>/action")
@telegram_required
def api_session_action(session_id):
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", ""))
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT * FROM miniapp_sessions WHERE id=%s AND user_id=%s FOR UPDATE", (session_id, user_id))
        row = cur.fetchone()
        if not row:
            raise ApiError("Партия не найдена", 404, "session_not_found")
        if row["status"] != "active":
            return jsonify(ok=True, session=session_public(cur, row, current_balance(cur, user_id)), duplicate=True)
        game, state, bet = row["game"], dict(row["state"]), Decimal(str(row["bet"]))
        finished, payout, label = False, Decimal("0"), None

        if game == "tictactoe":
            if action != "move": raise ApiError("Неизвестное действие")
            try: index = int(payload.get("index"))
            except (TypeError, ValueError): raise ApiError("Выберите клетку")
            if index not in range(9) or state["board"][index]: raise ApiError("Клетка занята")
            state["board"][index] = "X"
            winner = ttt_winner(state["board"])
            if not winner:
                state["board"][ttt_bot_move(state["board"])] = "O"
                winner = ttt_winner(state["board"])
            if winner:
                finished = True
                if winner == "X": payout, label = qmoney(bet * coefficient(cur, game, "win")), "Вы выиграли!"
                elif winner == "draw": payout, label = qmoney(bet * coefficient(cur, game, "draw")), "Ничья"
                else: label = "Бот выиграл"

        elif game == "minesweeper":
            if action != "pick": raise ApiError("Неизвестное действие")
            try: index = int(payload.get("index"))
            except (TypeError, ValueError): raise ApiError("Выберите клетку")
            if index not in range(state["size"] ** 2): raise ApiError("Такой клетки нет")
            if index in state["revealed"]: raise ApiError("Клетка уже открыта")
            state["revealed"].append(index)
            if index in state["bombs"]:
                finished, label = True, "БОМБА!"
            elif len(state["revealed"]) >= state["target"]:
                finished, label = True, "Все клетки безопасны!"
                payout = qmoney(bet * coefficient(cur, game, "safe"))

        elif game == "blackjack":
            if action not in ("hit", "stand", "double"): raise ApiError("Неизвестное действие")
            if action == "double":
                if state["doubled"] or len(state["player"]) != 2: raise ApiError("Дабл сейчас недоступен")
                additional = Decimal(state["bet_total"])
                cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance>=%s RETURNING balance",
                            (additional, user_id, additional))
                if not cur.fetchone(): raise ApiError("Недостаточно средств для дабла", 409, "insufficient_balance")
                state["bet_total"] = money(additional * 2)
                state["doubled"] = True
                state["player"].append(state["deck"].pop())
                payout = settle_blackjack(cur, state)
                finished, label = True, state["result"]
            elif action == "hit":
                state["player"].append(state["deck"].pop())
                if hand_value(state["player"]) > 21:
                    finished, label = True, "Перебор"
                elif hand_value(state["player"]) == 21:
                    payout = settle_blackjack(cur, state)
                    finished, label = True, state["result"]
            else:
                payout = settle_blackjack(cur, state)
                finished, label = True, state["result"]

        elif game == "ladder":
            if action == "cashout":
                if state["step"] < 1: raise ApiError("Сначала пройдите один шаг")
                mult = ladder_multiplier(cur, state["step"])
                payout, label, finished = qmoney(bet * mult), f"Вы забрали x{money(mult)}", True
            elif action == "pick":
                try: row_index = int(payload.get("row"))
                except (TypeError, ValueError): raise ApiError("Выберите клетку")
                if row_index not in range(LADDER_ROWS): raise ApiError("Такой клетки нет")
                col = state["step"]
                if col >= len(LADDER_MINES): raise ApiError("Лесенка уже пройдена")
                state["revealed"].append([row_index, col])
                if state["grid"][col][row_index]:
                    state["mine"] = [row_index, col]
                    finished, label = True, "БОМБА!"
                else:
                    state["step"] += 1
                    if state["step"] == len(LADDER_MINES):
                        mult = ladder_multiplier(cur, state["step"])
                        payout, label, finished = qmoney(bet * mult), "Лесенка покорена!", True
            else: raise ApiError("Неизвестное действие")

        elif game == "lootbox_6x5":
            if action != "pick": raise ApiError("Неизвестное действие")
            try: index = int(payload.get("index"))
            except (TypeError, ValueError): raise ApiError("Выберите бокс")
            if index not in range(30): raise ApiError("Такого бокса нет")
            if index in state["opened"]: raise ApiError("Бокс уже открыт")
            state["opened"].append(index)
            state["attempts"] -= 1
            if index in state["winning"]: state["hits"] += 1
            if state["attempts"] == 0:
                finished = True
                hits = state["hits"]
                if hits == 0: payout, label = Decimal("0"), "Призов нет"
                elif hits == 1: payout, label = Decimal("3"), "Один приз — ставка возвращена"
                else:
                    payout = qmoney(coefficient(cur, game, f"{hits}_prize"))
                    label = f"Найдено призов: {hits}!"
        else:
            raise ApiError("Неизвестная игра")

        if finished:
            finish_session(cur, row, state, payout, label)
        else:
            cur.execute("UPDATE miniapp_sessions SET state=%s::jsonb,updated_at=NOW() WHERE id=%s RETURNING *",
                        (json.dumps(state, ensure_ascii=False), session_id))
        cur.execute("SELECT * FROM miniapp_sessions WHERE id=%s", (session_id,))
        updated = cur.fetchone()
        view = session_public(cur, updated, current_balance(cur, user_id))
    return jsonify(ok=True, session=view)


# ----- PvP rooms ------------------------------------------------------------

def pvp_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(RNG.choice(alphabet) for _ in range(6))


def pvp_view(row, user_id, balance):
    result = dict(row.get("result") or {})
    if row["status"] == "finished":
        result["title"] = (
            "Ничья" if not row.get("winner_id")
            else "Победа!" if row["winner_id"] == user_id
            else "Поражение"
        )
    return {
        "id": row["id"], "code": row["code"], "status": row["status"], "bet": money(row["bet"]),
        "is_owner": row["player1_id"] == user_id, "player1_id": row["player1_id"],
        "player2_id": row.get("player2_id"), "p1_roll": row.get("p1_roll"), "p2_roll": row.get("p2_roll"),
        "winner_id": row.get("winner_id"), "result": result, "balance": money(balance),
    }


# ----- Crash: PostgreSQL-backed multiplayer round --------------------------

def charge(cur, user_id, amount):
    """Deduct funds without incrementing games_played (used by Crash bets)."""
    amount = qmoney(amount)
    cur.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s RETURNING balance",
        (amount, user_id, amount),
    )
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
        balance = cur.fetchone()["balance"]
        raise ApiError(f"Недостаточно средств. Баланс: {money(balance)} ⭐", 409, "insufficient_balance")
    return row["balance"]


def _crash_multiplier_for_elapsed(round_row, now):
    start = round_row["betting_ends_at"]
    elapsed = max(0.0, (now - start).total_seconds())
    if now >= round_row["crash_at"]:
        return qmoney(round_row["crash_multiplier"])
    # Smooth exponential climb. The persisted crash point remains authoritative.
    return min(qmoney(round_row["crash_multiplier"]), qmoney(Decimal(str(math.exp(elapsed / 6.0)))))


def _new_crash_round(cur):
    now = datetime.now(timezone.utc)
    betting_ends = now + timedelta(seconds=10)
    # Most rounds end between roughly 1.5x and 20x, with a hard safety cap.
    random_value = max(0.000001, RNG.random())
    duration = max(1.8, min(75.0, -math.log(random_value) * 7.0 + 1.5))
    crash_multiplier = qmoney(Decimal(str(math.exp(duration / 6.0))))
    crash_multiplier = max(Decimal("1.01"), min(Decimal("50.00"), crash_multiplier))
    round_id = uuid.uuid4().hex
    cur.execute("""
        INSERT INTO miniapp_crash_rounds(id,status,betting_ends_at,crash_at,crash_multiplier)
        VALUES(%s,'betting',%s,%s,%s) RETURNING *
    """, (round_id, betting_ends, betting_ends + timedelta(seconds=duration), crash_multiplier))
    return cur.fetchone()


def _mark_crashed(cur, round_row, now):
    if round_row["status"] == "crashed":
        return round_row
    cur.execute("""
        UPDATE miniapp_crash_rounds SET status='crashed',crashed_at=%s WHERE id=%s RETURNING *
    """, (now, round_row["id"]))
    round_row = cur.fetchone()
    # Active bets remain losses. Settled bets are never touched.
    cur.execute("UPDATE miniapp_crash_bets SET status='lost' WHERE round_id=%s AND status='active'", (round_row["id"],))
    return round_row


def _current_crash_round(cur, create=True):
    now = datetime.now(timezone.utc)
    cur.execute("SELECT * FROM miniapp_crash_rounds ORDER BY created_at DESC LIMIT 1 FOR UPDATE")
    row = cur.fetchone()
    if not row:
        return _new_crash_round(cur) if create else None
    if row["status"] == "betting" and now >= row["betting_ends_at"]:
        cur.execute("UPDATE miniapp_crash_rounds SET status='running' WHERE id=%s RETURNING *", (row["id"],))
        row = cur.fetchone()
    if row["status"] == "running" and now >= row["crash_at"]:
        row = _mark_crashed(cur, row, now)
    if row["status"] == "crashed" and row["crashed_at"] and (now - row["crashed_at"]).total_seconds() >= 4:
        return _new_crash_round(cur) if create else row
    return row


def _crash_public(cur, round_row, user_id):
    now = datetime.now(timezone.utc)
    status = round_row["status"]
    if status == "betting":
        seconds_left = max(0, math.ceil((round_row["betting_ends_at"] - now).total_seconds()))
        multiplier = Decimal("1.00")
    elif status == "running":
        seconds_left = 0
        multiplier = _crash_multiplier_for_elapsed(round_row, now)
    else:
        seconds_left = 0
        multiplier = qmoney(round_row["crash_multiplier"])

    cur.execute("""
        SELECT b.id,b.user_id,b.amount,b.status,b.cashout_multiplier,b.payout,u.username
        FROM miniapp_crash_bets b LEFT JOIN users u ON u.user_id=b.user_id
        WHERE b.round_id=%s ORDER BY b.created_at ASC LIMIT 100
    """, (round_row["id"],))
    bets = []
    own_bet = None
    for bet in cur.fetchall():
        name = ("@" + bet["username"]) if bet.get("username") else "Игрок " + str(bet["user_id"])[-4:]
        item = {
            "name": name[:24], "amount": money(bet["amount"]), "status": bet["status"],
            "multiplier": money(bet["cashout_multiplier"] or 0), "payout": money(bet["payout"]),
            "is_you": bet["user_id"] == user_id,
        }
        bets.append(item)
        if bet["user_id"] == user_id:
            own_bet = item

    cur.execute("""
        SELECT crash_multiplier,crashed_at FROM miniapp_crash_rounds
        WHERE status='crashed' ORDER BY created_at DESC LIMIT 10
    """)
    recent = [{"multiplier": money(row["crash_multiplier"]), "at": row["crashed_at"].isoformat() if row["crashed_at"] else None}
              for row in cur.fetchall()]
    return {
        "round_id": round_row["id"], "status": status, "seconds_left": seconds_left,
        "multiplier": money(multiplier),
        "crash_multiplier": money(round_row["crash_multiplier"]) if status == "crashed" else None,
        "bets": bets, "own_bet": own_bet, "recent": recent,
    }


@app.get("/api/crash/state")
@telegram_required
def api_crash_state():
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user)
        round_row = _current_crash_round(cur)
        view = _crash_public(cur, round_row, user_id)
        view["balance"] = money(current_balance(cur, user_id))
    return jsonify(ok=True, crash=view)


@app.post("/api/crash/bet")
@telegram_required
def api_crash_bet():
    payload = request.get_json(silent=True) or {}
    bet = parse_bet(payload.get("bet"))
    request_id = valid_request_id(payload.get("request_id"))
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT * FROM miniapp_crash_bets WHERE request_id=%s AND user_id=%s", (request_id, user_id))
        duplicate = cur.fetchone()
        if duplicate:
            round_row = _current_crash_round(cur)
            view = _crash_public(cur, round_row, user_id)
            view["balance"] = money(current_balance(cur, user_id))
            return jsonify(ok=True, crash=view, duplicate=True)
        round_row = _current_crash_round(cur)
        now = datetime.now(timezone.utc)
        if round_row["status"] != "betting" or now >= round_row["betting_ends_at"]:
            raise ApiError("Ставки на этот раунд уже закрыты", 409, "betting_closed")
        cur.execute("SELECT 1 FROM miniapp_crash_bets WHERE round_id=%s AND user_id=%s", (round_row["id"], user_id))
        if cur.fetchone():
            raise ApiError("У вас уже есть ставка в этом раунде")
        charge(cur, user_id, bet)
        cur.execute("UPDATE users SET games_played=games_played+1 WHERE user_id=%s", (user_id,))
        cur.execute("""
            INSERT INTO miniapp_crash_bets(round_id,user_id,amount,request_id)
            VALUES(%s,%s,%s,%s)
        """, (round_row["id"], user_id, bet, request_id))
        view = _crash_public(cur, round_row, user_id)
        view["balance"] = money(current_balance(cur, user_id))
    return jsonify(ok=True, crash=view)


@app.post("/api/crash/cashout")
@telegram_required
def api_crash_cashout():
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        round_row = _current_crash_round(cur)
        now = datetime.now(timezone.utc)
        if round_row["status"] == "running" and now >= round_row["crash_at"]:
            round_row = _mark_crashed(cur, round_row, now)
        cur.execute("""
            SELECT * FROM miniapp_crash_bets WHERE round_id=%s AND user_id=%s FOR UPDATE
        """, (round_row["id"], user_id))
        bet_row = cur.fetchone()
        if not bet_row:
            raise ApiError("В этом раунде нет вашей ставки", 404, "bet_not_found")
        if bet_row["status"] == "active" and round_row["status"] == "running":
            multiplier = _crash_multiplier_for_elapsed(round_row, now)
            payout = qmoney(Decimal(str(bet_row["amount"])) * multiplier)
            cur.execute("""
                UPDATE miniapp_crash_bets
                SET status='cashed',cashout_multiplier=%s,payout=%s WHERE id=%s
            """, (multiplier, payout, bet_row["id"]))
            credit(cur, user_id, payout)
            add_round(cur, f"crash:{bet_row['id']}", user_id, "crash", bet_row["amount"], payout, {
                "title": f"Cashout x{money(multiplier)}", "multiplier": money(multiplier), "game": "crash"
            })
        elif bet_row["status"] == "active":
            raise ApiError("Слишком поздно — ракета уже разбилась", 409, "crashed")
        view = _crash_public(cur, round_row, user_id)
        view["balance"] = money(current_balance(cur, user_id))
    return jsonify(ok=True, crash=view)


@app.post("/api/pvp/create")
@telegram_required
def api_pvp_create():
    payload = request.get_json(silent=True) or {}
    bet = parse_bet(payload.get("bet"))
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        debit(cur, user_id, bet)
        room_id = uuid.uuid4().hex
        for _ in range(10):
            code = pvp_code()
            cur.execute("SELECT 1 FROM miniapp_pvp_rooms WHERE code=%s", (code,))
            if not cur.fetchone(): break
        cur.execute("""
            INSERT INTO miniapp_pvp_rooms(id,code,player1_id,bet) VALUES(%s,%s,%s,%s) RETURNING *
        """, (room_id, code, user_id, bet))
        row = cur.fetchone()
        view = pvp_view(row, user_id, current_balance(cur, user_id))
    return jsonify(ok=True, room=view)


@app.post("/api/pvp/join")
@telegram_required
def api_pvp_join():
    code = str((request.get_json(silent=True) or {}).get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z2-9]{6}", code): raise ApiError("Введите код из 6 символов")
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT * FROM miniapp_pvp_rooms WHERE code=%s FOR UPDATE", (code,))
        room = cur.fetchone()
        if not room: raise ApiError("Комната не найдена", 404, "room_not_found")
        if room["player1_id"] == user_id: raise ApiError("Нельзя играть против себя")
        if room["status"] != "waiting": raise ApiError("Комната уже закрыта", 409, "room_closed")
        bet = Decimal(str(room["bet"]))
        debit(cur, user_id, bet)
        p1, p2 = RNG.randint(1, 6), RNG.randint(1, 6)
        winner = room["player1_id"] if p1 > p2 else user_id if p2 > p1 else None
        if winner:
            credit(cur, winner, bet * 2)
            label = "Победа" if winner == user_id else "Поражение"
        else:
            credit(cur, room["player1_id"], bet)
            credit(cur, user_id, bet)
            label = "Ничья"
        result = {"title": label, "p1_roll": p1, "p2_roll": p2}
        cur.execute("""
            UPDATE miniapp_pvp_rooms SET player2_id=%s,status='finished',p1_roll=%s,p2_roll=%s,
                winner_id=%s,result=%s::jsonb,updated_at=NOW() WHERE id=%s RETURNING *
        """, (user_id, p1, p2, winner, json.dumps(result, ensure_ascii=False), room["id"]))
        updated = cur.fetchone()
        add_round(cur, f"pvp:{room['id']}:{room['player1_id']}", room["player1_id"], "pvp", bet,
                  bet * 2 if winner == room["player1_id"] else bet if winner is None else 0, result)
        add_round(cur, f"pvp:{room['id']}:{user_id}", user_id, "pvp", bet,
                  bet * 2 if winner == user_id else bet if winner is None else 0, result)
        view = pvp_view(updated, user_id, current_balance(cur, user_id))
    return jsonify(ok=True, room=view)


@app.get("/api/pvp/<room_id>")
@telegram_required
def api_pvp_status(room_id):
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user)
        cur.execute("SELECT * FROM miniapp_pvp_rooms WHERE id=%s AND (player1_id=%s OR player2_id=%s)",
                    (room_id, user_id, user_id))
        room = cur.fetchone()
        if not room: raise ApiError("Комната не найдена", 404, "room_not_found")
        view = pvp_view(room, user_id, current_balance(cur, user_id))
    return jsonify(ok=True, room=view)


@app.post("/api/pvp/<room_id>/cancel")
@telegram_required
def api_pvp_cancel(room_id):
    user_id = g.telegram_user["id"]
    with transaction() as (_, cur):
        upsert_and_get_user(cur, g.telegram_user, lock=True)
        cur.execute("SELECT * FROM miniapp_pvp_rooms WHERE id=%s FOR UPDATE", (room_id,))
        room = cur.fetchone()
        if not room or room["player1_id"] != user_id: raise ApiError("Комната не найдена", 404, "room_not_found")
        if room["status"] == "waiting":
            credit(cur, user_id, room["bet"])
            cur.execute("UPDATE miniapp_pvp_rooms SET status='cancelled',updated_at=NOW() WHERE id=%s RETURNING *", (room_id,))
            room = cur.fetchone()
        view = pvp_view(room, user_id, current_balance(cur, user_id))
    return jsonify(ok=True, room=view)


# The self-contained interface is appended below so deployment needs only app.py.
HTML_PAGE = r'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
  <meta name="theme-color" content="#0b0d12">
  <title>Royal Spin</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root{
      --bg:#090b10;--surface:#12151d;--surface-2:#191d27;--line:rgba(255,255,255,.075);
      --text:#f7f7fa;--muted:#8d93a2;--purple:#886cff;--purple-2:#aa8cff;
      --green:#53e3a6;--red:#ff6577;--gold:#ffc55c;--shadow:0 18px 60px rgba(0,0,0,.45);
      --safe-top:max(14px,env(safe-area-inset-top));--safe-bottom:max(16px,env(safe-area-inset-bottom));
    }
    *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
    html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    body{padding:var(--safe-top) 16px calc(90px + var(--safe-bottom));overflow-x:hidden}
    button,input{font:inherit}button{color:inherit}button:disabled{opacity:.5;pointer-events:none}
    .app{width:100%;max-width:720px;margin:0 auto}.hidden{display:none!important}
    .topbar{display:flex;align-items:center;justify-content:space-between;margin:4px 0 18px}
    .brand{display:flex;align-items:center;gap:10px;font-weight:780;letter-spacing:-.02em}
    .brand-mark{width:36px;height:36px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(145deg,var(--purple-2),#5d45e9);box-shadow:0 8px 25px rgba(136,108,255,.28)}
    .user-pill{display:flex;align-items:center;gap:8px;border:1px solid var(--line);background:var(--surface);padding:5px 9px 5px 5px;border-radius:100px;max-width:50%}
    .avatar{width:29px;height:29px;border-radius:50%;object-fit:cover;background:linear-gradient(145deg,#2a3040,#181c25);display:grid;place-items:center;font-weight:700;font-size:12px}
    .user-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;font-weight:650}
    .hero{position:relative;overflow:hidden;padding:22px;border:1px solid rgba(255,255,255,.09);border-radius:24px;background:radial-gradient(120% 140% at 90% 10%,rgba(136,108,255,.35),transparent 52%),linear-gradient(145deg,#191d28,#11141b);box-shadow:var(--shadow)}
    .hero:after{content:"";position:absolute;right:-45px;bottom:-65px;width:170px;height:170px;border:1px solid rgba(255,255,255,.1);border-radius:50%;box-shadow:0 0 0 25px rgba(255,255,255,.018),0 0 0 50px rgba(255,255,255,.012)}
    .eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.13em;color:#b1b5c1;font-weight:750}
    .balance-row{display:flex;align-items:flex-end;gap:10px;margin:7px 0 5px;position:relative;z-index:1}
    .balance{font-size:38px;line-height:1;font-weight:850;letter-spacing:-.055em}.star{font-size:23px;margin-bottom:3px}
    .hero-meta{font-size:13px;color:var(--muted)}
    .refresh{position:absolute;right:18px;top:18px;width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:rgba(255,255,255,.06);z-index:2;cursor:pointer}
    .section-head{display:flex;justify-content:space-between;align-items:center;margin:25px 1px 13px}
    .section-head h2{font-size:18px;margin:0;letter-spacing:-.02em}.section-head span{font-size:12px;color:var(--muted)}
    .games{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
    .game-card{text-align:left;min-height:142px;padding:16px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(150deg,var(--surface-2),var(--surface));position:relative;overflow:hidden;cursor:pointer;transition:transform .16s ease,border-color .16s ease}
    .game-card:active{transform:scale(.975)}.game-card:hover{border-color:rgba(136,108,255,.35)}
    .game-card:after{content:"";position:absolute;width:75px;height:75px;border-radius:50%;right:-25px;top:-28px;background:var(--glow,rgba(136,108,255,.12));filter:blur(4px)}
    .game-emoji{font-size:34px;filter:drop-shadow(0 8px 15px rgba(0,0,0,.3))}.game-name{font-size:15px;font-weight:760;margin-top:13px}.game-desc{font-size:11px;line-height:1.35;color:var(--muted);margin-top:4px;max-width:90%}
    .game-tag{position:absolute;right:10px;bottom:10px;border:1px solid var(--line);background:rgba(255,255,255,.045);padding:4px 7px;border-radius:8px;font-size:10px;font-weight:700;color:#c7cad3}
    .nav{position:fixed;z-index:30;left:50%;bottom:calc(9px + var(--safe-bottom));transform:translateX(-50%);width:calc(100% - 28px);max-width:690px;height:66px;border:1px solid rgba(255,255,255,.09);border-radius:22px;background:rgba(20,23,31,.9);backdrop-filter:blur(18px);display:grid;grid-template-columns:repeat(3,1fr);box-shadow:0 14px 45px rgba(0,0,0,.42);padding:6px}
    .nav button{border:0;background:none;border-radius:16px;color:var(--muted);font-size:11px;display:grid;place-items:center;gap:1px;cursor:pointer}.nav .ico{font-size:20px;line-height:1}.nav button.active{background:rgba(136,108,255,.14);color:#c9bfff}
    .panel{animation:fade .2s ease}@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
    .history-list{display:grid;gap:10px}.history-item{display:flex;align-items:center;gap:12px;padding:13px;border-radius:17px;border:1px solid var(--line);background:var(--surface)}
    .history-icon{width:40px;height:40px;border-radius:13px;background:var(--surface-2);display:grid;place-items:center;font-size:22px}.history-main{min-width:0;flex:1}.history-title{font-size:14px;font-weight:700}.history-time{font-size:11px;color:var(--muted);margin-top:3px}.history-money{text-align:right;font-size:13px;font-weight:750}.history-money.win{color:var(--green)}.history-bet{font-size:10px;color:var(--muted);margin-top:3px}
    .profile-card{padding:22px;border-radius:23px;border:1px solid var(--line);background:var(--surface);text-align:center}.profile-avatar{width:74px;height:74px;border-radius:24px;margin:0 auto 12px;background:linear-gradient(145deg,#2d3342,#181b23);display:grid;place-items:center;font-size:26px;font-weight:800}.profile-name{font-size:20px;font-weight:800}.profile-user{color:var(--muted);font-size:13px;margin-top:4px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}.stat{padding:15px;border-radius:16px;background:var(--surface-2)}.stat b{display:block;font-size:20px}.stat span{font-size:11px;color:var(--muted)}
    .sheet-wrap{position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.62);backdrop-filter:blur(5px);opacity:0;pointer-events:none;transition:opacity .2s}.sheet-wrap.open{opacity:1;pointer-events:auto}
    .sheet{position:absolute;left:50%;bottom:0;transform:translate(-50%,105%);width:100%;max-width:720px;max-height:92vh;overflow:auto;border-radius:28px 28px 0 0;border:1px solid var(--line);border-bottom:0;background:#11141b;padding:10px 18px calc(20px + var(--safe-bottom));transition:transform .28s cubic-bezier(.2,.85,.25,1);box-shadow:0 -20px 80px rgba(0,0,0,.5)}
    .sheet-wrap.open .sheet{transform:translate(-50%,0)}.handle{width:42px;height:5px;border-radius:10px;background:#303541;margin:0 auto 14px}.sheet-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}.sheet-title{font-size:20px;font-weight:820;letter-spacing:-.03em}.close{width:35px;height:35px;border-radius:12px;border:0;background:var(--surface-2);cursor:pointer;font-size:18px}
    .game-stage{text-align:center;padding:9px 0 17px}.stage-emoji{font-size:68px;line-height:1.1;filter:drop-shadow(0 15px 25px rgba(0,0,0,.35));animation:float 2.8s ease-in-out infinite;display:inline-block}.stage-emoji.anim-football{animation:footballKick 1.35s ease-in-out infinite}.stage-emoji.anim-basketball{animation:basketBounce 1.15s ease-in-out infinite}.stage-emoji.anim-dice{animation:diceRoll 1.3s cubic-bezier(.35,.05,.55,1) infinite}@keyframes float{50%{transform:translateY(-5px)}}@keyframes footballKick{0%,100%{transform:translate(-4px,5px) rotate(-10deg)}45%{transform:translate(8px,-18px) rotate(22deg)}70%{transform:translate(17px,0) rotate(45deg)}}@keyframes basketBounce{0%,100%{transform:translateY(-9px) rotate(-6deg)}45%{transform:translateY(21px) rotate(7deg)}70%{transform:translateY(-2px) rotate(-2deg)}}@keyframes diceRoll{0%,100%{transform:rotate(0) translateY(0)}28%{transform:rotate(115deg) translateY(-13px)}56%{transform:rotate(245deg) translateY(2px)}80%{transform:rotate(330deg) translateY(-4px)}}.stage-copy{font-size:13px;color:var(--muted);line-height:1.45;max-width:380px;margin:10px auto 0}
    .bet-box{padding:14px;border-radius:18px;background:var(--surface);border:1px solid var(--line);margin:4px 0 13px}.bet-label{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:9px}.bet-input{display:flex;align-items:center;gap:8px;background:var(--surface-2);border-radius:14px;padding:4px 10px}.bet-input input{width:100%;border:0;outline:0;background:transparent;color:var(--text);font-size:20px;font-weight:780;padding:9px 2px}.chips{display:flex;gap:7px;margin-top:9px;overflow:auto}.chip,.choice{border:1px solid var(--line);background:var(--surface-2);border-radius:12px;padding:9px 12px;font-size:12px;white-space:nowrap;cursor:pointer}.chip:active,.choice:active{transform:scale(.97)}.choice.selected{border-color:var(--purple);background:rgba(136,108,255,.16);color:#d8d1ff}
    .choice-row{display:flex;gap:8px;flex-wrap:wrap;margin:11px 0}.choice{flex:1;min-width:88px;font-size:14px;padding:12px}
    .primary,.secondary,.danger{width:100%;border:0;border-radius:16px;padding:15px 18px;font-weight:780;cursor:pointer}.primary{background:linear-gradient(135deg,var(--purple-2),#6b52f1);box-shadow:0 10px 26px rgba(113,84,244,.22)}.secondary{background:var(--surface-2);border:1px solid var(--line);margin-top:8px}.danger{background:rgba(255,101,119,.12);color:#ff8795;border:1px solid rgba(255,101,119,.2);margin-top:8px}
    .loader{width:18px;height:18px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;display:inline-block;animation:spin .7s linear infinite;vertical-align:-4px}@keyframes spin{to{transform:rotate(360deg)}}
    .result-card{text-align:center;padding:8px 0 2px}.result-icon{font-size:74px;margin:5px 0}.result-title{font-size:25px;font-weight:850;letter-spacing:-.04em}.result-title.win{color:var(--green)}.result-title.loss{color:var(--red)}.result-pay{margin:9px 0 18px;color:var(--muted)}.result-pay b{color:var(--text)}
    .detail{display:flex;justify-content:space-between;padding:12px 2px;border-bottom:1px solid var(--line);font-size:13px}.detail span{color:var(--muted)}
    .grid{display:grid;gap:7px;margin:13px auto 18px}.grid button{aspect-ratio:1;border:1px solid var(--line);border-radius:12px;background:var(--surface-2);font-size:20px;cursor:pointer}.grid button:active{transform:scale(.95)}.grid-3{grid-template-columns:repeat(3,1fr);max-width:310px}.grid-5{grid-template-columns:repeat(5,1fr);max-width:410px}.grid-6{grid-template-columns:repeat(6,1fr);max-width:470px}.grid-7{grid-template-columns:repeat(7,1fr);max-width:520px;gap:5px}.grid-7 button{border-radius:9px;font-size:15px}.grid button.safe{background:rgba(83,227,166,.12);border-color:rgba(83,227,166,.25)}.grid button.mine{background:rgba(255,101,119,.12);border-color:rgba(255,101,119,.3)}.grid button.active-cell{border-color:rgba(136,108,255,.7);background:rgba(136,108,255,.14)}
    .cards{display:flex;gap:7px;justify-content:center;flex-wrap:wrap;margin:11px 0 17px}.playing-card{min-width:48px;height:68px;padding:7px;border-radius:10px;background:#f7f5ef;color:#11131a;display:grid;place-items:center;font-size:16px;font-weight:800;box-shadow:0 8px 16px rgba(0,0,0,.22)}.playing-card.red{color:#d43d50}.playing-card.back{background:linear-gradient(145deg,#7b63e9,#4c36bd);color:white}.hand-label{font-size:12px;color:var(--muted);margin-top:12px}.actions{display:flex;gap:8px}.actions button{flex:1}
    .code-box{font-size:30px;letter-spacing:.18em;font-weight:850;text-align:center;padding:17px;border-radius:18px;background:var(--surface-2);border:1px dashed rgba(136,108,255,.45);margin:12px 0;cursor:pointer}.versus{display:flex;justify-content:center;align-items:center;gap:25px;margin:20px 0}.die{width:76px;height:76px;border-radius:22px;background:var(--surface-2);display:grid;place-items:center;font-size:34px;border:1px solid var(--line)}
    .crash-board{padding:16px;border-radius:21px;background:radial-gradient(90% 130% at 50% 110%,rgba(136,108,255,.26),transparent 65%),var(--surface);border:1px solid var(--line);margin-bottom:12px}.crash-track{height:154px;position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(165deg,rgba(136,108,255,.08),rgba(11,13,18,.85));border:1px solid rgba(255,255,255,.05)}.crash-track:after{content:"";position:absolute;left:8%;right:8%;bottom:26px;height:1px;background:linear-gradient(90deg,transparent,var(--purple),transparent);opacity:.6}.rocket{position:absolute;left:9%;bottom:30px;font-size:42px;transform-origin:center;filter:drop-shadow(0 0 18px rgba(255,197,92,.75));z-index:2}.rocket.flying{animation:rocketFly 8s linear infinite}.rocket.crashed{animation:rocketCrash .7s ease-out forwards}.rocket-flame{position:absolute;left:3px;bottom:-15px;font-size:19px;transform:rotate(-38deg)}@keyframes rocketFly{0%{left:8%;bottom:28px;transform:rotate(-5deg)}48%{left:47%;bottom:67px;transform:rotate(-14deg)}100%{left:88%;bottom:126px;transform:rotate(-25deg)}}@keyframes rocketCrash{to{left:74%;bottom:12px;transform:rotate(72deg);filter:grayscale(1)}}.crash-number{font-size:42px;font-weight:900;letter-spacing:-.06em;text-align:center;line-height:1;margin:15px 0 5px}.crash-number.running{color:var(--green)}.crash-number.crashed{color:var(--red)}.crash-countdown{text-align:center;color:var(--muted);font-size:12px;margin-bottom:11px}.bets-board{max-height:170px;overflow:auto;border:1px solid var(--line);border-radius:16px;background:var(--surface);margin:12px 0}.bets-head,.bet-line{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;padding:10px 12px;font-size:12px}.bets-head{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--line)}.bet-line+.bet-line{border-top:1px solid var(--line)}.bet-line .stake{font-weight:750}.bet-line .state{font-size:10px;color:var(--muted)}.bet-line .state.cashed{color:var(--green)}.recent-crashes{display:flex;gap:7px;overflow:auto;margin:9px 0 2px}.recent-crashes span{padding:7px 9px;border-radius:10px;background:rgba(255,101,119,.1);border:1px solid rgba(255,101,119,.18);color:#ff9ba6;font-weight:750;font-size:11px;white-space:nowrap}
    .empty{text-align:center;padding:50px 15px;color:var(--muted)}.empty i{display:block;font-style:normal;font-size:50px;margin-bottom:10px}.skeleton{height:74px;border-radius:17px;background:linear-gradient(90deg,var(--surface) 25%,var(--surface-2) 45%,var(--surface) 65%);background-size:300% 100%;animation:shimmer 1.3s infinite}@keyframes shimmer{to{background-position:-150% 0}}
    .toast{position:fixed;z-index:120;left:50%;bottom:calc(87px + var(--safe-bottom));transform:translate(-50%,20px);width:max-content;max-width:calc(100% - 32px);background:#262b36;border:1px solid var(--line);border-radius:14px;padding:11px 15px;font-size:13px;opacity:0;pointer-events:none;transition:.2s;box-shadow:var(--shadow)}.toast.show{opacity:1;transform:translate(-50%,0)}.toast.error{color:#ffadb6}
    .fatal{position:fixed;z-index:200;inset:0;background:var(--bg);padding:35px;display:grid;place-items:center;text-align:center}.fatal-icon{font-size:58px}.fatal h1{font-size:23px}.fatal p{color:var(--muted);line-height:1.5;max-width:380px}
    @media(min-width:560px){.games{grid-template-columns:repeat(3,minmax(0,1fr))}.game-card{min-height:150px}.sheet{bottom:16px;border-radius:28px;border-bottom:1px solid var(--line);max-height:88vh}.sheet-wrap.open .sheet{transform:translate(-50%,-16px)}}
  </style>
</head>
<body>
  <main class="app">
    <header class="topbar">
      <div class="brand"><div class="brand-mark">✦</div><span>Royal Spin</span></div>
      <div class="user-pill"><div class="avatar" id="topAvatar">?</div><div class="user-name" id="topName">Загрузка…</div></div>
    </header>

    <section id="gamesPanel" class="panel">
      <div class="hero">
        <button class="refresh" id="refreshBalance" aria-label="Обновить баланс">↻</button>
        <div class="eyebrow">Ваш баланс</div>
        <div class="balance-row"><div class="balance" id="balance">—</div><div class="star">⭐</div></div>
        <div class="hero-meta"><span id="gamesCount">0</span> сыгранных партий</div>
      </div>
      <div class="section-head"><h2>Выберите игру</h2><span id="gameTotal"></span></div>
      <div class="games" id="games"></div>
    </section>

    <section id="historyPanel" class="panel hidden">
      <div class="section-head"><h2>История игр</h2><span>последние 30</span></div>
      <div id="historyList" class="history-list"><div class="skeleton"></div><div class="skeleton"></div></div>
    </section>

    <section id="profilePanel" class="panel hidden">
      <div class="section-head"><h2>Профиль</h2><span>Telegram</span></div>
      <div class="profile-card">
        <div class="profile-avatar" id="profileAvatar">?</div>
        <div class="profile-name" id="profileName">—</div>
        <div class="profile-user" id="profileUsername">—</div>
        <div class="stats"><div class="stat"><b id="profileBalance">—</b><span>баланс ⭐</span></div><div class="stat"><b id="profileGames">0</b><span>партий</span></div></div>
      </div>
    </section>
  </main>

  <nav class="nav">
    <button class="active" data-panel="games"><span class="ico">🎮</span><span>Игры</span></button>
    <button data-panel="history"><span class="ico">◴</span><span>История</span></button>
    <button data-panel="profile"><span class="ico">☺</span><span>Профиль</span></button>
  </nav>

  <div class="sheet-wrap" id="sheetWrap"><div class="sheet" id="sheet"><div class="handle"></div><div class="sheet-head"><div class="sheet-title" id="sheetTitle">Игра</div><button class="close" id="closeSheet">×</button></div><div id="sheetBody"></div></div></div>
  <div class="toast" id="toast"></div>
  <div class="fatal hidden" id="fatal"><div><div class="fatal-icon">📲</div><h1 id="fatalTitle">Откройте в Telegram</h1><p id="fatalText">Это приложение использует защищённую авторизацию Telegram Mini Apps.</p></div></div>

  <script>
  (()=>{
    'use strict';
    const tg=window.Telegram?.WebApp;
    if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#090b10');tg.setBackgroundColor('#090b10')}catch(_){}}
    const initData=tg?.initData||'';
    const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
    const state={user:null,game:null,session:null,choice:null,size:5,pvpTimer:null,crashTimer:null,busy:false};
    const icons={dice:'🎲',basketball:'🏀',football:'⚽',roulette:'🎰',darts:'🎯',tictactoe:'❎',minesweeper:'💣',rps:'✊',coinflip:'🪙',blackjack:'🃏',ladder:'📊',lootbox_2x2:'🎁',lootbox_3x3:'🎁',lootbox_6x5:'📦',pvp:'⚔️',crash:'🚀'};
    const labels={dice:'Кубик',basketball:'Баскетбол',football:'Футбол',roulette:'Рулетка',darts:'Дартс',tictactoe:'Крестики-нолики',minesweeper:'Сапёр',rps:'КНБ',coinflip:'Орёл и решка',blackjack:'Блэкджек',ladder:'Лесенка',lootbox_2x2:'Лутбокс 2×2',lootbox_3x3:'Лутбокс 3×3',lootbox_6x5:'Лутбокс 6×5',pvp:'PvP Дуэль',crash:'Crash'};
    const games=[
      {id:'dice',emoji:'🎲',name:'Кубик',desc:'Число решает множитель',tag:'до x3',kind:'instant',glow:'rgba(136,108,255,.22)'},
      {id:'roulette',emoji:'🎰',name:'Рулетка',desc:'Фрукты и джекпот 777',tag:'до x10',kind:'instant',glow:'rgba(255,197,92,.2)'},
      {id:'crash',emoji:'🚀',name:'Crash',desc:'Ставки онлайн · 10 секунд',tag:'онлайн',kind:'crash',glow:'rgba(255,101,119,.2)'},
      {id:'blackjack',emoji:'🃏',name:'Блэкджек',desc:'Наберите ближе к 21',tag:'x2.5',kind:'session',glow:'rgba(255,101,119,.18)'},
      {id:'minesweeper',emoji:'💣',name:'Сапёр',desc:'Открывайте безопасные клетки',tag:'x1.5',kind:'session',glow:'rgba(255,101,119,.16)'},
      {id:'ladder',emoji:'📊',name:'Лесенка',desc:'Риск растёт с каждым шагом',tag:'до x7⁷',kind:'session',glow:'rgba(83,227,166,.16)'},
      {id:'tictactoe',emoji:'❎',name:'Крестики-нолики',desc:'Сыграйте против бота',tag:'x2',kind:'session',glow:'rgba(136,108,255,.18)'},
      {id:'basketball',emoji:'🏀',name:'Баскетбол',desc:'Попадание приносит выигрыш',tag:'x1.85',kind:'instant',glow:'rgba(255,151,69,.18)'},
      {id:'football',emoji:'⚽',name:'Футбол',desc:'Забейте гол',tag:'x1.7',kind:'instant',glow:'rgba(83,227,166,.16)'},
      {id:'darts',emoji:'🎯',name:'Дартс',desc:'Цельтесь в яблочко',tag:'до x5',kind:'instant',glow:'rgba(255,101,119,.18)'},
      {id:'coinflip',emoji:'🪙',name:'Орёл и решка',desc:'Угадайте сторону монеты',tag:'x1.95',kind:'instant',glow:'rgba(255,197,92,.18)'},
      {id:'rps',emoji:'✊',name:'КНБ',desc:'Камень, ножницы, бумага',tag:'x2',kind:'instant',glow:'rgba(136,108,255,.16)'},
      {id:'lootbox_2x2',emoji:'🎁',name:'Лутбокс 2×2',desc:'Один приз в четырёх боксах',tag:'x2',kind:'instant',glow:'rgba(83,227,166,.16)'},
      {id:'lootbox_3x3',emoji:'🎁',name:'Лутбокс 3×3',desc:'Найдите приз среди девяти',tag:'x3',kind:'instant',glow:'rgba(77,152,255,.17)'},
      {id:'lootbox_6x5',emoji:'📦',name:'Лутбокс 6×5',desc:'Три попытки найти три приза',tag:'3 ⭐',kind:'session',glow:'rgba(136,108,255,.2)'},
      {id:'pvp',emoji:'⚔️',name:'PvP Дуэль',desc:'Кубик против другого игрока',tag:'x2',kind:'pvp',glow:'rgba(255,101,119,.18)'}
    ];

    const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const reqId=()=>crypto.randomUUID().replaceAll('-','');
    const buzz=type=>{try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){}};
    function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className='toast show'+(error?' error':'');clearTimeout(el._t);el._t=setTimeout(()=>el.className='toast',2600)}
    async function api(path,options={}){
      const headers={'X-Telegram-Init-Data':initData,...(options.body?{'Content-Type':'application/json'}:{})};
      const res=await fetch(path,{...options,headers:{...headers,...(options.headers||{})}});
      let data={};try{data=await res.json()}catch(_){data={error:'Сервер вернул неверный ответ'}}
      if(!res.ok||data.ok===false){const e=new Error(data.error||'Ошибка запроса');e.code=data.code;e.status=res.status;throw e}return data;
    }
    function money(v){return Number(v||0).toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2})}
    function updateUser(values={}){if(!state.user)return;Object.assign(state.user,values);$('#balance').textContent=money(state.user.balance);$('#gamesCount').textContent=state.user.games_played||0;$('#profileBalance').textContent=money(state.user.balance);$('#profileGames').textContent=state.user.games_played||0}
    function applyProfile(user){state.user=user;const initials=(user.name||'?').split(/\s+/).map(x=>x[0]).join('').slice(0,2).toUpperCase();
      $('#topName').textContent=user.name;$('#profileName').textContent=user.name;$('#profileUsername').textContent=user.username?'@'+user.username:'Telegram ID '+user.id;
      ['#topAvatar','#profileAvatar'].forEach(sel=>{const el=$(sel);if(user.photo_url){el.innerHTML=`<img src="${esc(user.photo_url)}" style="width:100%;height:100%;border-radius:inherit;object-fit:cover" alt="">`}else el.textContent=initials});updateUser();}
    function renderGames(){const root=$('#games');root.innerHTML=games.map(g=>`<button class="game-card" data-game="${g.id}" style="--glow:${g.glow}"><div class="game-emoji">${g.emoji}</div><div class="game-name">${g.name}</div><div class="game-desc">${g.desc}</div><div class="game-tag">${g.tag}</div></button>`).join('');$('#gameTotal').textContent=games.length+' режимов';root.addEventListener('click',e=>{const card=e.target.closest('[data-game]');if(card)openGame(games.find(x=>x.id===card.dataset.game))})}
    async function loadMe(){try{const data=await api('/api/me');applyProfile(data.user)}catch(e){$('#fatal').classList.remove('hidden');if(e.code==='server_not_configured'){$('#fatalTitle').textContent='Сервер не настроен';$('#fatalText').textContent=e.message}else{$('#fatalTitle').textContent='Откройте в Telegram';$('#fatalText').textContent=e.message}}}
    async function refreshMe(){try{const data=await api('/api/me');applyProfile(data.user);toast('Баланс обновлён')}catch(e){toast(e.message,true)}}
    function openSheet(){tg?.BackButton?.show();$('#sheetWrap').classList.add('open');document.body.style.overflow='hidden'}
    function closeSheet(){clearInterval(state.pvpTimer);clearInterval(state.crashTimer);state.pvpTimer=null;state.crashTimer=null;tg?.BackButton?.hide();$('#sheetWrap').classList.remove('open');document.body.style.overflow='';setTimeout(()=>{$('#sheetBody').innerHTML='';state.session=null},250)}
    tg?.BackButton?.onClick(closeSheet);$('#closeSheet').onclick=closeSheet;$('#sheetWrap').addEventListener('click',e=>{if(e.target===$('#sheetWrap'))closeSheet()});
    function betBox(fixed=false){return fixed?`<div class="bet-box"><div class="bet-label"><span>Фиксированная ставка</span><b>3 ⭐</b></div></div>`:`<div class="bet-box"><div class="bet-label"><span>Ставка</span><span>Баланс: ${money(state.user?.balance)} ⭐</span></div><div class="bet-input"><input id="betInput" inputmode="decimal" value="1" aria-label="Ставка"><b>⭐</b></div><div class="chips">${[1,5,10,25,50,100].map(x=>`<button class="chip" data-bet="${x}">${x}</button>`).join('')}</div></div>`}
    function choicesFor(game){
      if(game.id==='rps')return `<div class="choice-row"><button class="choice" data-choice="rock">🪨 Камень</button><button class="choice" data-choice="scissors">✂ Ножницы</button><button class="choice" data-choice="paper">📄 Бумага</button></div>`;
      if(game.id==='coinflip')return `<div class="choice-row"><button class="choice" data-choice="heads">🦅 Орёл</button><button class="choice" data-choice="tails">🪙 Решка</button></div>`;
      if(game.id==='minesweeper')return `<div class="choice-row"><button class="choice selected" data-size="5">5×5 · 6 мин</button><button class="choice" data-size="7">7×7 · 12 мин</button></div>`;
      if(game.id==='lootbox_2x2'||game.id==='lootbox_3x3'){const n=game.id.endsWith('2x2')?4:9;return `<div class="grid grid-3" style="${n===4?'max-width:210px;grid-template-columns:repeat(2,1fr)':''}">${Array.from({length:n},(_,i)=>`<button class="choice" data-choice="${i}">📦</button>`).join('')}</div>`}return '';
    }
    function wireIntro(game){$$('.chip').forEach(b=>b.onclick=()=>{$('#betInput').value=b.dataset.bet});$$('[data-choice]').forEach(b=>b.onclick=()=>{$$('[data-choice]').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');state.choice=b.dataset.choice});$$('[data-size]').forEach(b=>b.onclick=()=>{$$('[data-size]').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');state.size=Number(b.dataset.size)});$('#startGame').onclick=()=>startGame(game)}
    function gameAnimation(game){const cls=game.id==='football'?'anim-football':game.id==='basketball'?'anim-basketball':game.id==='dice'?'anim-dice':'';return `<div class="stage-emoji ${cls}">${game.emoji}</div>`}
    function openGame(game){state.game=game;state.choice=null;state.size=5;$('#sheetTitle').textContent=game.name;openSheet();if(game.kind==='pvp')return renderPvpHome();if(game.kind==='crash')return renderCrashHome();
      const fixed=game.id==='lootbox_6x5';$('#sheetBody').innerHTML=`<div class="game-stage">${gameAnimation(game)}<div class="stage-copy">${game.desc}. Все результаты рассчитываются на сервере, баланс обновляется автоматически.</div></div>${betBox(fixed)}${choicesFor(game)}<button class="primary" id="startGame">${fixed?'Открыть за 3 ⭐':'Играть'}</button>`;wireIntro(game)}
    function getBet(){const value=$('#betInput')?.value?.replace(',','.');if(!value||Number(value)<1)throw new Error('Минимальная ставка — 1 ⭐');return value}
    async function startGame(game){if(state.busy)return;let bet='3';try{if(game.id!=='lootbox_6x5')bet=getBet();if(['rps','coinflip','lootbox_2x2','lootbox_3x3'].includes(game.id)&&state.choice===null)throw new Error('Сначала сделайте выбор');state.busy=true;$('#startGame').innerHTML='<span class="loader"></span> Играем…';$('#startGame').disabled=true;
        if(game.kind==='instant'){const data=await api('/api/play',{method:'POST',body:JSON.stringify({game:game.id,bet,choice:state.choice,request_id:reqId()})});renderInstant(data.result)}
        else{const data=await api('/api/session/start',{method:'POST',body:JSON.stringify({game:game.id,bet,options:{size:state.size},request_id:reqId()})});state.session=data.session;updateUser({games_played:(state.user.games_played||0)+1});renderSession(data.session)}
      }catch(e){toast(e.message,true);buzz('error');const b=$('#startGame');if(b){b.disabled=false;b.textContent='Играть'}}finally{state.busy=false}}
    function resultExtra(r){if(r.game==='rps'){const map={rock:'🪨',scissors:'✂',paper:'📄'};return `<div class="detail"><span>Вы</span><b>${map[r.choice]}</b></div><div class="detail"><span>Бот</span><b>${map[r.bot]}</b></div>`}if(r.game==='coinflip'){const map={heads:'🦅 Орёл',tails:'🪙 Решка'};return `<div class="detail"><span>Вы выбрали</span><b>${map[r.choice]}</b></div><div class="detail"><span>Выпало</span><b>${map[r.landed]}</b></div>`}if(r.game.startsWith('lootbox_')){const n=r.game.endsWith('2x2')?4:9;return `<div class="grid grid-3" style="${n===4?'max-width:210px':''}">${Array.from({length:n},(_,i)=>`<button disabled class="${i===r.winning?'safe':''} ${i===r.choice&&i!==r.winning?'mine':''}">${i===r.winning?'🎁':i===r.choice?'❌':'📦'}</button>`).join('')}</div>`}if(r.symbol)return `<div class="result-icon">${r.symbol}</div>`;if(r.value)return `<div class="detail"><span>Значение</span><b>${r.value}</b></div>`;return ''}
    function renderInstant(r){updateUser({balance:r.balance,games_played:(state.user.games_played||0)+1});const win=Number(r.payout)>0;buzz(win?'success':'error');$('#sheetBody').innerHTML=`<div class="result-card"><div class="result-icon">${r.emoji||icons[r.game]}</div><div class="result-title ${win?'win':'loss'}">${esc(r.title)}</div><div class="result-pay">${win?'Начислено':'Ставка проиграна'}: <b>${money(r.payout)} ⭐</b>${r.bonus?`<br>Бонус серии: <b>${money(r.bonus)} ⭐</b>`:''}</div></div>${resultExtra(r)}<div class="detail"><span>Ставка</span><b>${money(r.bet)} ⭐</b></div><div class="detail"><span>Новый баланс</span><b>${money(r.balance)} ⭐</b></div><button class="primary" id="again">Сыграть ещё</button><button class="secondary" id="done">Готово</button>`;$('#again').onclick=()=>openGame(state.game);$('#done').onclick=closeSheet}
    function crashBets(c){if(!c.bets?.length)return '<div class="empty" style="padding:18px">Пока никто не поставил</div>';return `<div class="bets-head"><span>Игрок</span><span>Ставка</span><span>Статус</span></div>${c.bets.map(b=>`<div class="bet-line"><span>${esc(b.name)}${b.is_you?' · вы':''}</span><span class="stake">${money(b.amount)} ⭐</span><span class="state ${b.status}">${b.status==='active'?'летит':b.status==='cashed'?'забрал':'краш'}</span></div>`).join('')}`}
    function renderCrashHome(){state.crashRound=null;$('#sheetBody').innerHTML='<div id="crashMount"><div class="empty"><span class="loader"></span> Загружаем раунд…</div></div>';updateCrash();clearInterval(state.crashTimer);state.crashTimer=setInterval(updateCrash,650)}
    async function updateCrash(){if(state.game?.id!=='crash')return;try{const data=await api('/api/crash/state');state.crashRound=data.crash;updateUser({balance:data.crash.balance||state.user.balance});renderCrash(data.crash)}catch(e){if(e.status!==401)toast(e.message,true)}}
    function renderCrash(c){const oldBet=$('#betInput')?.value||'1';const rocketClass=c.status==='running'?'flying':c.status==='crashed'?'crashed':'';const recent=c.recent?.length?`<div class="recent-crashes">${c.recent.map(x=>`<span>${money(x.multiplier)}x</span>`).join('')}</div>`:'';let body=`<div class="crash-board"><div class="crash-track"><div class="rocket ${rocketClass}">🚀<span class="rocket-flame">🔥</span></div></div><div class="crash-number ${c.status}">${c.status==='betting'?'Ожидание':money(c.multiplier)+'x'}</div><div class="crash-countdown">${c.status==='betting'?'Ставки закрываются через '+c.seconds_left+' сек':c.status==='running'?'Заберите выигрыш до краша':'Краш: '+money(c.crash_multiplier)+'x'}</div></div>${recent}<div class="bets-board">${crashBets(c)}</div>`;
      if(c.status==='betting'){body+=betBox(false)+'<button class="primary" id="crashBet">Поставить на этот раунд</button>'}else if(c.status==='running'&&c.own_bet?.status==='active'){body+=`<button class="primary" id="crashCashout">Забрать на ${money(c.multiplier)}x</button>`}else if(c.status==='running'){body+='<div class="stage-copy">Раунд уже идёт. Следующая ставка откроется после краша.</div>'}else{body+='<button class="secondary" id="crashRefresh">Следующий раунд</button>'}
      $('#sheetBody').innerHTML=body;if($('#betInput'))$('#betInput').value=oldBet;if($('#crashBet'))$('#crashBet').onclick=crashBet;if($('#crashCashout'))$('#crashCashout').onclick=crashCashout;if($('#crashRefresh'))$('#crashRefresh').onclick=updateCrash}
    async function crashBet(){if(state.busy)return;try{const bet=getBet();state.busy=true;const data=await api('/api/crash/bet',{method:'POST',body:JSON.stringify({bet,request_id:reqId()})});state.crashRound=data.crash;updateUser({balance:data.crash.balance||state.user.balance});renderCrash(data.crash);toast('Ставка принята');buzz('success')}catch(e){toast(e.message,true);buzz('error')}finally{state.busy=false}}
    async function crashCashout(){if(state.busy)return;try{state.busy=true;const data=await api('/api/crash/cashout',{method:'POST',body:'{}'});state.crashRound=data.crash;updateUser({balance:data.crash.balance||state.user.balance,games_played:(state.user.games_played||0)+1});renderCrash(data.crash);toast('Выигрыш зачислен');buzz('success')}catch(e){toast(e.message,true);buzz('error')}finally{state.busy=false}}
    async function sessionAction(action,extra={}){if(state.busy)return;try{state.busy=true;const data=await api(`/api/session/${state.session.id}/action`,{method:'POST',body:JSON.stringify({action,...extra})});state.session=data.session;renderSession(data.session)}catch(e){toast(e.message,true);buzz('error')}finally{state.busy=false}}
    function finishButtons(){return `<button class="primary" id="again">Сыграть ещё</button><button class="secondary" id="done">Готово</button>`}
    function wireFinish(){const a=$('#again'),d=$('#done');if(a)a.onclick=()=>openGame(state.game);if(d)d.onclick=closeSheet}
    function renderSession(s){updateUser({balance:s.balance});state.session=s;
      if(s.game==='tictactoe')renderTtt(s);else if(s.game==='minesweeper')renderMines(s);else if(s.game==='blackjack')renderBlackjack(s);else if(s.game==='ladder')renderLadder(s);else if(s.game==='lootbox_6x5')renderLootbox6(s)}
    function statusBlock(s){if(!s.finished)return '';const win=Number(s.payout)>0;buzz(win?'success':'error');return `<div class="result-card"><div class="result-title ${win?'win':'loss'}">${esc(s.result)}</div><div class="result-pay">Начислено: <b>${money(s.payout)} ⭐</b></div></div>`}
    function renderTtt(s){$('#sheetBody').innerHTML=`${statusBlock(s)}<div class="grid grid-3">${s.board.map((v,i)=>`<button data-i="${i}" ${v||s.finished?'disabled':''}>${v==='X'?'❌':v==='O'?'⭕':'·'}</button>`).join('')}</div>${s.finished?finishButtons():'<div class="stage-copy">Вы играете крестиками. Ваш ход.</div>'}`;if(!s.finished)$$('[data-i]').forEach(b=>b.onclick=()=>sessionAction('move',{index:Number(b.dataset.i)}));wireFinish()}
    function renderMines(s){const revealed=new Set(s.revealed),bombs=new Set(s.bombs||[]);$('#sheetBody').innerHTML=`${statusBlock(s)}<div class="stage-copy">Открыто ${s.revealed.filter(x=>!bombs.has(x)).length} из ${s.target} безопасных клеток</div><div class="grid grid-${s.size}">${Array.from({length:s.size*s.size},(_,i)=>{const open=revealed.has(i)||s.finished&&bombs.has(i);return `<button data-i="${i}" ${open||s.finished?'disabled':''} class="${open?(bombs.has(i)?'mine':'safe'):''}">${open?(bombs.has(i)?'💣':'✓'):'·'}</button>`}).join('')}</div>${s.finished?finishButtons():''}`;if(!s.finished)$$('[data-i]').forEach(b=>b.onclick=()=>sessionAction('pick',{index:Number(b.dataset.i)}));wireFinish()}
    function cardHtml(card){if(card==='🂠')return '<div class="playing-card back">✦</div>';const red=card.endsWith('♥')||card.endsWith('♦');return `<div class="playing-card ${red?'red':''}">${esc(card)}</div>`}
    function renderBlackjack(s){$('#sheetBody').innerHTML=`${statusBlock(s)}<div class="hand-label">Дилер ${s.dealer_value!==null?'· '+s.dealer_value:''}</div><div class="cards">${s.dealer.map(cardHtml).join('')}</div><div class="hand-label">Ваша рука · ${s.player_value}</div><div class="cards">${s.player.map(cardHtml).join('')}</div>${s.finished?finishButtons():`<div class="actions"><button class="primary" data-act="hit">Взять</button><button class="secondary" style="margin:0" data-act="stand">Хватит</button></div>${s.can_double?'<button class="danger" data-act="double">Дабл · ещё '+money(s.bet)+' ⭐</button>':''}`}`;if(!s.finished)$$('[data-act]').forEach(b=>b.onclick=()=>sessionAction(b.dataset.act));wireFinish()}
    function renderLadder(s){const rev=new Map(s.revealed.map(x=>[x[0]+':'+x[1],true])),mine=s.mine?s.mine[0]+':'+s.mine[1]:'';let cells='';for(let r=7;r>=0;r--)for(let c=0;c<7;c++){const key=r+':'+c,open=rev.has(key),active=c===s.step&&!s.finished;cells+=`<button data-row="${r}" ${active?'':'disabled'} class="${key===mine?'mine':open?'safe':active?'active-cell':''}">${key===mine?'💣':open?'✓':active?'·':'·'}</button>`}const potential=(Number(s.bet)*Number(s.multiplier)).toFixed(2);$('#sheetBody').innerHTML=`${statusBlock(s)}<div class="detail"><span>Пройдено</span><b>${s.step}/7</b></div><div class="detail"><span>Множитель</span><b>x${money(s.multiplier)}</b></div><div class="grid grid-7">${cells}</div>${s.finished?finishButtons():`${s.step?`<button class="primary" id="cashout">Забрать ${money(potential)} ⭐</button>`:'<div class="stage-copy">Выберите безопасную клетку в первом столбце</div>'}`}`;if(!s.finished)$$('[data-row]:not([disabled])').forEach(b=>b.onclick=()=>sessionAction('pick',{row:Number(b.dataset.row)}));if($('#cashout'))$('#cashout').onclick=()=>sessionAction('cashout');wireFinish()}
    function renderLootbox6(s){const opened=new Set(s.opened),winning=new Set(s.winning||[]);$('#sheetBody').innerHTML=`${statusBlock(s)}<div class="detail"><span>Попыток осталось</span><b>${s.attempts}</b></div><div class="detail"><span>Призов найдено</span><b>${s.hits}</b></div><div class="grid grid-6">${Array.from({length:30},(_,i)=>`<button data-i="${i}" ${opened.has(i)||s.finished?'disabled':''} class="${opened.has(i)?(winning.has(i)?'safe':'mine'):''}">${opened.has(i)?(winning.has(i)?'🎁':'·'):'📦'}</button>`).join('')}</div>${s.finished?finishButtons():''}`;if(!s.finished)$$('[data-i]').forEach(b=>b.onclick=()=>sessionAction('pick',{index:Number(b.dataset.i)}));wireFinish()}
    function renderPvpHome(){state.pvpRoom=null;$('#sheetBody').innerHTML=`<div class="game-stage"><div class="stage-emoji">⚔️</div><div class="stage-copy">Создайте комнату и отправьте код сопернику — или войдите по его коду.</div></div>${betBox(false)}<button class="primary" id="createRoom">Создать комнату</button><div class="detail" style="margin:9px 0"><span></span><b>или</b><span></span></div><div class="bet-input"><input id="roomCode" maxlength="6" placeholder="КОД КОМНАТЫ" style="text-transform:uppercase;text-align:center;letter-spacing:.15em"></div><button class="secondary" id="joinRoom">Войти по коду</button>`;$$('.chip').forEach(b=>b.onclick=()=>{$('#betInput').value=b.dataset.bet});$('#createRoom').onclick=createPvp;$('#joinRoom').onclick=joinPvp}
    async function createPvp(){try{const bet=getBet();const data=await api('/api/pvp/create',{method:'POST',body:JSON.stringify({bet})});updateUser({games_played:(state.user.games_played||0)+1});renderPvpRoom(data.room);pollPvp(data.room.id)}catch(e){toast(e.message,true)}}
    async function joinPvp(){try{const code=$('#roomCode').value.trim().toUpperCase();const data=await api('/api/pvp/join',{method:'POST',body:JSON.stringify({code})});updateUser({games_played:(state.user.games_played||0)+1});renderPvpRoom(data.room)}catch(e){toast(e.message,true)}}
    function pollPvp(id){clearInterval(state.pvpTimer);state.pvpTimer=setInterval(async()=>{try{const d=await api('/api/pvp/'+id);if(d.room.status!=='waiting'){clearInterval(state.pvpTimer);renderPvpRoom(d.room)}}catch(_){}},2000)}
    function renderPvpRoom(r){state.pvpRoom=r;updateUser({balance:r.balance});if(r.status==='waiting'){$('#sheetBody').innerHTML=`<div class="game-stage"><div class="stage-emoji">⏳</div><div class="stage-copy">Ожидаем второго игрока. Нажмите на код, чтобы скопировать.</div></div><div class="code-box" id="copyCode">${r.code}</div><div class="detail"><span>Ставка каждого</span><b>${money(r.bet)} ⭐</b></div><button class="danger" id="cancelRoom">Отменить и вернуть ставку</button>`;$('#copyCode').onclick=async()=>{await navigator.clipboard?.writeText(r.code);toast('Код скопирован')};$('#cancelRoom').onclick=async()=>{try{const d=await api(`/api/pvp/${r.id}/cancel`,{method:'POST',body:'{}'});updateUser({balance:d.room.balance});closeSheet();toast('Ставка возвращена')}catch(e){toast(e.message,true)}}}else{clearInterval(state.pvpTimer);const won=r.winner_id===state.user.id,draw=!r.winner_id;$('#sheetBody').innerHTML=`<div class="result-card"><div class="result-title ${draw?'':won?'win':'loss'}">${draw?'Ничья':won?'Победа!':'Поражение'}</div><div class="result-pay">Ставка: <b>${money(r.bet)} ⭐</b></div></div><div class="versus"><div class="die">🎲 ${r.p1_roll}</div><b>VS</b><div class="die">🎲 ${r.p2_roll}</div></div><button class="primary" id="again">Новая дуэль</button><button class="secondary" id="done">Готово</button>`;$('#again').onclick=renderPvpHome;$('#done').onclick=closeSheet}}
    async function loadHistory(){const root=$('#historyList');root.innerHTML='<div class="skeleton"></div><div class="skeleton"></div>';try{const d=await api('/api/history?limit=30');if(!d.items.length){root.innerHTML='<div class="empty"><i>◴</i>Здесь появятся результаты ваших игр</div>';return}root.innerHTML=d.items.map(x=>{const net=Number(x.payout)-Number(x.bet),win=net>=0;const date=new Date(x.created_at);return `<div class="history-item"><div class="history-icon">${icons[x.game]||'🎮'}</div><div class="history-main"><div class="history-title">${labels[x.game]||esc(x.game)}</div><div class="history-time">${date.toLocaleString('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})}</div></div><div><div class="history-money ${win?'win':''}">${net>=0?'+':''}${money(net)} ⭐</div><div class="history-bet">ставка ${money(x.bet)}</div></div></div>`}).join('')}catch(e){root.innerHTML=`<div class="empty"><i>!</i>${esc(e.message)}</div>`}}
    function switchPanel(name){$$('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.panel===name));['games','history','profile'].forEach(x=>$(`#${x}Panel`).classList.toggle('hidden',x!==name));if(name==='history')loadHistory()}
    $$('.nav button').forEach(b=>b.onclick=()=>switchPanel(b.dataset.panel));$('#refreshBalance').onclick=refreshMe;
    renderGames();loadMe();
  })();
  </script>
</body>
</html>'''


@app.get("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
