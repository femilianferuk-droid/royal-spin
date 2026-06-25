"""
Royal Spin — Telegram Mini App
Backend: Flask + serverless-wsgi
DB: PostgreSQL
Все тексты и игровая логика на русском языке.
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8923229410:AAFvjPnloV6L4_kfnWE39gWW3CEXiLB8zjo")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bothost_db_e7be6fc4ab15:dNEy8t5wXfBCOaZlrwKQ4T3VDsC7oiHP_J_BdDAM2UI@node1.pghost.ru:15810/bothost_db_e7be6fc4ab15",
)
MIN_STAKE = 1
MAX_STAKE = 500

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
    """
    if not init_data or not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        items = sorted(parsed.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in items)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
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
                cur.execute(
                    """
                    INSERT INTO transactions (user_id, amount, game_type, win, detail)
                    VALUES (%s, 5, 'welcome', TRUE, 'Приветственный бонус')
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
                raise ValueError("insufficient_balance")
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


def adjust_balance(user_id: int, delta: int, game_type: str, detail: str) -> dict:
    """Корректировка баланса без изменения статистики игр (используется для блокировки ставки)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("user not found")
            new_balance = int(row["balance"]) + delta
            if new_balance < 0:
                raise ValueError("insufficient_balance")
            cur.execute(
                """
                UPDATE users SET balance = %s WHERE user_id = %s
                RETURNING balance, games_played, games_won
                """,
                (new_balance, user_id),
            )
            stats = cur.fetchone()
            cur.execute(
                """
                INSERT INTO transactions (user_id, amount, game_type, win, detail)
                VALUES (%s, %s, %s, FALSE, %s)
                """,
                (user_id, delta, game_type, detail),
            )
            conn.commit()
            return dict(stats)
    finally:
        put_conn(conn)


# ============ GAME LOGIC ============

# --- 🎲 Кубик: один кубик, игрок выбирает число 1–6 ---
# Множитель = выбранное число. EV сильно зависит от риска.
def play_dice(stake: int, target: int):
    d = random.randint(1, 6)
    if d == target:
        payout = stake * target
        return {"dice": d, "target": target, "win": True, "payout": payout, "mult": target}
    return {"dice": d, "target": target, "win": False, "payout": 0, "mult": 0}


# --- 🏀 Баскетбол: попадание x1.85 ---
def play_basketball(stake: int):
    # ~54% попадание, чтобы EV ~ 1.0
    if random.random() < 0.54:
        return {"made": True, "win": True, "payout": int(stake * 1.85), "mult": 1.85}
    return {"made": False, "win": False, "payout": 0, "mult": 0}


# --- ⚽ Футбол: гол x1.7 ---
def play_football(stake: int):
    if random.random() < 0.59:
        # round() вместо int() — иначе при ставке 1: int(1*1.7)=1, payout==stake,
        # delta = payout - stake = 0, и при «победе» баланс не меняется.
        return {
            "scored": True, "win": True, "payout": round(stake * 1.7), "mult": 1.7,
            "position": random.choice(["top-left", "top-right", "bottom-left", "bottom-right", "center"])
        }
    return {"scored": False, "win": False, "payout": 0, "mult": 0, "position": "saved"}


# --- 🎰 Рулетка: 3 барабана, фрукты x2, 777 x4 ---
def play_roulette(stake: int):
    symbols = ["🍒", "🍋", "🍉", "🍇", "7"]
    weights = [22, 22, 22, 22, 12]  # 7 чуть реже
    r1 = random.choices(symbols, weights=weights, k=1)[0]
    r2 = random.choices(symbols, weights=weights, k=1)[0]
    r3 = random.choices(symbols, weights=weights, k=1)[0]
    if r1 == r2 == r3 == "7":
        return {"reels": [r1, r2, r3], "win": True, "payout": stake * 4, "mult": 4, "kind": "777"}
    if r1 == r2 == r3:
        return {"reels": [r1, r2, r3], "win": True, "payout": stake * 2, "mult": 2, "kind": "fruits"}
    return {"reels": [r1, r2, r3], "win": False, "payout": 0, "mult": 0, "kind": "lose"}


# --- 🎯 Дартс: яблочко x5, центр x2 ---
def play_darts(stake: int):
    r = random.random()
    if r < 0.10:
        return {"hit": "bullseye", "win": True, "payout": stake * 5, "mult": 5}
    if r < 0.50:
        return {"hit": "center", "win": True, "payout": stake * 2, "mult": 2}
    return {"hit": "miss", "win": False, "payout": 0, "mult": 0}


# --- 🎮 Крестики-нолики: пошагово против бота, победа x2 ---
TTT_LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]

# Кто чем играет. Игрок — всегда "X". Бот — "O".
PLAYER_MARK = "X"
BOT_MARK = "O"


def _ttt_winner(board):
    for ln in TTT_LINES:
        a, b, c = ln[0], ln[1], ln[2]
        if board[a] != "" and board[a] == board[b] == board[c]:
            return board[a]
    return None


def _ttt_empty(board):
    return [i for i in range(9) if board[i] == ""]


def _ttt_minimax(board, depth, is_bot_turn, alpha, beta):
    """Минимакс с альфа-бета отсечением. Возвращает оценку позиции для бота (O)."""
    w = _ttt_winner(board)
    if w == BOT_MARK:
        return 10 - depth
    if w == PLAYER_MARK:
        return depth - 10
    if not _ttt_empty(board):
        return 0
    if is_bot_turn:
        best = -1000
        for i in _ttt_empty(board):
            board[i] = BOT_MARK
            score = _ttt_minimax(board, depth + 1, False, alpha, beta)
            board[i] = ""
            best = max(best, score)
            alpha = max(alpha, best)
            if beta <= alpha:
                break
        return best
    else:
        best = 1000
        for i in _ttt_empty(board):
            board[i] = PLAYER_MARK
            score = _ttt_minimax(board, depth + 1, True, alpha, beta)
            board[i] = ""
            best = min(best, score)
            beta = min(beta, best)
            if beta <= alpha:
                break
        return best


def _ttt_best_move(board):
    """Лучший ход бота. В 18% случаев — случайный (бот "старается" но не неуязвим)."""
    empties = _ttt_empty(board)
    if not empties:
        return None
    # Бот иногда "промахивается" — иначе он непроигрышный и играть неинтересно.
    if random.random() < 0.18:
        return random.choice(empties)
    best_score = -1000
    best_moves = []
    for i in empties:
        board[i] = BOT_MARK
        score = _ttt_minimax(board, 0, False, -1000, 1000)
        board[i] = ""
        if score > best_score:
            best_score = score
            best_moves = [i]
        elif score == best_score:
            best_moves.append(i)
    return random.choice(best_moves) if best_moves else random.choice(empties)


def ttt_new_game(stake: int):
    """Создаёт новую партию. Случайно выбирает, кто ходит первым."""
    board = [""] * 9
    # 50/50 кто начинает
    player_first = random.random() < 0.5
    if not player_first:
        # Бот ходит первым (X у игрока, но бот играет O, значит крестики у бота, но мы
        # фиксируем метки: игрок всегда X. Если бот ходит первым — игрок всё равно X,
        # значит ход бота O будет ВТОРЫМ. Чтобы бот реально ходил первым, поменяем метки.)
        # Реализация: пусть игрок ходит либо X либо O в зависимости от того, кто первый.
        return {"board": board, "player_first": False, "first_move_made": True,
                "win": False, "payout": 0, "mult": 0, "outcome": "continue",
                "player_mark": BOT_MARK, "bot_mark": PLAYER_MARK,
                "stake": stake, "bot_move": _ttt_best_move_with_mark(board, PLAYER_MARK, BOT_MARK)}
    return {"board": board, "player_first": True, "first_move_made": False,
            "win": False, "payout": 0, "mult": 0, "outcome": "continue",
            "player_mark": PLAYER_MARK, "bot_mark": BOT_MARK,
            "stake": stake, "bot_move": None}


def _ttt_best_move_with_mark(board, bot_mark, player_mark):
    """Лучший ход бота для заданной расстановки меток."""
    empties = _ttt_empty(board)
    if not empties:
        return None
    if random.random() < 0.18:
        return random.choice(empties)
    best_score = -1000
    best_moves = []
    for i in empties:
        board[i] = bot_mark
        score = _ttt_minimax_mark(board, 0, False, bot_mark, player_mark, -1000, 1000)
        board[i] = ""
        if score > best_score:
            best_score = score
            best_moves = [i]
        elif score == best_score:
            best_moves.append(i)
    return random.choice(best_moves) if best_moves else random.choice(empties)


def _ttt_minimax_mark(board, depth, is_bot_turn, bot_mark, player_mark, alpha, beta):
    w = _ttt_winner(board)
    if w == bot_mark:
        return 10 - depth
    if w == player_mark:
        return depth - 10
    if not _ttt_empty(board):
        return 0
    if is_bot_turn:
        best = -1000
        for i in _ttt_empty(board):
            board[i] = bot_mark
            score = _ttt_minimax_mark(board, depth + 1, False, bot_mark, player_mark, alpha, beta)
            board[i] = ""
            best = max(best, score)
            alpha = max(alpha, best)
            if beta <= alpha:
                break
        return best
    else:
        best = 1000
        for i in _ttt_empty(board):
            board[i] = player_mark
            score = _ttt_minimax_mark(board, depth + 1, True, bot_mark, player_mark, alpha, beta)
            board[i] = ""
            best = min(best, score)
            beta = min(beta, best)
            if beta <= alpha:
                break
        return best


def ttt_make_move(stake: int, board, move: int, player_mark: str, bot_mark: str):
    """Игрок сделал ход. Сервер проверяет результат и при необходимости ходит ботом.
    Возвращает полное состояние. Один вызов = один ход игрока.
    """
    if move < 0 or move > 8 or board[move] != "":
        return {"error": "bad_move"}
    board = list(board)
    board[move] = player_mark

    # Победа игрока?
    w = _ttt_winner(board)
    if w == player_mark:
        return {"board": board, "win": True, "payout": stake * 2, "mult": 2,
                "outcome": "win", "bot_move": None}
    # Ничья (поле заполнено до хода бота)?
    if not _ttt_empty(board):
        return {"board": board, "win": False, "payout": 0, "mult": 0,
                "outcome": "draw", "bot_move": None}

    # Ход бота
    bot_move = _ttt_best_move_with_mark(board, bot_mark, player_mark)
    if bot_move is not None:
        board[bot_move] = bot_mark

    # Бот победил?
    w = _ttt_winner(board)
    if w == bot_mark:
        return {"board": board, "win": False, "payout": 0, "mult": 0,
                "outcome": "lose", "bot_move": bot_move}
    # Ничья после хода бота?
    if not _ttt_empty(board):
        return {"board": board, "win": False, "payout": 0, "mult": 0,
                "outcome": "draw", "bot_move": bot_move}

    # Продолжаем
    return {"board": board, "win": False, "payout": 0, "mult": 0,
            "outcome": "continue", "bot_move": bot_move}


# --- 🎮 Сапёр: выбор размера поля 3x3 / 4x4 / 5x5 / 6x6 ---
MINES_SIZES = {3: 2.0, 4: 1.7, 5: 1.5, 6: 1.3}  # размер -> множитель


def play_mines(stake: int, pick: int, size: int = 5):
    """size — сторона квадрата (3..6). 1 мина, остальное безопасно."""
    if size not in MINES_SIZES:
        size = 5
    total = size * size
    if pick < 0 or pick >= total:
        pick = 0
    mine = random.randint(0, total - 1)
    mult = MINES_SIZES[size]
    if pick == mine:
        return {"mine": mine, "pick": pick, "size": size,
                "win": False, "payout": 0, "mult": 0}
    return {"mine": mine, "pick": pick, "size": size,
            "win": True, "payout": int(stake * mult), "mult": mult}


# --- 🎮 КНБ: камень-ножницы-бумага, победа x2 ---
RPS_MOVES = ["rock", "paper", "scissors"]
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def play_rps(stake: int, player_move: str):
    if player_move not in RPS_MOVES:
        player_move = "rock"
    ai_move = random.choice(RPS_MOVES)
    if player_move == ai_move:
        # Ничья: награды нет, ставка не возвращается
        return {"player": player_move, "ai": ai_move, "draw": True, "win": False,
                "payout": 0, "mult": 0}
    if RPS_BEATS[player_move] == ai_move:
        return {"player": player_move, "ai": ai_move, "draw": False, "win": True,
                "payout": stake * 2, "mult": 2}
    return {"player": player_move, "ai": ai_move, "draw": False, "win": False,
            "payout": 0, "mult": 0}


# --- 🪙 Орёл и решка: угадал x1.95 ---
def play_coin(stake: int, guess: str):
    if guess not in ("heads", "tails"):
        guess = "heads"
    result = random.choice(["heads", "tails"])
    if result == guess:
        return {"result": result, "guess": guess, "win": True,
                "payout": int(stake * 1.95), "mult": 1.95}
    return {"result": result, "guess": guess, "win": False, "payout": 0, "mult": 0}


# --- 🃏 Блэкджек: победа x2, блэкджек x2.5 ---
def _deal_card(deck):
    return deck.pop()


def _hand_value(hand):
    total = sum(hand)
    aces = hand.count(11)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _new_deck():
    # Карты: 11 = туз (1 или 11)
    deck = [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11] * 4
    random.shuffle(deck)
    return deck


def play_blackjack(stake: int):
    deck = _new_deck()
    player = [_deal_card(deck), _deal_card(deck)]
    dealer = [_deal_card(deck), _deal_card(deck)]

    p_val = _hand_value(player)
    d_val = _hand_value(dealer)
    player_blackjack = (p_val == 21 and len(player) == 2)
    dealer_blackjack = (d_val == 21 and len(dealer) == 2)

    # Игрок добирает, пока <= 14 (мягкая стратегия)
    while p_val <= 14:
        player.append(_deal_card(deck))
        p_val = _hand_value(player)
        if len(player) > 6:
            break

    # Дилер добирает до 17
    while d_val < 17:
        dealer.append(_deal_card(deck))
        d_val = _hand_value(dealer)
        if len(dealer) > 6:
            break

    # Определяем исход
    if player_blackjack and not dealer_blackjack:
        kind = "blackjack"
        win = True
        mult = 2.5
    elif dealer_blackjack and not player_blackjack:
        kind = "lose"
        win = False
        mult = 0
    elif p_val > 21:
        kind = "bust"
        win = False
        mult = 0
    elif d_val > 21:
        kind = "win"
        win = True
        mult = 2
    elif p_val > d_val:
        kind = "win"
        win = True
        mult = 2
    elif p_val == d_val:
        kind = "push"
        win = False
        mult = 0  # ничья — награды нет, ставка не возвращается
    else:
        kind = "lose"
        win = False
        mult = 0

    payout = int(stake * mult) if win else 0
    return {
        "player": player, "dealer": dealer,
        "player_val": p_val, "dealer_val": d_val,
        "win": win, "payout": payout, "mult": mult if win else 0,
        "kind": kind,
    }


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
                return jsonify({"ok": False, "error": "not_found"}), 404
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


def _commit_game(uid: int, game_type: str, stake: int, result: dict):
    """Записать результат игры в БД и вернуть jsonify."""
    payout = int(result.get("payout", 0))
    delta = payout - stake
    try:
        stats = record_game(uid, game_type, delta, result["win"], json.dumps(result, default=str))
    except ValueError:
        current = get_balance(uid)
        return jsonify({"ok": False, "error": "insufficient_balance", "balance": current}), 400
    return jsonify({
        "ok": True,
        "result": result,
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


def _validate_stake(body: dict):
    """Возвращает (stake, error_response_or_None)."""
    stake = int(body.get("stake", MIN_STAKE))
    if stake < MIN_STAKE or stake > MAX_STAKE:
        return None, jsonify({"ok": False, "error": f"Ставка от {MIN_STAKE} до {MAX_STAKE}"}), 400
    return stake, None, None


@app.route("/api/game/dice", methods=["POST"])
@require_auth
def api_dice():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    target = int(body.get("target", 3))
    target = max(1, min(6, target))
    try:
        result = play_dice(stake, target)
    except Exception:
        log.exception("dice error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "dice", stake, result)


@app.route("/api/game/basketball", methods=["POST"])
@require_auth
def api_basketball():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        result = play_basketball(stake)
    except Exception:
        log.exception("basketball error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "basketball", stake, result)


@app.route("/api/game/football", methods=["POST"])
@require_auth
def api_football():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        result = play_football(stake)
    except Exception:
        log.exception("football error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "football", stake, result)


@app.route("/api/game/roulette", methods=["POST"])
@require_auth
def api_roulette():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        result = play_roulette(stake)
    except Exception:
        log.exception("roulette error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "roulette", stake, result)


@app.route("/api/game/darts", methods=["POST"])
@require_auth
def api_darts():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        result = play_darts(stake)
    except Exception:
        log.exception("darts error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "darts", stake, result)


@app.route("/api/game/ttt/new", methods=["POST"])
@require_auth
def api_ttt_new():
    """Создать новую партию крестиков-ноликов. Списывает ставку.
    Возвращает: board, player_first, player_mark, bot_mark, bot_move (если бот ходил первым).
    """
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        state = ttt_new_game(stake)
    except Exception:
        log.exception("ttt_new error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    # Списываем ставку сразу (при создании партии). Это только блокировка средств,
    # статистика игр не меняется до окончания партии.
    try:
        stats = adjust_balance(uid, -stake, "ttt", "Ставка заблокирована")
    except ValueError:
        current = get_balance(uid)
        return jsonify({"ok": False, "error": "insufficient_balance", "balance": current}), 400
    return jsonify({
        "ok": True,
        "stake": stake,
        "board": state["board"],
        "player_first": state["player_first"],
        "player_mark": state["player_mark"],
        "bot_mark": state["bot_mark"],
        "bot_move": state.get("bot_move"),
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


@app.route("/api/game/ttt/move", methods=["POST"])
@require_auth
def api_ttt_move():
    """Ход игрока. Возвращает доску, ход бота, исход (continue/win/lose/draw).
    Если win — начисляем выигрыш (stake * 2). Если lose/draw — ничего не начисляем.
    """
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    board = body.get("board")
    move = body.get("move")
    player_mark = body.get("player_mark", "X")
    bot_mark = body.get("bot_mark", "O")
    if not isinstance(board, list) or len(board) != 9:
        return jsonify({"ok": False, "error": "bad_board"}), 400
    try:
        move_i = int(move)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_move"}), 400
    try:
        state = ttt_make_move(stake, board, move_i, player_mark, bot_mark)
    except Exception:
        log.exception("ttt_move error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500

    if state.get("error") == "bad_move":
        return jsonify({"ok": False, "error": "Клетка занята"}), 400

    delta = 0
    if state["outcome"] == "win":
        delta = state["payout"] - stake  # списывали при new, теперь добавляем выигрыш (payout = stake*2, delta = stake)
    # При continue / lose / draw — дельта 0 (ставка уже списана).

    if delta != 0:
        # При победе: возвращаем ставку + начисляем выигрыш. Статистика обновляется.
        stats = record_game(uid, "ttt", delta, state["win"],
                            json.dumps({"outcome": state["outcome"]}, default=str))
    elif state["outcome"] in ("lose", "draw"):
        # При поражении/ничьей: фиксируем игру в статистике, баланс уже списан при new.
        stats = record_game(uid, "ttt", 0, False,
                            json.dumps({"outcome": state["outcome"]}, default=str))
    else:
        # continue — партия ещё идёт, статистику не трогаем
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT balance, games_played, games_won FROM users WHERE user_id = %s", (uid,))
                stats = cur.fetchone()
        finally:
            put_conn(conn)

    return jsonify({
        "ok": True,
        "board": state["board"],
        "bot_move": state.get("bot_move"),
        "outcome": state["outcome"],
        "win": state["win"],
        "payout": state["payout"],
        "mult": state["mult"],
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


@app.route("/api/game/mines", methods=["POST"])
@require_auth
def api_mines():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    pick = int(body.get("pick", 0))
    size = int(body.get("size", 5))
    try:
        result = play_mines(stake, pick, size)
    except Exception:
        log.exception("mines error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "mines", stake, result)


@app.route("/api/game/rps", methods=["POST"])
@require_auth
def api_rps():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    move = body.get("move", "rock")
    try:
        result = play_rps(stake, move)
    except Exception:
        log.exception("rps error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "rps", stake, result)


@app.route("/api/game/coin", methods=["POST"])
@require_auth
def api_coin():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    guess = body.get("guess", "heads")
    try:
        result = play_coin(stake, guess)
    except Exception:
        log.exception("coin error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "coin", stake, result)


@app.route("/api/game/blackjack", methods=["POST"])
@require_auth
def api_blackjack():
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        result = play_blackjack(stake)
    except Exception:
        log.exception("blackjack error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    return _commit_game(uid, "blackjack", stake, result)


# ============ FRONTEND ============
INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
<meta name="theme-color" content="#000000" />
<title>Royal Spin — Telegram Звёзды</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #000000;
    --bg2: #111111;
    --bg3: #1a1a1a;
    --gold: #ffd60a;
    --gold2: #ffea00;
    --gold3: #ffaa00;
    --purple: #ffd60a;
    --pink: #ffea00;
    --green: #10b981;
    --red: #ef4444;
    --blue: #3b82f6;
    --text: #ffffff;
    --muted: #8a8a8a;
    --card: rgba(255, 214, 10, 0.04);
    --border: rgba(255, 214, 10, 0.35);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0; height: 100%; overflow-x: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    background: radial-gradient(ellipse at top, #1a1a0a 0%, var(--bg) 60%) fixed;
  }
  .app { min-height: 100%; padding: 16px 16px 32px; max-width: 520px; margin: 0 auto; }

  /* HEADER */
  .header {
    display: flex; align-items: center; gap: 12px;
    padding: 14px; border-radius: 18px;
    background: linear-gradient(135deg, rgba(255,214,10,0.14), rgba(255,234,0,0.10));
    border: 1px solid var(--border);
    box-shadow: 0 8px 32px rgba(255,214,10,0.14);
    margin-bottom: 18px;
  }
  .avatar {
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, var(--gold), var(--purple));
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; font-weight: 700; color: #000000;
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
    color: #000000;
  }
  .balance-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .balance-val { font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 4px; }
  .star { display: inline-block; width: 16px; height: 16px; }

  /* TITLE */
  .title { text-align: center; margin: 8px 0 18px; }
  .title h1 {
    margin: 0; font-size: 32px; font-weight: 900;
    color: #f7c948;
    background: linear-gradient(90deg,
      #b8860b 0%,
      #f7c948 18%,
      #fff5b3 35%,
      #ffd86b 50%,
      #fff5b3 65%,
      #f7c948 82%,
      #b8860b 100%);
    background-size: 220% 100%;
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent;
    color: transparent;
    letter-spacing: 1.5px;
    animation: shimmer 2.6s linear infinite;
    filter: drop-shadow(0 0 6px rgba(247,201,72,0.55))
            drop-shadow(0 0 14px rgba(247,201,72,0.35));
  }
  @keyframes shimmer {
    0%   { background-position:   0% 0; }
    100% { background-position: 220% 0; }
  }
  .title p { margin: 6px 0 0; color: var(--muted); font-size: 13px; }

  /* GAME GRID */
  .games { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .game-card {
    position: relative; padding: 14px 8px; border-radius: 18px;
    background: var(--card); border: 1px solid var(--border);
    text-align: center; cursor: pointer;
    transition: transform 0.12s, box-shadow 0.12s;
    overflow: hidden;
  }
  .game-card::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(135deg, transparent, rgba(255,214,10,0.10));
    opacity: 0; transition: opacity 0.15s;
  }
  .game-card:active { transform: scale(0.96); }
  .game-card:hover::before { opacity: 1; }
  .game-icon { font-size: 34px; margin-bottom: 4px; display: block; }
  .game-name { font-weight: 700; font-size: 13px; }
  .game-sub { font-size: 10px; color: var(--muted); margin-top: 2px; }

  /* MODAL */
  .modal-back {
    position: fixed; inset: 0; background: rgba(0,0,0,0.75);
    display: none; align-items: flex-end; justify-content: center;
    z-index: 100; backdrop-filter: blur(8px);
  }
  .modal-back.show { display: flex; animation: fadeIn 0.15s; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  .modal {
    width: 100%; max-width: 520px; max-height: 92vh;
    background: linear-gradient(180deg, var(--bg2), var(--bg));
    border-radius: 24px 24px 0 0; padding: 20px;
    border-top: 1px solid var(--border);
    animation: slideUp 0.18s cubic-bezier(.2,.8,.3,1);
    overflow-y: auto;
    position: relative;
  }
  @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
  .modal h2 {
    margin: 0 0 4px; text-align: center;
    font-size: 20px;
    background: linear-gradient(90deg, var(--gold), var(--pink));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .modal-sub { text-align: center; color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  .close-btn {
    position: absolute; top: 14px; right: 14px;
    width: 30px; height: 30px; border-radius: 50%;
    background: rgba(255,255,255,0.08); border: none; color: var(--text);
    font-size: 16px; cursor: pointer; z-index: 5;
  }
  .stake-row {
    display: flex; gap: 6px; margin: 12px 0;
    justify-content: center;
  }
  .stake-btn {
    flex: 1; padding: 9px 0;
    border-radius: 10px; border: 1px solid var(--border);
    background: var(--card); color: var(--text); font-weight: 700;
    cursor: pointer; transition: 0.12s; font-size: 13px;
  }
  .stake-btn.active {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000000; border-color: var(--gold);
  }
  .play-btn {
    width: 100%; padding: 14px; border: none; border-radius: 14px;
    background: linear-gradient(135deg, var(--purple), var(--pink));
    color: #000000; font-size: 16px; font-weight: 800; cursor: pointer;
    box-shadow: 0 6px 20px rgba(255,214,10,0.45);
    transition: 0.12s;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .play-btn:active { transform: scale(0.97); }
  .play-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* DICE */
  .dice-arena { display: flex; gap: 16px; justify-content: center; margin: 14px 0; perspective: 600px; }
  .dice {
    width: 80px; height: 80px; position: relative;
    transform-style: preserve-3d;
  }
  .dice.rolling { animation: diceRoll 0.6s linear; }
  @keyframes diceRoll {
    0%   { transform: rotateX(0deg)   rotateY(0deg)   rotateZ(0deg); }
    25%  { transform: rotateX(180deg) rotateY(90deg)  rotateZ(45deg); }
    50%  { transform: rotateX(360deg) rotateY(180deg) rotateZ(90deg); }
    75%  { transform: rotateX(540deg) rotateY(270deg) rotateZ(135deg); }
    100% { transform: rotateX(720deg) rotateY(360deg) rotateZ(180deg); }
  }
  .dice-face {
    position: absolute; inset: 0;
    background: linear-gradient(135deg, #fff, #e5e5f7);
    border-radius: 12px; border: 2px solid var(--gold);
    display: grid; padding: 8px;
    box-shadow: inset 0 0 12px rgba(247,201,72,0.3), 0 6px 16px rgba(0,0,0,0.3);
  }
  .dice-face.front  { transform: translateZ(40px); }
  .dice-face.back   { transform: rotateY(180deg) translateZ(40px); }
  .dice-face.right  { transform: rotateY(90deg)  translateZ(40px); }
  .dice-face.left   { transform: rotateY(-90deg) translateZ(40px); }
  .dice-face.top    { transform: rotateX(90deg)  translateZ(40px); }
  .dice-face.bottom { transform: rotateX(-90deg) translateZ(40px); }
  .dot { width: 14px; height: 14px; border-radius: 50%; background: #000000; align-self: center; justify-self: center; }
  .f1 { display: grid; place-items: center; }
  .f2 { display: grid; grid-template-columns: 1fr 1fr; align-items: center; justify-items: center; }
  .f3 { display: grid; grid-template-columns: 1fr 1fr 1fr; align-items: center; justify-items: center; }
  .f4 { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 8px; padding: 12px; }
  .f5 { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 4px; padding: 8px; align-items: center; justify-items: center; }
  .f6 { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr 1fr; gap: 4px; padding: 8px; align-items: center; justify-items: center; }

  .num-picker { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin: 10px 0 6px; }
  .num-btn {
    padding: 10px 0; border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card); color: var(--text);
    font-weight: 800; font-size: 16px; cursor: pointer;
    transition: 0.1s;
  }
  .num-btn.active {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000000; border-color: var(--gold);
    transform: scale(1.05);
  }

  /* FOOTBALL — игрок снизу, ворота сверху; мяч летит СНИЗУ ВВЕРХ */
  .fb-field {
    position: relative; height: 220px;
    background: linear-gradient(180deg, #1f4530 0%, #2d5a3d 100%);
    border-radius: 16px; margin: 12px 0;
    overflow: hidden;
    border: 2px solid rgba(255,255,255,0.15);
  }
  .fb-field::before {
    content: ""; position: absolute; inset: 10px;
    border: 2px solid rgba(255,255,255,0.3); border-radius: 8px;
  }
  .fb-goal {
    position: absolute; left: 50%; transform: translateX(-50%);
    top: 10px; width: 160px; height: 50px;
    border: 3px solid #fff; border-top: none;
    background: rgba(255,255,255,0.05);
  }
  .fb-keeper {
    position: absolute; left: 50%; top: 15px; transform: translateX(-50%);
    width: 36px; height: 50px;
    transition: left 0.25s cubic-bezier(.4,1.6,.5,1), top 0.25s;
    font-size: 32px; text-align: center;
  }
  .fb-ball {
    position: absolute; left: 50%; bottom: 6px; transform: translateX(-50%);
    width: 28px; height: 28px; font-size: 26px; line-height: 28px; text-align: center;
    transition: all 0.45s cubic-bezier(.4,.1,.4,1);
  }
  .fb-ball.shoot { animation: ballShoot 0.55s forwards; }
  @keyframes ballShoot {
    0%   { left: 50%; bottom: 6px;  transform: translateX(-50%) scale(1); }
    55%  { bottom: 60%; left: var(--bx, 50%); transform: translateX(-50%) scale(1.1); }
    100% { bottom: 78%; left: var(--bx, 50%); transform: translateX(-50%) scale(1.35); }
  }

  /* BASKETBALL */
  .bb-court {
    position: relative; height: 220px;
    background: linear-gradient(180deg, #b8860b 0%, #8b6508 100%);
    border-radius: 16px; margin: 12px 0;
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
    transition: all 0.5s cubic-bezier(.3,.1,.4,1);
  }
  .bb-ball.shoot { animation: bbShoot 0.5s forwards; }
  @keyframes bbShoot {
    0%   { left: 20px;  bottom: 8px;  transform: rotate(0deg); }
    40%  { left: 100px; bottom: 180px; transform: rotate(180deg) scale(1.1); }
    65%  { left: 180px; bottom: 120px; transform: rotate(360deg) scale(1); }
    85%  { left: calc(100% - 80px); bottom: 60px; transform: rotate(540deg) scale(0.9); }
    100% { left: calc(100% - 70px); bottom: 50px; transform: rotate(720deg) scale(0.85); }
  }

  /* ROULETTE */
  .slot {
    display: flex; gap: 6px; justify-content: center;
    background: linear-gradient(180deg, #0a0a0a, #000000);
    padding: 12px; border-radius: 14px;
    border: 2px solid var(--gold);
    margin: 12px 0;
  }
  .reel {
    flex: 1; max-width: 80px; height: 90px;
    background: #fff; color: #000000;
    border-radius: 10px; overflow: hidden;
    position: relative;
    display: flex; align-items: center; justify-content: center;
    font-size: 50px;
    border: 2px solid #333;
  }
  .reel-inner {
    transition: transform 0.4s cubic-bezier(.4, 1.4, .5, 1);
  }

  /* DARTS */
  .dartboard {
    position: relative; width: 200px; height: 200px;
    margin: 12px auto;
    border-radius: 50%;
    background: radial-gradient(circle at center,
      #f7c948 0 12%, #000000 12% 18%,
      #f7c948 18% 30%, #000000 30% 36%,
      #f7c948 36% 50%, #000000 50% 56%,
      #f7c948 56% 100%);
    border: 4px solid #5c3a00;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }
  .dartboard::before, .dartboard::after {
    content: ""; position: absolute; inset: 0;
    border-radius: 50%;
    pointer-events: none;
  }
  .dartboard::before {
    background:
      linear-gradient(transparent 49%, rgba(0,0,0,0.3) 49% 51%, transparent 51%),
      linear-gradient(90deg, transparent 49%, rgba(0,0,0,0.3) 49% 51%, transparent 51%);
  }
  .dart {
    position: absolute; left: 50%; top: 50%;
    font-size: 28px; line-height: 1;
    transform: translate(-50%, -50%);
    transition: all 0.35s cubic-bezier(.4, 1.4, .5, 1);
    z-index: 5;
  }
  .dart.bullseye { left: 50%; top: 50%; }
  .dart.center-tl { left: 32%; top: 32%; }
  .dart.center-tr { left: 68%; top: 32%; }
  .dart.center-bl { left: 32%; top: 68%; }
  .dart.center-br { left: 68%; top: 68%; }
  .dart.miss { left: 12%; top: 12%; opacity: 0.4; }

  /* TTT */
  .ttt-board {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 6px; margin: 12px auto;
    max-width: 280px;
    background: var(--purple);
    padding: 6px;
    border-radius: 12px;
  }
  .ttt-cell {
    aspect-ratio: 1;
    background: var(--bg2);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 36px; font-weight: 900;
    cursor: pointer;
    transition: background 0.1s;
    user-select: none;
  }
  .ttt-cell:not(.filled):active { background: var(--card); }
  .ttt-cell.filled { cursor: default; }
  .ttt-cell.x { color: var(--blue); }
  .ttt-cell.o { color: var(--pink); }
  .ttt-cell.win { background: linear-gradient(135deg, var(--gold), var(--gold2)); color: #000000; }

  /* MINES */
  .mines-grid {
    display: grid; grid-template-columns: repeat(5, 1fr);
    gap: 6px; margin: 12px auto;
    max-width: 360px;
  }
  .mine-cell {
    aspect-ratio: 1;
    border-radius: 10px;
    background: linear-gradient(135deg, #0a0a0a, #000000);
    border: 2px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 32px;
    cursor: pointer;
    transition: 0.12s;
  }
  .mine-cell:active { transform: scale(0.95); }
  .mine-cell.safe { background: linear-gradient(135deg, var(--green), #047857); border-color: var(--green); }
  .mine-cell.mine { background: linear-gradient(135deg, var(--red), #991b1b); border-color: var(--red); }

  /* RPS */
  .rps-arena { display: flex; gap: 16px; align-items: center; justify-content: center; margin: 14px 0; }
  .rps-pick {
    width: 80px; height: 80px;
    background: var(--card);
    border: 2px solid var(--border);
    border-radius: 16px;
    display: flex; align-items: center; justify-content: center;
    font-size: 40px;
  }
  .rps-pick.win-anim { animation: rpsWin 0.4s; }
  .rps-pick.lose-anim { animation: rpsLose 0.4s; }
  @keyframes rpsWin { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.15); border-color: var(--gold); } }
  @keyframes rpsLose { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
  .rps-vs { font-size: 20px; font-weight: 900; color: var(--muted); }
  .rps-buttons { display: flex; gap: 8px; justify-content: center; margin: 10px 0; }
  .rps-btn {
    flex: 1; padding: 14px 0; font-size: 30px;
    border-radius: 12px; border: 2px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer;
    transition: 0.1s;
  }
  .rps-btn:active { transform: scale(0.95); }
  .rps-btn.active { background: linear-gradient(135deg, var(--purple), var(--pink)); border-color: var(--gold); }

  /* COIN */
  .coin-wrap {
    display: flex; justify-content: center; margin: 14px 0;
    perspective: 600px;
  }
  .coin {
    width: 120px; height: 120px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    display: flex; align-items: center; justify-content: center;
    font-size: 50px;
    color: #000000; font-weight: 900;
    border: 4px solid #b8860b;
    box-shadow: 0 8px 24px rgba(247,201,72,0.4);
    transform-style: preserve-3d;
    transition: transform 0.5s;
  }
  .coin.flip-heads { animation: flipHeads 0.5s forwards; }
  .coin.flip-tails { animation: flipTails 0.5s forwards; }
  @keyframes flipHeads {
    0% { transform: rotateY(0); } 100% { transform: rotateY(1800deg); }
  }
  @keyframes flipTails {
    0% { transform: rotateY(0); } 100% { transform: rotateY(1980deg); }
  }
  .coin-buttons { display: flex; gap: 8px; }
  .coin-btn {
    flex: 1; padding: 14px 0; font-size: 18px; font-weight: 800;
    border-radius: 12px; border: 2px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer;
  }
  .coin-btn.active { background: linear-gradient(135deg, var(--gold), var(--gold2)); color: #000000; }

  /* BLACKJACK */
  .bj-table {
    background: linear-gradient(135deg, #047857, #064e3b);
    border-radius: 16px; padding: 16px;
    margin: 12px 0;
    border: 3px solid #b8860b;
  }
  .bj-hand {
    margin: 8px 0;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .bj-label { font-weight: 700; font-size: 12px; color: rgba(255,255,255,0.8); min-width: 60px; }
  .bj-cards { display: flex; gap: 6px; flex-wrap: wrap; }
  .bj-card {
    background: #fff; color: #000000;
    width: 36px; height: 50px;
    border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 900; font-size: 18px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.4);
    animation: dealCard 0.2s;
  }
  .bj-card.red { color: var(--red); }
  .bj-total { font-weight: 800; margin-left: auto; color: #fff; }
  @keyframes dealCard { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }

  /* RESULT */
  .result-text {
    text-align: center; font-size: 16px; font-weight: 800;
    margin: 8px 0; min-height: 22px;
  }
  .result-text.win { color: var(--gold); }
  .result-text.lose { color: var(--muted); }
  .result-text.draw { color: var(--blue); }

  /* TOAST */
  .toast {
    position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
    padding: 12px 18px; border-radius: 12px;
    font-weight: 800; font-size: 14px; z-index: 200;
    box-shadow: 0 6px 24px rgba(0,0,0,0.5);
    animation: toastIn 0.18s, toastOut 0.18s 1.4s forwards;
    text-align: center; min-width: 200px;
  }
  .toast.win { background: linear-gradient(135deg, var(--gold), var(--gold2)); color: #000000; }
  .toast.lose { background: linear-gradient(135deg, #555, #222); color: white; }
  .toast.draw { background: linear-gradient(135deg, var(--blue), #1e40af); color: white; }
  @keyframes toastIn { from { opacity: 0; transform: translateX(-50%) translateY(-20px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }
  @keyframes toastOut { to { opacity: 0; transform: translateX(-50%) translateY(-20px); } }

  .loading {
    position: fixed; inset: 0; background: var(--bg);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000; flex-direction: column; gap: 14px;
  }
  .spinner {
    width: 44px; height: 44px; border-radius: 50%;
    border: 4px solid rgba(247,201,72,0.2);
    border-top-color: var(--gold);
    animation: spin 0.6s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .stats {
    margin-top: 16px; padding: 12px; border-radius: 14px;
    background: var(--card); border: 1px solid var(--border);
    display: flex; justify-content: space-around; text-align: center;
  }
  .stat-val { font-size: 18px; font-weight: 800; color: var(--gold); }
  .stat-lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; }

  .min-bet-note {
    text-align: center; color: var(--muted); font-size: 11px;
    margin-top: 6px;
  }

  /* CUSTOM STAKE INPUT */
  .stake-row { flex-wrap: wrap; }
  .stake-row .custom-stake-wrap {
    flex: 1 1 100%;
    display: flex; align-items: center; gap: 6px;
    margin-top: 4px;
  }
  .custom-stake-wrap label {
    font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .custom-stake-input {
    flex: 1;
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card); color: var(--text);
    font-weight: 800; font-size: 14px;
    text-align: center;
    outline: none;
    -moz-appearance: textfield;
  }
  .custom-stake-input::-webkit-outer-spin-button,
  .custom-stake-input::-webkit-inner-spin-button {
    -webkit-appearance: none; margin: 0;
  }
  .custom-stake-input:focus {
    border-color: var(--gold);
    box-shadow: 0 0 0 2px rgba(247,201,72,0.25);
  }

  /* MINES SIZE PICKER */
  .size-picker {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 6px; margin: 10px 0 6px;
  }
  .size-btn {
    padding: 8px 0; border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card); color: var(--text);
    font-weight: 800; font-size: 13px; cursor: pointer;
    transition: 0.1s;
  }
  .size-btn.active {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000000; border-color: var(--gold);
  }
</style>
</head>
<body>

<div class="loading" id="loading">
  <div class="spinner"></div>
  <div style="color:var(--muted);font-size:13px;">Загрузка Royal Spin…</div>
</div>

<div class="app" id="app" style="display:none;">

  <div class="header">
    <div class="avatar" id="avatar">?</div>
    <div class="user-info">
      <div class="user-name" id="userName">Игрок</div>
      <div class="user-handle" id="userHandle">@username</div>
    </div>
    <div class="balance-box">
      <div class="balance-label">БАЛАНС</div>
      <div class="balance-val">
        <svg class="star" viewBox="0 0 24 24" fill="#000000"><path d="M12 2l2.9 6.9L22 10l-5.5 4.7L18 22l-6-3.7L6 22l1.5-7.3L2 10l7.1-1.1L12 2z"/></svg>
        <span id="balanceVal">0</span>
      </div>
    </div>
  </div>

  <div class="title">
    <h1>👑 ROYAL SPIN 👑</h1>
    <p>Выбери игру · Рискни звездой · Выиграй корону</p>
  </div>

  <div class="games">
    <div class="game-card" onclick="openGame('dice')">
      <span class="game-icon">🎲</span>
      <div class="game-name">Кубик</div>
      <div class="game-sub">коэф. от числа</div>
    </div>
    <div class="game-card" onclick="openGame('basketball')">
      <span class="game-icon">🏀</span>
      <div class="game-name">Баскетбол</div>
      <div class="game-sub">попадание x1.85</div>
    </div>
    <div class="game-card" onclick="openGame('football')">
      <span class="game-icon">⚽</span>
      <div class="game-name">Футбол</div>
      <div class="game-sub">гол x1.7</div>
    </div>
    <div class="game-card" onclick="openGame('roulette')">
      <span class="game-icon">🎰</span>
      <div class="game-name">Рулетка</div>
      <div class="game-sub">фрукты x2 · 777 x4</div>
    </div>
    <div class="game-card" onclick="openGame('darts')">
      <span class="game-icon">🎯</span>
      <div class="game-name">Дартс</div>
      <div class="game-sub">яблочко x5 · центр x2</div>
    </div>
    <div class="game-card" onclick="openGame('ttt')">
      <span class="game-icon">🎮</span>
      <div class="game-name">Крестики-нолики</div>
      <div class="game-sub">победа x2</div>
    </div>
    <div class="game-card" onclick="openGame('mines')">
      <span class="game-icon">🎮</span>
      <div class="game-name">Сапёр</div>
      <div class="game-sub">безопасно x1.5</div>
    </div>
    <div class="game-card" onclick="openGame('rps')">
      <span class="game-icon">🎮</span>
      <div class="game-name">КНБ</div>
      <div class="game-sub">победа x2</div>
    </div>
    <div class="game-card" onclick="openGame('coin')">
      <span class="game-icon">🪙</span>
      <div class="game-name">Орёл и решка</div>
      <div class="game-sub">угадал x1.95</div>
    </div>
    <div class="game-card" onclick="openGame('blackjack')">
      <span class="game-icon">🃏</span>
      <div class="game-name">Блэкджек</div>
      <div class="game-sub">победа x2 · BJ x2.5</div>
    </div>
  </div>

  <div class="stats">
    <div><div class="stat-val" id="statGames">0</div><div class="stat-lbl">Игры</div></div>
    <div><div class="stat-val" id="statWon">0</div><div class="stat-lbl">Победы</div></div>
    <div><div class="stat-val" id="statRate">0%</div><div class="stat-lbl">% Побед</div></div>
  </div>

  <div style="text-align:center;color:var(--muted);font-size:11px;margin-top:20px;">
    Только Telegram Звёзды · 18+ · Играй ответственно
  </div>
</div>

<!-- DICE MODAL -->
<div class="modal-back" id="modal-dice" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎲 Кубик</h2>
    <div class="modal-sub">Выбери число (1–6) и брось кубик</div>
    <div class="dice-arena">
      <div class="dice" id="diceEl"><div class="dice-face front f1"><div class="dot"></div></div><div class="dice-face back f6"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face right f3"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face left f4"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="dice-face top f2"><div class="dot"></div><div class="dot"></div></div><div class="dice-face bottom f5"><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>
    </div>
    <div class="num-picker" id="dicePicker">
      <button class="num-btn" onclick="setDiceTarget(1,this)">1</button>
      <button class="num-btn" onclick="setDiceTarget(2,this)">2</button>
      <button class="num-btn active" onclick="setDiceTarget(3,this)">3</button>
      <button class="num-btn" onclick="setDiceTarget(4,this)">4</button>
      <button class="num-btn" onclick="setDiceTarget(5,this)">5</button>
      <button class="num-btn" onclick="setDiceTarget(6,this)">6</button>
    </div>
    <div class="result-text" id="diceResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="diceBtn" onclick="playDice()">БРОСИТЬ</button>
    <div class="min-bet-note">Множитель = выбранное число · Мин. ставка: 1 ⭐</div>
  </div>
</div>

<!-- BASKETBALL MODAL -->
<div class="modal-back" id="modal-basketball" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🏀 Баскетбол</h2>
    <div class="modal-sub">Попадание в кольцо — x1.85</div>
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
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="bbBtn" onclick="playBasketball()">БРОСИТЬ</button>
  </div>
</div>

<!-- FOOTBALL MODAL -->
<div class="modal-back" id="modal-football" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>⚽ Футбол</h2>
    <div class="modal-sub">Ударь по воротам — гол x1.7</div>
    <div class="fb-field">
      <div class="fb-goal"></div>
      <div class="fb-keeper" id="fbKeeper">🧤</div>
      <div class="fb-ball" id="fbBall">⚽</div>
    </div>
    <div class="result-text" id="fbResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="fbBtn" onclick="playFootball()">УДАР</button>
  </div>
</div>

<!-- ROULETTE MODAL -->
<div class="modal-back" id="modal-roulette" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎰 Рулетка</h2>
    <div class="modal-sub">3 одинаковых фрукта — x2 · три семёрки — x4</div>
    <div class="slot">
      <div class="reel" id="reel0"><span>🍒</span></div>
      <div class="reel" id="reel1"><span>🍋</span></div>
      <div class="reel" id="reel2"><span>🍉</span></div>
    </div>
    <div class="result-text" id="roulResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="roulBtn" onclick="playRoulette()">КРУТИТЬ</button>
  </div>
</div>

<!-- DARTS MODAL -->
<div class="modal-back" id="modal-darts" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎯 Дартс</h2>
    <div class="modal-sub">Яблочко x5 · Попадание в центр x2</div>
    <div class="dartboard">
      <div class="dart" id="dartEl" style="display:none;">🎯</div>
    </div>
    <div class="result-text" id="dartResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="dartBtn" onclick="playDarts()">МЕТНУТЬ</button>
  </div>
</div>

<!-- TIC TAC TOE MODAL -->
<div class="modal-back" id="modal-ttt" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎮 Крестики-нолики</h2>
    <div class="modal-sub">Собери 3 в ряд — победа x2. Бот играет по очереди с тобой.</div>
    <div class="ttt-board" id="tttBoard"></div>
    <div class="result-text" id="tttResult">Нажми «Начать партию»</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="tttBtn" onclick="tttStartOrReset()">НАЧАТЬ ПАРТИЮ</button>
  </div>
</div>

<!-- MINES MODAL -->
<div class="modal-back" id="modal-mines" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎮 Сапёр</h2>
    <div class="modal-sub">Выбери размер поля и клетку</div>
    <div class="size-picker" id="minesSize">
      <button class="size-btn" onclick="setMinesSize(3,this)">3×3 · x2.0</button>
      <button class="size-btn" onclick="setMinesSize(4,this)">4×4 · x1.7</button>
      <button class="size-btn active" onclick="setMinesSize(5,this)">5×5 · x1.5</button>
      <button class="size-btn" onclick="setMinesSize(6,this)">6×6 · x1.3</button>
    </div>
    <div class="mines-grid" id="minesGrid"></div>
    <div class="result-text" id="minesResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="minesBtn" onclick="resetMines()">ЗАНОВО</button>
  </div>
</div>

<!-- RPS MODAL -->
<div class="modal-back" id="modal-rps" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🎮 Камень · Ножницы · Бумага</h2>
    <div class="modal-sub">Победа x2 · Ничья — возврат</div>
    <div class="rps-arena">
      <div class="rps-pick" id="rpsPlayer">❓</div>
      <div class="rps-vs">VS</div>
      <div class="rps-pick" id="rpsAI">❓</div>
    </div>
    <div class="result-text" id="rpsResult">Выбери жест</div>
    <div class="rps-buttons">
      <button class="rps-btn" onclick="playRPS('rock')">✊</button>
      <button class="rps-btn" onclick="playRPS('paper')">✋</button>
      <button class="rps-btn" onclick="playRPS('scissors')">✌️</button>
    </div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <div class="min-bet-note">Ставка списывается при выборе жеста</div>
  </div>
</div>

<!-- COIN MODAL -->
<div class="modal-back" id="modal-coin" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🪙 Орёл и решка</h2>
    <div class="modal-sub">Угадай сторону — x1.95</div>
    <div class="coin-wrap">
      <div class="coin" id="coinEl">?</div>
    </div>
    <div class="result-text" id="coinResult">Выбери сторону</div>
    <div class="coin-buttons" style="margin-top:10px;">
      <button class="coin-btn" onclick="playCoin('heads')">🦅 Орёл</button>
      <button class="coin-btn" onclick="playCoin('tails')">⭐ Решка</button>
    </div>
    <div class="stake-row" style="margin-top:14px;">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <div class="min-bet-note">Ставка списывается при выборе стороны</div>
  </div>
</div>

<!-- BLACKJACK MODAL -->
<div class="modal-back" id="modal-blackjack" onclick="if(event.target===this)closeModal()">
  <div class="modal" style="position:relative;">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h2>🃏 Блэкджек</h2>
    <div class="modal-sub">21 очко — x2.5 · Победа — x2</div>
    <div class="bj-table">
      <div class="bj-hand">
        <div class="bj-label">Дилер</div>
        <div class="bj-cards" id="bjDealer"></div>
        <div class="bj-total" id="bjDealerTotal"></div>
      </div>
      <div class="bj-hand">
        <div class="bj-label">Игрок</div>
        <div class="bj-cards" id="bjPlayer"></div>
        <div class="bj-total" id="bjPlayerTotal"></div>
      </div>
    </div>
    <div class="result-text" id="bjResult">&nbsp;</div>
    <div class="stake-row">
      <button class="stake-btn active" onclick="setStake(1,this)">⭐ 1</button>
      <button class="stake-btn" onclick="setStake(3,this)">⭐ 3</button>
      <button class="stake-btn" onclick="setStake(5,this)">⭐ 5</button>
      <button class="stake-btn" onclick="setStake(10,this)">⭐ 10</button>
      <div class="custom-stake-wrap">
        <label>Своя ставка:</label>
        <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
      </div>
    </div>
    <button class="play-btn" id="bjBtn" onclick="playBlackjack()">ИГРАТЬ</button>
  </div>
</div>

<script>
  const tg = window.Telegram ? window.Telegram.WebApp : null;
  let currentStake = 1;
  let userData = null;
  let diceTarget = 3;

  const RPS_EMOJI = { rock: '✊', paper: '✋', scissors: '✌️' };
  const RPS_NAME = { rock: 'Камень', paper: 'Бумага', scissors: 'Ножницы' };
  const COIN_LABEL = { heads: 'Орёл', tails: 'Решка' };

  if (tg) {
    tg.ready();
    tg.expand();
    tg.setHeaderColor('#000000');
    tg.setBackgroundColor('#000000');
  }

  function haptic(kind) {
    if (tg && tg.HapticFeedback) {
      if (kind === 'win') tg.HapticFeedback.notificationOccurred('success');
      else if (kind === 'lose') tg.HapticFeedback.notificationOccurred('error');
      else tg.HapticFeedback.impactOccurred('light');
    }
  }

  function setStake(v, el) {
    currentStake = parseInt(v) || 1;
    if (currentStake < MIN_STAKE) currentStake = MIN_STAKE;
    if (currentStake > MAX_STAKE) currentStake = MAX_STAKE;
    document.querySelectorAll('.stake-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.custom-stake-input').forEach(i => i.value = currentStake);
    if (el && el.classList && el.classList.contains('stake-btn')) el.classList.add('active');
  }

  function setCustomStake(input) {
    let v = parseInt(input.value);
    if (isNaN(v)) v = MIN_STAKE;
    v = Math.max(MIN_STAKE, Math.min(MAX_STAKE, v));
    input.value = v;
    currentStake = v;
    document.querySelectorAll('.stake-btn').forEach(b => b.classList.remove('active'));
  }

  function setDiceTarget(v, el) {
    diceTarget = v;
    document.querySelectorAll('#dicePicker .num-btn').forEach(b => b.classList.remove('active'));
    if (el) el.classList.add('active');
  }

  function applyUser(u, isNew) {
    userData = u;
    const name = u.first_name || u.username || 'Игрок';
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
    if (!resp.ok) throw new Error(data.error || ('http_' + resp.status));
    return data;
  }

  async function bootstrap() {
    try {
      const initData = tg ? tg.initData : '';
      if (!initData) { showDevMode(); return; }
      const r = await api('/api/auth', {});
      applyUser(r.user, r.is_new);
      if (r.is_new && tg && tg.showAlert) {
        tg.showAlert('Добро пожаловать в Royal Spin! 🎁 Вы получили 5 звёзд в подарок.');
      }
      document.getElementById('loading').style.display = 'none';
      document.getElementById('app').style.display = '';
    } catch (e) {
      console.error(e);
      showDevMode();
    }
  }

  function showDevMode() {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('app').style.display = '';
    applyUser({id: 0, first_name: 'Гость', username: 'preview', photo_url: null,
               balance: 5, games_played: 0, games_won: 0}, true);
    if (tg && tg.showAlert) {
      tg.showAlert('Откройте Mini App из Telegram для игры на настоящие звёзды.');
    }
  }

  function openGame(name) {
    haptic('light');
    document.getElementById('modal-' + name).classList.add('show');
    // Сбросить визуал для игр, где нужно
    if (name === 'ttt') resetTTT();
    if (name === 'mines') resetMines();
    if (name === 'rps') {
      document.getElementById('rpsPlayer').textContent = '❓';
      document.getElementById('rpsAI').textContent = '❓';
      document.getElementById('rpsResult').textContent = 'Выбери жест';
      document.getElementById('rpsResult').className = 'result-text';
    }
    if (name === 'coin') {
      const c = document.getElementById('coinEl');
      c.className = 'coin';
      c.textContent = '?';
      document.getElementById('coinResult').textContent = 'Выбери сторону';
      document.getElementById('coinResult').className = 'result-text';
    }
  }
  function closeModal() {
    document.querySelectorAll('.modal-back').forEach(m => m.classList.remove('show'));
  }

  function toast(text, kind) {
    const t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 1700);
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

  function applyResult(r) {
    setBalance(r.balance);
    updateStats(r.games_played, r.games_won);
  }

  // ===== DICE =====
  function faceRot(n) {
    const map = {
      1: {x: 0, y: 0},
      2: {x: -90, y: 0},
      3: {x: 0, y: -90},
      4: {x: 0, y: 90},
      5: {x: 90, y: 0},
      6: {x: 180, y: 0}
    };
    const c = map[n];
    return 'rotateX(' + c.x + 'deg) rotateY(' + c.y + 'deg)';
  }

  async function playDice() {
    const btn = document.getElementById('diceBtn');
    btn.disabled = true;
    const d = document.getElementById('diceEl');
    d.classList.add('rolling');
    d.style.transform = '';
    document.getElementById('diceResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/dice', { stake: currentStake, target: diceTarget });
      setTimeout(() => {
        d.classList.remove('rolling');
        d.style.transform = faceRot(r.result.dice);
        const txt = document.getElementById('diceResult');
        const net = r.result.payout - currentStake;
        if (r.result.win) {
          txt.className = 'result-text win';
          txt.textContent = 'Выпало ' + r.result.dice + ' · ПОБЕДА x' + r.result.mult + ' (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else {
          txt.className = 'result-text lose';
          txt.textContent = 'Выпало ' + r.result.dice + ' · ПРОВАЛ (−' + currentStake + ' ⭐)';
          toast('💀 −' + currentStake + ' звёзд', 'lose');
          haptic('lose');
        }
        applyResult(r);
      }, 600);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, 700);
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
    keeper.style.left = '50%'; keeper.style.top = '15px';
    document.getElementById('fbResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/football', { stake: currentStake });
      const pos = r.result.position;
      // Вратарь реагирует (цель — ворота сверху поля)
      setTimeout(() => {
        if (pos === 'top-left')         { keeper.style.left = '25%'; keeper.style.top = '40px'; }
        else if (pos === 'top-right')   { keeper.style.left = '75%'; keeper.style.top = '40px'; }
        else if (pos === 'bottom-left') { keeper.style.left = '35%'; keeper.style.top = '15px'; }
        else if (pos === 'bottom-right'){ keeper.style.left = '65%'; keeper.style.top = '15px'; }
        else                            { keeper.style.left = '50%'; keeper.style.top = '30px'; }
      }, 80);
      setTimeout(() => {
        let bx = '50%';
        if (pos === 'top-left')         bx = '25%';
        else if (pos === 'top-right')   bx = '75%';
        else if (pos === 'bottom-left') bx = '35%';
        else if (pos === 'bottom-right')bx = '65%';
        ball.style.setProperty('--bx', bx);
        ball.classList.add('shoot');
        const txt = document.getElementById('fbResult');
        const net = r.result.payout - currentStake;
        setTimeout(() => {
          if (r.result.win) {
            txt.className = 'result-text win';
            txt.textContent = 'ГОЛ! ⚽ ПОБЕДА x1.7 (+' + net + ' ⭐)';
            toast('🎉 +' + net + ' звёзд', 'win');
            haptic('win');
          } else {
            txt.className = 'result-text lose';
            txt.textContent = 'СЕЙВ 🧤 ПРОВАЛ (−' + currentStake + ' ⭐)';
            toast('💀 −' + currentStake + ' звёзд', 'lose');
            haptic('lose');
          }
          applyResult(r);
        }, 420);
      }, 200);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, 900);
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
        const net = r.result.payout - currentStake;
        setTimeout(() => {
          if (r.result.win) {
            txt.className = 'result-text win';
            txt.textContent = 'ПОПАДАНИЕ! 🏀 x1.85 (+' + net + ' ⭐)';
            toast('🎉 +' + net + ' звёзд', 'win');
            haptic('win');
          } else {
            txt.className = 'result-text lose';
            txt.textContent = 'ПРОМАХ 😢 (−' + currentStake + ' ⭐)';
            toast('💀 −' + currentStake + ' звёзд', 'lose');
            haptic('lose');
          }
          applyResult(r);
        }, 480);
      }, 80);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, 800);
    }
  }

  // ===== ROULETTE =====
  async function playRoulette() {
    const btn = document.getElementById('roulBtn');
    btn.disabled = true;
    document.getElementById('roulResult').innerHTML = '&nbsp;';
    const reels = [document.getElementById('reel0'), document.getElementById('reel1'), document.getElementById('reel2')];
    reels.forEach(r => r.firstElementChild.style.transform = '');
    try {
      const r = await api('/api/game/roulette', { stake: currentStake });
      const finalSymbols = r.result.reels;
      const stops = [
        ['🍒','🍋','🍉','🍇','7'],
        ['🍒','🍋','🍉','🍇','7'],
        ['🍒','🍋','🍉','🍇','7']
      ];
      // Анимация прокрутки
      reels.forEach((reel, idx) => {
        let count = 0;
        const max = 8 + idx * 4;
        const iv = setInterval(() => {
          const sym = stops[idx][Math.floor(Math.random() * stops[idx].length)];
          reel.firstElementChild.textContent = sym;
          count++;
          if (count >= max) {
            clearInterval(iv);
            reel.firstElementChild.textContent = finalSymbols[idx];
          }
        }, 60);
      });
      const totalDur = 60 * (8 + 2 * 4) + 200;
      setTimeout(() => {
        const txt = document.getElementById('roulResult');
        const net = r.result.payout - currentStake;
        if (r.result.kind === '777') {
          txt.className = 'result-text win';
          txt.textContent = 'ДЖЕКПОТ! 7️⃣7️⃣7️ x4 (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else if (r.result.kind === 'fruits') {
          txt.className = 'result-text win';
          txt.textContent = 'ФРУКТЫ! ' + finalSymbols.join('') + ' x2 (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else {
          txt.className = 'result-text lose';
          txt.textContent = 'ПРОВАЛ (' + finalSymbols.join(' ') + ') (−' + currentStake + ' ⭐)';
          toast('💀 −' + currentStake + ' звёзд', 'lose');
          haptic('lose');
        }
        applyResult(r);
      }, totalDur);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, totalDur_safe());
    }
  }
  function totalDur_safe() { return 60 * (8 + 8) + 300; }

  // ===== DARTS =====
  async function playDarts() {
    const btn = document.getElementById('dartBtn');
    btn.disabled = true;
    const dart = document.getElementById('dartEl');
    dart.style.display = 'block';
    dart.className = 'dart';
    document.getElementById('dartResult').innerHTML = '&nbsp;';
    try {
      const r = await api('/api/game/darts', { stake: currentStake });
      const hit = r.result.hit;
      // Случайная промежуточная позиция
      dart.classList.add('center-tl');
      setTimeout(() => {
        let cls = 'miss';
        if (hit === 'bullseye') cls = 'bullseye';
        else if (hit === 'center') {
          const opts = ['center-tl', 'center-tr', 'center-bl', 'center-br'];
          cls = opts[Math.floor(Math.random() * opts.length)];
        } else {
          cls = 'miss';
        }
        dart.className = 'dart ' + cls;
        const txt = document.getElementById('dartResult');
        const net = r.result.payout - currentStake;
        if (hit === 'bullseye') {
          txt.className = 'result-text win';
          txt.textContent = 'ЯБЛОЧКО! 🎯 x5 (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else if (hit === 'center') {
          txt.className = 'result-text win';
          txt.textContent = 'В ЦЕНТР! x2 (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else {
          txt.className = 'result-text lose';
          txt.textContent = 'ПРОМАХ (−' + currentStake + ' ⭐)';
          toast('💀 −' + currentStake + ' звёзд', 'lose');
          haptic('lose');
        }
        applyResult(r);
      }, 380);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, 700);
    }
  }

  // ===== TTT (пошаговая игра против бота) =====
  const TTT_LINES = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]];
  let tttBoard = ["","","","","","","","",""];
  let tttPlayerMark = "X";
  let tttBotMark = "O";
  let tttOver = true;

  function buildTTTBoard() {
    const b = document.getElementById('tttBoard');
    b.innerHTML = '';
    for (let i = 0; i < 9; i++) {
      const d = document.createElement('div');
      d.className = 'ttt-cell';
      d.dataset.i = i;
      d.onclick = function() { tttClick(i, this); };
      b.appendChild(d);
    }
  }

  function renderTTT() {
    const cells = document.querySelectorAll('#tttBoard .ttt-cell');
    cells.forEach((c, idx) => {
      const v = tttBoard[idx];
      if (v) {
        c.textContent = v;
        c.className = 'ttt-cell filled ' + (v === tttPlayerMark ? 'x' : 'o');
      } else {
        c.textContent = '';
        c.className = 'ttt-cell';
      }
    });
  }

  function tttWinningLine(board, mark) {
    for (const ln of TTT_LINES) {
      if (ln.every(j => board[j] === mark)) return ln;
    }
    return null;
  }

  function highlightWin(line) {
    if (!line) return;
    const cells = document.querySelectorAll('#tttBoard .ttt-cell');
    cells.forEach((c, idx) => { if (line.includes(idx)) c.classList.add('win'); });
  }

  async function tttStartOrReset() {
    if (!tttOver) {
      // Партия идёт — кнопка делает «ЗАНОВО»
      buildTTTBoard();
      tttBoard = ["","","","","","","","",""];
      tttOver = true;
      document.getElementById('tttResult').textContent = 'Нажми «Начать партию»';
      document.getElementById('tttResult').className = 'result-text';
      document.getElementById('tttBtn').textContent = 'НАЧАТЬ ПАРТИЮ';
      return;
    }
    const btn = document.getElementById('tttBtn');
    btn.disabled = true;
    btn.textContent = 'ПОДКЛЮЧЕНИЕ…';
    buildTTTBoard();
    try {
      const r = await api('/api/game/ttt/new', { stake: currentStake });
      tttBoard = r.board;
      tttPlayerMark = r.player_mark;
      tttBotMark = r.bot_mark;
      tttOver = false;
      setBalance(r.balance);
      const txt = document.getElementById('tttResult');
      if (r.player_first) {
        txt.textContent = 'Ты ходишь первым (' + tttPlayerMark + '). Ставка списана.';
      } else {
        txt.textContent = 'Бот ходит первым (' + tttBotMark + '). Ставка списана.';
        // Бот уже сделал ход на сервере
        renderTTTWithBotMove(r.board, r.bot_move, () => {
          txt.textContent = 'Твой ход (' + tttPlayerMark + ')';
        });
      }
      renderTTT();
      document.getElementById('tttBtn').textContent = 'ЗАНОВО';
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
      document.getElementById('tttResult').textContent = 'Ошибка запуска';
    } finally {
      btn.disabled = false;
    }
  }

  function renderTTTWithBotMove(board, botMove, cb) {
    if (botMove == null) { if (cb) cb(); return; }
    setTimeout(() => {
      tttBoard[botMove] = tttBotMark;
      const cells = document.querySelectorAll('#tttBoard .ttt-cell');
      const c = cells[botMove];
      c.textContent = tttBotMark;
      c.className = 'ttt-cell filled ' + (tttBotMark === tttPlayerMark ? 'x' : 'o');
      haptic('light');
      if (cb) cb();
    }, 380);
  }

  async function tttClick(i, el) {
    if (tttOver) return;
    if (el.classList.contains('filled')) return;
    if (tttBoard[i] !== "") return;
    el.textContent = tttPlayerMark;
    el.classList.add('filled', tttPlayerMark === tttPlayerMark ? 'x' : 'o');
    haptic('light');
    try {
      const r = await api('/api/game/ttt/move', {
        stake: currentStake, board: tttBoard, move: i,
        player_mark: tttPlayerMark, bot_mark: tttBotMark
      });
      tttBoard = r.board;
      // Если бот ходил — сначала показываем ход бота, потом итог
      if (r.bot_move != null && r.outcome !== 'continue') {
        renderTTTWithBotMove(tttBoard, r.bot_move, () => finishTTT(r));
      } else if (r.bot_move != null) {
        // Бот ходит, партия продолжается
        renderTTTWithBotMove(tttBoard, r.bot_move, () => {
          const txt = document.getElementById('tttResult');
          txt.textContent = 'Твой ход (' + tttPlayerMark + ')';
        });
      } else {
        // Бот не ходил (игра завершилась до его хода)
        renderTTT();
        finishTTT(r);
      }
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
      el.textContent = '';
      el.classList.remove('filled', 'x', 'o');
    }
  }

  function finishTTT(r) {
    renderTTT();
    const winLine = r.outcome === 'win' ? tttWinningLine(tttBoard, tttPlayerMark)
                  : r.outcome === 'lose' ? tttWinningLine(tttBoard, tttBotMark)
                  : null;
    highlightWin(winLine);
    tttOver = true;
    document.getElementById('tttBtn').textContent = 'НОВАЯ ПАРТИЯ';
    const txt = document.getElementById('tttResult');
    const net = (r.payout || 0) - currentStake;
    if (r.outcome === 'win') {
      txt.className = 'result-text win';
      txt.textContent = 'ПОБЕДА! 🏆 x2 (+' + net + ' ⭐)';
      toast('🎉 +' + net + ' звёзд', 'win');
      haptic('win');
    } else if (r.outcome === 'draw') {
      txt.className = 'result-text draw';
      txt.textContent = 'НИЧЬЯ · без награды (−' + currentStake + ' ⭐)';
      toast('Ничья — ставка не возвращается', 'draw');
      haptic('light');
    } else {
      txt.className = 'result-text lose';
      txt.textContent = 'ПОРАЖЕНИЕ (−' + currentStake + ' ⭐)';
      toast('💀 −' + currentStake + ' звёзд', 'lose');
      haptic('lose');
    }
    applyResult(r);
  }

  // Старая функция оставлена для совместимости с openGame('ttt')
  function resetTTT() {
    buildTTTBoard();
    tttBoard = ["","","","","","","","",""];
    tttOver = true;
    document.getElementById('tttResult').textContent = 'Нажми «Начать партию»';
    document.getElementById('tttResult').className = 'result-text';
    document.getElementById('tttBtn').textContent = 'НАЧАТЬ ПАРТИЮ';
  }

  // ===== MINES =====
  let minesSize = 5;
  function setMinesSize(s, el) {
    minesSize = s;
    document.querySelectorAll('#minesSize .size-btn').forEach(b => b.classList.remove('active'));
    if (el) el.classList.add('active');
    renderMinesGrid();
  }
  function renderMinesGrid() {
    const grid = document.getElementById('minesGrid');
    grid.style.gridTemplateColumns = 'repeat(' + minesSize + ', 1fr)';
    grid.innerHTML = '';
    const total = minesSize * minesSize;
    for (let i = 0; i < total; i++) {
      const d = document.createElement('div');
      d.className = 'mine-cell';
      d.dataset.i = i;
      d.onclick = function() { minesClick(i, this); };
      grid.appendChild(d);
    }
    document.getElementById('minesResult').innerHTML = '&nbsp;';
  }
  function resetMines() {
    renderMinesGrid();
  }
  async function minesClick(i, el) {
    if (el.classList.contains('safe') || el.classList.contains('mine')) return;
    haptic('light');
    try {
      const r = await api('/api/game/mines', { stake: currentStake, pick: i, size: minesSize });
      const cells = document.querySelectorAll('#minesGrid .mine-cell');
      cells.forEach((c, idx) => {
        if (idx === r.result.mine) {
          c.classList.add('mine');
          c.textContent = '💣';
        } else if (idx === r.result.pick) {
          c.classList.add('safe');
          c.textContent = '💎';
        }
      });
      const txt = document.getElementById('minesResult');
      const net = r.result.payout - currentStake;
      if (r.result.win) {
        txt.className = 'result-text win';
        txt.textContent = 'БЕЗОПАСНО! 💎 x' + r.result.mult + ' (+' + net + ' ⭐)';
        toast('🎉 +' + net + ' звёзд', 'win');
        haptic('win');
      } else {
        txt.className = 'result-text lose';
        txt.textContent = 'МИНА! 💥 (−' + currentStake + ' ⭐)';
        toast('💀 −' + currentStake + ' звёзд', 'lose');
        haptic('lose');
      }
      applyResult(r);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    }
  }

  // ===== RPS =====
  async function playRPS(move) {
    haptic('light');
    try {
      const r = await api('/api/game/rps', { stake: currentStake, move: move });
      document.getElementById('rpsPlayer').textContent = RPS_EMOJI[r.result.player];
      document.getElementById('rpsAI').textContent = '❓';
      const cells = [document.getElementById('rpsPlayer'), document.getElementById('rpsAI')];
      cells.forEach(c => c.classList.remove('win-anim', 'lose-anim'));
      setTimeout(() => {
        document.getElementById('rpsAI').textContent = RPS_EMOJI[r.result.ai];
        const txt = document.getElementById('rpsResult');
        const net = r.result.payout - currentStake;
        if (r.result.win) {
          document.getElementById('rpsPlayer').classList.add('win-anim');
          document.getElementById('rpsAI').classList.add('lose-anim');
          txt.className = 'result-text win';
          txt.textContent = RPS_NAME[r.result.player] + ' бьёт ' + RPS_NAME[r.result.ai] + '! x2 (+' + net + ' ⭐)';
          toast('🎉 +' + net + ' звёзд', 'win');
          haptic('win');
        } else if (r.result.draw) {
          txt.className = 'result-text draw';
          txt.textContent = 'НИЧЬЯ · ставка возвращена';
          toast('Возврат ставки', 'draw');
        } else {
          document.getElementById('rpsAI').classList.add('win-anim');
          document.getElementById('rpsPlayer').classList.add('lose-anim');
          txt.className = 'result-text lose';
          txt.textContent = RPS_NAME[r.result.ai] + ' бьёт ' + RPS_NAME[r.result.player] + ' (−' + currentStake + ' ⭐)';
          toast('💀 −' + currentStake + ' звёзд', 'lose');
          haptic('lose');
        }
        applyResult(r);
      }, 250);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    }
  }

  // ===== COIN =====
  async function playCoin(guess) {
    haptic('light');
    const c = document.getElementById('coinEl');
    c.className = 'coin';
    c.textContent = '?';
    document.getElementById('coinResult').textContent = 'Подбрасываем...';
    document.getElementById('coinResult').className = 'result-text';
    try {
      const r = await api('/api/game/coin', { stake: currentStake, guess: guess });
      const side = r.result.result;
      // Анимация
      let flips = 0;
      const iv = setInterval(() => {
        c.textContent = flips % 2 === 0 ? '🦅' : '⭐';
        flips++;
        if (flips > 8) {
          clearInterval(iv);
          c.classList.add(side === 'heads' ? 'flip-heads' : 'flip-tails');
          setTimeout(() => {
            c.textContent = side === 'heads' ? '🦅' : '⭐';
            const txt = document.getElementById('coinResult');
            const net = r.result.payout - currentStake;
            if (r.result.win) {
              txt.className = 'result-text win';
              txt.textContent = 'УГАДАЛ! ' + COIN_LABEL[side] + ' · x1.95 (+' + net + ' ⭐)';
              toast('🎉 +' + net + ' звёзд', 'win');
              haptic('win');
            } else {
              txt.className = 'result-text lose';
              txt.textContent = 'НЕ УГАДАЛ · ' + COIN_LABEL[side] + ' (−' + currentStake + ' ⭐)';
              toast('💀 −' + currentStake + ' звёзд', 'lose');
              haptic('lose');
            }
            applyResult(r);
          }, 100);
        }
      }, 60);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    }
  }

  // ===== BLACKJACK =====
  function renderBJHand(cards) {
    return cards.map(v => {
      const display = v === 11 ? 'A' : v;
      const red = (v === 1 || v === 11);
      return '<span class="bj-card' + (red ? ' red' : '') + '">' + display + '</span>';
    }).join('');
  }
  async function playBlackjack() {
    const btn = document.getElementById('bjBtn');
    btn.disabled = true;
    document.getElementById('bjResult').innerHTML = '&nbsp;';
    document.getElementById('bjPlayer').innerHTML = '';
    document.getElementById('bjDealer').innerHTML = '';
    document.getElementById('bjPlayerTotal').textContent = '';
    document.getElementById('bjDealerTotal').textContent = '';
    try {
      const r = await api('/api/game/blackjack', { stake: currentStake });
      const playerEl = document.getElementById('bjPlayer');
      const dealerEl = document.getElementById('bjDealer');
      const playerTotalEl = document.getElementById('bjPlayerTotal');
      const dealerTotalEl = document.getElementById('bjDealerTotal');
      // Раздаём по одной
      const playerCards = r.result.player;
      const dealerCards = r.result.dealer;
      let pi = 0, di = 0;
      const dealOne = (cb) => {
        if (pi < playerCards.length) {
          playerEl.innerHTML += renderBJHand([playerCards[pi]]);
          pi++;
          setTimeout(dealOne, 120);
        } else if (di < dealerCards.length) {
          dealerEl.innerHTML += renderBJHand([dealerCards[di]]);
          di++;
          setTimeout(dealOne, 120);
        } else {
          // Готово — показать итоги
          playerTotalEl.textContent = r.result.player_val;
          dealerTotalEl.textContent = r.result.dealer_val;
          const txt = document.getElementById('bjResult');
          const net = r.result.payout - currentStake;
          if (r.result.kind === 'blackjack') {
            txt.className = 'result-text win';
            txt.textContent = 'БЛЭКДЖЕК! 🃏 x2.5 (+' + net + ' ⭐)';
            toast('🎉 +' + net + ' звёзд', 'win');
            haptic('win');
          } else if (r.result.kind === 'win') {
            txt.className = 'result-text win';
            txt.textContent = 'ПОБЕДА! x2 (+' + net + ' ⭐)';
            toast('🎉 +' + net + ' звёзд', 'win');
            haptic('win');
          } else if (r.result.kind === 'push') {
            txt.className = 'result-text lose';
            txt.textContent = 'НИЧЬЯ · без награды (−' + currentStake + ' ⭐)';
            toast('Ничья — ставка не возвращается', 'draw');
            haptic('light');
          } else if (r.result.kind === 'bust') {
            txt.className = 'result-text lose';
            txt.textContent = 'ПЕРЕБОР! 💥 (−' + currentStake + ' ⭐)';
            toast('💀 −' + currentStake + ' звёзд', 'lose');
            haptic('lose');
          } else {
            txt.className = 'result-text lose';
            txt.textContent = 'ПРОВАЛ (−' + currentStake + ' ⭐)';
            toast('💀 −' + currentStake + ' звёзд', 'lose');
            haptic('lose');
          }
          applyResult(r);
        }
      };
      dealOne();
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      setTimeout(() => { btn.disabled = false; }, 120 * 18 + 400);
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
