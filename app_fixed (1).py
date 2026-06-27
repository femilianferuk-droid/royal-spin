"""
Royal Spin — Telegram Mini App
Backend: Flask + serverless-wsgi
DB: PostgreSQL (ОБЩАЯ с zakaz-test/bot.py)
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
from decimal import Decimal
from urllib.parse import parse_qsl
from functools import wraps

from flask import Flask, request, jsonify, Response
from flask.json.provider import DefaultJSONProvider
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# ═══════════════════════════════════════════════════════════════════════════════
# ОБЩАЯ СТРУКТУРА БД с zakaz-test/bot.py  (ЕДИНАЯ схема → общий баланс)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Royal Spin (mini app) и zakaz-test (tg-бот) ОБЯЗАНЫ работать с одной
# и той же PostgreSQL-инстанцией. Только тогда баланс, бонусы, games_played,
# промокоды, invoices и выводы — общие между ботом и mini app.
#
# Обе программы по умолчанию используют один DSN:
#     postgresql://postgres:postgres@localhost:5432/postgres
#
# Схема таблиц — ЕДИНАЯ (см. init_schema()). Мини-апп создаёт/мигрирует
# ВСЕ таблицы, которые использует бот, и наоборот — бот при первом старте
# создаст те же таблицы, init_schema() в mini app доводит их до полного
# вида (добавляет first_name, last_name, photo_url, games_won, last_seen
# и приводит тип balance к DECIMAL для совместимости).
#
# ⚠️ ВАЖНО для прода:
#   На проде ОБЯЗАТЕЛЬНО задай одинаковый DATABASE_URL в ENV для ОБОИХ
#   приложений. Например:
#       export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
#   в unit-файле systemd / Dockerfile / docker-compose / env-файле.
#
#   Дефолтный URL royal-spin раньше указывал на отдельную royal-spin-БД
#   на node1.pghost.ru — из-за этого mini app и бот жили в РАЗНЫХ базах
#   и баланс расходился. Теперь дефолт — localhost, как у zakaz-test/bot.py.
#
# ─────────────────────────────────────────────────────────────────────────────
# ТАБЛИЦЫ (единая схема, владелец — royal-spin/app.py::init_schema):
#   users             — user_id PK, username, first/last_name, photo_url,
#                       balance DECIMAL, bonus_claimed, games_played INT,
#                       games_won INT, created_at/last_seen TIMESTAMPTZ
#   transactions      — история ставок/выплат mini app
#   balance_log       — аудит-трейл всех изменений balance (lazy)
#   invoices          — заявки на пополнение (бот + mini app)
#   promocodes        — справочник промокодов
#   promo_uses        — кто/когда активировал
#   withdrawals       — заявки на вывод
#   broadcast_log     — лог админ-рассылок
#   required_channels — обязательные каналы подписки
#   game_coefficients — коэффициенты игр
#   media             — медиа-файлы разделов
#   settings          — KV-настройки
#   crypto_payments   — крипто-платежи
#   pvp_duels         — PvP дуэли
#   user_access       — флаг доступа юзера (для рассылки)
# ─────────────────────────────────────────────────────────────────────────────
# КАНОНИЧЕСКИЕ ФУНКЦИИ БАЛАНСА (используются обеими сторонами):
#   get_balance(uid)        — int из общего users.balance
#   update_balance(uid,Δ)   — атомарно через SELECT … FOR UPDATE
#   set_balance(uid, value)  — атомарный SET с row-lock
#   deduct_balance(uid,amt)  — списание с проверкой остатка
#   claim_bonus(uid, amount=5) — идемпотентный welcome-бонус
#   upsert_user(uid, ...)    — создание/обновление профиля (mini app)
# ─────────────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
MIN_STAKE = 1
MAX_STAKE = 500

# ─────────────────────────────────────────────────────────────────────────────
# HOTFIX 2026-06-27: 405 на /api/balance в Mini App
# ─────────────────────────────────────────────────────────────────────────────
# Проблема: validate_init_data() ссылается на BOT_TOKEN, но переменная нигде
# не была объявлена — на любом защищённом роуте падал NameError -> 500 ->
# Mini App показывал "Ошибка соединения с общей БД".
#
# Задаём BOT_TOKEN здесь из ENV. Должно совпадать с токеном бота
# zakaz-test/bot.py, иначе HMAC-проверка initData не пройдёт.
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_BOT_TOKEN_HERE")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("royal-spin")

app = Flask(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HOTFIX 2026-06-27: CORS + OPTIONS preflight для Mini App
# ─────────────────────────────────────────────────────────────────────────────
# Telegram WebApp может работать через прокси/CDN, который шлёт POST вместо
# GET (или режет кастомные заголовки). Плюс нужен нормальный ответ на
# OPTIONS preflight, иначе браузерный fetch в WebApp блокируется.
#
# Разрешаем любой origin (Telegram WebApp всегда https), основные методы и
# основные заголовки — initData может идти и в X-Telegram-Init-Data, и в
# теле POST, поэтому Content-Type обязателен.
# ─────────────────────────────────────────────────────────────────────────────
@app.after_request
def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-Telegram-Init-Data, X-Requested-With"
    )
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@app.errorhandler(405)
def _method_not_allowed(_e):
    """Мини-апп интерпретирует 405 как «нет связи с БД». Возвращаем 200 + ok=False."""
    return (
        jsonify(
            {
                "ok": False,
                "error": "method_not_allowed",
                "hint": "use GET or POST",
            }
        ),
        200,
    )


@app.errorhandler(500)
def _internal_error(e):
    """500 от валидации/БД тоже превращаем в читаемый ответ — иначе фронт показывает «Ошибка соединения с общей БД»."""
    log.exception("internal error: %s", e)
    return (
        jsonify({"ok": False, "error": "internal_error", "detail": str(e)[:200]}),
        200,
    )


# Глобальный OPTIONS-обработчик для ВСЕХ путей. Без него любой preflight от
# Telegram WebApp вернёт 405, fetch в JS упадёт, фронт решит, что «БД недоступна».
@app.route("/<path:_any>", methods=["OPTIONS"])
def _options_handler(_any=None):
    return ("", 204)


# Кастомный JSON-провайдер: Decimal из PostgreSQL (balance) не сериализуется
# стандартным jsonify. Приводим к int, если значение целое, иначе к float.
class _DecimalJSONProvider(DefaultJSONProvider):
    def default(self, o):  # type: ignore[override]
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


app.json = _DecimalJSONProvider(app)

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
    except Exception as e:
        log.error(f"DB pool init failed: {e}")
        db_pool = None
        return db_pool
    # init_schema() выполняем отдельно от создания пула.
    # Если миграция упадёт (например, из-за старой кривой схемы), пул всё равно
    # останется жить — приложение сможет читать/писать в то, что уже есть.
    try:
        init_schema()
    except Exception as e:
        log.exception(f"init_schema failed (continuing with existing pool): {e}")
    return db_pool


def get_conn():
    p = init_db_pool()
    if p is None:
        raise RuntimeError("DB unavailable")
    return p.getconn()


def put_conn(conn):
    """
    Возвращает соединение в пул. ВАЖНО: psycopg2 в режиме autocommit=False
    держит транзакцию открытой с момента первого запроса до явного
    commit/rollback. Если просто отдать соединение обратно — следующий
    пользователь пула получит его со старым snapshot-ом, в котором НЕ видно
    изменений, сделанных другими процессами (например, ботом). Это приводит
    к расхождению баланса: бот пишет 405, mini app читает 0.

    Поэтому всегда закрываем транзакцию перед возвратом в пул. commit()
    на SELECT-only транзакции безопасен (no-op по данным), но фиксирует
    границу snapshot-а.
    """
    if db_pool is not None and conn is not None:
        try:
            if conn.status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        db_pool.putconn(conn)


def _safe_exec(cur, conn, sql: str, label: str, results: list) -> bool:
    """
    Выполняет один шаг миграции. Если шаг падает — делает rollback,
    логирует warning и добавляет запись в results. Возвращает True при успехе.
    Это позволяет init_schema не валиться целиком из-за одного битого ALTER
    (например, если колонка уже имеет несовместимый тип и USING не отрабатывает).
    """
    try:
        cur.execute(sql)
        conn.commit()
        results.append({"step": label, "ok": True})
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        msg = str(e).strip().splitlines()[0][:200]
        log.warning(f"migration step '{label}' skipped: {msg}")
        results.append({"step": label, "ok": False, "error": msg})
        return False


def init_schema():
    """
    Инициализирует/мигрирует схему БД под общую с zakaz-test структуру.

    Каждый шаг выполняется независимо — если один ALTER падает (например,
    из-за несовместимого типа в существующей таблице), остальные продолжают
    работать. Это критично для hot-fix-ей, когда БД уже в каком-то
    промежуточном состоянии после старой версии royal-spin.
    """
    conn = get_conn()
    results: list = []
    try:
        with conn.cursor() as cur:
            # ---- users: создаём с полной структурой ----
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id      BIGINT PRIMARY KEY,
                    username     TEXT,
                    first_name   TEXT,
                    last_name    TEXT,
                    photo_url    TEXT,
                    balance      DECIMAL DEFAULT 0,
                    bonus_claimed BOOLEAN DEFAULT FALSE,
                    games_played INT DEFAULT 0,
                    games_won    INTEGER NOT NULL DEFAULT 0,
                    created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    last_seen    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
                """,
                "create_table_users",
                results,
            )

            # ---- Миграция типов под zakaz-test ----
            # Если колонка уже DECIMAL/INT — ALTER отработает как no-op.
            # Если нет — USING balance::DECIMAL / games_played::INT безопасно кастит.
            _safe_exec(
                cur, conn,
                "ALTER TABLE users "
                "ALTER COLUMN balance TYPE DECIMAL USING balance::DECIMAL, "
                "ALTER COLUMN balance SET DEFAULT 0",
                "alter_balance_to_decimal",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users "
                "ALTER COLUMN games_played TYPE INT USING games_played::INT, "
                "ALTER COLUMN games_played SET DEFAULT 0",
                "alter_games_played_to_int",
                results,
            )

            # ---- bonus_claimed + royal-spin-специфичные колонки ----
            # ADD COLUMN IF NOT EXISTS — PostgreSQL идемпотентная операция.
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN DEFAULT FALSE",
                "add_bonus_claimed",
                results,
            )
            # Если колонка bonus_claimed была создана как INTEGER (например,
            # в какой-то старой миграции или внешним скриптом) — приводим к BOOLEAN.
            # USING с явным ::BOOLEAN кастит 0/1 → false/true.
            _safe_exec(
                cur, conn,
                "ALTER TABLE users "
                "ALTER COLUMN bonus_claimed TYPE BOOLEAN "
                "USING CASE WHEN bonus_claimed::int = 0 THEN FALSE ELSE TRUE END",
                "alter_bonus_claimed_to_boolean",
                results,
            )
            # ---- Унификация типа created_at ----
            # zakaz-test/bot.py создаёт created_at как TIMESTAMP,
            # royal-spin/app.py — как TIMESTAMP WITH TIME ZONE.
            # Если таблицу создал бот, а mini app стартует вторым — колонка
            # будет TIMESTAMP. Если наоборот — TIMESTAMP WITH TIME ZONE.
            # Чтобы оба читали/писали согласованно, приводим к
            # TIMESTAMP WITH TIME ZONE (timezone-aware). USING с AT TIME ZONE
            # 'UTC' безопасно конвертирует уже записанные значения из naive
            # в aware, трактуя их как UTC.
            _safe_exec(
                cur, conn,
                "ALTER TABLE users "
                "ALTER COLUMN created_at TYPE TIMESTAMP WITH TIME ZONE "
                "USING created_at AT TIME ZONE 'UTC'",
                "alter_created_at_to_tz",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT",
                "add_first_name",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT",
                "add_last_name",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_url TEXT",
                "add_photo_url",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS games_won INTEGER NOT NULL DEFAULT 0",
                "add_games_won",
                results,
            )
            _safe_exec(
                cur, conn,
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW()",
                "add_last_seen",
                results,
            )

            # ---- transactions (специфичная royal-spin таблица) ----
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    amount      INTEGER NOT NULL,
                    game_type   TEXT NOT NULL,
                    win         BOOLEAN NOT NULL,
                    detail      TEXT,
                    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
                """,
                "create_transactions",
                results,
            )
            _safe_exec(
                cur, conn,
                "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at DESC)",
                "create_idx_tx_user",
                results,
            )

            # ---- Общие с zakaz-test таблицы ----
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    amount      DECIMAL,
                    status      TEXT DEFAULT 'pending',
                    created_at  TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_invoices",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS promocodes (
                    id            SERIAL PRIMARY KEY,
                    code          TEXT UNIQUE,
                    amount        DECIMAL,
                    max_uses      INT,
                    current_uses  INT DEFAULT 0,
                    is_active     BOOLEAN DEFAULT TRUE
                )
                """,
                "create_promocodes",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS promo_uses (
                    id        SERIAL PRIMARY KEY,
                    promo_id  INT,
                    user_id   BIGINT,
                    used_at   TIMESTAMP DEFAULT NOW(),
                    UNIQUE(promo_id, user_id)
                )
                """,
                "create_promo_uses",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    username    TEXT,
                    amount      DECIMAL,
                    status      TEXT DEFAULT 'pending',
                    created_at  TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_withdrawals",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS broadcast_log (
                    id            SERIAL PRIMARY KEY,
                    admin_id      BIGINT,
                    message       TEXT,
                    entities_json TEXT,
                    file_id       TEXT,
                    sent_count    INT DEFAULT 0,
                    created_at    TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_broadcast_log",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS required_channels (
                    id            SERIAL PRIMARY KEY,
                    channel_id    BIGINT,
                    channel_url   TEXT,
                    channel_name  TEXT,
                    created_at    TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_required_channels",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS game_coefficients (
                    id           SERIAL PRIMARY KEY,
                    game_name    TEXT NOT NULL,
                    event_name   TEXT NOT NULL,
                    coefficient  DECIMAL DEFAULT 1,
                    UNIQUE(game_name, event_name)
                )
                """,
                "create_game_coefficients",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS media (
                    id          SERIAL PRIMARY KEY,
                    section     TEXT UNIQUE,
                    media_type  TEXT,
                    file_id     TEXT
                )
                """,
                "create_media",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key     TEXT PRIMARY KEY,
                    value   TEXT
                )
                """,
                "create_settings",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS crypto_payments (
                    id              SERIAL PRIMARY KEY,
                    user_id         BIGINT,
                    amount_stars    DECIMAL,
                    amount_crypto   DECIMAL,
                    currency        TEXT,
                    invoice_id      TEXT,
                    status          TEXT DEFAULT 'pending',
                    created_at      TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_crypto_payments",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS pvp_duels (
                    id           SERIAL PRIMARY KEY,
                    room_code    TEXT UNIQUE,
                    player1_id   BIGINT,
                    player2_id   BIGINT DEFAULT NULL,
                    bet          DECIMAL,
                    status       TEXT DEFAULT 'waiting',
                    p1_dice      INT DEFAULT NULL,
                    p2_dice      INT DEFAULT NULL,
                    winner_id    BIGINT DEFAULT NULL,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_pvp_duels",
                results,
            )
            _safe_exec(
                cur, conn,
                """
                CREATE TABLE IF NOT EXISTS user_access (
                    user_id     BIGINT PRIMARY KEY,
                    status      TEXT DEFAULT 'allowed',
                    updated_at  TIMESTAMP DEFAULT NOW()
                )
                """,
                "create_user_access",
                results,
            )
    finally:
        put_conn(conn)

    failed = [r for r in results if not r["ok"]]
    if failed:
        log.warning(f"init_schema: {len(failed)} шагов упало (см. /api/debug/fix-schema)")
    return results


# ============ TELEGRAM INIT DATA VALIDATION ============
def validate_init_data(init_data: str) -> dict | None:
    """
    Validate the raw initData string from Telegram WebApp.
    Returns the user dict if valid, else None.

    ВАЖНО: data_check_string строится из СЫРЫХ (не URL-декодированных)
    значений, ровно как их прислал Telegram. parse_qsl их декодирует,
    и HMAC перестаёт совпадать — отсюда 401 и "Откройте Mini App из Telegram".
    """
    if not init_data or not BOT_TOKEN or BOT_TOKEN.startswith("PUT_"):
        return None
    try:
        # 1. Парсим "сырым" способом, чтобы сохранить оригинальные значения.
        raw_pairs = []   # для data_check_string (как прислал Telegram)
        decoded = {}     # для user / auth_date (нужны декодированные)
        for part in init_data.split("&"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
            else:
                k, v = part, ""
            raw_pairs.append((k, v))
            decoded[k] = parse_qs_value(v)

        received_hash = decoded.pop("hash", None)
        if not received_hash:
            return None

        # 2. data_check_string из СЫРЫХ пар (не декодированных), отсортированных по ключу.
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(raw_pairs) if k != "hash"
        )
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            log.warning("initData hash mismatch — проверь BOT_TOKEN / домен")
            return None

        auth_date = int(decoded.get("auth_date", "0") or "0")
        if not auth_date or abs(time.time() - auth_date) > 86400:
            return None

        user_json = decoded.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        log.warning(f"initData validation error: {e}")
        return None


def parse_qs_value(v: str) -> str:
    """Безопасный URL-decode значения (заменя + на пробел)."""
    from urllib.parse import unquote_plus
    return unquote_plus(v)


# ============ AUTH DECORATOR ============
def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # 1. Кастомный заголовок (приоритет)
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        # 2. Тело POST-запроса (fallback для прокси, режущих заголовки)
        if not init_data and request.is_json:
            init_data = (request.get_json(silent=True) or {}).get("initData", "")
        # 3. Query-параметр (ещё один fallback — некоторые прокси не пускают POST
        #    с кастомными заголовками, тогда фронт делает GET с ?_init=...)
        if not init_data:
            init_data = request.args.get("_init", "")
        user = validate_init_data(init_data)
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        request.tg_user = user
        return fn(*args, **kwargs)

    return wrapper


# ============ USER HELPERS ============
def upsert_user(tg_user: dict) -> dict:
    """
    Bulletproof-версия. Гарантии:
      • Никогда не бросает исключение наружу — любой сбой изолирован.
      • Использует INSERT ... ON CONFLICT DO UPDATE — один SQL, без гонок и
        без UniqueViolation для обработки.
      • Каждая второстепенная операция (бонус, user_access, transactions)
        делается через ОТДЕЛЬНОЕ соединение из пула. Падение одной операции
        не трогает остальные.
      • Возвращает {"user": dict, "is_new": bool} в любом случае.

    Баланс бонуса — идемпотентный через bonus_claimed=FALSE (тот же SQL,
    что и zakaz-test/bot.py:claim_bonus). Никаких SAVEPOINT'ов, никаких
    каскадных откатов.
    """
    user_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name")
    last_name = tg_user.get("last_name")
    photo_url = tg_user.get("photo_url")

    # Шаг 1: апсёрт юзера (один SQL, атомарный). Определяем is_new по
    # affected rows: если строка была вставлена — is_new=True, иначе False.
    is_new = False
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO users
                        (user_id, username, first_name, last_name, photo_url,
                         last_seen)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username   = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name  = EXCLUDED.last_name,
                        photo_url  = EXCLUDED.photo_url,
                        last_seen  = NOW()
                    RETURNING *, (xmax = 0) AS was_inserted
                    """,
                    (user_id, username, first_name, last_name, photo_url),
                )
                row = cur.fetchone()
                conn.commit()
                if row is None:
                    # Теоретически не должно случаться (RETURNING всегда даёт строку
                    # для INSERT ... ON CONFLICT DO UPDATE), но защищаемся.
                    raise RuntimeError("upsert returned no row")
                is_new = bool(row.get("was_inserted"))
                user_row = dict(row)
        finally:
            put_conn(conn)
    except Exception as e:
        log.exception(f"upsert_user main upsert failed for uid={user_id}")
        # Последний шанс — попробовать просто прочитать. Если и это упадёт,
        # возвращаем минимальный профиль, чтобы фронт хоть что-то показал.
        try:
            conn = get_conn()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM users WHERE user_id = %s",
                        (user_id,),
                    )
                    row2 = cur.fetchone()
                    conn.commit()
                    if row2:
                        user_row = dict(row2)
                    else:
                        user_row = {
                            "user_id": user_id,
                            "username": username,
                            "first_name": first_name,
                            "last_name": last_name,
                            "photo_url": photo_url,
                            "balance": 0,
                            "bonus_claimed": False,
                            "games_played": 0,
                            "games_won": 0,
                        }
            finally:
                put_conn(conn)
        except Exception as e2:
            log.exception(f"upsert_user fallback read failed for uid={user_id}")
            user_row = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "photo_url": photo_url,
                "balance": 0,
                "bonus_claimed": False,
                "games_played": 0,
                "games_won": 0,
            }

    # Шаг 2: бонус — ОТДЕЛЬНОЕ соединение. Атомарный идемпотентный UPDATE.
    # Если юзер только что создан И bonus_claimed=FALSE — начислим +5.
    # Если уже получал (от бота или mini app) — UPDATE не затронет ни одной строки.
    try:
        applied, new_bal = claim_bonus(user_id, amount=5, source="mini_app:welcome")
        user_row["balance"] = int(new_bal)
        user_row["bonus_claimed"] = True
        if applied:
            # Шаг 3a: лог транзакции — тоже отдельное соединение, не критично если упадёт.
            try:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO transactions (user_id, amount, game_type, win, detail)
                            VALUES (%s, %s, 'welcome', TRUE, 'Приветственный бонус')
                            """,
                            (user_id, 5),
                        )
                        conn.commit()
                finally:
                    put_conn(conn)
            except Exception as e3:
                log.warning(f"welcome bonus transaction log skipped for uid={user_id}: {e3}")
    except Exception as e:
        # Бонус не критичен — профиль уже сохранён, фронт покажет ok.
        log.warning(f"claim_bonus failed for uid={user_id}: {e}")

    # Шаг 3b: user_access — отдельное соединение. Нужно для рассылки из бота.
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_access (user_id, status)
                    VALUES (%s, 'allowed')
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id,),
                )
                conn.commit()
        finally:
            put_conn(conn)
    except Exception as e:
        log.warning(f"user_access upsert failed for uid={user_id}: {e}")

    # Шаг 3c: balance_log для аудита (если была запись о бонусе). Тоже изолированно.
    # Не критично, если таблицы balance_log ещё нет.
    return {"user": user_row, "is_new": is_new}


# ─────────────────────────────────────────────────────────────────────────────
# КАНОНИЧЕСКИЕ ФУНКЦИИ РАБОТЫ С БАЛАНСОМ (ОБЩАЯ ЛОГИКА С zakaz-test/bot.py)
# ─────────────────────────────────────────────────────────────────────────────
#
# Баланс пользователя — единый: живёт в общей таблице `users.balance`
# (DECIMAL DEFAULT 0). Любое изменение проходит через эти хелперы:
#
#   • get_balance(user_id)            — прочитать (int для API, Decimal внутри)
#   • get_balance_decimal(user_id)    — прочитать как Decimal (для сравнений)
#   • update_balance(uid, delta, ...) — атомарное delta с FOR UPDATE
#   • set_balance(uid, amount)        — атомарный SET с FOR UPDATE
#   • claim_bonus(uid, amount=5)      — идемпотентный бонус (ровно как bot.py)
#   • deduct_balance(uid, amount)     — списание с проверкой остатка
#
# Все операции идут под SELECT ... FOR UPDATE, чтобы исключить гонки между
# ботом и mini app. Decimal-арифметика исключает потерю точности.
#
# Контракт совместим с bot.py:update_balance / set_balance / deduct_balance /
# claim_bonus — одинаковые имена, одинаковая семантика, одинаковая идемпотентность
# бонуса (UPDATE ... WHERE bonus_claimed = FALSE).

BALANCE_MIN = Decimal("0")          # баланс не может уходить в минус


# ─────────────────────────────────────────────────────────────────────────────
# КАНОНИЧЕСКИЕ ФУНКЦИИ РАБОТЫ С БАЛАНСОМ (ОБЩАЯ ЛОГИКА С zakaz-test/bot.py)
# ─────────────────────────────────────────────────────────────────────────────
#
# Баланс пользователя — единый: живёт в общей таблице `users.balance`
# (DECIMAL DEFAULT 0). Любое изменение проходит через эти хелперы:
#
#   • get_balance(user_id)            — прочитать (int для API, Decimal внутри)
#   • get_balance_decimal(user_id)    — прочитать как Decimal (для сравнений)
#   • update_balance(uid, delta, ...) — атомарное delta с FOR UPDATE
#   • set_balance(uid, amount)        — атомарный SET с FOR UPDATE
#   • claim_bonus(uid, amount=5)      — идемпотентный бонус (ровно как bot.py)
#   • deduct_balance(uid, amount)     — списание с проверкой остатка
#
# Все операции идут под SELECT ... FOR UPDATE, чтобы исключить гонки между
# ботом и mini app. Decimal-арифметика исключает потерю точности.
#
# Контракт совместим с bot.py:update_balance / set_balance / deduct_balance /
# claim_bonus — одинаковые имена, одинаковая семантика, одинаковая идемпотентность
# бонуса (UPDATE ... WHERE bonus_claimed = FALSE).

BALANCE_MIN = Decimal("0")          # баланс не может уходить в минус


def get_balance(user_id: int) -> int:
    """Возвращает текущий баланс как int. Для UI / API контракта."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            # balance хранится как DECIMAL (общая с zakaz-test структура),
            # наружу отдаём int, чтобы не ломать клиентский контракт.
            return int(row[0]) if row and row[0] is not None else 0
    finally:
        put_conn(conn)


def get_balance_decimal(user_id: int) -> Decimal:
    """Возвращает баланс как Decimal (без потери точности)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return Decimal(row[0]) if row and row[0] is not None else Decimal("0")
    finally:
        put_conn(conn)


def _log_balance_change(conn, cur, user_id: int, before: Decimal, after: Decimal,
                        source: str, allow_negative: bool = False) -> None:
    """
    Логирует изменение баланса в balance_log. Делается через SAVEPOINT,
    чтобы падение логирования не убило основную транзакцию.

    Таблица balance_log создаётся лениво при первом вызове.
    Это общая таблица для бота и mini app — аудит-трейл всех изменений.

    NB: SAVEPOINT name включает id() пула и микросекунды — гарантированно
    уникально в пределах одной транзакции. Если всё-таки что-то падает,
    используем ОТДЕЛЬНОЕ соединение из пула — вообще не трогаем основное.
    """
    if before == after:
        return
    # Process-global counter гарантирует уникальность savepoint-имени
    # даже при двух вызовах в одну микросекунду.
    _log_balance_change_isolated.counter = getattr(_log_balance_change_isolated, "counter", 0) + 1
    sp = f"sp_balance_log_{id(conn)}_{int(time.time() * 1000000)}_{_log_balance_change_isolated.counter}"
    try:
        cur.execute(f"SAVEPOINT {sp}")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_log (
                id          BIGSERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                delta       DECIMAL NOT NULL,
                balance_before DECIMAL NOT NULL,
                balance_after  DECIMAL NOT NULL,
                source      TEXT NOT NULL,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_balance_log_user ON balance_log(user_id, created_at DESC)"
        )
        cur.execute(
            """
            INSERT INTO balance_log (user_id, delta, balance_before, balance_after, source)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, after - before, before, after, source[:64]),
        )
        cur.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception as e:
        # Не критично — основная транзакция должна жить дальше.
        log.warning(f"balance_log savepoint failed ({e}), trying isolated connection")
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        except Exception:
            pass
        # Фоллбек: отдельное соединение — вообще не трогаем основную транзакцию.
        _log_balance_change_isolated(user_id, before, after, source)


def _log_balance_change_isolated(user_id: int, before: Decimal, after: Decimal,
                                 source: str) -> None:
    """
    Фоллбек для _log_balance_change. Пишет в balance_log через ОТДЕЛЬНОЕ
    соединение из пула. Полностью изолировано от основной транзакции —
    даже если упадёт, основная транзакция не пострадает.
    """
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS balance_log (
                        id          BIGSERIAL PRIMARY KEY,
                        user_id     BIGINT NOT NULL,
                        delta       DECIMAL NOT NULL,
                        balance_before DECIMAL NOT NULL,
                        balance_after  DECIMAL NOT NULL,
                        source      TEXT NOT NULL,
                        created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_balance_log_user ON balance_log(user_id, created_at DESC)"
                )
                cur.execute(
                    """
                    INSERT INTO balance_log (user_id, delta, balance_before, balance_after, source)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (user_id, after - before, before, after, source[:64]),
                )
                conn.commit()
        finally:
            put_conn(conn)
    except Exception as e:
        log.warning(f"balance_log isolated write failed for uid={user_id}: {e}")


def update_balance(user_id: int, delta, *, allow_negative: bool = False,
                   source: str = "mini_app") -> Decimal:
    """
    Атомарно изменяет баланс пользователя на delta (может быть отрицательным).

    • SELECT ... FOR UPDATE — захватываем row lock, чтобы бот и mini app
      не наложились друг на друга с гонкой read-modify-write.
    • Decimal-арифметика — не теряем точность.
    • По умолчанию баланс не уходит в минус (raise ValueError).
    • Логируем изменение в balance_log (аудит, общая таблица с ботом).
    • Возвращаем итоговый баланс как Decimal.
    """
    delta_dec = Decimal(str(delta))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"user {user_id} not found")
            before = Decimal(row["balance"]) if row["balance"] is not None else Decimal("0")
            after = before + delta_dec
            if not allow_negative and after < BALANCE_MIN:
                raise ValueError("insufficient_balance")
            cur.execute(
                "UPDATE users SET balance = %s WHERE user_id = %s RETURNING balance",
                (after, user_id),
            )
            new_row = cur.fetchone()
            final = Decimal(new_row["balance"]) if new_row and new_row["balance"] is not None else after
            _log_balance_change(conn, cur, user_id, before, final, source=source,
                                allow_negative=allow_negative)
            conn.commit()
            return final
    finally:
        put_conn(conn)


def set_balance(user_id: int, amount, *, source: str = "mini_app") -> Decimal:
    """
    Атомарно УСТАНАВЛИВАЕТ баланс в точное значение (admin override / коррекция).
    Аналог bot.py:set_balance, но под row lock и с логированием.
    """
    new_amount = Decimal(str(amount))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"user {user_id} not found")
            before = Decimal(row["balance"]) if row["balance"] is not None else Decimal("0")
            cur.execute(
                "UPDATE users SET balance = %s WHERE user_id = %s RETURNING balance",
                (new_amount, user_id),
            )
            new_row = cur.fetchone()
            final = Decimal(new_row["balance"]) if new_row and new_row["balance"] is not None else new_amount
            _log_balance_change(conn, cur, user_id, before, final, source=source)
            conn.commit()
            return final
    finally:
        put_conn(conn)


def deduct_balance(user_id: int, amount, *, source: str = "mini_app") -> Decimal:
    """
    Списывает amount, только если баланс не уйдёт в минус.
    Аналог bot.py:deduct_balance, но под row lock (исключает гонки).
    Возвращает новый баланс. Бросает ValueError при нехватке средств.
    """
    amount_dec = Decimal(str(amount))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"user {user_id} not found")
            before = Decimal(row["balance"]) if row["balance"] is not None else Decimal("0")
            if before < amount_dec:
                raise ValueError("insufficient_balance")
            after = before - amount_dec
            cur.execute(
                "UPDATE users SET balance = %s WHERE user_id = %s RETURNING balance",
                (after, user_id),
            )
            new_row = cur.fetchone()
            final = Decimal(new_row["balance"]) if new_row and new_row["balance"] is not None else after
            _log_balance_change(conn, cur, user_id, before, final, source=source)
            conn.commit()
            return final
    finally:
        put_conn(conn)


def claim_bonus(user_id: int, amount=5, *, source: str = "mini_app") -> tuple:
    """
    Идемпотентный бонус. Ровно тот же SQL, что в bot.py:claim_bonus:
        UPDATE users SET balance = balance + 5, bonus_claimed = TRUE
        WHERE user_id = %s AND bonus_claimed = FALSE
    Если юзер уже получил бонус раньше (от бота или от mini app) — UPDATE не
    затронет ни одной строки (bonus_claimed=TRUE), и бонус не начислится.

    Возвращает (applied: bool, new_balance: Decimal).
    """
    amount_dec = Decimal(str(amount))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Атомарный UPDATE: либо строка обновилась (бонус реально новый),
            # либо ничего (бонус уже был). RETURNING даёт нам итог.
            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s, bonus_claimed = TRUE
                WHERE user_id = %s AND bonus_claimed = FALSE
                RETURNING balance
                """,
                (amount_dec, user_id),
            )
            row = cur.fetchone()
            if row is not None:
                # Бонус сработал — логируем.
                before = Decimal(row["balance"]) - amount_dec
                after = Decimal(row["balance"])
                _log_balance_change(conn, cur, user_id, before, after, source=source)
                conn.commit()
                return True, Decimal(row["balance"])
            # Бонус уже был — перечитываем текущий баланс.
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            existing = cur.fetchone()
            conn.commit()
            return False, Decimal(existing["balance"]) if existing else Decimal("0")
    finally:
        put_conn(conn)


def record_game(user_id: int, game_type: str, delta: int, win: bool, detail: str) -> dict:
    """
    Атомарно фиксирует исход игры в общей БД:
      1. SELECT ... FOR UPDATE — захватываем row lock (исключает гонки с ботом
         и с другими воркерами mini app).
      2. Decimal-арифметика — не теряем копейки.
      3. UPDATE users — баланс + статистика одним выражением.
      4. INSERT INTO transactions — аудит-лог игры (общая таблица, бот тоже может писать).
      5. balance_log — change-audit (см. update_balance).

    Используется единый путь для всех игр mini app. Бот (zakaz-test/bot.py)
    пишет в те же таблицы — рассинхрона быть не должно.
    """
    delta_dec = Decimal(str(delta))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("user not found")
            before = Decimal(row["balance"]) if row["balance"] is not None else Decimal("0")
            after = before + delta_dec
            if after < BALANCE_MIN:
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
                (after, 1 if win else 0, user_id),
            )
            stats = cur.fetchone()
            _log_balance_change(conn, cur, user_id, before, after, source=f"mini_app:game:{game_type}")
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
    """
    Корректировка баланса без изменения статистики игр
    (используется для блокировки ставки в пошаговых играх).
    Тот же row-lock + Decimal + balance_log путь, что и record_game.
    """
    delta_dec = Decimal(str(delta))
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("user not found")
            before = Decimal(row["balance"]) if row["balance"] is not None else Decimal("0")
            after = before + delta_dec
            if after < BALANCE_MIN:
                raise ValueError("insufficient_balance")
            cur.execute(
                """
                UPDATE users SET balance = %s WHERE user_id = %s
                RETURNING balance, games_played, games_won
                """,
                (after, user_id),
            )
            stats = cur.fetchone()
            _log_balance_change(conn, cur, user_id, before, after, source=f"mini_app:adjust:{game_type}")
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
    # Используем round() вместо int() — иначе при ставке 1: int(1*1.85)=1, payout==stake,
    # delta = payout - stake = 0, и при «победе» баланс не меняется (баг: 0 звёзд).
    if random.random() < 0.54:
        return {"made": True, "win": True, "payout": round(stake * 1.85), "mult": 1.85}
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
    # round() вместо int() — иначе при stake=1 и mult=1.5/1.7: int(1*1.5)=1,
    # payout==stake, delta=0, и при «победе» баланс не меняется.
    return {"mine": mine, "pick": pick, "size": size,
            "win": True, "payout": round(stake * mult), "mult": mult}


# --- 🎮 КНБ: камень-ножницы-бумага, победа x2 ---
RPS_MOVES = ["rock", "paper", "scissors"]
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def play_rps(stake: int, player_move: str):
    if player_move not in RPS_MOVES:
        player_move = "rock"
    ai_move = random.choice(RPS_MOVES)
    if player_move == ai_move:
        # Ничья: фронтенд обещал «возврат ставки», поэтому возвращаем её игроку (delta=0).
        # Раньше тут был payout:0 и фронт показывал возврат, но сервер забирал ставку — баг.
        return {"player": player_move, "ai": ai_move, "draw": True, "win": False,
                "payout": stake, "mult": 1, "refund": True}
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
        # round() вместо int() — иначе при ставке 1: int(1*1.95)=1, payout==stake,
        # delta=0, и при «победе» баланс не меняется.
        return {"result": result, "guess": guess, "win": True,
                "payout": round(stake * 1.95), "mult": 1.95}
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


# --- 🃏 Блэкджек: пошаговый режим (hit / stand) ---
# Используется для нового ручного режима с кнопками «ВЗЯТЬ» и «ОСТАНОВИТЬСЯ».
# Карты дилера: dealer[0] видна всегда, dealer[1+] скрыта, пока dealer_revealed=False.

def blackjack_new_game(stake: int):
    """Начинает новую партию блэкджека. Сдаёт по 2 карты игроку и дилеру.
    Ставка списывается отдельно на /api/game/blackjack/new."""
    deck = _new_deck()
    player = [_deal_card(deck), _deal_card(deck)]
    dealer = [_deal_card(deck), _deal_card(deck)]
    return {
        "player": player,
        "dealer": dealer,
        "player_val": _hand_value(player),
        "dealer_val": _hand_value(dealer),
        "dealer_revealed": False,  # 2-я карта дилера скрыта до конца партии
        "stake": stake,
    }


def blackjack_hit(player, dealer):
    """Игрок берёт ещё одну карту. Если перебор — игра завершается (busted)."""
    player = list(player)
    dealer = list(dealer)
    deck = _new_deck()
    player.append(_deal_card(deck))
    p_val = _hand_value(player)
    busted = p_val > 21
    return {
        "player": player,
        "dealer": dealer,
        "player_val": p_val,
        "dealer_val": _hand_value(dealer),
        "busted": busted,
        # При переборе игра заканчивается — карты дилера раскрываются (как в казино).
        "dealer_revealed": busted,
    }


def blackjack_stand(player, dealer, stake):
    """Игрок остановился. Дилер добирает до 17. Определяется исход и начисляется выигрыш.
    Возвращает НЕ финальный json для клиента — только состояние игры; баланс считает эндпоинт."""
    # Защита: на «стоп» у игрока должно быть минимум 2 карты
    if len(player) < 2:
        return {"error": "need_min_2_cards"}
    player = list(player)
    dealer = list(dealer)
    deck = _new_deck()

    p_val = _hand_value(player)
    d_val = _hand_value(dealer)
    player_blackjack = (p_val == 21 and len(player) == 2)
    dealer_blackjack = (d_val == 21 and len(dealer) == 2)

    # Дилер добирает до 17
    while d_val < 17:
        dealer.append(_deal_card(deck))
        d_val = _hand_value(dealer)
        if len(dealer) > 6:
            break

    # Определяем исход
    if player_blackjack and not dealer_blackjack:
        kind, win, mult = "blackjack", True, 2.5
    elif dealer_blackjack and not player_blackjack:
        kind, win, mult = "lose", False, 0
    elif p_val > 21:
        kind, win, mult = "bust", False, 0
    elif d_val > 21:
        kind, win, mult = "win", True, 2
    elif p_val > d_val:
        kind, win, mult = "win", True, 2
    elif p_val == d_val:
        kind, win, mult = "push", False, 0
    else:
        kind, win, mult = "lose", False, 0

    # round() чтобы не получить delta=0 при дробных mult (как с баскетболом).
    # Доп. защита: при победе игрок должен получить минимум +1 ⭐ (на случай stake=1 mult=2.5
    # где round(2.5)=2, delta=1 — это ок, но если бы mult<1 — тоже было бы +1).
    payout = round(stake * mult) if win else 0
    if win and payout <= stake:
        payout = stake + 1
    return {
        "player": player,
        "dealer": dealer,
        "player_val": p_val,
        "dealer_val": d_val,
        "win": win,
        "payout": payout,
        "mult": mult if win else 0,
        "kind": kind,
        "dealer_revealed": True,
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


@app.route("/api/debug/fix-schema", methods=["GET", "POST"])
def api_debug_fix_schema():
    """
    Принудительно перепрогоняет init_schema() и возвращает детальный
    отчёт по каждому шагу миграции (успех / ошибка с сообщением).
    Используй, если /api/auth падает с upsert_failed — этот endpoint
    дольёт недостающие колонки и поправит типы.

    Пример:
      curl https://<mini-app>/api/debug/fix-schema
    """
    try:
        results = init_schema()
        failed = [r for r in results if not r["ok"]]
        return jsonify({
            "ok": len(failed) == 0,
            "total": len(results),
            "ok_steps": len(results) - len(failed),
            "failed_steps": len(failed),
            "results": results,
        })
    except Exception as e:
        log.exception("fix-schema crashed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/debug/db", methods=["GET"])
def api_debug_db():
    """
    Диагностика: к какой БД реально подключён mini app.
    Показывает:
      - замаскированный DATABASE_URL,
      - current_database() / inet_server_addr() / version(),
      - сколько юзеров в таблице users,
      - баланс конкретного user_id (если передан ?user_id=...).

    Используй этот эндпоинт, чтобы сравнить с тем, что показывает бот:
      curl https://<mini-app-host>/api/debug/db
      curl https://<mini-app-host>/api/debug/db?user_id=123456789
    """
    try:
        # Маскируем пароль в URL для безопасности.
        masked = DATABASE_URL
        try:
            import re as _re
            masked = _re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", DATABASE_URL)
        except Exception:
            pass

        conn = get_conn()
        try:
            info = {"ok": True, "database_url": masked}
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port(), version()")
                db, addr, port, ver = cur.fetchone()
                info["current_database"] = db
                info["server_addr"] = str(addr) if addr else None
                info["server_port"] = port
                info["pg_version"] = ver

                cur.execute("SELECT COUNT(*) FROM users")
                info["users_count"] = cur.fetchone()[0]

                # Если передали user_id — покажем его реальный баланс из БД.
                uid = request.args.get("user_id", type=int)
                if uid is not None:
                    cur.execute(
                        "SELECT user_id, username, balance, bonus_claimed, games_played "
                        "FROM users WHERE user_id = %s",
                        (uid,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        info["user"] = None
                    else:
                        info["user"] = {
                            "user_id": row[0],
                            "username": row[1],
                            "balance": int(row[2]) if row[2] is not None else 0,
                            "bonus_claimed": row[3],
                            "games_played": row[4],
                        }
            return jsonify(info)
        finally:
            put_conn(conn)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/auth", methods=["POST"])
@require_auth
def api_auth():
    """
    Авторизация + апсёрт профиля. Всегда возвращает 200 — при любом сбое
    отдаёт {"ok": False, "error": "..."}, чтобы фронт не показывал
    «Не удалось обновить профиль» из-за 500.
    """
    tg_user = request.tg_user
    try:
        result = upsert_user(tg_user)
        u = result["user"]
        return jsonify({
            "ok": True,
            "is_new": result["is_new"],
            "user": {
                "id": u["user_id"],
                "username": u.get("username"),
                "first_name": u.get("first_name"),
                "last_name": u.get("last_name"),
                "photo_url": u.get("photo_url"),
                "balance": int(u.get("balance") or 0),
                "games_played": int(u.get("games_played") or 0),
                "games_won": int(u.get("games_won") or 0),
            },
        })
    except Exception as e:
        # upsert_user сам по себе bulletproof, но на всякий случай — последний рубеж.
        # Всё равно возвращаем 200 + минимальный профиль, чтобы фронт не падал.
        log.exception("api_auth: upsert_user blew up despite bulletproof wrapper")
        try:
            uid = int(tg_user["id"])
        except Exception:
            uid = 0
        return jsonify({
            "ok": False,
            "error": "upsert_failed",
            "detail": str(e),
            "user": {
                "id": uid,
                "username": tg_user.get("username"),
                "first_name": tg_user.get("first_name"),
                "last_name": tg_user.get("last_name"),
                "photo_url": tg_user.get("photo_url"),
                "balance": 0,
                "games_played": 0,
                "games_won": 0,
            },
        })


# HOTFIX 2026-06-27: добавили POST + OPTIONS, чтобы Mini App не получал 405
# при fetch('/api/user') с телом или заголовками.
@app.route("/api/user", methods=["GET", "POST", "OPTIONS"])
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


# HOTFIX 2026-06-27: добавили POST + OPTIONS — основная причина «Ошибка
# соединения с общей БД». Mini App / прокси шлют POST, Flask возвращал 405,
# fetch видел !response.ok и фронт показывал это сообщение.
@app.route("/api/balance", methods=["GET", "POST", "OPTIONS"])
@require_auth
def api_balance():
    """
    Лёгкий эндпоинт только-для-чтения. Возвращает баланс и базовую статистику
    БЕЗ upsert_user — даже если /api/auth упал из-за временной проблемы со схемой,
    фронт сможет дёрнуть сюда и показать пользователю реальный баланс из общей БД.
    """
    uid = int(request.tg_user["id"])
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, username, balance, games_played, games_won "
                "FROM users WHERE user_id = %s",
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return jsonify({
                "ok": True,
                "user": {
                    "id": row["user_id"],
                    "username": row["username"],
                    "balance": row["balance"],
                    "games_played": row["games_played"],
                    "games_won": row["games_won"],
                },
            })
    finally:
        put_conn(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ████████████  СИНХРОНИЗАЦИЯ БАЛАНСА С zakaz-test/bot.py  █████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
#
# База у mini app и бота общая (одна и та же PostgreSQL, таблица users.balance).
# Это значит:
#   • Чтение из любого из сервисов видит одни и те же данные (гарантия
#     транзакционной целостности PostgreSQL + наши row-lock'и).
#   • Любое изменение, сделанное ботом, видно mini app при следующем запросе,
#     и наоборот.
#
# Чтобы поддержать явную синхронизацию (например, когда фронт mini app открыт
# параллельно с ботом и хочет «до-проверить» баланс) — есть три эндпоинта:
#
#   GET  /api/sync/balance              → текущий баланс из общей БД (канонический)
#   POST /api/sync/recalc               → если есть подозрение на drift,
#                                          перепроверяет и возвращает разницу
#   POST /api/sync/apply                → атомарное обновление баланса из бота
#                                          (бот тоже может позвать для применения
#                                          изменений, сделанных вне транзакции)
#
# Дополнительно: таблица balance_log (создаётся лениво при первом изменении)
# хранит полный аудит-трейл — кто, когда, откуда (source) изменил баланс.
# Это позволяет восстановить consensus, если что-то пошло не так.

# HOTFIX 2026-06-27: добавили POST + OPTIONS — симметрично с /api/balance.
@app.route("/api/sync/balance", methods=["GET", "POST", "OPTIONS"])
@require_auth
def api_sync_balance():
    """
    Канонический баланс пользователя из общей БД.
    Используй, если на клиенте сохранён старый/кэшированный баланс —
    этот эндпоинт всегда вернёт актуальное значение.
    """
    uid = int(request.tg_user["id"])
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT user_id, username, balance, games_played, games_won,
                       bonus_claimed, last_seen
                FROM users WHERE user_id = %s
                """,
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return jsonify({
                "ok": True,
                "balance": int(row["balance"]) if row["balance"] is not None else 0,
                "balance_decimal": str(row["balance"]) if row["balance"] is not None else "0",
                "games_played": row["games_played"],
                "games_won": row["games_won"],
                "bonus_claimed": row["bonus_claimed"],
                "last_seen": row["last_seen"].isoformat() if row.get("last_seen") else None,
            })
    finally:
        put_conn(conn)


@app.route("/api/sync/recalc", methods=["POST"])
@require_auth
def api_sync_recalc():
    """
    Перепроверка баланса. Клиент (фронт mini app) шлёт свой текущий known_balance
    (то, что у него отрисовано). Сервер сверяется с каноническим балансом в БД
    и возвращает:
      • drift = canonical - known   (если drift != 0 — клиент должен обновить UI)
      • last_change — последняя запись в balance_log, чтобы понять, кто поменял
    Это нужно, когда mini app открыт параллельно с ботом: бот пополнил баланс,
    а UI mini app ещё показывает старое значение.
    """
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    known_balance = body.get("known_balance")
    try:
        known = int(known_balance) if known_balance is not None else None
    except (TypeError, ValueError):
        known = None

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT balance, games_played, games_won FROM users WHERE user_id = %s",
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404
            canonical = int(row["balance"]) if row["balance"] is not None else 0
            drift = (canonical - known) if known is not None else 0

            last_change = None
            try:
                cur.execute(
                    """
                    SELECT delta, balance_before, balance_after, source, created_at
                    FROM balance_log
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (uid,),
                )
                lc = cur.fetchone()
                if lc:
                    last_change = {
                        "delta": str(lc["delta"]),
                        "balance_before": str(lc["balance_before"]),
                        "balance_after": str(lc["balance_after"]),
                        "source": lc["source"],
                        "created_at": lc["created_at"].isoformat() if lc.get("created_at") else None,
                    }
            except Exception:
                # balance_log может не существовать в очень старых инсталляциях — не критично.
                pass

            return jsonify({
                "ok": True,
                "canonical_balance": canonical,
                "known_balance": known,
                "drift": drift,
                "needs_update": known is not None and drift != 0,
                "last_change": last_change,
                "games_played": row["games_played"],
                "games_won": row["games_won"],
            })
    finally:
        put_conn(conn)


@app.route("/api/sync/apply", methods=["POST"])
@require_auth
def api_sync_apply():
    """
    Атомарное изменение баланса через канонический хелпер update_balance.
    Поддерживает и +, и -. Используется, если клиенту нужно сразу применить
    дельту (например, бот прислал push «+10 ⭐», и mini app подтверждает это
    через /api/sync/apply вместо отдельного эндпоинта бота).

    Body:
        { "delta": <int|float>, "source": "<optional source label>" }
    """
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    if "delta" not in body:
        return jsonify({"ok": False, "error": "missing_delta"}), 400
    try:
        delta = Decimal(str(body["delta"]))
    except Exception:
        return jsonify({"ok": False, "error": "bad_delta"}), 400
    source = str(body.get("source", "mini_app:sync_apply"))[:64]
    try:
        new_balance = update_balance(uid, delta, source=source)
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "insufficient_balance",
            "balance": int(get_balance(uid)),
        }), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({
        "ok": True,
        "balance": int(new_balance),
        "balance_decimal": str(new_balance),
        "applied_delta": str(delta),
    })


@app.route("/api/sync/bonus", methods=["POST"])
@require_auth
def api_sync_bonus():
    """
    Идемпотентный бонус. Если юзеру ещё не начислялся welcome-бонус — начислит +5.
    Если уже был (от бота или от mini app) — вернёт applied=False.
    Использует тот же SQL, что и bot.py:claim_bonus (UPDATE ... WHERE bonus_claimed=FALSE).
    """
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    amount = Decimal(str(body.get("amount", 5)))
    try:
        applied, new_balance = claim_bonus(uid, amount=amount, source="mini_app:bonus")
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({
        "ok": True,
        "applied": applied,
        "balance": int(new_balance),
        "balance_decimal": str(new_balance),
    })


def _commit_game(uid: int, game_type: str, stake: int, result: dict):
    """Записать результат игры в БД и вернуть jsonify."""
    payout = int(result.get("payout", 0))
    # Защита: при победе игрок должен получить хотя бы +1 ⭐ от ставки.
    # Иначе при stake=1 и слабых дробных mult (например, mines 1.3x или blackjack 2.5x,
    # где round(2.5)=2) payout==stake, delta=0, и при «победе» баланс не меняется.
    # Это общий фикс для ВСЕХ однокнопочных игр.
    if result.get("win") and payout <= stake:
        payout = stake + 1
        if isinstance(result, dict):
            result["payout"] = payout  # фронт тоже видит скорректированную выплату
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
        # Ставка уже списана при /api/game/ttt/new (adjust_balance(-stake)),
        # теперь возвращаем полный payout (stake*2). Раньше тут было payout-stake,
        # и после −stake в new игрок получал net=0 (x2 не работал, баг).
        delta = state["payout"]  # для stake=1: +2, после −1 в new → net +1 ⭐
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
    """Старый авто-режим блэкджека (оставлен для обратной совместимости).
    Для нового режима с hit/stand используй /api/game/blackjack/{new,hit,stand}."""
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


# --- Пошаговый блэкджек: новые эндпоинты для ручного режима (hit / stand) ---

@app.route("/api/game/blackjack/new", methods=["POST"])
@require_auth
def api_blackjack_new():
    """Начало партии: сдаём по 2 карты, списываем ставку.
    Возвращает состояние со скрытой 2-й картой дилера."""
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    if get_balance(uid) < stake:
        return jsonify({"ok": False, "error": "Недостаточно звёзд", "balance": get_balance(uid)}), 400
    try:
        state = blackjack_new_game(stake)
    except Exception:
        log.exception("blackjack_new error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    # Списываем ставку (как блокировка средств до окончания партии)
    try:
        stats = adjust_balance(uid, -stake, "blackjack", "Ставка заблокирована")
    except ValueError:
        current = get_balance(uid)
        return jsonify({"ok": False, "error": "insufficient_balance", "balance": current}), 400
    return jsonify({
        "ok": True,
        "stake": stake,
        "player": state["player"],
        "dealer": state["dealer"],
        "player_val": state["player_val"],
        # Скрываем реальное значение дилера пока не раскрыт — на фронте покажем «?»
        "dealer_val": state["dealer_val"] if state["dealer_revealed"] else None,
        "dealer_revealed": state["dealer_revealed"],
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


@app.route("/api/game/blackjack/hit", methods=["POST"])
@require_auth
def api_blackjack_hit():
    """Игрок берёт ещё карту. При переборе (>21) — поражение, списываем игру в статистике."""
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    player = body.get("player")
    dealer = body.get("dealer")
    if not isinstance(player, list) or not isinstance(dealer, list):
        return jsonify({"ok": False, "error": "bad_hands"}), 400
    try:
        state = blackjack_hit(player, dealer)
    except Exception:
        log.exception("blackjack_hit error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500
    if state.get("busted"):
        # При переборе списываем поражение в статистике; баланс уже изменён на /new
        stats = record_game(uid, "blackjack", 0, False,
                            json.dumps({"outcome": "bust"}, default=str))
        outcome = "bust"
        win = False
        payout = 0
        mult = 0
        kind = "bust"
    else:
        # Партия идёт дальше — статистику не трогаем
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT balance, games_played, games_won FROM users WHERE user_id = %s", (uid,))
                stats = cur.fetchone()
        finally:
            put_conn(conn)
        outcome = "continue"
        win = False
        payout = 0
        mult = 0
        kind = "continue"
    return jsonify({
        "ok": True,
        "player": state["player"],
        "dealer": state["dealer"],
        "player_val": state["player_val"],
        "dealer_val": state["dealer_val"] if state["dealer_revealed"] else None,
        "busted": state["busted"],
        "dealer_revealed": state["dealer_revealed"],
        "outcome": outcome,
        "win": win,
        "payout": payout,
        "mult": mult,
        "kind": kind,
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


@app.route("/api/game/blackjack/stand", methods=["POST"])
@require_auth
def api_blackjack_stand():
    """Игрок остановился. Дилер играет, определяется исход, начисляется выигрыш.
    Серверная защита: у игрока должно быть минимум 2 карты (иначе 400)."""
    uid = int(request.tg_user["id"])
    body = request.get_json(silent=True) or {}
    stake, err, code = _validate_stake(body)
    if err:
        return err, code
    player = body.get("player")
    dealer = body.get("dealer")
    if not isinstance(player, list) or not isinstance(dealer, list):
        return jsonify({"ok": False, "error": "bad_hands"}), 400
    # Серверная защита: нельзя остановиться с <2 карт на руках (по запросу)
    if len(player) < 2:
        return jsonify({"ok": False, "error": "Нужно минимум 2 карты, чтобы остановиться"}), 400
    try:
        state = blackjack_stand(player, dealer, stake)
    except Exception:
        log.exception("blackjack_stand error")
        return jsonify({"ok": False, "error": "Ошибка игры"}), 500

    # Ставка уже списана на /new. На win возвращаем полный payout (после -stake в new → net +stake).
    if state.get("win"):
        delta = state["payout"]
        stats = record_game(uid, "blackjack", delta, True,
                            json.dumps({"outcome": state["kind"]}, default=str))
    else:
        # Поражение/ничья — фиксируем в статистике, баланс уже списан
        stats = record_game(uid, "blackjack", 0, False,
                            json.dumps({"outcome": state["kind"]}, default=str))

    return jsonify({
        "ok": True,
        "player": state["player"],
        "dealer": state["dealer"],
        "player_val": state["player_val"],
        "dealer_val": state["dealer_val"],
        "win": state["win"],
        "payout": state["payout"],
        "mult": state["mult"],
        "kind": state["kind"],
        "dealer_revealed": state["dealer_revealed"],
        "balance": stats["balance"],
        "games_played": stats["games_played"],
        "games_won": stats["games_won"],
    })


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

  /* ===== BALANCE DIAGNOSTIC BANNER ===== */
  .balance-banner {
    margin: 10px 14px 0; padding: 10px 12px;
    border-radius: 12px; font-size: 12px;
    display: none; align-items: flex-start; gap: 10px;
    background: rgba(255, 80, 80, 0.12);
    border: 1px solid rgba(255, 80, 80, 0.45);
    color: #ffb3b3;
  }
  .balance-banner.show { display: flex; }
  .balance-banner.warn {
    background: rgba(247, 201, 72, 0.12);
    border-color: rgba(247, 201, 72, 0.45);
    color: #f7c948;
  }
  .balance-banner.ok {
    background: rgba(80, 200, 120, 0.10);
    border-color: rgba(80, 200, 120, 0.40);
    color: #7fdba0;
  }
  .balance-banner .icon {
    font-size: 18px; line-height: 1; flex: 0 0 auto;
  }
  .balance-banner .body { flex: 1; }
  .balance-banner .title { font-weight: 800; margin-bottom: 3px; }
  .balance-banner .meta { font-size: 11px; opacity: 0.85; font-family: ui-monospace, monospace; word-break: break-all; }
  .balance-banner .close {
    cursor: pointer; opacity: 0.7; padding: 0 4px;
    font-size: 16px; line-height: 1;
  }
  .balance-banner .close:hover { opacity: 1; }

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

  /* ===== NEW: STAKE INPUT (только ввод, без пресетов) ===== */
  .stake-row {
    margin: 14px 0;
    padding: 0;
  }
  .stake-row .custom-stake-wrap {
    display: flex; flex-direction: column; gap: 8px;
    width: 100%;
  }
  .stake-row .custom-stake-wrap > label {
    font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1.5px;
    text-align: center; font-weight: 700;
  }
  .stake-input-group {
    display: flex; align-items: stretch;
    background: linear-gradient(135deg, rgba(255,214,10,0.10), rgba(255,170,0,0.04));
    border: 2px solid var(--gold);
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 4px 16px rgba(255,214,10,0.20), inset 0 0 12px rgba(255,214,10,0.05);
  }
  .stake-adjust {
    width: 56px; flex-shrink: 0;
    border: none; background: transparent;
    color: var(--gold); font-size: 26px; font-weight: 800;
    cursor: pointer; transition: 0.12s;
    border-right: 1px solid rgba(255,214,10,0.25);
  }
  .stake-adjust:last-child { border-right: none; border-left: 1px solid rgba(255,214,10,0.25); }
  .stake-adjust:active { background: rgba(255,214,10,0.18); }
  .custom-stake-input {
    flex: 1;
    padding: 14px 8px;
    border: none;
    background: transparent;
    color: var(--text);
    font-weight: 900; font-size: 24px;
    text-align: center;
    outline: none;
    -moz-appearance: textfield;
    font-family: inherit;
  }
  .custom-stake-input::-webkit-outer-spin-button,
  .custom-stake-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
  .stake-quick-row {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;
  }
  .quick-stake {
    padding: 8px 0;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--text);
    font-weight: 800; font-size: 11px;
    cursor: pointer; transition: 0.12s;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .quick-stake:active { transform: scale(0.95); background: rgba(255,214,10,0.18); }
  .quick-stake:hover { border-color: var(--gold); color: var(--gold); }

  /* ===== NEW: HERO BANNER ===== */
  .hero-banner {
    position: relative;
    padding: 18px 18px 16px;
    border-radius: 20px;
    margin-bottom: 14px;
    background:
      linear-gradient(135deg, rgba(255,214,10,0.16), rgba(255,170,0,0.04) 60%, transparent),
      linear-gradient(180deg, #1a0f00 0%, #0a0500 100%);
    border: 2px solid var(--gold);
    box-shadow: 0 8px 28px rgba(255,214,10,0.20), inset 0 0 24px rgba(255,214,10,0.05);
    overflow: hidden;
  }
  .hero-banner::before {
    content: ""; position: absolute; inset: 0;
    background:
      radial-gradient(ellipse at top right, rgba(255,214,10,0.20), transparent 55%),
      radial-gradient(ellipse at bottom left, rgba(255,170,0,0.10), transparent 50%);
    animation: heroGlow 4s ease-in-out infinite;
    pointer-events: none;
  }
  @keyframes heroGlow {
    0%, 100% { opacity: 0.7; }
    50% { opacity: 1; }
  }
  .hero-content { position: relative; z-index: 1; }
  .hero-title {
    font-size: 22px; font-weight: 900;
    background: linear-gradient(90deg, #f7c948, #fff5b3, #f7c948, #fff5b3);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    background-size: 220% 100%;
    animation: shimmer 3s linear infinite;
    margin: 0 0 4px;
    line-height: 1.15;
  }
  .hero-sub {
    font-size: 12px; color: rgba(255,255,255,0.7);
    margin: 0 0 12px;
    line-height: 1.4;
  }
  .hero-cta {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 18px;
    border-radius: 30px;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000;
    font-weight: 800; font-size: 13px;
    border: none; cursor: pointer;
    box-shadow: 0 4px 14px rgba(255,214,10,0.5);
    text-transform: uppercase; letter-spacing: 0.5px;
    transition: 0.12s;
    font-family: inherit;
  }
  .hero-cta:active { transform: scale(0.97); }
  .hero-sparkle {
    position: absolute; top: 10px; right: 14px;
    font-size: 20px;
    animation: spinSlow 6s linear infinite;
  }
  @keyframes spinSlow { to { transform: rotate(360deg); } }


  /* ===== NEW: GAME CARD BADGES & IMPROVEMENTS ===== */
  .games { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .game-card {
    position: relative;
    padding: 18px 8px 12px;
    border-radius: 18px;
    background: linear-gradient(160deg, var(--card), rgba(0,0,0,0.5));
    border: 1px solid var(--border);
    text-align: center; cursor: pointer;
    transition: transform 0.12s, box-shadow 0.18s, border-color 0.18s;
    overflow: hidden;
  }
  .game-card::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(135deg, transparent 40%, rgba(255,214,10,0.16));
    opacity: 0; transition: opacity 0.18s;
    pointer-events: none;
  }
  .game-card:active { transform: scale(0.96); }
  .game-card:hover {
    border-color: var(--gold);
    box-shadow: 0 8px 24px rgba(255,214,10,0.22);
  }
  .game-card:hover::before { opacity: 1; }
  .game-card .game-badge {
    position: absolute; top: 6px; right: 6px;
    padding: 2px 7px; border-radius: 8px;
    font-size: 9px; font-weight: 800;
    text-transform: uppercase; letter-spacing: 0.6px;
    z-index: 2;
  }
  .game-card .game-badge.hot {
    background: linear-gradient(135deg, #ef4444, #b91c1c);
    color: white;
    box-shadow: 0 0 8px rgba(239,68,68,0.55);
    animation: badgePulse 2s ease-in-out infinite;
  }
  .game-card .game-badge.new {
    background: linear-gradient(135deg, #10b981, #047857);
    color: white;
    box-shadow: 0 0 8px rgba(16,185,129,0.45);
  }
  .game-card .game-badge.top {
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000;
    box-shadow: 0 0 8px rgba(255,214,10,0.55);
  }
  @keyframes badgePulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.06); }
  }
  .game-icon-wrap {
    width: 56px; height: 56px; margin: 0 auto 8px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(255,214,10,0.18), rgba(255,170,0,0.06));
    display: flex; align-items: center; justify-content: center;
    border: 1px solid rgba(255,214,10,0.28);
    position: relative; z-index: 1;
  }
  .game-icon {
    font-size: 30px; display: block;
    filter: drop-shadow(0 0 6px rgba(255,214,10,0.3));
  }
  .game-name {
    font-weight: 800; font-size: 13px;
    position: relative; z-index: 1;
  }
  .game-sub {
    font-size: 10px; color: var(--muted);
    margin-top: 3px;
    position: relative; z-index: 1;
  }
  .game-mult {
    display: inline-block; margin-top: 6px;
    padding: 2px 8px; border-radius: 6px;
    background: rgba(255,214,10,0.12);
    color: var(--gold);
    font-size: 10px; font-weight: 800;
    position: relative; z-index: 1;
  }

  /* ===== NEW: STATS BLOCK ===== */
  .stats {
    margin-top: 16px; padding: 16px 14px;
    border-radius: 18px;
    background: linear-gradient(135deg, rgba(255,214,10,0.06), rgba(0,0,0,0.3));
    border: 1px solid var(--border);
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }
  .stat-cell { text-align: center; }
  .stat-val {
    font-size: 22px; font-weight: 900;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    line-height: 1.1;
  }
  .stat-lbl {
    font-size: 9px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1.2px;
    margin-top: 4px;
    font-weight: 700;
  }
  .stat-bar {
    grid-column: 1 / -1;
    height: 6px; background: rgba(255,255,255,0.08);
    border-radius: 3px; overflow: hidden;
    margin-top: 6px;
    position: relative;
  }
  .stat-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--gold3), var(--gold), var(--gold2));
    border-radius: 3px;
    transition: width 0.6s cubic-bezier(.4,1.4,.5,1);
    box-shadow: 0 0 8px rgba(255,214,10,0.4);
  }

  /* ===== NEW: HEADER EXTRA ===== */
  .header {
    display: flex; align-items: center; gap: 12px;
    padding: 14px;
    border-radius: 18px;
    background: linear-gradient(135deg, rgba(255,214,10,0.14), rgba(255,234,0,0.10));
    border: 1px solid var(--border);
    box-shadow: 0 8px 32px rgba(255,214,10,0.14);
    margin-bottom: 14px;
  }
  .avatar {
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, var(--gold), var(--purple));
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; font-weight: 700; color: #000000;
    border: 2px solid var(--gold);
    overflow: hidden; flex-shrink: 0;
    box-shadow: 0 0 12px rgba(255,214,10,0.3);
  }
  .balance-box {
    text-align: right; padding: 6px 12px; border-radius: 12px;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    color: #000000;
    box-shadow: 0 4px 14px rgba(255,214,10,0.35);
    min-width: 86px;
  }
  .balance-val { font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 4px; justify-content: flex-end; }

  /* Modal close button (used in profile & others) */
  .modal-close {
    position: absolute; top: 12px; right: 12px;
    width: 32px; height: 32px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: rgba(0,0,0,0.4);
    color: var(--gold);
    font-size: 16px; font-weight: 800;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
    transition: transform 0.14s ease, background 0.14s ease, color 0.14s ease;
    z-index: 5;
  }
  .modal-close:active { transform: scale(0.9); }
  .modal-close:hover { background: rgba(255,214,10,0.18); color: #fff; }

  /* ===== NEW: BOTTOM NAVIGATION BAR ===== */
  .bottom-nav {
    position: fixed;
    left: 8px; right: 8px; bottom: 8px;
    z-index: 90;
    display: flex; gap: 8px;
    padding: 6px;
    border-radius: 22px;
    background: linear-gradient(135deg, rgba(20,20,20,0.92), rgba(10,10,10,0.95));
    border: 1px solid rgba(255,214,10,0.30);
    box-shadow:
      0 12px 30px rgba(0,0,0,0.55),
      0 0 22px rgba(255,214,10,0.10),
      inset 0 0 14px rgba(255,214,10,0.06);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    max-width: 520px;
    margin: 0 auto;
    transition: transform 0.32s cubic-bezier(.4,1.4,.5,1), opacity 0.22s ease;
    transform: translateY(0);
    opacity: 1;
  }
  .bottom-nav.hidden {
    transform: translateY(140%);
    opacity: 0;
    pointer-events: none;
  }
  .nav-btn {
    flex: 1;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 2px;
    padding: 9px 6px;
    border-radius: 16px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    font-family: inherit;
    font-size: 11px; font-weight: 800;
    text-transform: uppercase; letter-spacing: 0.6px;
    cursor: pointer;
    transition: transform 0.16s ease, background 0.16s ease, color 0.16s ease, border-color 0.16s ease;
    position: relative;
    overflow: hidden;
    -webkit-tap-highlight-color: transparent;
  }
  .nav-btn:active { transform: scale(0.94); }
  .nav-btn .nav-icon {
    font-size: 22px; line-height: 1;
    filter: drop-shadow(0 0 4px rgba(255,214,10,0));
    transition: filter 0.2s ease, transform 0.2s ease;
  }
  .nav-btn.active {
    background: linear-gradient(135deg, rgba(255,214,10,0.20), rgba(255,170,0,0.06));
    border-color: rgba(255,214,10,0.55);
    color: var(--gold);
    box-shadow: 0 0 14px rgba(255,214,10,0.18), inset 0 0 10px rgba(255,214,10,0.05);
  }
  .nav-btn.active .nav-icon {
    filter: drop-shadow(0 0 8px rgba(255,214,10,0.65));
    animation: navIconPop 0.42s cubic-bezier(.4,1.6,.5,1);
  }
  .nav-btn::before {
    content: ""; position: absolute; left: 50%; top: 50%;
    width: 0; height: 0;
    border-radius: 50%;
    background: rgba(255,214,10,0.30);
    transform: translate(-50%, -50%);
    transition: width 0.4s ease, height 0.4s ease, opacity 0.4s ease;
    opacity: 0;
    pointer-events: none;
  }
  .nav-btn:active::before {
    width: 140%; height: 140%;
    opacity: 1;
    transition: width 0s, height 0s, opacity 0s;
  }
  @keyframes navIconPop {
    0%   { transform: scale(1); }
    45%  { transform: scale(1.28) rotate(-8deg); }
    100% { transform: scale(1) rotate(0); }
  }
  @keyframes navBarIn {
    from { transform: translateY(140%); opacity: 0; }
    to   { transform: translateY(0);    opacity: 1; }
  }
  .app.with-nav { padding-bottom: 88px; }

  /* ===== NEW: PROFILE MODAL ===== */
  .profile-modal-content {
    text-align: center;
    padding: 8px 4px 4px;
  }
  .profile-avatar {
    width: 92px; height: 92px;
    border-radius: 50%;
    margin: 0 auto 14px;
    background: linear-gradient(135deg, var(--gold), var(--purple));
    display: flex; align-items: center; justify-content: center;
    font-size: 38px; font-weight: 900; color: #000;
    border: 3px solid var(--gold);
    overflow: hidden;
    box-shadow: 0 0 22px rgba(255,214,10,0.4);
    animation: profileFloat 4s ease-in-out infinite;
  }
  .profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
  @keyframes profileFloat {
    0%, 100% { transform: translateY(0) scale(1); }
    50%      { transform: translateY(-4px) scale(1.03); }
  }
  .profile-name {
    font-size: 20px; font-weight: 900;
    background: linear-gradient(90deg, var(--gold), var(--gold2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    margin-bottom: 2px;
  }
  .profile-handle {
    font-size: 12px; color: var(--muted); margin-bottom: 18px;
  }
  .profile-balance-card {
    margin: 14px 0;
    padding: 18px 16px;
    border-radius: 18px;
    background: linear-gradient(135deg, rgba(255,214,10,0.16), rgba(255,170,0,0.04));
    border: 2px solid var(--gold);
    box-shadow: 0 6px 22px rgba(255,214,10,0.20);
    animation: cardPulse 3s ease-in-out infinite;
  }
  @keyframes cardPulse {
    0%, 100% { box-shadow: 0 6px 22px rgba(255,214,10,0.20); }
    50%      { box-shadow: 0 6px 32px rgba(255,214,10,0.45); }
  }
  .profile-balance-label {
    font-size: 11px; color: rgba(0,0,0,0.7); font-weight: 800;
    text-transform: uppercase; letter-spacing: 1.5px;
    color: #5a4400;
  }
  .profile-balance-val {
    font-size: 40px; font-weight: 900; color: #000;
    display: flex; align-items: center; justify-content: center; gap: 8px;
    margin-top: 4px;
    text-shadow: 0 2px 0 rgba(255,255,255,0.2);
  }
  .profile-balance-val svg { width: 28px; height: 28px; }
  .profile-stats-grid {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 8px; margin: 14px 0 6px;
  }
  .profile-stat {
    padding: 12px 6px;
    border-radius: 14px;
    background: var(--card);
    border: 1px solid var(--border);
    transition: transform 0.18s ease, border-color 0.18s ease;
  }
  .profile-stat:active { transform: scale(0.96); }
  .profile-stat-val {
    font-size: 20px; font-weight: 900;
    background: linear-gradient(135deg, var(--gold), var(--gold2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .profile-stat-lbl {
    font-size: 9px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1px;
    margin-top: 4px; font-weight: 700;
  }
  .profile-section-title {
    margin: 18px 0 8px;
    font-size: 11px; font-weight: 800;
    color: var(--muted);
    text-transform: uppercase; letter-spacing: 1.5px;
    display: flex; align-items: center; gap: 8px;
  }
  .profile-section-title::before, .profile-section-title::after {
    content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, transparent, var(--border), transparent);
  }
  .profile-tip {
    padding: 12px 14px;
    border-radius: 12px;
    background: rgba(255,214,10,0.06);
    border: 1px solid rgba(255,214,10,0.20);
    font-size: 12px;
    color: rgba(255,255,255,0.75);
    line-height: 1.5;
    text-align: left;
  }
  .profile-tip b { color: var(--gold); }

  /* ===== NEW: MODES SCREEN (полноэкранный список режимов) ===== */
  .modes-screen {
    position: fixed; inset: 0;
    z-index: 95;
    background: radial-gradient(ellipse at top, #1a1a0a 0%, var(--bg) 60%);
    display: none;
    flex-direction: column;
    padding: 16px 16px 100px;
    overflow-y: auto;
    animation: modesScreenIn 0.28s cubic-bezier(.2,.8,.3,1);
  }
  .modes-screen.show { display: flex; }
  @keyframes modesScreenIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .modes-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
    padding: 6px 4px;
  }
  .modes-header h2 {
    margin: 0; font-size: 24px; font-weight: 900;
    background: linear-gradient(90deg, #b8860b, #f7c948, #fff5b3, #f7c948, #b8860b);
    background-size: 220% 100%;
    -webkit-background-clip: text; background-clip: text; color: transparent;
    animation: shimmer 3s linear infinite;
  }
  .modes-close {
    width: 36px; height: 36px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--gold);
    font-size: 18px; font-weight: 800;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
    transition: 0.12s;
  }
  .modes-close:active { transform: scale(0.92); }
  .modes-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .modes-grid .game-card {
    opacity: 0;
    animation: cardFadeUp 0.42s cubic-bezier(.2,.8,.3,1) forwards;
  }

  /* ===== NEW: GAME CARD ANIMATION ON LOAD ===== */
  .games .game-card {
    opacity: 0;
    transform: translateY(8px);
    animation: cardFadeUp 0.42s cubic-bezier(.2,.8,.3,1) forwards;
  }
  .games .game-card:nth-child(1) { animation-delay: 0.04s; }
  .games .game-card:nth-child(2) { animation-delay: 0.10s; }
  .games .game-card:nth-child(3) { animation-delay: 0.16s; }
  .games .game-card:nth-child(4) { animation-delay: 0.22s; }
  .games .game-card:nth-child(5) { animation-delay: 0.28s; }
  .games .game-card:nth-child(6) { animation-delay: 0.34s; }
  .games .game-card:nth-child(7) { animation-delay: 0.40s; }
  .games .game-card:nth-child(8) { animation-delay: 0.46s; }
  .games .game-card:nth-child(9) { animation-delay: 0.52s; }
  @keyframes cardFadeUp {
    from { opacity: 0; transform: translateY(12px) scale(0.96); }
    to   { opacity: 1; transform: translateY(0)    scale(1); }
  }
  .game-card:hover {
    transform: translateY(-2px);
    border-color: var(--gold);
    box-shadow: 0 8px 22px rgba(255,214,10,0.20);
  }
  .game-card .game-icon-wrap {
    transition: transform 0.32s cubic-bezier(.4,1.6,.5,1);
  }
  .game-card:active .game-icon-wrap { transform: scale(0.85) rotate(-8deg); }
  .game-card:hover .game-icon-wrap {
    animation: iconBounce 0.55s ease;
  }
  @keyframes iconBounce {
    0%, 100% { transform: translateY(0)    rotate(0); }
    30%      { transform: translateY(-4px) rotate(-6deg); }
    60%      { transform: translateY(-2px) rotate(4deg); }
  }

  /* ===== NEW: BALANCE FLASH ANIMATION ===== */
  @keyframes balanceUp {
    0%   { transform: scale(1); color: var(--gold); }
    40%  { transform: scale(1.18); color: #fff; text-shadow: 0 0 14px var(--gold); }
    100% { transform: scale(1); color: var(--gold); }
  }
  .balance-val.flash-up   { animation: balanceUp 0.55s ease; }
  .balance-val.flash-down { animation: balanceDown 0.55s ease; }
  @keyframes balanceDown {
    0%   { transform: scale(1); }
    40%  { transform: scale(1.15); }
    100% { transform: scale(1); }
  }
  .balance-box.flash-win  { animation: balanceBoxWin 0.7s ease; }
  @keyframes balanceBoxWin {
    0%, 100% { box-shadow: 0 4px 14px rgba(255,214,10,0.35); }
    50%      { box-shadow: 0 0 26px rgba(255,214,10,0.95), 0 0 50px rgba(255,214,10,0.4); }
  }
  .balance-box.flash-lose { animation: balanceBoxLose 0.55s ease; }
  @keyframes balanceBoxLose {
    0%, 100% { filter: brightness(1); }
    50%      { filter: brightness(0.7) saturate(0.6); }
  }

  /* ===== NEW: HEADER & HERO ENTRY ===== */
  .header { animation: headerIn 0.45s cubic-bezier(.2,.8,.3,1); }
  @keyframes headerIn {
    from { opacity: 0; transform: translateY(-10px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .hero-banner { animation: heroIn 0.5s 0.05s cubic-bezier(.2,.8,.3,1) both; }
  @keyframes heroIn {
    from { opacity: 0; transform: translateY(14px) scale(0.98); }
    to   { opacity: 1; transform: translateY(0)    scale(1); }
  }
  .section-title {
    animation: sectionIn 0.4s cubic-bezier(.2,.8,.3,1) both;
  }
  @keyframes sectionIn {
    from { opacity: 0; transform: translateX(-8px); }
    to   { opacity: 1; transform: translateX(0); }
  }
  .stats { animation: headerIn 0.45s 0.2s cubic-bezier(.2,.8,.3,1) both; }

  /* ===== NEW: BUTTON HOVER/PRESS POLISH ===== */
  button { transition: transform 0.14s ease, box-shadow 0.14s ease, background 0.14s ease, border-color 0.14s ease; }
  .modal-back.show .modal { animation: slideUp 0.26s cubic-bezier(.2,.8,.3,1); }
</style>
</head>
<body>

<div class="loading" id="loading">
  <div class="spinner"></div>
  <div style="color:var(--muted);font-size:13px;">Загрузка Royal Spin…</div>
</div>

<div class="app with-nav" id="app" style="display:none;">

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

  <div class="hero-banner" id="heroBanner">
    <span class="hero-sparkle">✨</span>
    <div class="hero-content">
      <h2 class="hero-title">👑 Добро пожаловать 👑</h2>
      <p class="hero-sub">Испытай удачу в 10 играх. Рискни звёздами — забери корону!</p>
      <button class="hero-cta" onclick="openGame('roulette')">🎰 Начать играть</button>
    </div>
  </div>

  <!-- BALANCE DIAGNOSTIC BANNER (показывается при расхождении/ошибке баланса) -->
  <div class="balance-banner" id="balanceBanner">
    <span class="icon" id="balanceBannerIcon">⚠️</span>
    <div class="body">
      <div class="title" id="balanceBannerTitle">Проблема с балансом</div>
      <div class="meta" id="balanceBannerMeta"></div>
    </div>
    <span class="close" onclick="dismissBalanceBanner()">✕</span>
  </div>

  <div class="section-title">⚡ Хиты</div>
  <div class="games">
    <div class="game-card" onclick="openGame('roulette')">
      <span class="game-badge top">TOP</span>
      <div class="game-icon-wrap"><span class="game-icon">🎰</span></div>
      <div class="game-name">Рулетка</div>
      <div class="game-sub">фрукты · 777</div>
      <div class="game-mult">до x4</div>
    </div>
    <div class="game-card" onclick="openGame('darts')">
      <span class="game-badge hot">HOT</span>
      <div class="game-icon-wrap"><span class="game-icon">🎯</span></div>
      <div class="game-name">Дартс</div>
      <div class="game-sub">яблочко x5</div>
      <div class="game-mult">до x5</div>
    </div>
    <div class="game-card" onclick="openGame('basketball')">
      <span class="game-badge hot">HOT</span>
      <div class="game-icon-wrap"><span class="game-icon">🏀</span></div>
      <div class="game-name">Баскетбол</div>
      <div class="game-sub">попадание</div>
      <div class="game-mult">x1.85</div>
    </div>
    <div class="game-card" onclick="openGame('blackjack')">
      <span class="game-badge top">TOP</span>
      <div class="game-icon-wrap"><span class="game-icon">🃏</span></div>
      <div class="game-name">Блэкджек</div>
      <div class="game-sub">21 очко</div>
      <div class="game-mult">до x2.5</div>
    </div>
  </div>

  <div class="section-title">🎮 Все игры</div>
  <div class="games">
    <div class="game-card" onclick="openGame('dice')">
      <div class="game-icon-wrap"><span class="game-icon">🎲</span></div>
      <div class="game-name">Кубик</div>
      <div class="game-sub">выбери число</div>
      <div class="game-mult">до x6</div>
    </div>
    <div class="game-card" onclick="openGame('football')">
      <div class="game-icon-wrap"><span class="game-icon">⚽</span></div>
      <div class="game-name">Футбол</div>
      <div class="game-sub">пенальти</div>
      <div class="game-mult">x1.7</div>
    </div>
    <div class="game-card" onclick="openGame('ttt')">
      <span class="game-badge new">NEW</span>
      <div class="game-icon-wrap"><span class="game-icon">🎮</span></div>
      <div class="game-name">Крестики-нолики</div>
      <div class="game-sub">против бота</div>
      <div class="game-mult">x2</div>
    </div>
    <div class="game-card" onclick="openGame('mines')">
      <span class="game-badge new">NEW</span>
      <div class="game-icon-wrap"><span class="game-icon">💣</span></div>
      <div class="game-name">Сапёр</div>
      <div class="game-sub">безопасная клетка</div>
      <div class="game-mult">до x2</div>
    </div>
    <div class="game-card" onclick="openGame('rps')">
      <div class="game-icon-wrap"><span class="game-icon">✊</span></div>
      <div class="game-name">Камень · Ножницы · Бумага</div>
      <div class="game-sub">классика</div>
      <div class="game-mult">x2</div>
    </div>
    <div class="game-card" onclick="openGame('coin')">
      <div class="game-icon-wrap"><span class="game-icon">🪙</span></div>
      <div class="game-name">Орёл и Решка</div>
      <div class="game-sub">угадай сторону</div>
      <div class="game-mult">x1.95</div>
    </div>
  </div>

  <div class="stats">
    <div class="stat-cell"><div class="stat-val" id="statGames">0</div><div class="stat-lbl">Игр сыграно</div></div>
    <div class="stat-cell"><div class="stat-val" id="statWon">0</div><div class="stat-lbl">Побед</div></div>
    <div class="stat-cell"><div class="stat-val" id="statRate">0%</div><div class="stat-lbl">% Побед</div></div>
    <div class="stat-bar"><div class="stat-bar-fill" id="statBarFill" style="width:0%"></div></div>
  </div>

  <div style="text-align:center;color:var(--muted);font-size:11px;margin-top:20px;line-height:1.6;">
    👑 Royal Spin · Telegram Звёзды · 18+ · Играй ответственно<br/>
    <span style="opacity:0.6;">Решение остаться — твоё. Удачи за столом.</span>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
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
    <div class="modal-sub">21 очко — x2.5 · Победа — x2 · ВЗЯТЬ или ОСТАНОВИТЬСЯ</div>
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
    <div class="result-text" id="bjResult">Нажми «Начать партию»</div>
    <div class="stake-row">
      <div class="custom-stake-wrap">
        <label>⭐ Ваша ставка</label>
        <div class="stake-input-group">
          <button class="stake-adjust" onclick="adjustStake(-1)" type="button">−</button>
          <input type="number" class="custom-stake-input" min="1" max="500" value="1" oninput="setCustomStake(this)" />
          <button class="stake-adjust" onclick="adjustStake(1)" type="button">+</button>
        </div>
        <div class="stake-quick-row">
          <button class="quick-stake" onclick="quickStake('min')" type="button">МИН</button>
          <button class="quick-stake" onclick="quickStake('half')" type="button">½ БАЛАНСА</button>
          <button class="quick-stake" onclick="quickStake('all')" type="button">ALL-IN</button>
          <button class="quick-stake" onclick="quickStake('max')" type="button">МАКС</button>
        </div>
      </div>
    </div>
    <!-- Кнопки управления: старт / взять / остановиться / заново -->
    <button class="play-btn" id="bjStartBtn" onclick="blackjackStart()">НАЧАТЬ ПАРТИЮ</button>
    <div id="bjActions" style="display:none; gap:8px;">
      <button class="play-btn" id="bjHitBtn" onclick="blackjackHit()" style="background:linear-gradient(135deg, #10b981, #047857);">＋ ВЗЯТЬ КАРТУ</button>
      <button class="play-btn" id="bjStandBtn" onclick="blackjackStand()" style="background:linear-gradient(135deg, #ef4444, #991b1b); margin-top:8px;">■ ОСТАНОВИТЬСЯ</button>
    </div>
    <button class="play-btn" id="bjBtn" style="display:none;" onclick="blackjackReset()">НОВАЯ ПАРТИЯ</button>
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
    currentStake = parseInt(v) || MIN_STAKE;
    if (currentStake < MIN_STAKE) currentStake = MIN_STAKE;
    if (currentStake > MAX_STAKE) currentStake = MAX_STAKE;
    document.querySelectorAll('.custom-stake-input').forEach(i => i.value = currentStake);
    haptic('light');
  }

  function setCustomStake(input) {
    let v = parseInt(input.value);
    if (isNaN(v) || v < MIN_STAKE) v = MIN_STAKE;
    if (v > MAX_STAKE) v = MAX_STAKE;
    currentStake = v;
    // Если значение превысило максимум / ниже минимума, нормализуем поле
    input.value = v;
  }

  // +/- на 1 (с шагом 1, но если текущая ставка <10 — шаг 1; >=10 — шаг 5)
  function adjustStake(delta) {
    const step = currentStake >= 10 ? 5 : 1;
    let v = currentStake + delta * step;
    if (v < MIN_STAKE) v = MIN_STAKE;
    if (v > MAX_STAKE) v = MAX_STAKE;
    currentStake = v;
    document.querySelectorAll('.custom-stake-input').forEach(i => i.value = v);
    haptic('light');
  }

  // Быстрый выбор: МИН / ½ БАЛАНСА / ALL-IN / МАКС
  function quickStake(kind) {
    let v = MIN_STAKE;
    const bal = (userData && typeof userData.balance === 'number') ? userData.balance : 0;
    if (kind === 'min') {
      v = MIN_STAKE;
    } else if (kind === 'max') {
      v = MAX_STAKE;
    } else if (kind === 'half') {
      v = Math.max(MIN_STAKE, Math.floor(bal / 2));
    } else if (kind === 'all') {
      v = Math.max(MIN_STAKE, Math.min(MAX_STAKE, bal));
    }
    if (v < MIN_STAKE) v = MIN_STAKE;
    if (v > MAX_STAKE) v = MAX_STAKE;
    currentStake = v;
    document.querySelectorAll('.custom-stake-input').forEach(i => i.value = v);
    haptic('light');
  }

  function setDiceTarget(v, el) {
    diceTarget = v;
    document.querySelectorAll('#dicePicker .num-btn').forEach(b => b.classList.remove('active'));
    if (el) el.classList.add('active');
  }

  // Безопасный рендер аватарки с fallback на инициалы
  function renderAvatar(container, u) {
    if (!container) return;
    const name = (u && (u.first_name || u.username)) || 'Игрок';
    if (u && u.photo_url) {
      const img = document.createElement('img');
      img.src = u.photo_url;
      img.alt = name;
      img.referrerPolicy = 'no-referrer';
      img.onerror = function () {
        // Если фото не загрузилось — показываем инициалы
        if (img.parentNode) img.parentNode.removeChild(img);
        container.textContent = (name[0] || '?').toUpperCase();
      };
      container.innerHTML = '';
      container.appendChild(img);
    } else {
      container.textContent = (name[0] || '?').toUpperCase();
    }
  }

  // Безопасный эскейп текста для textContent (защита от XSS)
  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text == null ? '' : String(text);
  }

  function applyUser(u, isNew) {
    if (!u) return;
    userData = u;
    const name = u.first_name || u.username || 'Игрок';
    const handle = u.username ? '@' + u.username : ('id' + (u.id ? ':' + u.id : ''));
    setText('userName', name);
    setText('userHandle', handle);
    renderAvatar(document.getElementById('avatar'), u);
    if (typeof u.balance === 'number') setText('balanceVal', u.balance);
    setText('statGames', u.games_played || 0);
    setText('statWon', u.games_won || 0);
    const played = u.games_played || 0;
    const won = u.games_won || 0;
    const rate = played > 0 ? Math.round((won / played) * 100) : 0;
    setText('statRate', rate + '%');
    // Кэшируем для мгновенного отображения при следующих загрузках
    try {
      localStorage.setItem('rs_user', JSON.stringify(u));
    } catch (_) {}
    refreshProfileView();
  }

  // ────────────────────────────────────────────────────────────────────
  // ДИАГНОСТИКА БАЛАНСА (видимая пользователю, если что-то не так)
  // ────────────────────────────────────────────────────────────────────
  // Если /api/auth и /api/balance вернули разные балансы — скорее всего
  // mini app подключён к другой PostgreSQL-инстанции, чем бот.
  // В этом случае показываем баннер с подробностями + DB-диагностикой,
  // чтобы пользователь сразу увидел проблему и мог её починить
  // (выставить одинаковый DATABASE_URL).
  async function runBalanceDiagnostics(authUser, canonical) {
    try {
      const dbg = await api('/api/debug/db', {});
      const myId = (authUser && authUser.id) || (canonical && canonical.id) || (userData && userData.id);
      let dbgUser = null;
      if (myId) {
        try {
          dbgUser = await api('/api/debug/db?user_id=' + encodeURIComponent(myId), {});
        } catch (_) {}
      }
      const meta = [];
      if (dbg && dbg.current_database) meta.push('БД: ' + dbg.current_database);
      if (dbg && dbg.server_addr) meta.push('postgres: ' + dbg.server_addr + ':' + (dbg.server_port || '?'));
      if (dbg && typeof dbg.users_count === 'number') meta.push('users в таблице: ' + dbg.users_count);
      if (dbgUser && dbgUser.user) {
        meta.push('баланс из БД: ' + dbgUser.user.balance);
      }

      // Определяем, есть ли расхождение между /api/auth и /api/balance.
      const authBal = authUser && typeof authUser.balance === 'number' ? authUser.balance : null;
      const canonBal = canonical && typeof canonical.balance === 'number' ? canonical.balance : null;
      const mismatch = authBal !== null && canonBal !== null && authBal !== canonBal;

      if (mismatch) {
        showBalanceBanner({
          level: 'error',
          title: 'Расхождение баланса между /api/auth и /api/balance',
          meta: 'auth=' + authBal + ', balance=' + canonBal + ' | ' + meta.join(' · '),
        });
      } else if (canonBal === 0 && myId) {
        // Подозрительно: юзер с конкретным id есть, а баланс 0.
        // Возможно бот пишет в другую БД.
        showBalanceBanner({
          level: 'warn',
          title: 'Баланс = 0 в общей БД',
          meta: meta.join(' · ') + ' · Если в боте не 0 — DATABASE_URL mini app и бота различаются',
        });
      } else if (canonBal !== null && canonBal > 0) {
        // Всё ок — короткий зелёный баннер на 1.5 сек, чтобы было видно
        // что баланс реально подтянулся.
        showBalanceBanner({
          level: 'ok',
          title: '✓ Баланс синхронизирован: ' + canonBal,
          meta: meta.join(' · '),
        });
        setTimeout(dismissBalanceBanner, 2500);
      }
    } catch (e) {
      console.warn('runBalanceDiagnostics failed:', e);
    }
  }

  function showBalanceBanner(opts) {
    const banner = document.getElementById('balanceBanner');
    if (!banner) return;
    banner.classList.remove('error', 'warn', 'ok');
    if (opts.level === 'error') {
      banner.classList.add('show');
      // красный — дефолт
    } else if (opts.level === 'warn') {
      banner.classList.add('show', 'warn');
    } else if (opts.level === 'ok') {
      banner.classList.add('show', 'ok');
    } else {
      banner.classList.add('show');
    }
    const ic = document.getElementById('balanceBannerIcon');
    if (ic) ic.textContent = opts.level === 'error' ? '⚠️'
      : opts.level === 'warn' ? '⚠️'
      : opts.level === 'ok' ? '✅' : 'ℹ️';
    setText('balanceBannerTitle', opts.title || '');
    setText('balanceBannerMeta', opts.meta || '');
  }

  function dismissBalanceBanner() {
    const banner = document.getElementById('balanceBanner');
    if (banner) banner.classList.remove('show');
  }

  // Мгновенный рендер из кэша / initDataUnsafe — до ответа API
  function applyFromUnsafe(u) {
    if (!u || !u.id) return false;
    applyUser({
      id: u.id,
      first_name: u.first_name || '',
      last_name: u.last_name || '',
      username: u.username || null,
      photo_url: u.photo_url || null,
      balance: 0,
      games_played: 0,
      games_won: 0,
    }, true);
    return true;
  }

  async function api(path, body) {
    const initData = tg ? tg.initData : '';
    const headers = {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
    };
    let url = path;
    let opts = { method: body ? 'POST' : 'GET', headers };
    if (body) {
      // initData дублируем и в header и в body — бэкенд берёт header,
      // body нужен как fallback для некоторых прокси.
      opts.body = JSON.stringify({ initData, ...body });
    } else if (initData) {
      // Для GET кладём initData в query (?initData=...) на случай, если
      // прокси режет кастомные заголовки. Бэкенд это тоже умеет.
      const sep = path.includes('?') ? '&' : '?';
      url = path + sep + '_init=' + encodeURIComponent(initData);
    }
    const resp = await fetch(url, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ('http_' + resp.status));
    return data;
  }

  async function bootstrap() {
    // 1. Мгновенно показываем пользователя из Telegram SDK (без ожидания API).
    //    initDataUnsafe — НЕбезопасный источник, но для UI это нормально:
    //    сервер всё равно валидирует initData через HMAC при каждом запросе.
    try {
      if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
        applyFromUnsafe(tg.initDataUnsafe.user);
      } else {
        // 2. Если SDK недоступен (открыли не из Telegram) — берём из кэша localStorage,
        //    чтобы юзер видел свой профиль, но без серверной валидации.
        try {
          const cached = localStorage.getItem('rs_user');
          if (cached) applyFromUnsafe(JSON.parse(cached));
        } catch (_) {}
      }
    } catch (e) {
      console.warn('unsafe user render failed:', e);
    }

    // 3. Прячем лоадер сразу — UI уже рабочий
    const loading = document.getElementById('loading');
    const appEl = document.getElementById('app');
    if (loading) loading.style.display = 'none';
    if (appEl) appEl.style.display = '';

    // 4. Проверяем авторизацию через API. Если не из Telegram — показываем dev-mode.
    const initData = tg ? tg.initData : '';
    if (!initData) {
      if (!userData) showDevMode();
      return;
    }

    let authUser = null;
    try {
      const r = await api('/api/auth', {});
      authUser = r.user;
      applyUser(r.user, r.is_new);
      if (r.is_new && tg && tg.showAlert) {
        tg.showAlert('Добро пожаловать в Royal Spin! 👑');
      }
    } catch (e) {
      console.warn('auth failed, will rely on /api/balance (shared DB):', e);
    }

    // ────────────────────────────────────────────────────────────────────
    // КАНОНИЧЕСКИЙ БАЛАНС ИЗ ОБЩЕЙ БД
    // ────────────────────────────────────────────────────────────────────
    // БД общая с zakaz-test/bot.py, поэтому единственный источник правды —
    // строка в users.balance. /api/auth мог вернуть stale (например, если
    // запись только что была создана ботом, а транзакция mini app взяла
    // старый снапшот). Поэтому ВСЕГДА после auth дёргаем лёгкий /api/balance
    // и перетираем локальный стейт его ответом. Это гарантирует:
    //   1. Баланс, который бот поднял через update_balance — виден сразу.
    //   2. Никаких «у меня в боте 101.95, а тут 0» — всё тянется из общей
    //      PostgreSQL-инстанции напрямую.
    // Если /api/balance тоже упал — НЕ показываем тост «профиль не загрузился»,
    // а выводим видимый баннер с описанием ошибки и DB-диагностикой.
    try {
      const r = await api('/api/balance', {});
      if (r && r.ok && r.user) {
        const canonical = r.user;
        const merged = Object.assign({}, authUser || userData || {}, canonical, {
          // /api/auth мог отдать старые имя/username — мерджим, но
          // канонический баланс/статистика всегда побеждают.
          id: (authUser && authUser.id) || (userData && userData.id),
        });
        console.info('[royal-spin] canonical balance from shared DB:', {
          auth_had_returned: authUser ? { balance: authUser.balance, games_played: authUser.games_played } : null,
          canonical_balance: canonical.balance,
          canonical_games_played: canonical.games_played,
          canonical_games_won: canonical.games_won,
          will_render: merged.balance,
        });
        applyUser(merged, false);
        // Запускаем расширенную диагностику: какой postgres, какая БД,
        // сколько юзеров в таблице. Если расхождение с ботом — сразу видно
        // по server_addr / current_database.
        runBalanceDiagnostics(authUser, canonical);
      } else {
        const reason = (r && r.error) ? r.error : 'unknown';
        showBalanceBanner({
          level: 'error',
          title: 'Не удалось получить баланс из общей БД',
          meta: 'Ответ /api/balance: ' + reason + '. Mini app подключён не к той PostgreSQL?',
        });
        toast('⚠️ Баланс недоступен. Игра может быть недоступна.', 'lose');
        if (!authUser && (!userData || !userData.id)) showDevMode();
      }
    } catch (e2) {
      console.warn('/api/balance fallback failed:', e2);
      showBalanceBanner({
        level: 'error',
        title: 'Ошибка соединения с общей БД',
        meta: 'fetch /api/balance → ' + (e2.message || e2),
      });
      toast('⚠️ Не удалось связаться с сервером баланса.', 'lose');
      if (!authUser && (!userData || !userData.id)) showDevMode();
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
    // Закрыть экран режимов, если он открыт
    const ms = document.getElementById('modesScreen');
    if (ms) ms.classList.remove('show');
    document.getElementById('modal-' + name).classList.add('show');
    setNavVisible(false);  // скрыть нижнюю панель в режиме игры
    // Сбросить визуал для игр, где нужно
    if (name === 'ttt') resetTTT();
    if (name === 'mines') resetMines();
    if (name === 'blackjack') blackjackReset();
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
    // Если моды-экран открыт — оставляем его как есть и показываем навбар.
    // Если мод-экран закрыт (игровая модалка просто поверх главной) — возвращаемся на home.
    if (!document.getElementById('modesScreen').classList.contains('show')) {
      setActiveNav('home');
      setNavVisible(true);
    } else {
      setNavVisible(true);
    }
  }

  // ===== BOTTOM NAVIGATION =====
  let currentTab = 'home';

  function setNavVisible(visible) {
    const nav = document.getElementById('bottomNav');
    if (!nav) return;
    if (visible) nav.classList.remove('hidden');
    else nav.classList.add('hidden');
  }

  function setActiveNav(tab) {
    currentTab = tab;
    const ids = { home: 'navHomeBtn', modes: 'navModesBtn', profile: 'navProfileBtn' };
    Object.values(ids).forEach(id => {
      const b = document.getElementById(id);
      if (b) b.classList.remove('active');
    });
    const activeId = ids[tab];
    if (activeId) {
      const b = document.getElementById(activeId);
      if (b) b.classList.add('active');
    }
  }

  function navGo(tab) {
    haptic('light');
    // закрываем открытые модалки
    closeModal();
    closeModes();
    if (tab === 'home') {
      setActiveNav('home');
    } else if (tab === 'modes') {
      openModes();
      setActiveNav('modes');
    } else if (tab === 'profile') {
      openProfile();
      setActiveNav('profile');
    }
  }

  // ===== MODES SCREEN =====
  const GAMES_META = [
    { id: 'roulette',   icon: '🎰', name: 'Рулетка',          sub: 'фрукты · 777',     mult: 'до x4',  badge: 'TOP' },
    { id: 'darts',      icon: '🎯', name: 'Дартс',            sub: 'яблочко x5',       mult: 'до x5',  badge: 'HOT' },
    { id: 'basketball', icon: '🏀', name: 'Баскетбол',        sub: 'попадание',        mult: 'x1.85',  badge: 'HOT' },
    { id: 'blackjack',  icon: '🃏', name: 'Блэкджек',         sub: '21 очко',          mult: 'до x2.5',badge: 'TOP' },
    { id: 'dice',       icon: '🎲', name: 'Кубик',            sub: 'выбери число',     mult: 'до x6'  },
    { id: 'football',   icon: '⚽', name: 'Футбол',           sub: 'пенальти',         mult: 'x1.7'   },
    { id: 'ttt',        icon: '🎮', name: 'Крестики-нолики',  sub: 'против бота',      mult: 'x2',    badge: 'NEW' },
    { id: 'mines',      icon: '💣', name: 'Сапёр',            sub: 'безопасная клетка',mult: 'до x2',  badge: 'NEW' },
    { id: 'rps',        icon: '✊', name: 'Камень-Ножницы-Бумага', sub: 'классика',     mult: 'x2'     },
    { id: 'coin',       icon: '🪙', name: 'Орёл и Решка',     sub: 'угадай сторону',   mult: 'x1.95'  },
  ];

  function buildModesGrid() {
    const grid = document.getElementById('modesGrid');
    if (!grid || grid.dataset.built === '1') return;
    grid.innerHTML = GAMES_META.map((g, i) => {
      const badgeHtml = g.badge
        ? `<span class="game-badge ${g.badge === 'NEW' ? 'new' : g.badge === 'HOT' ? 'hot' : 'top'}">${g.badge}</span>`
        : '';
      return `
        <div class="game-card" style="animation-delay:${0.04 + i * 0.05}s" onclick="openGame('${g.id}')">
          ${badgeHtml}
          <div class="game-icon-wrap"><span class="game-icon">${g.icon}</span></div>
          <div class="game-name">${g.name}</div>
          <div class="game-sub">${g.sub}</div>
          <div class="game-mult">${g.mult}</div>
        </div>`;
    }).join('');
    grid.dataset.built = '1';
  }

  function openModes() {
    buildModesGrid();
    // Сначала скрыть любые открытые модалки игр
    document.querySelectorAll('.modal-back').forEach(m => m.classList.remove('show'));
    setNavVisible(false);
    const scr = document.getElementById('modesScreen');
    scr.classList.add('show');
    // Перезапустим анимации для карточек
    const cards = scr.querySelectorAll('.game-card');
    cards.forEach(c => {
      c.style.animation = 'none';
      void c.offsetWidth;
      c.style.animation = '';
    });
  }

  function closeModes() {
    const scr = document.getElementById('modesScreen');
    if (scr) scr.classList.remove('show');
    setActiveNav('home');
    setNavVisible(true);
  }

  // ===== PROFILE =====
  function openProfile() {
    refreshProfileView();
    setNavVisible(false);
    document.getElementById('modal-profile').classList.add('show');
  }

  function refreshProfileView() {
    const u = userData;
    if (!u) return;
    const name = u.first_name || u.username || 'Игрок';
    renderAvatar(document.getElementById('profileAvatar'), u);
    setText('profileName', name);
    setText('profileHandle', u.username ? '@' + u.username : ('id' + (u.id ? ':' + u.id : '')));
    setText('profileBalance', u.balance != null ? u.balance : 0);
    setText('profileGames', u.games_played || 0);
    setText('profileWon', u.games_won || 0);
    const played = u.games_played || 0;
    const won = u.games_won || 0;
    const rate = played > 0 ? Math.round((won / played) * 100) : 0;
    setText('profileRate', rate + '%');
  }

  function toast(text, kind) {
    const t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 1700);
  }

  function setBalance(b, animate) {
    const prev = (userData && typeof userData.balance === 'number') ? userData.balance : null;
    userData.balance = b;
    const el = document.getElementById('balanceVal');
    if (el) {
      el.textContent = b;
      if (animate && prev !== null && b !== prev) {
        const cls = b > prev ? 'flash-up' : 'flash-down';
        el.classList.remove('flash-up', 'flash-down');
        // force reflow to restart animation
        void el.offsetWidth;
        el.classList.add(cls);
        const box = el.closest('.balance-box');
        if (box) {
          box.classList.remove('flash-win', 'flash-lose');
          void box.offsetWidth;
          box.classList.add(b > prev ? 'flash-win' : 'flash-lose');
        }
      }
    }
  }

  function updateStats(played, won) {
    userData.games_played = played;
    userData.games_won = won;
    document.getElementById('statGames').textContent = played;
    document.getElementById('statWon').textContent = won;
    const rate = played > 0 ? Math.round((won / played) * 100) : 0;
    document.getElementById('statRate').textContent = rate + '%';
    const bar = document.getElementById('statBarFill');
    if (bar) bar.style.width = rate + '%';
    // Зеркалим в модалку профиля, если открыта
    const pg = document.getElementById('profileGames');
    if (pg) pg.textContent = played;
    const pw = document.getElementById('profileWon');
    if (pw) pw.textContent = won;
    const pr = document.getElementById('profileRate');
    if (pr) pr.textContent = rate + '%';
  }

  function applyResult(r) {
    setBalance(r.balance, true);
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
  // ===== BLACKJACK (пошаговый: hit / stand) =====
  // Локальное состояние партии
  let bjState = null;       // { player: [], dealer: [], dealer_revealed, stake, ... }
  let bjBusy = false;       // блокировка на время сетевых вызовов
  let bjDealerHiddenHTML = '<span class="bj-card" style="background:linear-gradient(135deg,#1f2937,#111827);color:#ffd60a;">?</span>';

  function bjCardHTML(v) {
    const display = v === 11 ? 'A' : v;
    const red = (v === 1 || v === 11);
    return '<span class="bj-card' + (red ? ' red' : '') + '">' + display + '</span>';
  }

  // Рендерит руку; скрытые карты показывает как «?» (как в казино)
  function bjRenderHand(cards, hideFromIdx) {
    return cards.map((v, i) => (i >= hideFromIdx ? bjDealerHiddenHTML : bjCardHTML(v))).join('');
  }

  // Переключение видимости кнопок управления
  function bjSetControls(state) {
    // state: 'idle' | 'playing' | 'over'
    const startBtn = document.getElementById('bjStartBtn');
    const actions = document.getElementById('bjActions');
    const resetBtn = document.getElementById('bjBtn');
    startBtn.style.display = (state === 'idle') ? '' : 'none';
    actions.style.display = (state === 'playing') ? 'flex' : 'none';
    resetBtn.style.display = (state === 'over') ? '' : 'none';
    actions.style.flexDirection = 'column';
    // Если всего 2 карты — кнопка «ОСТАНОВИТЬСЯ» включена (минимальный порог выполнен).
    // Защита от <2 карт остаётся ещё и на сервере.
    const standBtn = document.getElementById('bjStandBtn');
    if (standBtn && bjState) {
      standBtn.disabled = (bjState.player.length < 2);
    }
  }

  function bjRender(state) {
    const playerEl = document.getElementById('bjPlayer');
    const dealerEl = document.getElementById('bjDealer');
    const playerTotalEl = document.getElementById('bjPlayerTotal');
    const dealerTotalEl = document.getElementById('bjDealerTotal');
    playerEl.innerHTML = bjRenderHand(state.player, 999);  // все открыты
    playerTotalEl.textContent = state.player_val;
    if (state.dealer_revealed) {
      dealerEl.innerHTML = bjRenderHand(state.dealer, 999);
      dealerTotalEl.textContent = state.dealer_val;
    } else {
      // Скрываем всё, кроме первой карты дилера
      dealerEl.innerHTML = bjRenderHand(state.dealer, 1);
      dealerTotalEl.textContent = '?';
    }
  }

  function bjShowResult(r, opts) {
    // opts: { busted } — если перебор случился на hit, выигрыша нет
    const txt = document.getElementById('bjResult');
    const net = (r.payout || 0) - currentStake;
    if (r.kind === 'blackjack') {
      txt.className = 'result-text win';
      txt.textContent = 'БЛЭКДЖЕК! 🃏 x2.5 (+' + net + ' ⭐)';
      toast('🎉 +' + net + ' звёзд', 'win');
      haptic('win');
    } else if (r.kind === 'win') {
      txt.className = 'result-text win';
      txt.textContent = 'ПОБЕДА! x2 (+' + net + ' ⭐)';
      toast('🎉 +' + net + ' звёзд', 'win');
      haptic('win');
    } else if (r.kind === 'push') {
      txt.className = 'result-text draw';
      txt.textContent = 'НИЧЬЯ · без награды (−' + currentStake + ' ⭐)';
      toast('Ничья — ставка не возвращается', 'draw');
      haptic('light');
    } else if (r.kind === 'bust') {
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

  // Начать новую партию: списываем ставку, получаем по 2 карты
  async function blackjackStart() {
    if (bjBusy) return;
    bjBusy = true;
    document.getElementById('bjStartBtn').disabled = true;
    document.getElementById('bjResult').textContent = 'Раздача…';
    document.getElementById('bjResult').className = 'result-text';
    try {
      const r = await api('/api/game/blackjack/new', { stake: currentStake });
      bjState = {
        player: r.player,
        dealer: r.dealer,
        player_val: r.player_val,
        dealer_revealed: r.dealer_revealed,
        stake: r.stake,
      };
      // Списываем ставку
      setBalance(r.balance);
      bjRender(bjState);
      bjSetControls('playing');
      const txt = document.getElementById('bjResult');
      if (r.player_val === 21 && r.player.length === 2) {
        // Мгновенный блэкджек — у игрока уже 21 с 2 карт. По правилам:
        // если дилер тоже не покажет блэкджек, победа x2.5. Авто-stand.
        txt.textContent = 'БЛЭКДЖЕК! 🃏 Авто-остановка…';
        await blackjackStand();
      } else {
        txt.textContent = 'Твой ход. ВЗЯТЬ или ОСТАНОВИТЬСЯ';
      }
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
      document.getElementById('bjResult').textContent = 'Ошибка запуска';
    } finally {
      bjBusy = false;
      document.getElementById('bjStartBtn').disabled = false;
    }
  }

  // Взять ещё карту
  async function blackjackHit() {
    if (bjBusy || !bjState) return;
    bjBusy = true;
    document.getElementById('bjHitBtn').disabled = true;
    document.getElementById('bjStandBtn').disabled = true;
    haptic('light');
    try {
      const r = await api('/api/game/blackjack/hit', {
        stake: currentStake, player: bjState.player, dealer: bjState.dealer,
      });
      bjState.player = r.player;
      bjState.dealer = r.dealer;
      bjState.player_val = r.player_val;
      bjState.dealer_revealed = r.dealer_revealed;
      bjRender(bjState);
      if (r.busted) {
        // Перебор — игра окончена, карты дилера раскрыты
        bjShowResult({ ...r, kind: 'bust', payout: 0 });
        bjState = null;
        bjSetControls('over');
        document.getElementById('bjResult').textContent = 'ПЕРЕБОР! 💥 (−' + currentStake + ' ⭐)';
      } else {
        document.getElementById('bjResult').textContent = 'Твой ход (' + r.player_val + '). ВЗЯТЬ или ОСТАНОВИТЬСЯ';
      }
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
    } finally {
      bjBusy = false;
      document.getElementById('bjHitBtn').disabled = false;
      // Stand можно снова включить, если у игрока >=2 карт
      if (bjState) {
        document.getElementById('bjStandBtn').disabled = (bjState.player.length < 2);
      }
    }
  }

  // Остановиться: дилер играет, определяется исход
  async function blackjackStand() {
    if (bjBusy || !bjState) return;
    // Клиентская защита: нужно минимум 2 карты на руках
    if (bjState.player.length < 2) {
      toast('Нужно минимум 2 карты', 'lose');
      return;
    }
    bjBusy = true;
    document.getElementById('bjHitBtn').disabled = true;
    document.getElementById('bjStandBtn').disabled = true;
    haptic('light');
    const txt = document.getElementById('bjResult');
    txt.textContent = 'Дилер играет…';
    try {
      const r = await api('/api/game/blackjack/stand', {
        stake: currentStake, player: bjState.player, dealer: bjState.dealer,
      });
      bjState.player = r.player;
      bjState.dealer = r.dealer;
      bjState.player_val = r.player_val;
      bjState.dealer_revealed = true;
      // Сначала визуально раскрываем карты дилера (через короткую паузу для драмы)
      document.getElementById('bjDealer').innerHTML = bjRenderHand(r.dealer, 999);
      document.getElementById('bjDealerTotal').textContent = r.dealer_val;
      bjShowResult(r);
      bjState = null;
      bjSetControls('over');
    } catch (e) {
      toast('Ошибка: ' + e.message, 'lose');
      // Возвращаем кнопки, если сервер отказал
      if (bjState) {
        document.getElementById('bjHitBtn').disabled = false;
        document.getElementById('bjStandBtn').disabled = (bjState.player.length < 2);
      }
    } finally {
      bjBusy = false;
    }
  }

  // Сбросить в режим «Начать партию»
  function blackjackReset() {
    bjState = null;
    bjBusy = false;
    document.getElementById('bjPlayer').innerHTML = '';
    document.getElementById('bjDealer').innerHTML = '';
    document.getElementById('bjPlayerTotal').textContent = '';
    document.getElementById('bjDealerTotal').textContent = '';
    document.getElementById('bjResult').textContent = 'Нажми «Начать партию»';
    document.getElementById('bjResult').className = 'result-text';
    bjSetControls('idle');
  }

  // Старая функция оставлена как алиас для совместимости (если где-то вызывается)
  async function playBlackjack() { return blackjackStart(); }

  bootstrap();
</script>

<!-- ============ BOTTOM NAVIGATION ============ -->
<nav class="bottom-nav" id="bottomNav">
  <button class="nav-btn active" id="navHomeBtn" onclick="navGo('home')">
    <span class="nav-icon">🏠</span>
    <span>Главная</span>
  </button>
  <button class="nav-btn" id="navModesBtn" onclick="navGo('modes')">
    <span class="nav-icon">🎮</span>
    <span>Режимы</span>
  </button>
  <button class="nav-btn" id="navProfileBtn" onclick="navGo('profile')">
    <span class="nav-icon">👤</span>
    <span>Профиль</span>
  </button>
</nav>

<!-- ============ MODES SCREEN ============ -->
<div class="modes-screen" id="modesScreen">
  <div class="modes-header">
    <h2>🎮 Режимы</h2>
    <button class="modes-close" onclick="closeModes()" aria-label="Закрыть">✕</button>
  </div>
  <div class="modes-grid" id="modesGrid">
    <!-- Карточки заполняются JS из того же списка что и на главной -->
  </div>
</div>

<!-- ============ PROFILE MODAL ============ -->
<div class="modal-back" id="modal-profile">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div class="profile-modal-content">
      <div class="profile-avatar" id="profileAvatar">?</div>
      <div class="profile-name" id="profileName">Игрок</div>
      <div class="profile-handle" id="profileHandle">@username</div>

      <div class="profile-balance-card">
        <div class="profile-balance-label">⭐ Твой баланс</div>
        <div class="profile-balance-val">
          <svg viewBox="0 0 24 24" fill="#000000"><path d="M12 2l2.9 6.9L22 10l-5.5 4.7L18 22l-6-3.7L6 22l1.5-7.3L2 10l7.1-1.1L12 2z"/></svg>
          <span id="profileBalance">0</span>
        </div>
      </div>

      <div class="profile-stats-grid">
        <div class="profile-stat">
          <div class="profile-stat-val" id="profileGames">0</div>
          <div class="profile-stat-lbl">Игр сыграно</div>
        </div>
        <div class="profile-stat">
          <div class="profile-stat-val" id="profileWon">0</div>
          <div class="profile-stat-lbl">Побед</div>
        </div>
        <div class="profile-stat">
          <div class="profile-stat-val" id="profileRate">0%</div>
          <div class="profile-stat-lbl">Винрейт</div>
        </div>
      </div>

      <div class="profile-section-title">ℹ️ О приложении</div>
      <div class="profile-tip">
        <b>Royal Spin</b> — мини-приложение Telegram.<br>
        Играй в 10 режимов на звёзды. Ставки от <b>1 ⭐</b> до <b>500 ⭐</b>.
        Нажми <b>Режимы</b> внизу, чтобы выбрать игру.
      </div>
    </div>
  </div>
</div>

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
