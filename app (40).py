"""
Mini App — копия игрового бота @zakaz в формате веб-приложения.

ВАЖНО: база данных — ОБЩАЯ с ботом. Все таблицы лежат в одной PostgreSQL,
       баланс пользователей синхронизирован с ботом в обе стороны.

Запуск:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    export SESSION_SECRET="change-me"
    export TELEGRAM_BOT_TOKEN="..."   # опционально, для входа через Telegram WebApp
    python app.py
    # или
    gunicorn -w 2 -b 0.0.0.0:8080 app:app
"""

import hashlib
import hmac
import json
import os
import random
import secrets
import time
import urllib.parse
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

DATABASE_URL = os.environ["DATABASE_URL"]  # обязательная переменная
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BONUS_AMOUNT = Decimal(os.environ.get("BONUS_AMOUNT", "5"))
MIN_BET = Decimal("1")


# ═══════════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 дней


# ═══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Расширяет схему бота, добавляя таблицы/колонки, нужные только мини-аппу.
    Если таблиц из бота ещё нет (мини-апп подняли раньше) — создаёт минимальные."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Минимальная таблица users — для дев-режима. В продакшене бот уже создал её.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance DECIMAL DEFAULT 0,
                    bonus_claimed BOOLEAN DEFAULT FALSE,
                    games_played INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Колонка пароля в users — для логина по username/password
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
            # Таблица коэффициентов — в боте создаётся в init_db, но в дев-режиме тоже нужна
            cur.execute("""
                CREATE TABLE IF NOT EXISTS game_coefficients (
                    id SERIAL PRIMARY KEY,
                    game_name TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    coefficient DECIMAL DEFAULT 1,
                    UNIQUE(game_name, event_name)
                )
            """)

            # Состояния активных игр (in-memory у бота лежат в ОЗУ, у нас — в БД)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS miniapp_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    game TEXT NOT NULL,
                    state JSONB NOT NULL,
                    bet DECIMAL NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_miniapp_games_user ON miniapp_games(user_id, game)")
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# USER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def upsert_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, users.username)
            """, (user_id, username))
        conn.commit()


def get_user(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()


def get_user_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
            return cur.fetchone()


def claim_bonus_if_needed(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET balance = balance + %s, bonus_claimed = TRUE "
                "WHERE user_id = %s AND COALESCE(bonus_claimed, FALSE) = FALSE",
                (BONUS_AMOUNT, user_id),
            )
        conn.commit()


def update_balance(user_id: int, delta: Decimal):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (delta, user_id),
            )
        conn.commit()


def increment_games_played(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (user_id,))
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# COEFFICIENTS
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_COEFFICIENTS = [
    ("dice", "1", 0), ("dice", "2", 0.3), ("dice", "3", 0.5),
    ("dice", "4", 1), ("dice", "5", 1.5), ("dice", "6", 3),
    ("dice", "bonus_3x6", 5),
    ("basketball", "win", 1.85),
    ("football", "win", 1.7),
    ("roulette", "777", 4), ("roulette", "fruit", 2),
    ("roulette", "series_3x777", 10),
    ("lootbox_2x2", "win", 2),
    ("lootbox_3x3", "win", 3),
    ("lootbox_6x5", "1_prize", 1), ("lootbox_6x5", "2_prize", 9),
    ("lootbox_6x5", "3_prize", 30),
    ("darts", "bullseye", 5), ("darts", "center", 2),
    ("darts", "edge", 1.5), ("darts", "miss", 0),
    ("tictactoe", "win", 2), ("tictactoe", "draw", 1),
    ("minesweeper", "safe", 1.5), ("minesweeper", "bomb", 0),
    ("rps", "win", 2), ("rps", "draw", 1),
    ("coinflip", "win", 1.95),
    ("blackjack", "win", 2), ("blackjack", "push", 1),
    ("blackjack", "blackjack", 2.5),
    ("ladder", "step_1", 1.40),
    ("ladder", "step_2", 1.89),
    ("ladder", "step_3", 2.38),
    ("ladder", "step_4", 3.50),
    ("ladder", "step_5", 7.00),
    ("ladder", "step_6", 7.00),
    ("ladder", "step_7", 7.00),
]


def init_coefficients():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for game, event, coeff in DEFAULT_COEFFICIENTS:
                cur.execute(
                    "SELECT 1 FROM game_coefficients WHERE game_name = %s AND event_name = %s",
                    (game, event),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO game_coefficients (game_name, event_name, coefficient) VALUES (%s, %s, %s)",
                        (game, event, Decimal(str(coeff))),
                    )
        conn.commit()


def get_coefficient(game_name: str, event_name: str) -> Decimal:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT coefficient FROM game_coefficients WHERE game_name = %s AND event_name = %s",
                (game_name, event_name),
            )
            row = cur.fetchone()
            return Decimal(str(row[0])) if row else Decimal("1")


def get_all_coefficients() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT game_name, event_name, coefficient FROM game_coefficients")
            rows = cur.fetchall()
    result = defaultdict(dict)
    for r in rows:
        result[r["game_name"]][r["event_name"]] = float(r["coefficient"])
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def current_user() -> Optional[dict]:
    uid = session.get("user_id")
    if not uid:
        return None
    return get_user(uid)


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def validate_telegram_init_data(init_data: str) -> Optional[dict]:
    """Валидирует подпись Telegram WebApp initData. Возвращает dict с user или None."""
    if not TELEGRAM_BOT_TOKEN or not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_value = parsed.pop("hash", None)
        if not hash_value:
            return None
        data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_value):
            return None
        user_json = parsed.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ACTIVE GAMES (miniapp_games)
# ═══════════════════════════════════════════════════════════════════════════════

def save_game(user_id: int, game: str, state: dict, bet: Decimal):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM miniapp_games WHERE user_id = %s AND game = %s", (user_id, game))
            cur.execute(
                "INSERT INTO miniapp_games (user_id, game, state, bet) VALUES (%s, %s, %s, %s)",
                (user_id, game, json.dumps(state, default=str), bet),
            )
        conn.commit()


def load_game(user_id: int, game: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM miniapp_games WHERE user_id = %s AND game = %s", (user_id, game))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "state": row["state"] if isinstance(row["state"], dict) else json.loads(row["state"]),
        "bet": Decimal(str(row["bet"])),
    }


def delete_game(user_id: int, game: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM miniapp_games WHERE user_id = %s AND game = %s", (user_id, game))
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# BLACKJACK HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def create_deck() -> list:
    deck = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def card_value(card) -> int:
    r = card["rank"]
    if r in ("J", "Q", "K"):
        return 10
    if r == "A":
        return 11
    return int(r)


def hand_value(hand) -> int:
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c["rank"] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_hand(hand, hide_first: bool = False) -> str:
    if hide_first:
        return f"🂠 {hand[1]['rank']}{hand[1]['suit']}"
    return " ".join(f"{c['rank']}{c['suit']}" for c in hand)


# ═══════════════════════════════════════════════════════════════════════════════
# TICTACTOE
# ═══════════════════════════════════════════════════════════════════════════════

def check_winner(board) -> Optional[str]:
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def get_best_move(board, bot_symbol: str, player_symbol: str) -> int:
    # minimax
    def minimax(state, depth, is_max):
        winner = check_winner(state)
        if winner == bot_symbol:
            return 10 - depth
        if winner == player_symbol:
            return depth - 10
        if winner == "draw":
            return 0
        scores = []
        for i, v in enumerate(state):
            if not v:
                state[i] = bot_symbol if is_max else player_symbol
                scores.append(minimax(state, depth + 1, not is_max))
                state[i] = None
        return max(scores) if is_max else min(scores)

    best_score = -999
    best_move = None
    for i, v in enumerate(board):
        if not v:
            board[i] = bot_symbol
            score = minimax(board, 0, False)
            board[i] = None
            if score > best_score:
                best_score = score
                best_move = i
    return best_move if best_move is not None else next((i for i, v in enumerate(board) if not v), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# LADDER
# ═══════════════════════════════════════════════════════════════════════════════

LADDER_COLS = 7
LADDER_ROWS = 4
LADDER_MINES_PER_COLUMN = [2, 2, 2, 3, 3, 3, 4]
LADDER_STEP_MULTIPLIERS = [Decimal("1.40"), Decimal("1.89"), Decimal("2.38"), Decimal("3.50"),
                           Decimal("7.00"), Decimal("7.00"), Decimal("7.00")]


def ladder_cumulative_multiplier(step: int) -> Decimal:
    mult = Decimal("1")
    for i in range(min(step, LADDER_COLS)):
        mult *= LADDER_STEP_MULTIPLIERS[i]
    return mult.quantize(Decimal("0.01"))


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — PAGE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — AUTH API
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/register")
def register():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip().lstrip("@")
    password = data.get("password") or ""
    if not username or len(username) < 3:
        return jsonify({"error": "username_min_3"}), 400
    if len(password) < 4:
        return jsonify({"error": "password_min_4"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "username_taken"}), 409

    # Генерируем user_id. ВАЖНО: должен совпадать с ID в боте, если хотите
    # общий баланс. Здесь создаём «локального» user_id, отдельного от бота.
    # Если хочется связаться с ботом — используйте /api/auth/telegram.
    user_id = secrets.randbits(40) | (1 << 39)  # > 2^39
    password_hash = generate_password_hash(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, username, password_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, password_hash = EXCLUDED.password_hash",
                (user_id, username, password_hash),
            )
        conn.commit()
    upsert_user(user_id, username)
    claim_bonus_if_needed(user_id)
    session.permanent = True
    session["user_id"] = user_id
    return jsonify({"ok": True, "user": serialize_user(user_id)})


@app.post("/api/auth/login")
def login():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip().lstrip("@")
    password = data.get("password") or ""
    user = get_user_by_username(username)
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "invalid_credentials"}), 401
    session.permanent = True
    session["user_id"] = user["user_id"]
    return jsonify({"ok": True, "user": serialize_user(user["user_id"])})


@app.post("/api/auth/telegram")
def auth_telegram():
    """Авторизация через Telegram WebApp initData. Привязывается к user_id из Telegram."""
    data = request.get_json(force=True) or {}
    init_data = data.get("initData", "")
    tg_user = validate_telegram_init_data(init_data)
    if not tg_user:
        return jsonify({"error": "invalid_init_data"}), 400

    tg_id = int(tg_user["id"])
    username = tg_user.get("username") or tg_user.get("first_name") or f"tg{tg_id}"
    upsert_user(tg_id, username)
    claim_bonus_if_needed(tg_id)
    session.permanent = True
    session["user_id"] = tg_id
    return jsonify({"ok": True, "user": serialize_user(tg_id), "telegram": True})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"user": serialize_user(user["user_id"])})


def serialize_user(user_id: int) -> dict:
    u = get_user(user_id) or {}
    return {
        "user_id": user_id,
        "username": u.get("username"),
        "balance": float(u.get("balance") or 0),
        "games_played": int(u.get("games_played") or 0),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — TOPUP / PROMO (заглушки для мини-аппа)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/topup")
@login_required
def topup():
    """
    Заглушка пополнения. В боте пополнение идёт через Telegram Stars / Crypto.
    Здесь оставлен dev-эндпоинт: ручное начисление для теста.
    В продакшене отключите его или оберните в проверку прав админа.
    """
    data = request.get_json(force=True) or {}
    try:
        amount = Decimal(str(data.get("amount", "0")))
    except Exception:
        return jsonify({"error": "bad_amount"}), 400
    if amount <= 0 or amount > Decimal("100000"):
        return jsonify({"error": "bad_amount"}), 400
    update_balance(session["user_id"], amount)
    return jsonify({"ok": True, "user": serialize_user(session["user_id"])})


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES — GAMES
# ═══════════════════════════════════════════════════════════════════════════════

def _check_balance(uid: int, bet: Decimal):
    user = get_user(uid)
    if not user or Decimal(user["balance"]) < bet:
        return False
    return True


# ─── DICE ────────────────────────────────────────────────────────────────────
@app.post("/api/games/dice")
@login_required
def game_dice():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET:
        return jsonify({"error": "min_bet"}), 400
    if not _check_balance(uid, bet):
        return jsonify({"error": "insufficient_funds"}), 400

    update_balance(uid, -bet)
    val = random.randint(1, 6)
    coeff = get_coefficient("dice", str(val))
    win = (bet * coeff).quantize(Decimal("0.01"))

    history = list(session.get("dice_history") or [])
    history.append({"val": val, "bet": float(bet)})
    if len(history) > 3:
        history.pop(0)

    bonus = Decimal("0")
    if len(history) == 3 and all(h["val"] == 6 for h in history):
        bonus_coeff = get_coefficient("dice", "bonus_3x6")
        avg = Decimal(str(sum(h["bet"] for h in history) / 3))
        bonus = (avg * bonus_coeff).quantize(Decimal("0.01"))
        history = []

    session["dice_history"] = history
    if win > 0:
        update_balance(uid, win)
    if bonus > 0:
        update_balance(uid, bonus)
    increment_games_played(uid)

    return jsonify({
        "roll": val, "coefficient": float(coeff),
        "win": float(win), "bonus": float(bonus),
        "user": serialize_user(uid),
    })


# ─── BASKETBALL ──────────────────────────────────────────────────────────────
@app.post("/api/games/basketball")
@login_required
def game_basketball():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    # Telegram basketball dice: 4,5 = попадание
    val = random.choices([1, 2, 3, 4, 5], weights=[1, 1, 1, 2, 1])[0]
    win = Decimal("0")
    if val in (4, 5):
        coeff = get_coefficient("basketball", "win")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"roll": val, "win": float(win), "user": serialize_user(uid)})


# ─── FOOTBALL ────────────────────────────────────────────────────────────────
@app.post("/api/games/football")
@login_required
def game_football():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    # Telegram football dice: 3,4,5 = гол
    val = random.choices([1, 2, 3, 4, 5], weights=[1, 1, 2, 2, 1])[0]
    win = Decimal("0")
    if val in (3, 4, 5):
        coeff = get_coefficient("football", "win")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"roll": val, "win": float(win), "user": serialize_user(uid)})


# ─── ROULETTE ───────────────────────────────────────────────────────────────
@app.post("/api/games/roulette")
@login_required
def game_roulette():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    # Telegram 🎰: 1=bar, 22=вишня, 43=лимон, 64=777
    val = random.choices([1, 22, 43, 64, 0, 16, 32, 48], weights=[2, 2, 2, 1, 1, 1, 1, 1])[0]
    win = Decimal("0")
    bonus = Decimal("0")
    series = False
    label = "мимо"
    if val == 64:
        coeff = get_coefficient("roulette", "777")
        win = (bet * coeff).quantize(Decimal("0.01"))
        label = "777!"
        history = list(session.get("roulette_history") or [])
        history.append({"val": val, "bet": float(bet)})
        if len(history) > 3:
            history.pop(0)
        if len(history) == 3 and all(h["val"] == 64 for h in history):
            series_coeff = get_coefficient("roulette", "series_3x777")
            avg = Decimal(str(sum(h["bet"] for h in history) / 3))
            bonus = (avg * series_coeff).quantize(Decimal("0.01"))
            series = True
            history = []
        session["roulette_history"] = history
    elif val in (1, 22, 43):
        coeff = get_coefficient("roulette", "fruit")
        win = (bet * coeff).quantize(Decimal("0.01"))
        labels_fruit = {1: "BAR", 22: "Вишня", 43: "Лимон"}
        label = labels_fruit.get(val, "Фрукт")
        session["roulette_history"] = []
    if win > 0:
        update_balance(uid, win)
    if bonus > 0:
        update_balance(uid, bonus)
    increment_games_played(uid)
    return jsonify({"roll": val, "label": label, "win": float(win),
                    "bonus": float(bonus), "series": series,
                    "user": serialize_user(uid)})


# ─── DARTS ──────────────────────────────────────────────────────────────────
@app.post("/api/games/darts")
@login_required
def game_darts():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    # Telegram 🎯: 1=мимо, 2-4=попадание, 5=центр, 6=яблочко
    val = random.choices([1, 2, 3, 4, 5, 6], weights=[3, 2, 2, 2, 1, 1])[0]
    if val == 6:
        event, key = "Яблочко!", "bullseye"
    elif val == 5:
        event, key = "Центр!", "center"
    elif val in (2, 3, 4):
        event, key = "Попадание!", "edge"
    else:
        event, key = "Мимо!", "miss"
    coeff = get_coefficient("darts", key)
    win = (bet * coeff).quantize(Decimal("0.01"))
    if win > 0:
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"roll": val, "event": event, "win": float(win), "user": serialize_user(uid)})


# ─── COINFLIP ───────────────────────────────────────────────────────────────
@app.post("/api/games/coinflip")
@login_required
def game_coinflip():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
        choice = data.get("choice", "")
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400
    if choice not in ("heads", "tails"):
        return jsonify({"error": "bad_choice"}), 400

    update_balance(uid, -bet)
    result = random.choice(["heads", "tails"])
    win = Decimal("0")
    if choice == result:
        coeff = get_coefficient("coinflip", "win")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"result": result, "win": float(win), "user": serialize_user(uid)})


# ─── RPS ────────────────────────────────────────────────────────────────────
@app.post("/api/games/rps")
@login_required
def game_rps():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
        choice = data.get("choice", "")
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400
    if choice not in ("rock", "scissors", "paper"):
        return jsonify({"error": "bad_choice"}), 400

    update_balance(uid, -bet)
    bot = random.choice(["rock", "scissors", "paper"])
    win_against = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    outcome = "lose"
    win = Decimal("0")
    if choice == bot:
        outcome = "draw"
        coeff = get_coefficient("rps", "draw")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
    elif win_against[choice] == bot:
        outcome = "win"
        coeff = get_coefficient("rps", "win")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"choice": choice, "bot": bot, "outcome": outcome, "win": float(win), "user": serialize_user(uid)})


# ─── BLACKJACK ──────────────────────────────────────────────────────────────
@app.post("/api/games/blackjack/start")
@login_required
def bj_start():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    increment_games_played(uid)
    deck = create_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    state = {"deck": deck, "player": player, "dealer": dealer, "doubled": False}
    save_game(uid, "blackjack", state, bet)

    player_val = hand_value(player)
    dealer_val = hand_value(dealer)

    if player_val == 21:
        delete_game(uid, "blackjack")
        if dealer_val == 21:
            coeff = get_coefficient("blackjack", "push")
            refund = (bet * coeff).quantize(Decimal("0.01"))
            update_balance(uid, refund)
            return jsonify({"finished": True, "result": "push_blackjack",
                            "player": player, "dealer": dealer,
                            "player_val": player_val, "dealer_val": dealer_val,
                            "win": float(refund), "user": serialize_user(uid)})
        coeff = get_coefficient("blackjack", "blackjack")
        prize = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, prize)
        return jsonify({"finished": True, "result": "blackjack",
                        "player": player, "dealer": dealer,
                        "player_val": player_val, "dealer_val": dealer_val,
                        "win": float(prize), "user": serialize_user(uid)})

    return jsonify({
        "finished": False,
        "player": player, "dealer": dealer,
        "player_val": player_val, "dealer_val": dealer_val,
        "user": serialize_user(uid),
    })


@app.post("/api/games/blackjack/action")
@login_required
def bj_action():
    uid = session["user_id"]
    g = load_game(uid, "blackjack")
    if not g:
        return jsonify({"error": "no_game"}), 400
    action = (request.get_json(force=True) or {}).get("action", "")
    state = g["state"]
    bet = g["bet"]
    deck = state["deck"]
    player = state["player"]
    dealer = state["dealer"]

    if action == "hit":
        player.append(deck.pop())
        pv = hand_value(player)
        if pv > 21:
            delete_game(uid, "blackjack")
            return jsonify({"finished": True, "result": "bust",
                            "player": player, "dealer": dealer,
                            "player_val": pv, "dealer_val": hand_value(dealer),
                            "win": 0.0, "user": serialize_user(uid)})
        state["player"] = player
        state["deck"] = deck
        save_game(uid, "blackjack", state, bet)
        return jsonify({"finished": False, "player": player, "dealer": dealer,
                        "player_val": pv, "dealer_val": hand_value(dealer),
                        "user": serialize_user(uid)})

    if action == "double":
        if state.get("doubled") or len(player) != 2:
            return jsonify({"error": "cannot_double"}), 400
        if not _check_balance(uid, bet):
            return jsonify({"error": "insufficient_funds"}), 400
        update_balance(uid, -bet)
        bet = bet * 2
        state["doubled"] = True
        player.append(deck.pop())
        pv = hand_value(player)
        state["player"] = player
        state["deck"] = deck
        if pv > 21:
            delete_game(uid, "blackjack")
            return jsonify({"finished": True, "result": "bust",
                            "player": player, "dealer": dealer,
                            "player_val": pv, "dealer_val": hand_value(dealer),
                            "win": 0.0, "user": serialize_user(uid)})
        action = "stand"

    if action == "stand":
        while hand_value(dealer) < 17:
            dealer.append(deck.pop())
        pv = hand_value(player)
        dv = hand_value(dealer)
        delete_game(uid, "blackjack")
        if dv > 21:
            coeff = get_coefficient("blackjack", "win")
            win = (bet * coeff).quantize(Decimal("0.01"))
            update_balance(uid, win)
            return jsonify({"finished": True, "result": "dealer_bust",
                            "player": player, "dealer": dealer,
                            "player_val": pv, "dealer_val": dv, "win": float(win),
                            "user": serialize_user(uid)})
        if pv > dv:
            coeff = get_coefficient("blackjack", "win")
            win = (bet * coeff).quantize(Decimal("0.01"))
            update_balance(uid, win)
            return jsonify({"finished": True, "result": "win",
                            "player": player, "dealer": dealer,
                            "player_val": pv, "dealer_val": dv, "win": float(win),
                            "user": serialize_user(uid)})
        if pv == dv:
            coeff = get_coefficient("blackjack", "push")
            refund = (bet * coeff).quantize(Decimal("0.01"))
            update_balance(uid, refund)
            return jsonify({"finished": True, "result": "push",
                            "player": player, "dealer": dealer,
                            "player_val": pv, "dealer_val": dv, "win": float(refund),
                            "user": serialize_user(uid)})
        return jsonify({"finished": True, "result": "lose",
                        "player": player, "dealer": dealer,
                        "player_val": pv, "dealer_val": dv, "win": 0.0,
                        "user": serialize_user(uid)})

    return jsonify({"error": "bad_action"}), 400


# ─── TICTACTOE ──────────────────────────────────────────────────────────────
@app.post("/api/games/tictactoe/start")
@login_required
def ttt_start():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    update_balance(uid, -bet)
    increment_games_played(uid)
    board = [None] * 9
    bot_first = random.choice([True, False])
    state = {"board": board, "player_symbol": "X", "bot_symbol": "O"}
    if bot_first:
        move = get_best_move(board[:], "O", "X")
        board[move] = "O"
    save_game(uid, "tictactoe", state, bet)
    return jsonify({"board": board, "bot_first": bot_first, "user": serialize_user(uid)})


@app.post("/api/games/tictactoe/move")
@login_required
def ttt_move():
    uid = session["user_id"]
    g = load_game(uid, "tictactoe")
    if not g:
        return jsonify({"error": "no_game"}), 400
    data = request.get_json(force=True) or {}
    try:
        idx = int(data.get("idx", -1))
    except Exception:
        return jsonify({"error": "bad_idx"}), 400
    if not (0 <= idx < 9):
        return jsonify({"error": "bad_idx"}), 400

    state = g["state"]
    board = state["board"]
    bet = g["bet"]
    if board[idx]:
        return jsonify({"error": "cell_taken"}), 400
    board[idx] = "X"
    winner = check_winner(board)
    if winner == "X":
        coeff = get_coefficient("tictactoe", "win")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
        delete_game(uid, "tictactoe")
        return jsonify({"finished": True, "result": "win", "board": board, "win": float(win), "user": serialize_user(uid)})
    if winner == "draw":
        coeff = get_coefficient("tictactoe", "draw")
        refund = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, refund)
        delete_game(uid, "tictactoe")
        return jsonify({"finished": True, "result": "draw", "board": board, "win": float(refund), "user": serialize_user(uid)})
    # bot move
    bm = get_best_move(board[:], "O", "X")
    if bm is not None:
        board[bm] = "O"
    winner = check_winner(board)
    if winner == "O":
        delete_game(uid, "tictactoe")
        return jsonify({"finished": True, "result": "lose", "board": board, "win": 0.0, "user": serialize_user(uid)})
    if winner == "draw":
        coeff = get_coefficient("tictactoe", "draw")
        refund = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, refund)
        delete_game(uid, "tictactoe")
        return jsonify({"finished": True, "result": "draw", "board": board, "win": float(refund), "user": serialize_user(uid)})
    save_game(uid, "tictactoe", state, bet)
    return jsonify({"finished": False, "board": board, "user": serialize_user(uid)})


# ─── MINESWEEPER ────────────────────────────────────────────────────────────
@app.post("/api/games/minesweeper/start")
@login_required
def ms_start():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
        size = int(data.get("size", 5))
    except Exception:
        return jsonify({"error": "bad_input"}), 400
    if size not in (5, 7):
        size = 5
    bombs = 6 if size == 5 else 12
    max_safe = bombs
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400
    update_balance(uid, -bet)
    increment_games_played(uid)
    grid = [False] * (size * size)
    for b in random.sample(range(size * size), bombs):
        grid[b] = True
    state = {"grid": grid, "revealed": [], "size": size, "safe_count": 0, "max_safe": max_safe}
    save_game(uid, "minesweeper", state, bet)
    return jsonify({"size": size, "bombs": bombs, "max_safe": max_safe, "user": serialize_user(uid)})


@app.post("/api/games/minesweeper/reveal")
@login_required
def ms_reveal():
    uid = session["user_id"]
    g = load_game(uid, "minesweeper")
    if not g:
        return jsonify({"error": "no_game"}), 400
    data = request.get_json(force=True) or {}
    try:
        idx = int(data.get("idx", -1))
    except Exception:
        return jsonify({"error": "bad_idx"}), 400
    state = g["state"]
    bet = g["bet"]
    size = state["size"]
    if not (0 <= idx < size * size):
        return jsonify({"error": "bad_idx"}), 400
    if idx in state["revealed"]:
        return jsonify({"error": "already_revealed"}), 400
    state["revealed"].append(idx)
    if state["grid"][idx]:
        # bomb
        delete_game(uid, "minesweeper")
        return jsonify({"finished": True, "result": "bomb", "revealed": state["revealed"],
                        "grid": state["grid"], "user": serialize_user(uid)})
    state["safe_count"] += 1
    if state["safe_count"] >= state["max_safe"]:
        coeff = get_coefficient("minesweeper", "safe")
        win = (bet * coeff).quantize(Decimal("0.01"))
        update_balance(uid, win)
        delete_game(uid, "minesweeper")
        return jsonify({"finished": True, "result": "win", "revealed": state["revealed"],
                        "grid": state["grid"], "safe_count": state["safe_count"],
                        "win": float(win), "user": serialize_user(uid)})
    save_game(uid, "minesweeper", state, bet)
    return jsonify({"finished": False, "revealed": state["revealed"],
                    "safe_count": state["safe_count"], "max_safe": state["max_safe"],
                    "user": serialize_user(uid)})


# ─── LADDER ─────────────────────────────────────────────────────────────────
@app.post("/api/games/ladder/start")
@login_required
def ladder_start():
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400
    update_balance(uid, -bet)
    increment_games_played(uid)
    grid = []
    for col in range(LADDER_COLS):
        n_mines = LADDER_MINES_PER_COLUMN[col]
        cells = [True] * n_mines + [False] * (LADDER_ROWS - n_mines)
        random.shuffle(cells)
        grid.append(cells)
    state = {"grid": grid, "current_step": 0, "is_finished": False}
    save_game(uid, "ladder", state, bet)
    return jsonify({"cols": LADDER_COLS, "rows": LADDER_ROWS,
                    "mines_per_column": LADDER_MINES_PER_COLUMN,
                    "step_multipliers": [float(m) for m in LADDER_STEP_MULTIPLIERS],
                    "user": serialize_user(uid)})


@app.post("/api/games/ladder/open")
@login_required
def ladder_open():
    uid = session["user_id"]
    g = load_game(uid, "ladder")
    if not g:
        return jsonify({"error": "no_game"}), 400
    data = request.get_json(force=True) or {}
    try:
        col = int(data.get("col", -1))
        row = int(data.get("row", -1))
    except Exception:
        return jsonify({"error": "bad_input"}), 400
    state = g["state"]
    bet = g["bet"]
    if state.get("is_finished"):
        return jsonify({"error": "finished"}), 400
    if col != state["current_step"]:
        return jsonify({"error": "wrong_column"}), 400
    if not (0 <= col < LADDER_COLS and 0 <= row < LADDER_ROWS):
        return jsonify({"error": "bad_cell"}), 400
    is_mine = state["grid"][col][row]
    if is_mine:
        state["is_finished"] = True
        delete_game(uid, "ladder")
        return jsonify({"finished": True, "result": "mine",
                        "grid": state["grid"], "user": serialize_user(uid)})
    state["current_step"] += 1
    new_mult = ladder_cumulative_multiplier(state["current_step"])
    win_if_take = (bet * new_mult).quantize(Decimal("0.01"))
    if state["current_step"] >= LADDER_COLS:
        update_balance(uid, win_if_take)
        state["is_finished"] = True
        delete_game(uid, "ladder")
        return jsonify({"finished": True, "result": "complete",
                        "win": float(win_if_take),
                        "multiplier": float(new_mult),
                        "grid": state["grid"], "user": serialize_user(uid)})
    save_game(uid, "ladder", state, bet)
    return jsonify({"finished": False, "current_step": state["current_step"],
                    "multiplier": float(new_mult), "win_if_take": float(win_if_take),
                    "user": serialize_user(uid)})


@app.post("/api/games/ladder/take")
@login_required
def ladder_take():
    uid = session["user_id"]
    g = load_game(uid, "ladder")
    if not g:
        return jsonify({"error": "no_game"}), 400
    state = g["state"]
    bet = g["bet"]
    if state.get("is_finished") or state["current_step"] == 0:
        return jsonify({"error": "cannot_take"}), 400
    mult = ladder_cumulative_multiplier(state["current_step"])
    win = (bet * mult).quantize(Decimal("0.01"))
    update_balance(uid, win)
    delete_game(uid, "ladder")
    return jsonify({"finished": True, "result": "cashed_out",
                    "win": float(win), "multiplier": float(mult),
                    "user": serialize_user(uid)})


# ─── LOOTBOX ────────────────────────────────────────────────────────────────
@app.post("/api/games/lootbox/<size>")
@login_required
def game_lootbox(size):
    if size not in ("2x2", "3x3"):
        return jsonify({"error": "bad_size"}), 400
    uid = session["user_id"]
    data = request.get_json(force=True) or {}
    try:
        bet = Decimal(str(data.get("bet", "0")))
    except Exception:
        return jsonify({"error": "bad_bet"}), 400
    if bet < MIN_BET or not _check_balance(uid, bet):
        return jsonify({"error": "bet"}), 400

    n = 4 if size == "2x2" else 9
    win_box = random.randint(0, n - 1)
    chosen = random.randint(0, n - 1)
    won = chosen == win_box
    coeff = get_coefficient(f"lootbox_{size}", "win")
    win = (bet * coeff).quantize(Decimal("0.01")) if won else Decimal("0")
    update_balance(uid, -bet)
    if won:
        update_balance(uid, win)
    increment_games_played(uid)
    return jsonify({"size": size, "chosen": chosen, "win_box": win_box,
                    "won": won, "win": float(win), "user": serialize_user(uid)})


# ─── COEFFICIENTS (read-only) ───────────────────────────────────────────────
@app.get("/api/coefficients")
@login_required
def coefficients():
    return jsonify(get_all_coefficients())


# ═══════════════════════════════════════════════════════════════════════════════
# INDEX HTML
# ═══════════════════════════════════════════════════════════════════════════════

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
  <title>Mini App Games</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #0f1117; --panel: #1a1d28; --panel2: #232735; --line: #2c3142;
      --text: #e6e8ee; --muted: #8b91a5; --accent: #6c7cff; --accent2: #5466ff;
      --green: #2ecc71; --red: #ff5b6b; --gold: #f5c518; --cyan: #4dd0e1;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      overscroll-behavior: none; }
    body { min-height: 100vh; padding-bottom: 80px; }
    .container { max-width: 540px; margin: 0 auto; padding: 16px; }
    .topbar { display: flex; align-items: center; justify-content: space-between;
      padding: 14px 16px; background: var(--panel); border-bottom: 1px solid var(--line);
      position: sticky; top: 0; z-index: 50; }
    .topbar h1 { font-size: 18px; margin: 0; }
    .balance { background: var(--panel2); border: 1px solid var(--line);
      border-radius: 12px; padding: 6px 12px; font-weight: 600; color: var(--gold); }
    .tabs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin: 16px 0 12px; }
    .tab { background: var(--panel); border: 1px solid var(--line);
      color: var(--muted); padding: 10px 6px; border-radius: 12px; font-size: 12px;
      cursor: pointer; text-align: center; }
    .tab.active { background: var(--accent); color: white; border-color: var(--accent); }
    .card { background: var(--panel); border: 1px solid var(--line);
      border-radius: 16px; padding: 16px; margin-bottom: 12px; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row.between { justify-content: space-between; }
    .btn { display: inline-flex; align-items: center; justify-content: center;
      background: var(--accent); color: white; border: 0; padding: 12px 18px;
      border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer;
      width: 100%; }
    .btn.ghost { background: var(--panel2); color: var(--text); }
    .btn.green { background: var(--green); }
    .btn.red { background: var(--red); }
    .btn.gold { background: var(--gold); color: #1a1a1a; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .game-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .game { background: var(--panel); border: 1px solid var(--line);
      border-radius: 16px; padding: 16px 12px; text-align: center; cursor: pointer;
      transition: transform 0.1s; }
    .game:active { transform: scale(0.97); }
    .game .emoji { font-size: 36px; display: block; margin-bottom: 6px; }
    .game .name { font-weight: 600; }
    input { width: 100%; background: var(--panel2); color: var(--text);
      border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
      font-size: 16px; outline: none; }
    input:focus { border-color: var(--accent); }
    label { display: block; font-size: 12px; color: var(--muted); margin: 12px 0 4px; }
    .toast { position: fixed; left: 50%; transform: translateX(-50%);
      bottom: 90px; background: var(--panel2); border: 1px solid var(--line);
      padding: 10px 16px; border-radius: 12px; z-index: 100; max-width: 90vw;
      text-align: center; font-size: 14px; }
    .popup { position: fixed; inset: 0; background: rgba(0,0,0,0.6);
      z-index: 90; display: flex; align-items: center; justify-content: center; padding: 20px; }
    .popup .box { background: var(--panel); border: 1px solid var(--line);
      border-radius: 18px; padding: 20px; width: 100%; max-width: 400px; }
    .popup h2 { margin: 0 0 12px; }
    .popup .close { float: right; cursor: pointer; color: var(--muted); font-size: 22px; }
    .big-emoji { font-size: 70px; text-align: center; margin: 12px 0; }
    .result-win { color: var(--green); font-weight: 600; }
    .result-lose { color: var(--red); font-weight: 600; }
    .muted { color: var(--muted); font-size: 13px; }
    .coef { font-size: 12px; color: var(--muted); }
    .pill { display: inline-block; padding: 4px 10px; border-radius: 999px;
      background: var(--panel2); border: 1px solid var(--line); font-size: 12px; }
    .quick-bets { display: flex; gap: 6px; margin-top: 8px; }
    .quick-bets .btn { padding: 8px 10px; font-size: 13px; width: auto; flex: 1; }

    /* Сапёр */
    .ms-grid { display: grid; gap: 4px; margin: 12px 0; }
    .ms-cell { aspect-ratio: 1; background: var(--panel2);
      border: 1px solid var(--line); border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-size: 22px; cursor: pointer; }
    .ms-cell.revealed.safe { background: #1f3a2a; }
    .ms-cell.revealed.bomb { background: #4a1f25; }

    /* Крестики-нолики */
    .ttt-grid { display: grid; grid-template-columns: repeat(3, 1fr);
      gap: 6px; margin: 12px 0; max-width: 320px; margin-left: auto; margin-right: auto; }
    .ttt-cell { aspect-ratio: 1; background: var(--panel2);
      border: 1px solid var(--line); border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 36px; font-weight: 700; cursor: pointer; color: var(--text); }
    .ttt-cell.disabled { cursor: default; }

    /* Лесенка */
    .ladder { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap; margin: 12px 0; }
    .ladder-col { display: grid; grid-template-rows: repeat(4, 1fr); gap: 4px; }
    .ladder-cell { width: 50px; height: 50px; background: var(--panel2);
      border: 1px solid var(--line); border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; font-size: 18px; }
    .ladder-cell.done { opacity: 0.5; }
    .ladder-cell.locked { opacity: 0.3; cursor: not-allowed; }

    /* Блэкджек */
    .cards { font-size: 28px; padding: 10px; background: var(--panel2);
      border-radius: 10px; margin: 8px 0; min-height: 50px;
      display: flex; flex-wrap: wrap; gap: 6px; }
    .bj-actions { display: flex; gap: 8px; margin-top: 10px; }
    .bj-actions .btn { flex: 1; }

    /* Лутбокс */
    .lootbox { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }
    .lootbox.size-2 { grid-template-columns: repeat(2, 1fr); max-width: 240px; margin-left: auto; margin-right: auto; }
    .loot-cell { aspect-ratio: 1; background: var(--panel2);
      border: 1px solid var(--line); border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 32px; }
    .loot-cell.win { background: #1f3a2a; }
    .loot-cell.lose { background: #4a1f25; }
  </style>
</head>
<body>

<!-- AUTH -->
<div id="auth-screen" class="container" style="padding-top:40px;">
  <div class="card">
    <h2 style="margin:0 0 4px;">🎮 Mini App Games</h2>
    <p class="muted" style="margin:0 0 16px;">Войди, чтобы играть. Баланс общий с ботом.</p>
    <div id="auth-tabs" class="tabs" style="grid-template-columns: 1fr 1fr;">
      <div class="tab active" data-tab="login">Вход</div>
      <div class="tab" data-tab="register">Регистрация</div>
    </div>
    <div id="form-login">
      <label>Username</label>
      <input id="login-username" placeholder="@username" autocomplete="username">
      <label>Пароль</label>
      <input id="login-password" type="password" placeholder="••••••" autocomplete="current-password">
      <div style="height:14px"></div>
      <button class="btn" onclick="doLogin()">Войти</button>
    </div>
    <div id="form-register" style="display:none">
      <label>Username (минимум 3 символа)</label>
      <input id="reg-username" placeholder="@username" autocomplete="username">
      <label>Пароль (минимум 4 символа)</label>
      <input id="reg-password" type="password" placeholder="••••••" autocomplete="new-password">
      <div class="muted" style="margin-top:6px;">При регистрации начислим бонус ⭐</div>
      <div style="height:14px"></div>
      <button class="btn" onclick="doRegister()">Создать аккаунт</button>
    </div>
    <div style="margin-top:14px; text-align:center;">
      <button class="btn ghost" id="btn-tg" style="display:none" onclick="doTgLogin()">🔵 Войти через Telegram</button>
    </div>
    <div id="auth-error" class="result-lose" style="margin-top:10px; text-align:center;"></div>
  </div>
</div>

<!-- APP -->
<div id="app-screen" style="display:none">
  <div class="topbar">
    <h1>🎮 Mini App</h1>
    <div class="balance" id="balance-pill">⭐ 0</div>
  </div>
  <div class="container">
    <div class="tabs">
      <div class="tab active" data-tab="games">Игры</div>
      <div class="tab" data-tab="profile">Профиль</div>
      <div class="tab" data-tab="topup">Кошелёк</div>
      <div class="tab" data-tab="info">Инфо</div>
    </div>

    <!-- GAMES -->
    <div id="page-games">
      <div class="game-grid">
        <div class="game" data-game="dice">     <span class="emoji">🎲</span><span class="name">Кубик</span></div>
        <div class="game" data-game="basketball"><span class="emoji">🏀</span><span class="name">Баскетбол</span></div>
        <div class="game" data-game="football">  <span class="emoji">⚽</span><span class="name">Футбол</span></div>
        <div class="game" data-game="roulette">  <span class="emoji">🎰</span><span class="name">Рулетка</span></div>
        <div class="game" data-game="darts">     <span class="emoji">🎯</span><span class="name">Дартс</span></div>
        <div class="game" data-game="coinflip">  <span class="emoji">🪙</span><span class="name">Орёл/Решка</span></div>
        <div class="game" data-game="rps">       <span class="emoji">✌️</span><span class="name">КНБ</span></div>
        <div class="game" data-game="tictactoe"> <span class="emoji">❌</span><span class="name">Крестики</span></div>
        <div class="game" data-game="minesweeper"><span class="emoji">💣</span><span class="name">Сапёр</span></div>
        <div class="game" data-game="blackjack"> <span class="emoji">🃏</span><span class="name">Блэкджек</span></div>
        <div class="game" data-game="ladder">    <span class="emoji">📊</span><span class="name">Лесенка</span></div>
        <div class="game" data-game="lootbox">   <span class="emoji">📦</span><span class="name">Лутбокс</span></div>
      </div>
    </div>

    <!-- PROFILE -->
    <div id="page-profile" style="display:none">
      <div class="card">
        <h2 style="margin:0 0 8px;">👤 Профиль</h2>
        <div class="row between"><span class="muted">Username</span><b id="prof-username">—</b></div>
        <div class="row between"><span class="muted">User ID</span><b id="prof-uid">—</b></div>
        <div class="row between"><span class="muted">Баланс</span><b id="prof-balance" style="color:var(--gold)">0 ⭐</b></div>
        <div class="row between"><span class="muted">Игр сыграно</span><b id="prof-games">0</b></div>
        <div style="height:14px"></div>
        <button class="btn ghost" onclick="doLogout()">Выйти</button>
      </div>
    </div>

    <!-- TOPUP -->
    <div id="page-topup" style="display:none">
      <div class="card">
        <h2 style="margin:0 0 8px;">💰 Кошелёк</h2>
        <p class="muted" style="margin:0 0 12px;">
          Пополнение в мини-аппе в демо-режиме недоступно.<br>
          Используйте бота для оплаты ⭐ или криптой. Баланс общий.
        </p>
        <label>Сумма пополнения (тест)</label>
        <input id="topup-amount" type="number" min="1" placeholder="100">
        <div style="height:8px"></div>
        <button class="btn gold" onclick="doTopup()">Зачислить (dev)</button>
        <div class="muted" style="margin-top:6px;">Кнопка только для разработки. В продакшене отключите эндпоинт /api/topup.</div>
      </div>
    </div>

    <!-- INFO -->
    <div id="page-info" style="display:none">
      <div class="card">
        <h2 style="margin:0 0 8px;">ℹ️ Инфо</h2>
        <p class="muted">Мини-приложение с теми же играми и общим балансом, что и в основном боте.</p>
        <p class="muted">Все ставки, выигрыши и баланс хранятся в одной PostgreSQL базе данных. Любое изменение баланса в боте мгновенно отражается здесь, и наоборот.</p>
        <p class="muted">Используйте вкладку «Игры» для запуска. Минимальная ставка — 1 ⭐.</p>
      </div>
    </div>
  </div>
</div>

<!-- UNIVERSAL BET MODAL -->
<div id="bet-modal" class="popup" style="display:none">
  <div class="box">
    <span class="close" onclick="closeBetModal()">×</span>
    <h2 id="bet-title">Игра</h2>
    <div id="bet-coefs" class="coef" style="margin-bottom:8px;"></div>
    <div id="bet-extra"></div>
    <label>Ставка (мин 1 ⭐)</label>
    <input id="bet-input" type="number" min="1" value="10">
    <div class="quick-bets">
      <button class="btn ghost" onclick="setBet(10)">10</button>
      <button class="btn ghost" onclick="setBet(50)">50</button>
      <button class="btn ghost" onclick="setBet(100)">100</button>
      <button class="btn ghost" onclick="setBet(500)">500</button>
    </div>
    <div style="height:12px"></div>
    <button class="btn" id="bet-go" onclick="confirmBet()">Играть</button>
  </div>
</div>

<!-- GAME POPUP -->
<div id="game-modal" class="popup" style="display:none">
  <div class="box">
    <span class="close" onclick="closeGameModal()">×</span>
    <div id="game-content"></div>
  </div>
</div>

<div id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
let me = null;
let currentBetGame = null;
let currentBetExtra = {};
let ladderState = null;
let tttState = null;
let msState = null;
let bjState = null;

// === Telegram WebApp ===
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  // показать кнопку «Войти через Telegram» только если открыто в Telegram
  const tgBtn = document.getElementById('btn-tg');
  if (tgBtn) tgBtn.style.display = 'block';
}

function toast(msg, ms = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.style.display = 'none', ms);
}

async function api(url, opts = {}) {
  const r = await fetch(url, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (r.status === 401) { showAuth(); throw new Error('unauthorized'); }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'request failed');
  return data;
}

// === AUTH ===
function showAuth() {
  document.getElementById('auth-screen').style.display = 'block';
  document.getElementById('app-screen').style.display = 'none';
}
function showApp() {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('app-screen').style.display = 'block';
}
$$('#auth-tabs .tab').forEach(t => t.addEventListener('click', () => {
  $$('#auth-tabs .tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('form-login').style.display = t.dataset.tab === 'login' ? 'block' : 'none';
  document.getElementById('form-register').style.display = t.dataset.tab === 'register' ? 'block' : 'none';
}));
async function doLogin() {
  const err = $('#auth-error');
  err.textContent = '';
  try {
    const data = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({
      username: $('#login-username').value, password: $('#login-password').value }) });
    me = data.user; onLoggedIn();
  } catch (e) { err.textContent = errorText(e.message); }
}
async function doRegister() {
  const err = $('#auth-error');
  err.textContent = '';
  try {
    const data = await api('/api/auth/register', { method: 'POST', body: JSON.stringify({
      username: $('#reg-username').value, password: $('#reg-password').value }) });
    me = data.user; onLoggedIn();
  } catch (e) { err.textContent = errorText(e.message); }
}
async function doTgLogin() {
  const err = $('#auth-error');
  err.textContent = '';
  if (!tg || !tg.initData) { err.textContent = 'Откройте мини-апп в Telegram'; return; }
  try {
    const data = await api('/api/auth/telegram', { method: 'POST', body: JSON.stringify({
      initData: tg.initData }) });
    me = data.user; onLoggedIn();
  } catch (e) { err.textContent = errorText(e.message); }
}
function errorText(code) {
  return ({
    invalid_credentials: 'Неверный логин или пароль',
    username_taken: 'Этот username уже занят',
    username_min_3: 'Username должен быть минимум 3 символа',
    password_min_4: 'Пароль должен быть минимум 4 символа',
    invalid_init_data: 'Не удалось подтвердить Telegram',
    unauthorized: 'Требуется авторизация',
  })[code] || ('Ошибка: ' + code);
}
async function doLogout() {
  await api('/api/auth/logout', { method: 'POST' });
  me = null; showAuth();
}
function onLoggedIn() {
  showApp();
  refreshUI();
}
async function refreshUI() {
  try {
    const r = await api('/api/me');
    me = r.user;
  } catch (e) { return; }
  $('#balance-pill').textContent = `⭐ ${formatNum(me.balance)}`;
  $('#prof-username').textContent = '@' + (me.username || '—');
  $('#prof-uid').textContent = me.user_id;
  $('#prof-balance').textContent = formatNum(me.balance) + ' ⭐';
  $('#prof-games').textContent = me.games_played;
}
function formatNum(n) {
  return Number(n).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

// === TABS ===
$$('#app-screen .tab').forEach(t => t.addEventListener('click', () => {
  $$('#app-screen .tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const tab = t.dataset.tab;
  ['games','profile','topup','info'].forEach(p => {
    document.getElementById('page-' + p).style.display = (p === tab) ? 'block' : 'none';
  });
  if (tab === 'profile') refreshUI();
}));

// === GAMES ===
$$('.game').forEach(g => g.addEventListener('click', () => openBetModal(g.dataset.game)));

const COEFS = {}; // коэффициенты подгрузим при необходимости

async function loadCoefs() {
  if (Object.keys(COEFS).length) return COEFS;
  const data = await api('/api/coefficients');
  Object.assign(COEFS, data);
  return COEFS;
}

function openBetModal(game, extra = {}) {
  currentBetGame = game;
  currentBetExtra = extra;
  $('#bet-title').textContent = gameName(game) + (extra.title ? ' — ' + extra.title : '');
  $('#bet-input').value = extra.bet || 10;
  loadCoefs().then(c => {
    const coefText = coefFor(game, c);
    $('#bet-coefs').innerHTML = coefText;
  });
  // Специфичные поля
  const extra1 = $('#bet-extra');
  extra1.innerHTML = '';
  if (game === 'coinflip') {
    extra1.innerHTML = `
      <label>Ваш выбор</label>
      <div class="row">
        <button class="btn ghost" data-choice="heads" onclick="pickCoin(this,'heads')">🦅 Орёл</button>
        <button class="btn ghost" data-choice="tails" onclick="pickCoin(this,'tails')">🪙 Решка</button>
      </div>`;
  }
  if (game === 'rps') {
    extra1.innerHTML = `
      <label>Ваш выбор</label>
      <div class="row">
        <button class="btn ghost" onclick="pickRps(this,'rock')">🪨 Камень</button>
        <button class="btn ghost" onclick="pickRps(this,'scissors')">✂ Ножницы</button>
        <button class="btn ghost" onclick="pickRps(this,'paper')">📄 Бумага</button>
      </div>`;
  }
  if (game === 'minesweeper') {
    extra1.innerHTML = `
      <label>Размер поля</label>
      <div class="row">
        <button class="btn ghost" data-size="5" onclick="pickMsSize(this,5)">5×5 (6 мин)</button>
        <button class="btn ghost" data-size="7" onclick="pickMsSize(this,7)">7×7 (12 мин)</button>
      </div>`;
    currentBetExtra.ms_size = 5;
  }
  if (game === 'lootbox') {
    extra1.innerHTML = `
      <label>Размер лутбокса</label>
      <div class="row">
        <button class="btn ghost" onclick="pickLoot(this,'2x2')">2×2 (x2)</button>
        <button class="btn ghost" onclick="pickLoot(this,'3x3')">3×3 (x3)</button>
      </div>`;
    currentBetExtra.size = '2x2';
  }
  $('#bet-modal').style.display = 'flex';
}
function closeBetModal() { $('#bet-modal').style.display = 'none'; }
function setBet(v) { $('#bet-input').value = v; }
function pickCoin(b, v) { currentBetExtra.choice = v; markPick(b); }
function pickRps(b, v) { currentBetExtra.choice = v; markPick(b); }
function pickMsSize(b, v) { currentBetExtra.ms_size = v; markPick(b); }
function pickLoot(b, v) { currentBetExtra.size = v; markPick(b); }
function markPick(btn) {
  btn.parentElement.querySelectorAll('button').forEach(x => x.style.outline = '');
  btn.style.outline = '2px solid var(--accent)';
}
function gameName(g) {
  return ({ dice:'🎲 Кубик', basketball:'🏀 Баскетбол', football:'⚽ Футбол',
    roulette:'🎰 Рулетка', darts:'🎯 Дартс', coinflip:'🪙 Орёл/Решка',
    rps:'✌️ КНБ', tictactoe:'❌ Крестики-нолики', minesweeper:'💣 Сапёр',
    blackjack:'🃏 Блэкджек', ladder:'📊 Лесенка', lootbox:'📦 Лутбокс' })[g] || g;
}
function coefFor(g, c) {
  if (!c[g]) return '';
  const parts = Object.entries(c[g]).map(([k,v]) => `${k}: x${v}`);
  return 'Коэффициенты: ' + parts.join(', ');
}

async function confirmBet() {
  const bet = parseFloat($('#bet-input').value);
  if (!bet || bet < 1) { toast('Минимальная ставка 1 ⭐'); return; }
  closeBetModal();
  try {
    switch (currentBetGame) {
      case 'dice':       return playDice(bet);
      case 'basketball': return playBasketball(bet);
      case 'football':   return playFootball(bet);
      case 'roulette':   return playRoulette(bet);
      case 'darts':      return playDarts(bet);
      case 'coinflip':   return playCoinflip(bet, currentBetExtra.choice);
      case 'rps':        return playRps(bet, currentBetExtra.choice);
      case 'tictactoe':  return playTttStart(bet);
      case 'minesweeper':return playMsStart(bet, currentBetExtra.ms_size);
      case 'blackjack':  return playBjStart(bet);
      case 'ladder':     return playLadderStart(bet);
      case 'lootbox':    return playLootbox(bet, currentBetExtra.size);
    }
  } catch (e) { toast('Ошибка: ' + e.message); }
}

// === Индивидуальные игры ===
async function playDice(bet) {
  const r = await api('/api/games/dice', { method: 'POST', body: JSON.stringify({ bet }) });
  showResult(`🎲 Выпало <b>${r.roll}</b> · x${r.roll === 1 ? 0 : r.roll <= 3 ? 0.3 : r.roll <= 4 ? 1 : r.roll === 5 ? 1.5 : 3}`,
    r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Проигрыш', r.bonus > 0 ? `🎁 Бонус 3×6: +${formatNum(r.bonus)} ⭐` : '', meUpdate(r));
}
async function playBasketball(bet) {
  const r = await api('/api/games/basketball', { method: 'POST', body: JSON.stringify({ bet }) });
  showResult(`🏀 Бросок: ${r.roll}`, r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Мимо!', '', meUpdate(r));
}
async function playFootball(bet) {
  const r = await api('/api/games/football', { method: 'POST', body: JSON.stringify({ bet }) });
  showResult(`⚽ Удар: ${r.roll}`, r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Мимо!', '', meUpdate(r));
}
async function playRoulette(bet) {
  const r = await api('/api/games/roulette', { method: 'POST', body: JSON.stringify({ bet }) });
  const txt = r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Мимо!';
  const extra = r.series ? '🎉 Серия 3×777! Бонус: +' + formatNum(r.bonus) + ' ⭐' : '';
  showResult(`🎰 Выпало: ${r.label || '—'} (${r.roll})`, txt, extra, meUpdate(r));
}
async function playDarts(bet) {
  const r = await api('/api/games/darts', { method: 'POST', body: JSON.stringify({ bet }) });
  showResult(`🎯 ${r.event}`, r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Проигрыш', '', meUpdate(r));
}
async function playCoinflip(bet, choice) {
  if (!choice) { toast('Выберите сторону'); return; }
  const r = await api('/api/games/coinflip', { method: 'POST', body: JSON.stringify({ bet, choice }) });
  showResult(`🪙 Выпало: ${r.result === 'heads' ? '🦅 Орёл' : '🪙 Решка'}`,
    r.win > 0 ? `+${formatNum(r.win)} ⭐` : 'Не угадали', '', meUpdate(r));
}
async function playRps(bet, choice) {
  if (!choice) { toast('Выберите жест'); return; }
  const r = await api('/api/games/rps', { method: 'POST', body: JSON.stringify({ bet, choice }) });
  const icons = { rock:'🪨', scissors:'✂', paper:'📄' };
  const text = r.outcome === 'win' ? `+${formatNum(r.win)} ⭐` : r.outcome === 'draw' ? `Ничья: +${formatNum(r.win)} ⭐` : 'Проигрыш';
  showResult(`Вы: ${icons[r.choice]} vs Бот: ${icons[r.bot]}`, text, '', meUpdate(r));
}

async function playTttStart(bet) {
  const r = await api('/api/games/tictactoe/start', { method: 'POST', body: JSON.stringify({ bet }) });
  tttState = r;
  meUpdate(r);
  renderTtt();
}
function renderTtt() {
  const c = $('#game-content');
  c.innerHTML = `<h2>❌ Крестики-нолики</h2>
    <div class="muted">Вы играете за X, бот за O</div>
    <div class="ttt-grid" id="ttt-grid"></div>
    <div id="ttt-msg" class="muted"></div>
    <button class="btn ghost" style="margin-top:10px" onclick="closeGameModal()">Закрыть</button>`;
  const g = $('#ttt-grid');
  tttState.board.forEach((v, i) => {
    const cell = document.createElement('div');
    cell.className = 'ttt-cell' + (v ? ' disabled' : '');
    cell.textContent = v || '';
    cell.onclick = () => tttClick(i);
    g.appendChild(cell);
  });
  $('#game-modal').style.display = 'flex';
  $('#ttt-msg').textContent = tttState.bot_first ? 'Бот сходил первым. Ваш ход.' : 'Ваш ход.';
}
async function tttClick(i) {
  if (tttState.board[i] || !tttState.board.includes(null)) return;
  try {
    const r = await api('/api/games/tictactoe/move', { method: 'POST', body: JSON.stringify({ idx: i }) });
    tttState = r; meUpdate(r);
    if (r.finished) {
      cFinishTtt(r);
    } else {
      renderTtt();
    }
  } catch (e) { toast(e.message); }
}
function cFinishTtt(r) {
  const c = $('#game-content');
  let msg = '';
  if (r.result === 'win') msg = `<div class="result-win">Победа! +${formatNum(r.win)} ⭐</div>`;
  else if (r.result === 'draw') msg = `<div>Ничья. Возврат +${formatNum(r.win)} ⭐</div>`;
  else msg = `<div class="result-lose">Бот выиграл</div>`;
  c.innerHTML = `<h2>❌ Крестики-нолики</h2>
    <div class="ttt-grid">${r.board.map(v => `<div class="ttt-cell disabled">${v || ''}</div>`).join('')}</div>
    ${msg}<div class="muted">⭐ Баланс: ${formatNum(me.balance)}</div>
    <button class="btn" style="margin-top:10px" onclick="closeGameModal()">Готово</button>`;
}

async function playMsStart(bet, size) {
  const r = await api('/api/games/minesweeper/start', { method: 'POST', body: JSON.stringify({ bet, size }) });
  msState = r; meUpdate(r);
  renderMs();
}
function renderMs() {
  const c = $('#game-content');
  const total = msState.size * msState.size;
  c.innerHTML = `<h2>💣 Сапёр ${msState.size}×${msState.size}</h2>
    <div class="muted">Бомб: ${msState.bombs} · Откройте ${msState.max_safe} безопасных</div>
    <div class="ms-grid" id="ms-grid" style="grid-template-columns:repeat(${msState.size},1fr)"></div>
    <div id="ms-msg" class="muted"></div>
    <button class="btn ghost" style="margin-top:10px" onclick="closeGameModal()">Закрыть</button>`;
  const g = $('#ms-grid');
  for (let i = 0; i < total; i++) {
    const cell = document.createElement('div');
    cell.className = 'ms-cell'; cell.dataset.i = i;
    cell.onclick = () => msClick(i);
    g.appendChild(cell);
  }
  $('#game-modal').style.display = 'flex';
}
async function msClick(i) {
  if (!msState) return;
  try {
    const r = await api('/api/games/minesweeper/reveal', { method: 'POST', body: JSON.stringify({ idx: i }) });
    msState = r; meUpdate(r);
    if (r.finished) {
      const c = $('#game-content');
      const cells = $$('#ms-grid .ms-cell');
      r.revealed.forEach(idx => {
        const cell = cells[idx]; if (!cell) return;
        cell.classList.add('revealed', r.grid[idx] ? 'bomb' : 'safe');
        cell.textContent = r.grid[idx] ? '💣' : '✅';
      });
      let msg = '';
      if (r.result === 'bomb') msg = `<div class="result-lose">💥 Бомба! Ставка проиграна.</div>`;
      else msg = `<div class="result-win">🎉 Все безопасны! +${formatNum(r.win)} ⭐</div>`;
      c.insertAdjacentHTML('beforeend', msg +
        `<div class="muted">⭐ Баланс: ${formatNum(me.balance)}</div>
         <button class="btn" style="margin-top:10px" onclick="closeGameModal()">Готово</button>`);
    } else {
      $$('#ms-grid .ms-cell').forEach(cell => {
        const idx = parseInt(cell.dataset.i);
        if (r.revealed.includes(idx)) {
          cell.classList.add('revealed', 'safe');
          cell.textContent = '✅';
        }
      });
      $('#ms-msg').textContent = `Безопасно! ${r.safe_count}/${r.max_safe}`;
    }
  } catch (e) { toast(e.message); }
}

async function playBjStart(bet) {
  const r = await api('/api/games/blackjack/start', { method: 'POST', body: JSON.stringify({ bet }) });
  bjState = r; meUpdate(r);
  if (r.finished) { showBjFinished(r); } else { renderBj(); }
}
function cardText(c) { return c.rank + c.suit; }
function renderBj() {
  const c = $('#game-content');
  c.innerHTML = `<h2>🃏 Блэкджек</h2>
    <div class="muted">Ставка: ${formatNum(bjState.bet || '')} ⭐</div>
    <div>Ваша рука: <b>${bjState.player_val}</b></div>
    <div class="cards">${bjState.player.map(cardText).join(' ')}</div>
    <div>Рука дилера:</div>
    <div class="cards">🂠 ${cardText(bjState.dealer[1])}</div>
    <div class="bj-actions">
      <button class="btn green" onclick="bjAct('hit')">Ещё</button>
      <button class="btn" onclick="bjAct('stand')">Хватит</button>
      <button class="btn gold" onclick="bjAct('double')">×2</button>
    </div>
    <button class="btn ghost" style="margin-top:10px" onclick="closeGameModal()">Закрыть</button>`;
  $('#game-modal').style.display = 'flex';
}
async function bjAct(act) {
  try {
    const r = await api('/api/games/blackjack/action', { method: 'POST', body: JSON.stringify({ action: act }) });
    bjState = r; meUpdate(r);
    if (r.finished) showBjFinished(r); else renderBj();
  } catch (e) { toast(e.message); }
}
function showBjFinished(r) {
  const c = $('#game-content');
  const labels = { win:'🎉 Победа', lose:'❌ Проигрыш', push:'Ничья', bust:'💥 Перебор',
                   dealer_bust:'Дилер перебрал', blackjack:'BLACKJACK!', push_blackjack:'Ничья (BJ)' };
  let msg = '';
  const txt = labels[r.result] || r.result;
  if (r.result === 'win' || r.result === 'dealer_bust' || r.result === 'blackjack') {
    msg = `<div class="result-win">${txt} +${formatNum(r.win)} ⭐</div>`;
  } else if (r.result === 'push' || r.result === 'push_blackjack') {
    msg = `<div>${txt} +${formatNum(r.win)} ⭐</div>`;
  } else {
    msg = `<div class="result-lose">${txt}</div>`;
  }
  c.innerHTML = `<h2>🃏 Блэкджек</h2>
    <div class="muted">Ваша рука (${r.player_val}): ${r.player.map(cardText).join(' ')}</div>
    <div class="muted">Рука дилера (${r.dealer_val}): ${r.dealer.map(cardText).join(' ')}</div>
    ${msg}<div class="muted">⭐ Баланс: ${formatNum(me.balance)}</div>
    <button class="btn" style="margin-top:10px" onclick="closeGameModal()">Готово</button>`;
  $('#game-modal').style.display = 'flex';
}

async function playLadderStart(bet) {
  const r = await api('/api/games/ladder/start', { method: 'POST', body: JSON.stringify({ bet }) });
  ladderState = r; meUpdate(r);
  renderLadder();
}
function renderLadder() {
  const c = $('#game-content');
  const m = ladderState.step_multipliers;
  const cum = [1,1.4, 1.4*1.89, 1.4*1.89*2.38, 1.4*1.89*2.38*3.5, 1.4*1.89*2.38*3.5*7, 1.4*1.89*2.38*3.5*7*7, 1.4*1.89*2.38*3.5*7*7*7];
  c.innerHTML = `<h2>📊 Лесенка</h2>
    <div class="muted">Множители по столбикам: ${m.map(x=>'x'+x).join(' · ')}</div>
    <div class="muted">Мин: ${ladderState.mines_per_column.join(' · ')}</div>
    <div id="ladder-msg" class="muted" style="margin:8px 0"></div>
    <div class="ladder" id="ladder-grid"></div>
    <div style="height:10px"></div>
    <button class="btn gold" id="ladder-take" onclick="ladderTake()" style="display:none">💰 Забрать</button>
    <button class="btn ghost" style="margin-top:10px" onclick="closeGameModal()">Закрыть</button>`;
  const g = $('#ladder-grid');
  for (let col = 0; col < ladderState.cols; col++) {
    const colDiv = document.createElement('div');
    colDiv.className = 'ladder-col';
    for (let row = ladderState.rows - 1; row >= 0; row--) {
      const cell = document.createElement('div');
      cell.className = 'ladder-cell' + (col > 0 ? ' locked' : '');
      cell.textContent = col === 0 ? '🎁' : (row + 1);
      cell.dataset.col = col; cell.dataset.row = row;
      cell.onclick = () => ladderOpen(col, row);
      colDiv.appendChild(cell);
    }
    g.appendChild(colDiv);
  }
  $('#ladder-msg').textContent = 'Откройте безопасную ячейку в столбике 1.';
  $('#game-modal').style.display = 'flex';
}
async function ladderOpen(col, row) {
  if (!ladderState) return;
  if (col !== ladderState.current_step && ladderState.current_step !== undefined && !ladderState.finished) {
    const step = ladderState.current_step;
    if (col > step) return; // locked
  }
  try {
    const r = await api('/api/games/ladder/open', { method: 'POST', body: JSON.stringify({ col, row }) });
    ladderState = { ...ladderState, ...r, finished: r.finished };
    meUpdate(r);
    if (r.finished) { showLadderEnd(r); return; }
    // обновляем UI
    const step = r.current_step;
    const cells = $$('#ladder-grid .ladder-cell');
    // помечаем пройденные столбики
    cells.forEach(cell => {
      const c = parseInt(cell.dataset.col), ro = parseInt(cell.dataset.row);
      if (c < step) {
        cell.classList.add('done');
        cell.classList.remove('locked');
        cell.textContent = '✓';
      } else if (c === step) {
        cell.classList.remove('locked');
      } else {
        cell.classList.add('locked');
      }
    });
    $('#ladder-msg').innerHTML = `Столбик ${step+1} · множитель <b>x${r.multiplier}</b> · выигрыш <b>${formatNum(r.win_if_take)} ⭐</b>`;
    $('#ladder-take').style.display = step > 0 ? 'block' : 'none';
  } catch (e) { toast(e.message); }
}
async function ladderTake() {
  try {
    const r = await api('/api/games/ladder/take', { method: 'POST', body: '{}' });
    meUpdate(r);
    showLadderEnd(r);
  } catch (e) { toast(e.message); }
}
function showLadderEnd(r) {
  const c = $('#game-content');
  let msg = '';
  if (r.result === 'mine') msg = `<div class="result-lose">💥 Бомба! Ставка проиграна.</div>`;
  else if (r.result === 'cashed_out') msg = `<div class="result-win">💰 Забрали: +${formatNum(r.win)} ⭐ (x${r.multiplier})</div>`;
  else if (r.result === 'complete') msg = `<div class="result-win">🏆 Покорили лесенку! +${formatNum(r.win)} ⭐ (x${r.multiplier})</div>`;
  c.insertAdjacentHTML('beforeend', msg +
    `<div class="muted">⭐ Баланс: ${formatNum(me.balance)}</div>
     <button class="btn" style="margin-top:10px" onclick="closeGameModal()">Готово</button>`);
}

async function playLootbox(bet, size) {
  const r = await api('/api/games/lootbox/' + size, { method: 'POST', body: JSON.stringify({ bet }) });
  meUpdate(r);
  const n = size === '2x2' ? 4 : 9;
  const cells = Array.from({length: n}, (_, i) => {
    if (i === r.chosen && r.won) return '<div class="loot-cell win">🎁</div>';
    if (i === r.chosen && !r.won) return '<div class="loot-cell lose">❌</div>';
    if (i === r.win_box && !r.won) return '<div class="loot-cell win">🎁</div>';
    return '<div class="loot-cell">📦</div>';
  });
  const msg = r.won ? `<div class="result-win">🎉 Выигрыш: +${formatNum(r.win)} ⭐</div>`
                     : `<div class="result-lose">Не повезло</div>`;
  $('#game-content').innerHTML = `<h2>📦 Лутбокс ${size}</h2>
    <div class="lootbox size-${size === '2x2' ? '2' : ''}">${cells.join('')}</div>
    ${msg}<div class="muted">⭐ Баланс: ${formatNum(me.balance)}</div>
    <button class="btn" style="margin-top:10px" onclick="closeGameModal()">Готово</button>`;
  $('#game-modal').style.display = 'flex';
}

// === Прочие ===
function showResult(title, winText, extra = '', newMe = null) {
  if (newMe) me = newMe;
  refreshUI();
  $('#game-content').innerHTML = `<div class="big-emoji">${title.split(' ')[0]}</div>
    <h2 style="text-align:center">${title}</h2>
    <div style="text-align:center; font-size:18px; margin:8px 0" class="${winText.startsWith('+') ? 'result-win' : 'result-lose'}">${winText}</div>
    ${extra ? `<div style="text-align:center" class="result-win">${extra}</div>` : ''}
    <div class="muted" style="text-align:center">⭐ Баланс: ${formatNum(me.balance)}</div>
    <div style="height:10px"></div>
    <button class="btn" onclick="closeGameModal()">Готово</button>`;
  $('#game-modal').style.display = 'flex';
}
function closeGameModal() { $('#game-modal').style.display = 'none'; msState = null; ladderState = null; tttState = null; bjState = null; refreshUI(); }
function meUpdate(r) { if (r && r.user) me = r.user; refreshUI(); return r; }

async function doTopup() {
  const amt = parseFloat($('#topup-amount').value);
  if (!amt || amt < 1) { toast('Минимум 1 ⭐'); return; }
  try {
    const r = await api('/api/topup', { method: 'POST', body: JSON.stringify({ amount: amt }) });
    me = r.user; refreshUI();
    toast('Зачислено +' + formatNum(amt) + ' ⭐ (dev)');
  } catch (e) { toast('Ошибка пополнения'); }
}

// === INIT ===
(async function init() {
  try {
    const r = await api('/api/me');
    me = r.user; onLoggedIn();
  } catch (e) { showAuth(); }
})();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# BOOT
# ═══════════════════════════════════════════════════════════════════════════════

def bootstrap():
    init_db()
    init_coefficients()


bootstrap()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
