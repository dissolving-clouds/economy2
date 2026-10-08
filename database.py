"""Работа с базой данных (SQLite). Все данные хранятся в файле economy.db."""
import aiosqlite

DEFAULT_SETTINGS = {
    "currency": "🪙",              # значок валюты
    "daily_amount": "100",         # награда за /daily
    "daily_streak_bonus": "10",    # бонус за каждый день подряд (макс. 6 дней)
    "daily_cooldown_hours": "24",  # раз в сколько часов можно брать /daily
    "max_bet": "10000",            # максимальная ставка в казино и дуэлях
    "duel_commission": "5",        # комиссия с дуэли, %
    "room_price": "5000",          # цена любовной комнаты
    "room_days": "30",             # на сколько дней покупается комната
    "room_category_id": "0",       # ID категории для комнат (0 = без категории)
    "room_role_id": "0",           # ID роли, которую получают участники комнаты (0 = не выдавать)
    "transfer_commission": "10",   # комиссия за переводы между игроками, %
    "role_commission": "5",        # комиссия с продажи роли игрока, %
    "user_role_days": "15",        # срок жизни роли игрока (дни)
    "user_role_limit": "3",        # сколько ролей может создать один игрок
    "voice_reward": "1",           # монет за каждую минуту в голосовом канале (0 = выключено)
    "reminder_hours": "48",        # за сколько часов до конца срока напоминать в ЛС (0 = выключено)
    "reminder_fallback": "1",      # если ЛС закрыты — упомянуть в #корзина (1 = да, 0 = нет)
    "room_autorenew": "1",         # автопродление комнаты из копилки, если в ней хватает (1 = да)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    last_daily INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shop_roles (
    role_id INTEGER PRIMARY KEY,
    price INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rooms (
    owner_id INTEGER PRIMARY KEY,
    partner_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    role_id INTEGER NOT NULL DEFAULT 0,
    channel_id INTEGER NOT NULL DEFAULT 0,  -- 0 = голосового канала сейчас нет
    created_at INTEGER NOT NULL DEFAULT 0,  -- когда пара купила комнату (для «вместе N дней»)
    fund_owner INTEGER NOT NULL DEFAULT 0,  -- копилка на продление: взнос владельца
    fund_partner INTEGER NOT NULL DEFAULT 0,-- копилка на продление: взнос партнёра
    reminded INTEGER NOT NULL DEFAULT 0     -- 1 = напоминание о конце срока уже отправлено
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    from_id INTEGER NOT NULL,  -- 0 = «система» (казино, награды, администратор)
    to_id INTEGER NOT NULL,    -- 0 = «система»
    amount INTEGER NOT NULL,   -- сколько списано с отправителя
    fee INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'pay',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tx_from ON transactions (from_id);
CREATE INDEX IF NOT EXISTS idx_tx_to ON transactions (to_id);
CREATE TABLE IF NOT EXISTS roulette_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    color TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_roles (
    role_id INTEGER PRIMARY KEY,
    creator_id INTEGER NOT NULL,
    price INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    listed INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS inventory (
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    equipped INTEGER NOT NULL DEFAULT 1,
    reminded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, role_id)
);
CREATE TABLE IF NOT EXISTS stats (
    user_id INTEGER PRIMARY KEY,
    messages INTEGER NOT NULL DEFAULT 0,
    voice_seconds INTEGER NOT NULL DEFAULT 0
);
"""

STAT_COLUMNS = {"messages", "voice_seconds"}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self):
        """Дополняет старую базу новыми колонками (данные сохраняются)."""
        # Таблица transactions могла быть создана раньше с другой схемой (нет колонки ts и т.п.)
        async with self.conn.execute("PRAGMA table_info(transactions)") as cur:
            tx_cols = {r["name"] for r in await cur.fetchall()}
        need = {"ts", "from_id", "to_id", "amount", "fee", "kind", "note"}
        if not need <= tx_cols:
            await self.conn.executescript("""
                DROP INDEX IF EXISTS idx_tx_from;
                DROP INDEX IF EXISTS idx_tx_to;
                DROP TABLE IF EXISTS transactions_old;
                ALTER TABLE transactions RENAME TO transactions_old;
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    from_id INTEGER NOT NULL,
                    to_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    fee INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL DEFAULT 'pay',
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX idx_tx_from ON transactions (from_id);
                CREATE INDEX idx_tx_to ON transactions (to_id);
            """)
        # Старая таблица roulette_log могла быть без нужных колонок (ts и т.п.) — пересобираем
        async with self.conn.execute("PRAGMA table_info(roulette_log)") as cur:
            rl_cols = {r["name"] for r in await cur.fetchall()}
        if not {"ts", "user_id", "number", "color"} <= rl_cols:
            await self.conn.executescript("""
                DROP TABLE roulette_log;
                CREATE TABLE roulette_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    number INTEGER NOT NULL,
                    color TEXT NOT NULL
                );
            """)
            await self.conn.commit()

        async with self.conn.execute("PRAGMA table_info(rooms)") as cur:
            info = {r["name"]: r["pk"] for r in await cur.fetchall()}
        # Старая таблица комнат: ключом был channel_id. Пересобираем с ключом owner_id.
        if info.get("owner_id", 0) == 0:
            role_expr = "role_id" if "role_id" in info else "0"
            await self.conn.executescript(f"""
                CREATE TABLE rooms_new (
                    owner_id INTEGER PRIMARY KEY,
                    partner_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    role_id INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO rooms_new (owner_id, partner_id, expires_at, role_id, channel_id)
                    SELECT owner_id, partner_id, expires_at, {role_expr}, channel_id FROM rooms;
                DROP TABLE rooms;
                ALTER TABLE rooms_new RENAME TO rooms;
            """)

        # Новые колонки (напоминания, копилка, «вместе N дней») — добавляем, данные сохраняются
        await self._add_columns("rooms", {
            "created_at": "INTEGER NOT NULL DEFAULT 0",
            "fund_owner": "INTEGER NOT NULL DEFAULT 0",
            "fund_partner": "INTEGER NOT NULL DEFAULT 0",
            "reminded": "INTEGER NOT NULL DEFAULT 0",
        })
        await self._add_columns("inventory", {"reminded": "INTEGER NOT NULL DEFAULT 0"})
        # Старым комнатам дату создания считаем примерно: конец срока минус длительность комнаты
        days = await self.get_int("room_days")
        await self.conn.execute(
            "UPDATE rooms SET created_at = MAX(0, expires_at - ?) WHERE created_at = 0", (days * 86400,)
        )

    async def _add_columns(self, table: str, columns: dict):
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            have = {r["name"] for r in await cur.fetchall()}
        for name, decl in columns.items():
            if name not in have:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ---------- Настройки ----------
    async def get(self, key: str) -> str:
        async with self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else DEFAULT_SETTINGS[key]

    async def get_int(self, key: str) -> int:
        try:
            return int(await self.get(key))
        except ValueError:
            return int(DEFAULT_SETTINGS[key])

    async def set(self, key: str, value: str):
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self.conn.commit()

    async def fmt(self, amount: int) -> str:
        currency = await self.get("currency")
        return f"{amount:,}".replace(",", " ") + f" {currency}"

    # ---------- Пользователи и баланс ----------
    async def ensure_user(self, user_id: int):
        await self.conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()

    async def get_user(self, user_id: int):
        await self.ensure_user(user_id)
        async with self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

    async def get_balance(self, user_id: int) -> int:
        return (await self.get_user(user_id))["balance"]

    async def add_balance(self, user_id: int, amount: int) -> int:
        """Добавляет (или отнимает, если amount < 0) монеты. Баланс не уходит ниже 0."""
        await self.ensure_user(user_id)
        await self.conn.execute(
            "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id=?", (amount, user_id)
        )
        await self.conn.commit()
        return await self.get_balance(user_id)

    async def try_remove(self, user_id: int, amount: int) -> bool:
        """Списывает монеты, только если их хватает."""
        await self.ensure_user(user_id)
        cur = await self.conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?",
            (amount, user_id, amount),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def claim_daily(self, user_id: int, now: int, streak: int, reward: int):
        await self.ensure_user(user_id)
        await self.conn.execute(
            "UPDATE users SET balance = balance + ?, last_daily=?, streak=? WHERE user_id=?",
            (reward, now, streak, user_id),
        )
        await self.conn.commit()

    async def top(self, limit: int = 10):
        async with self.conn.execute(
            "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()

    async def rank_balance(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        async with self.conn.execute(
            "SELECT COUNT(*) + 1 AS r FROM users "
            "WHERE balance > (SELECT balance FROM users WHERE user_id=?)",
            (user_id,),
        ) as cur:
            return (await cur.fetchone())["r"]

    async def stats(self):
        async with self.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(balance), 0) AS s FROM users"
        ) as cur:
            users = await cur.fetchone()
        async with self.conn.execute("SELECT COUNT(*) AS c FROM shop_roles") as cur:
            shop = await cur.fetchone()
        async with self.conn.execute("SELECT COUNT(*) AS c FROM custom_roles WHERE listed=1") as cur:
            custom = await cur.fetchone()
        async with self.conn.execute("SELECT COUNT(*) AS c FROM rooms") as cur:
            rooms = await cur.fetchone()
        return users["c"], users["s"], shop["c"], custom["c"], rooms["c"]

    # ---------- Сообщения и голосовые ----------
    async def add_message(self, user_id: int):
        await self.conn.execute(
            "INSERT INTO stats (user_id, messages) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET messages = messages + 1",
            (user_id,),
        )
        await self.conn.commit()

    async def add_voice(self, user_ids: list[int], seconds: int, reward: int = 0):
        """Добавляет время в голосовых и (если reward > 0) монеты каждому из списка."""
        await self.conn.executemany(
            "INSERT INTO stats (user_id, voice_seconds) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET voice_seconds = voice_seconds + excluded.voice_seconds",
            [(uid, seconds) for uid in user_ids],
        )
        if reward > 0:
            await self.conn.executemany(
                "INSERT INTO users (user_id, balance) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance",
                [(uid, reward) for uid in user_ids],
            )
        await self.conn.commit()

    async def get_stats(self, user_id: int):
        await self.conn.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()
        async with self.conn.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

    async def rank_stat(self, user_id: int, column: str) -> int:
        if column not in STAT_COLUMNS:
            raise ValueError(column)
        await self.get_stats(user_id)
        async with self.conn.execute(
            f"SELECT COUNT(*) + 1 AS r FROM stats "
            f"WHERE {column} > (SELECT {column} FROM stats WHERE user_id=?)",
            (user_id,),
        ) as cur:
            return (await cur.fetchone())["r"]

    async def top_stat(self, column: str, limit: int = 10):
        if column not in STAT_COLUMNS:
            raise ValueError(column)
        async with self.conn.execute(
            f"SELECT user_id, {column} AS value FROM stats WHERE {column} > 0 "
            f"ORDER BY {column} DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()

    # ---------- Роли сервера (магазин администратора) ----------
    async def add_shop_role(self, role_id: int, price: int, description: str):
        await self.conn.execute(
            "INSERT OR REPLACE INTO shop_roles (role_id, price, description) VALUES (?, ?, ?)",
            (role_id, price, description),
        )
        await self.conn.commit()

    async def remove_shop_role(self, role_id: int):
        await self.conn.execute("DELETE FROM shop_roles WHERE role_id=?", (role_id,))
        await self.conn.commit()

    async def get_shop_roles(self):
        async with self.conn.execute("SELECT * FROM shop_roles ORDER BY price") as cur:
            return await cur.fetchall()

    async def get_shop_role(self, role_id: int):
        async with self.conn.execute("SELECT * FROM shop_roles WHERE role_id=?", (role_id,)) as cur:
            return await cur.fetchone()

    # ---------- Роли игроков ----------
    async def add_custom_role(self, role_id: int, creator_id: int, price: int, expires_at: int):
        await self.conn.execute(
            "INSERT INTO custom_roles (role_id, creator_id, price, expires_at, listed) VALUES (?, ?, ?, ?, 1)",
            (role_id, creator_id, price, expires_at),
        )
        await self.conn.commit()

    async def get_custom_role(self, role_id: int):
        async with self.conn.execute("SELECT * FROM custom_roles WHERE role_id=?", (role_id,)) as cur:
            return await cur.fetchone()

    async def get_listed_custom_roles(self, now: int):
        async with self.conn.execute(
            "SELECT * FROM custom_roles WHERE listed=1 AND expires_at>? ORDER BY price", (now,)
        ) as cur:
            return await cur.fetchall()

    async def all_custom_roles(self):
        async with self.conn.execute("SELECT * FROM custom_roles") as cur:
            return await cur.fetchall()

    async def count_custom_roles_of(self, creator_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM custom_roles WHERE creator_id=?", (creator_id,)
        ) as cur:
            return (await cur.fetchone())["c"]

    async def set_custom_listed(self, role_id: int, listed: int):
        await self.conn.execute("UPDATE custom_roles SET listed=? WHERE role_id=?", (listed, role_id))
        await self.conn.commit()

    async def update_custom_expiry(self, role_id: int, expires_at: int):
        await self.conn.execute("UPDATE custom_roles SET expires_at=? WHERE role_id=?", (expires_at, role_id))
        await self.conn.commit()

    async def get_expired_custom_roles(self, now: int):
        async with self.conn.execute("SELECT * FROM custom_roles WHERE expires_at<=?", (now,)) as cur:
            return await cur.fetchall()

    async def delete_custom_role(self, role_id: int):
        """Удаляет роль игрока и все её записи в инвентарях."""
        await self.conn.execute("DELETE FROM custom_roles WHERE role_id=?", (role_id,))
        await self.conn.execute("DELETE FROM inventory WHERE role_id=?", (role_id,))
        await self.conn.commit()

    # ---------- Инвентарь ----------
    async def add_item(self, user_id: int, role_id: int, expires_at: int, equipped: int = 1):
        await self.conn.execute(
            "INSERT OR REPLACE INTO inventory (user_id, role_id, expires_at, equipped, reminded) "
            "VALUES (?, ?, ?, ?, 0)",
            (user_id, role_id, expires_at, equipped),
        )
        await self.conn.commit()

    async def get_item(self, user_id: int, role_id: int):
        async with self.conn.execute(
            "SELECT * FROM inventory WHERE user_id=? AND role_id=?", (user_id, role_id)
        ) as cur:
            return await cur.fetchone()

    async def get_inventory(self, user_id: int, now: int):
        async with self.conn.execute(
            "SELECT * FROM inventory WHERE user_id=? AND (expires_at=0 OR expires_at>?) ORDER BY role_id",
            (user_id, now),
        ) as cur:
            return await cur.fetchall()

    async def set_equipped(self, user_id: int, role_id: int, equipped: int):
        await self.conn.execute(
            "UPDATE inventory SET equipped=? WHERE user_id=? AND role_id=?", (equipped, user_id, role_id)
        )
        await self.conn.commit()

    async def set_item_expiry(self, user_id: int, role_id: int, expires_at: int):
        await self.conn.execute(
            "UPDATE inventory SET expires_at=?, reminded=0 WHERE user_id=? AND role_id=?",
            (expires_at, user_id, role_id),
        )
        await self.conn.commit()

    async def remove_item(self, user_id: int, role_id: int):
        await self.conn.execute("DELETE FROM inventory WHERE user_id=? AND role_id=?", (user_id, role_id))
        await self.conn.commit()

    async def get_items_to_remind(self, now: int, until: int):
        """Предметы, срок которых закончится в ближайшее время, и напоминания ещё не было."""
        async with self.conn.execute(
            "SELECT * FROM inventory WHERE reminded=0 AND expires_at>? AND expires_at<=?", (now, until)
        ) as cur:
            return await cur.fetchall()

    async def mark_item_reminded(self, user_id: int, role_id: int):
        await self.conn.execute(
            "UPDATE inventory SET reminded=1 WHERE user_id=? AND role_id=?", (user_id, role_id)
        )
        await self.conn.commit()

    async def get_expired_items(self, now: int):
        async with self.conn.execute(
            "SELECT * FROM inventory WHERE expires_at>0 AND expires_at<=?", (now,)
        ) as cur:
            return await cur.fetchall()

    # ---------- Любовные комнаты ----------
    async def add_room(self, owner_id: int, partner_id: int, expires_at: int, role_id: int, created_at: int):
        await self.conn.execute(
            "INSERT INTO rooms (owner_id, partner_id, expires_at, role_id, channel_id, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (owner_id, partner_id, expires_at, role_id, created_at),
        )
        await self.conn.commit()

    async def get_room_of(self, user_id: int):
        async with self.conn.execute(
            "SELECT * FROM rooms WHERE owner_id=? OR partner_id=?", (user_id, user_id)
        ) as cur:
            return await cur.fetchone()

    async def get_room_by_channel(self, channel_id: int):
        async with self.conn.execute("SELECT * FROM rooms WHERE channel_id=?", (channel_id,)) as cur:
            return await cur.fetchone()

    async def all_rooms(self):
        async with self.conn.execute("SELECT * FROM rooms") as cur:
            return await cur.fetchall()

    async def set_room_channel(self, owner_id: int, channel_id: int):
        await self.conn.execute("UPDATE rooms SET channel_id=? WHERE owner_id=?", (channel_id, owner_id))
        await self.conn.commit()

    async def set_room_expiry(self, owner_id: int, expires_at: int):
        await self.conn.execute(
            "UPDATE rooms SET expires_at=?, reminded=0 WHERE owner_id=?", (expires_at, owner_id)
        )
        await self.conn.commit()

    async def set_room_fund(self, owner_id: int, fund_owner: int, fund_partner: int):
        await self.conn.execute(
            "UPDATE rooms SET fund_owner=?, fund_partner=? WHERE owner_id=?", (fund_owner, fund_partner, owner_id)
        )
        await self.conn.commit()

    async def add_room_fund(self, owner_id: int, is_owner: bool, amount: int):
        column = "fund_owner" if is_owner else "fund_partner"
        await self.conn.execute(f"UPDATE rooms SET {column} = {column} + ? WHERE owner_id=?", (amount, owner_id))
        await self.conn.commit()

    async def rooms_fund_total(self) -> int:
        async with self.conn.execute("SELECT COALESCE(SUM(fund_owner + fund_partner), 0) AS s FROM rooms") as cur:
            return (await cur.fetchone())["s"]

    async def get_rooms_to_remind(self, now: int, until: int):
        async with self.conn.execute(
            "SELECT * FROM rooms WHERE reminded=0 AND expires_at>? AND expires_at<=?", (now, until)
        ) as cur:
            return await cur.fetchall()

    async def mark_room_reminded(self, owner_id: int):
        await self.conn.execute("UPDATE rooms SET reminded=1 WHERE owner_id=?", (owner_id,))
        await self.conn.commit()

    async def delete_room(self, owner_id: int):
        await self.conn.execute("DELETE FROM rooms WHERE owner_id=?", (owner_id,))
        await self.conn.commit()

    # ---------- История переводов ----------
    async def add_transaction(self, ts: int, from_id: int, to_id: int, amount: int, fee: int,
                              kind: str = "pay", note: str = ""):
        await self.conn.execute(
            "INSERT INTO transactions (ts, from_id, to_id, amount, fee, kind, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, from_id, to_id, amount, fee, kind, note),
        )
        await self.conn.commit()

    async def get_transactions(self, user_id: int, limit: int = 15, offset: int = 0):
        async with self.conn.execute(
            "SELECT * FROM transactions WHERE from_id=? OR to_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, user_id, limit, offset),
        ) as cur:
            return await cur.fetchall()

    async def count_transactions(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE from_id=? OR to_id=?", (user_id, user_id)
        ) as cur:
            return (await cur.fetchone())["c"]

    # ---------- Журнал рулетки ----------
    async def add_roulette(self, ts: int, user_id: int, number: int, color: str):
        await self.conn.execute(
            "INSERT INTO roulette_log (ts, user_id, number, color) VALUES (?, ?, ?, ?)",
            (ts, user_id, number, color),
        )
        await self.conn.commit()

    async def get_roulette(self, limit: int = 20):
        async with self.conn.execute("SELECT * FROM roulette_log ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            return await cur.fetchall()

    async def roulette_color_counts(self):
        async with self.conn.execute("SELECT color, COUNT(*) AS c FROM roulette_log GROUP BY color") as cur:
            return {r["color"]: r["c"] for r in await cur.fetchall()}
