"""MySQL / MariaDB access for Gideon (works with XAMPP + phpMyAdmin).

If the database can't be reached, Gideon still runs, just without memory.
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import aiomysql

import auth

log = logging.getLogger("gideon.db")
MAX_LOGIN_ATTEMPTS = 5
SCHEMA = Path(__file__).parent / "database" / "schema.sql"


def db_name() -> str:
    """The MySQL database to use. Set DB_NAME in .env (default: gideon_db)."""
    return os.getenv("DB_NAME", "gideon_db").strip() or "gideon_db"


def schema_statements() -> list[str]:
    lines = [ln for ln in SCHEMA.read_text(encoding="utf-8").splitlines()
             if not ln.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    # The database name comes from DB_NAME in .env, so skip the CREATE DATABASE / USE lines in the file.
    return [x for x in statements if not x.upper().startswith(("CREATE DATABASE", "USE "))]


class Database:
    def __init__(self) -> None:
        self.pool: aiomysql.Pool | None = None
        self.last_error = ""  # why the last connect failed, shown on the login page
        self._lock = asyncio.Lock()

    @property
    def ok(self) -> bool:
        return self.pool is not None

    async def connect(self) -> None:
        cfg = dict(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            conn = await aiomysql.connect(**cfg)
            try:
                async with conn.cursor() as cur:
                    name = db_name()
                    await cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                    await cur.execute(f"USE `{name}`")
                    statements = schema_statements()
                    views = [x for x in statements if x.upper().startswith("CREATE OR REPLACE VIEW")]
                    for stmt in [x for x in statements if x not in views]:
                        await cur.execute(stmt)
                    await self._upgrade_old_tables(cur)
                    for stmt in views:  # views last: they need the upgraded columns
                        await cur.execute(stmt)
            finally:
                await conn.ensure_closed()
            self.pool = await aiomysql.create_pool(**cfg, db=db_name(), minsize=1, maxsize=5)
            self.last_error = ""
            log.info("Connected to MySQL database '%s'", db_name())
        except Exception as exc:  # noqa: BLE001
            self.pool = None
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("MySQL unavailable (%s). Gideon will run without memory.", exc)

    async def ensure(self) -> bool:
        """Try again to connect if MySQL was down earlier (for example XAMPP was started after server.py)."""
        if self.pool:
            return True
        async with self._lock:
            if not self.pool:
                await self.connect()
        return self.ok

    @staticmethod
    async def _upgrade_old_tables(cur) -> None:
        """Bring an older database up to date. Every step checks first, so it is safe to run again and
        again, even after an earlier upgrade stopped halfway."""

        async def has_column(table: str, column: str) -> bool:
            await cur.execute(f"SHOW COLUMNS FROM `{db_name()}`.`{table}` LIKE %s", (column,))
            return await cur.fetchone() is not None

        async def has_index(table: str, name: str) -> bool:
            await cur.execute(f"SHOW INDEX FROM `{db_name()}`.`{table}` WHERE Key_name = %s", (name,))
            return await cur.fetchone() is not None

        async def attempt(sql: str) -> None:
            try:
                await cur.execute(sql)
            except Exception as exc:  # noqa: BLE001 - a failed upgrade step must not stop Gideon from starting
                log.warning("Database upgrade step skipped (%s): %s", exc, sql[:90])

        # Old hashed column -> plain "password" (old hashes won't match what you type; reset them).
        if await has_column("users", "password_hash") and not await has_column("users", "password"):
            await attempt("ALTER TABLE users CHANGE password_hash password VARCHAR(255) NOT NULL")

        # First names and login-attempt tracking, added after the first login release.
        for column, ddl in (
            ("first_name", "ADD COLUMN first_name VARCHAR(50) NULL AFTER username"),
            ("role", "ADD COLUMN role ENUM('user','admin') NOT NULL DEFAULT 'user' AFTER first_name"),
            ("api_key", "ADD COLUMN api_key VARCHAR(255) NULL AFTER password"),
            ("login_attempts_left", "ADD COLUMN login_attempts_left TINYINT UNSIGNED NOT NULL DEFAULT 5"),
            ("status", "ADD COLUMN status ENUM('active','disabled') NOT NULL DEFAULT 'active'"),
            ("disabled_at", "ADD COLUMN disabled_at DATETIME NULL"),
        ):
            if not await has_column("users", column):
                await attempt(f"ALTER TABLE users {ddl}")

        # Databases made before logins existed get a user_id column on conversations and timers.
        for table, fk in (("conversations", "fk_conversations_user"), ("timers", "fk_timers_user")):
            if not await has_column(table, "user_id"):
                await attempt(f"ALTER TABLE {table} ADD COLUMN user_id INT UNSIGNED NULL AFTER id")
            if not await has_index(table, f"idx_{table}_user") and await has_column(table, "user_id"):
                await attempt(f"ALTER TABLE {table} ADD KEY idx_{table}_user (user_id)")
            await cur.execute(
                "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = %s "
                "AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s",
                (db_name(), table, fk),
            )
            if await cur.fetchone() is None and await has_column(table, "user_id"):
                await attempt(
                    f"ALTER TABLE {table} ADD CONSTRAINT {fk} FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE"
                )

        # Chats (topics): every conversation belongs to one chat.
        if not await has_column("conversations", "chat_id"):
            await attempt("ALTER TABLE conversations ADD COLUMN chat_id INT UNSIGNED NULL AFTER user_id")
        if await has_column("conversations", "chat_id"):
            if not await has_index("conversations", "idx_conversations_chat"):
                await attempt("ALTER TABLE conversations ADD KEY idx_conversations_chat (chat_id, id)")
            await cur.execute(
                "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = %s "
                "AND TABLE_NAME = 'conversations' AND CONSTRAINT_NAME = 'fk_conversations_chat'",
                (db_name(),),
            )
            if await cur.fetchone() is None:
                await attempt(
                    "ALTER TABLE conversations ADD CONSTRAINT fk_conversations_chat "
                    "FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE"
                )

    async def close(self) -> None:
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()

    async def _exec(self, sql: str, args=None):
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, args)
                    return cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            log.warning("Database write failed: %s", exc)
            return None

    async def _exec_count(self, sql: str, args=None) -> int:
        """Run a write and return how many rows it changed."""
        if not self.pool:
            return 0
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, args)
                    return cur.rowcount
        except Exception as exc:  # noqa: BLE001
            log.warning("Database write failed: %s", exc)
            return 0

    async def _all(self, sql: str, args=None) -> list[dict]:
        if not self.pool:
            return []
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(sql, args)
                    return list(await cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            log.warning("Database read failed: %s", exc)
            return []

    # conversations and messages
    async def start_conversation(self, user_id: int, chat_id: int, model: str, voice: str):
        conversation_id = await self._exec(
            "INSERT INTO conversations (user_id, chat_id, model, voice) VALUES (%s, %s, %s, %s)",
            (user_id, chat_id, model, voice),
        )
        await self._exec("UPDATE chats SET updated_at = %s WHERE id = %s", (datetime.now(), chat_id))
        return conversation_id

    async def end_conversation(self, conversation_id) -> None:
        if conversation_id:
            await self._exec("UPDATE conversations SET ended_at = NOW() WHERE id = %s", (conversation_id,))
            await self._exec(
                "UPDATE chats ch JOIN conversations c ON c.chat_id = ch.id SET ch.updated_at = %s WHERE c.id = %s",
                (datetime.now(), conversation_id),
            )

    async def add_message(self, conversation_id, role: str, channel: str, content: str) -> None:
        if conversation_id and content:
            await self._exec(
                "INSERT INTO messages (conversation_id, role, channel, content) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, channel, content),
            )

    async def recent_messages(self, user_id: int, chat_id: int, limit: int = 30) -> list[dict]:
        """The newest `limit` messages of one chat, oldest first. This is Gideon's memory for that chat."""
        return await self._all(
            "SELECT role, content FROM (SELECT m.id, m.role, m.content FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id WHERE c.user_id = %s AND c.chat_id = %s "
            "ORDER BY m.id DESC LIMIT %s) recent ORDER BY id",
            (user_id, chat_id, limit),
        )

    async def chat_history(self, user_id: int, chat_id: int, limit: int = 200) -> list[dict]:
        """The newest `limit` messages of one chat (with conversation and time), oldest first, for the transcript."""
        return await self._all(
            "SELECT id, conversation_id, role, content, created_at FROM ("
            "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id WHERE c.user_id = %s AND c.chat_id = %s "
            "ORDER BY m.id DESC LIMIT %s) recent ORDER BY id",
            (user_id, chat_id, limit),
        )

    # chats: separate topics, each with its own history and memory
    async def list_chats(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT ch.id, ch.title, ch.updated_at, COUNT(m.id) AS messages FROM chats ch "
            "LEFT JOIN conversations c ON c.chat_id = ch.id "
            "LEFT JOIN messages m ON m.conversation_id = c.id "
            "WHERE ch.user_id = %s GROUP BY ch.id, ch.title, ch.updated_at "
            "ORDER BY ch.updated_at DESC, ch.id DESC",
            (user_id,),
        )

    async def get_chat(self, user_id: int, chat_id: int):
        rows = await self._all("SELECT id, title FROM chats WHERE id = %s AND user_id = %s", (chat_id, user_id))
        return rows[0] if rows else None

    async def count_chats(self, user_id: int) -> int:
        rows = await self._all("SELECT COUNT(*) AS n FROM chats WHERE user_id = %s", (user_id,))
        return rows[0]["n"] if rows else 0

    async def create_chat(self, user_id: int, title: str):
        return await self._exec("INSERT INTO chats (user_id, title) VALUES (%s, %s)", (user_id, title))

    async def rename_chat(self, user_id: int, chat_id: int, title: str) -> None:
        await self._exec("UPDATE chats SET title = %s WHERE id = %s AND user_id = %s", (title, chat_id, user_id))

    async def delete_chat(self, user_id: int, chat_id: int) -> None:
        """Deletes the chat with all its conversations and messages."""
        await self._exec("DELETE FROM chats WHERE id = %s AND user_id = %s", (chat_id, user_id))

    async def default_chat_id(self, user_id: int):
        """The user's most recently used chat. A user with no chats gets one called General."""
        rows = await self._all(
            "SELECT id FROM chats WHERE user_id = %s ORDER BY updated_at DESC, id DESC LIMIT 1", (user_id,)
        )
        return rows[0]["id"] if rows else await self.create_chat(user_id, "General")

    async def adopt_conversations_into_chats(self) -> None:
        """Conversations saved before chats existed are put into each user's General chat."""
        for row in await self._all(
            "SELECT DISTINCT user_id FROM conversations WHERE chat_id IS NULL AND user_id IS NOT NULL"
        ):
            chat_id = await self.default_chat_id(row["user_id"])
            if chat_id:
                await self._exec(
                    "UPDATE conversations SET chat_id = %s WHERE user_id = %s AND chat_id IS NULL",
                    (chat_id, row["user_id"]),
                )

    # timers
    async def create_timer(self, user_id: int, conversation_id, label: str, seconds: int, ends_at: datetime):
        return await self._exec(
            "INSERT INTO timers (user_id, conversation_id, label, duration_seconds, started_at, ends_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, conversation_id, label[:100], seconds, datetime.now(), ends_at),
        )

    async def set_timer_status(self, timer_id: int, status: str) -> None:
        await self._exec("UPDATE timers SET status = %s WHERE id = %s", (status, timer_id))

    async def running_timers(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT id, label, duration_seconds, ends_at FROM timers "
            "WHERE user_id = %s AND status = 'running' ORDER BY ends_at",
            (user_id,),
        )

    # users and login sessions
    async def count_users(self) -> int:
        rows = await self._all("SELECT COUNT(*) AS n FROM users")
        return rows[0]["n"] if rows else 0

    async def get_user(self, username: str):
        rows = await self._all(
            "SELECT id, username, first_name, role, password, login_attempts_left, status FROM users WHERE username = %s",
            (username,),
        )
        return rows[0] if rows else None

    async def list_users(self) -> list[dict]:
        return await self._all(
            "SELECT username, first_name, role, status, login_attempts_left, "
            "(api_key IS NOT NULL AND api_key <> '') AS has_api_key, created_at, last_login_at "
            "FROM users ORDER BY id"
        )

    async def create_user(self, username: str, password: str, first_name: str | None = None,
                          role: str = "user", api_key: str | None = None):
        return await self._exec(
            "INSERT INTO users (username, first_name, role, password, api_key) VALUES (%s, %s, %s, %s, %s)",
            (username, first_name, role, password, api_key),
        )

    # admin dashboard
    ADMIN_COLUMNS = (
        "id, username, first_name, role, status, login_attempts_left, api_key, disabled_at, "
        "created_at, last_login_at"
    )
    EDITABLE_COLUMNS = {"first_name", "role", "api_key"}

    async def list_users_admin(self) -> list[dict]:
        return await self._all(f"SELECT {self.ADMIN_COLUMNS} FROM users ORDER BY id")

    async def get_user_by_id(self, user_id: int):
        rows = await self._all(f"SELECT {self.ADMIN_COLUMNS} FROM users WHERE id = %s", (user_id,))
        return rows[0] if rows else None

    async def update_user(self, user_id: int, fields: dict) -> None:
        """Save first_name, role and api_key. Only these columns are allowed."""
        sets = [f"{col} = %s" for col in fields if col in self.EDITABLE_COLUMNS]
        if sets:
            values = [fields[col] for col in fields if col in self.EDITABLE_COLUMNS]
            await self._exec(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*values, user_id))

    async def disable_user(self, user_id: int) -> None:
        await self._exec(
            "UPDATE users SET status = 'disabled', disabled_at = %s WHERE id = %s", (datetime.now(), user_id)
        )
        await self._exec("DELETE FROM login_sessions WHERE user_id = %s", (user_id,))

    async def count_admins(self) -> int:
        rows = await self._all("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")
        return rows[0]["n"] if rows else 0

    async def count_active_admins(self) -> int:
        rows = await self._all("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'")
        return rows[0]["n"] if rows else 0

    async def set_first_name(self, user_id: int, first_name: str) -> None:
        await self._exec("UPDATE users SET first_name = %s WHERE id = %s", (first_name, user_id))

    async def backfill_first_names(self) -> None:
        """Users without a first name get a guess from their username ('Shreyas_12' -> 'Shreyas')."""
        for row in await self._all("SELECT id, username FROM users WHERE first_name IS NULL OR first_name = ''"):
            await self.set_first_name(row["id"], auth.derive_first_name(row["username"]))

    # login attempts
    async def record_failed_login(self, user_id: int) -> int:
        """Use up one login attempt. At zero the account is disabled and signed out. Returns attempts left."""
        await self._exec(
            "UPDATE users SET login_attempts_left = GREATEST(login_attempts_left, 1) - 1 WHERE id = %s", (user_id,)
        )
        rows = await self._all("SELECT login_attempts_left FROM users WHERE id = %s", (user_id,))
        left = int(rows[0]["login_attempts_left"]) if rows else 0
        if left == 0:
            await self._exec(
                "UPDATE users SET status = 'disabled', disabled_at = %s WHERE id = %s", (datetime.now(), user_id)
            )
            await self._exec("DELETE FROM login_sessions WHERE user_id = %s", (user_id,))
        return left

    async def reset_login_attempts(self, user_id: int) -> None:
        await self._exec(
            "UPDATE users SET login_attempts_left = %s WHERE id = %s AND login_attempts_left <> %s",
            (MAX_LOGIN_ATTEMPTS, user_id, MAX_LOGIN_ATTEMPTS),
        )

    async def enable_user(self, user_id: int) -> None:
        await self._exec(
            "UPDATE users SET status = 'active', disabled_at = NULL, login_attempts_left = %s WHERE id = %s",
            (MAX_LOGIN_ATTEMPTS, user_id),
        )

    # deleting saved chat
    async def delete_messages(self, user_id: int, message_ids: list[int]) -> int:
        """Delete the chosen messages, but only ones that belong to this user."""
        ids = [int(i) for i in message_ids][:2000]
        if not ids:
            return 0
        marks = ",".join(["%s"] * len(ids))
        deleted = await self._exec_count(
            "DELETE m FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            f"WHERE c.user_id = %s AND m.id IN ({marks})",
            (user_id, *ids),
        )
        await self._drop_empty_conversations(user_id)
        return deleted

    async def clear_history(self, user_id: int, chat_id: int) -> int:
        """Delete every message in one chat. The chat itself stays."""
        deleted = await self._exec_count(
            "DELETE m FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.user_id = %s AND c.chat_id = %s",
            (user_id, chat_id),
        )
        await self._drop_empty_conversations(user_id)
        return deleted

    async def _drop_empty_conversations(self, user_id: int) -> None:
        """Remove finished conversations with no messages left. A conversation still open is kept."""
        await self._exec(
            "DELETE c FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
            "WHERE c.user_id = %s AND c.ended_at IS NOT NULL AND m.id IS NULL",
            (user_id,),
        )

    async def set_password(self, user_id: int, password: str) -> None:
        await self._exec("UPDATE users SET password = %s WHERE id = %s", (password, user_id))
        await self._exec("DELETE FROM login_sessions WHERE user_id = %s", (user_id,))  # sign out everywhere

    async def delete_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM users WHERE id = %s", (user_id,))

    async def touch_login(self, user_id: int) -> None:
        await self._exec("UPDATE users SET last_login_at = %s WHERE id = %s", (datetime.now(), user_id))

    async def adopt_orphans(self, user_id: int) -> None:
        """Give chats and timers saved before logins existed to this user."""
        await self._exec("UPDATE conversations SET user_id = %s WHERE user_id IS NULL", (user_id,))
        await self._exec("UPDATE timers SET user_id = %s WHERE user_id IS NULL", (user_id,))

    async def create_login_session(self, user_id: int, token_hash: str, expires_at: datetime) -> None:
        await self._exec(
            "INSERT INTO login_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash, user_id, expires_at),
        )

    async def get_session_user(self, token_hash: str):
        rows = await self._all(
            "SELECT u.id, u.username, u.first_name, u.role, u.api_key FROM login_sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > %s AND u.status = 'active'",
            (token_hash, datetime.now()),
        )
        return rows[0] if rows else None

    async def delete_login_session(self, token_hash: str) -> None:
        await self._exec("DELETE FROM login_sessions WHERE token_hash = %s", (token_hash,))

    async def purge_expired_sessions(self) -> None:
        await self._exec("DELETE FROM login_sessions WHERE expires_at <= %s", (datetime.now(),))