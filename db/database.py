"""
db/database.py
===============
PostgreSQL-backed storage for everything that used to live in flat JSON
files (bot_settings.py's sponsor/card/health settings are NOT part of this -
those stay JSON, see bot_settings.py's own comments). This module owns:

  * users            - one row per Telegram user who has ever hit /start
  * plans             - admin-managed subscription plans
  * subscriptions     - each user's currently active plan (1 row per user)
  * wallet_transactions - append-only ledger backing each user's balance
  * payment_requests  - card-to-card payments awaiting admin approval
                         (previously an in-memory dict in subscription.py -
                         moving it here means a bot restart no longer loses
                         pending requests)

Modeled on the connection-handling pattern from the nomone.py sample's
database.py (lazy connect/reconnect, RealDictCursor, explicit commit/
rollback), trimmed down to the tables this bot actually needs.
"""
import logging
import uuid
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_config: dict):
        self.db_config = db_config
        self.conn = None
        self.connect()
        self._create_tables()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def connect(self):
        try:
            if self.conn and not self.conn.closed:
                return
            self.conn = psycopg2.connect(**self.db_config)
            self.conn.autocommit = False
            logger.info("✅ Connected to PostgreSQL successfully")
        except psycopg2.OperationalError as e:
            if "does not exist" in str(e):
                logger.error(f"❌ Database '{self.db_config['database']}' does not exist!")
                logger.info("📌 Please run: python setup_db.py")
            else:
                logger.error(f"❌ Database connection error: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Database connection error: {e}")
            raise

    def get_cursor(self):
        try:
            if self.conn is None or self.conn.closed:
                self.connect()
            return self.conn.cursor(cursor_factory=RealDictCursor)
        except Exception as e:
            logger.error(f"Error getting cursor: {e}")
            self.connect()
            return self.conn.cursor(cursor_factory=RealDictCursor)

    def _execute(self, query, params=None, fetch=None, commit=True):
        """fetch: None (no fetch), 'one', or 'all'. Returns None / dict / list[dict]."""
        cur = self.get_cursor()
        try:
            cur.execute(query, params or ())
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            if commit:
                self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def _create_tables(self):
        cur = self.get_cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    balance BIGINT DEFAULT 0,
                    is_banned BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    id VARCHAR(32) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT DEFAULT '',
                    price BIGINT NOT NULL,
                    days INTEGER NOT NULL,
                    max_servers INTEGER NOT NULL,
                    max_tabs INTEGER NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    plan_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    plan_id VARCHAR(32),
                    plan_name VARCHAR(255),
                    max_servers INTEGER,
                    max_tabs INTEGER,
                    granted_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wallet_transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    amount BIGINT NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    description TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_requests (
                    request_id VARCHAR(32) PRIMARY KEY,
                    type VARCHAR(20) NOT NULL,
                    user_id BIGINT NOT NULL,
                    plan_id VARCHAR(32),
                    amount BIGINT NOT NULL,
                    card_digits VARCHAR(8),
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_id ON wallet_transactions(user_id)")
            self.conn.commit()
            logger.info("✅ Tables verified/created")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"❌ Error creating tables: {e}")
            raise
        finally:
            cur.close()

    # ==================================================================
    # 1. USERS
    # ==================================================================

    def get_or_create_user(self, user_id, username: str = "", first_name: str = "", last_name: str = "") -> dict:
        """Insert the user if new, otherwise refresh their name/username and
        last_seen_at. Called on every /start."""
        row = self._execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                last_seen_at = CURRENT_TIMESTAMP
            RETURNING *
            """,
            (user_id, username or "", first_name or "", last_name or ""),
            fetch="one",
        )
        return dict(row)

    def get_user(self, user_id):
        row = self._execute("SELECT * FROM users WHERE user_id = %s", (user_id,), fetch="one")
        return dict(row) if row else None

    def find_user_by_username(self, username: str):
        row = self._execute(
            "SELECT * FROM users WHERE lower(username) = lower(%s)",
            (username.lstrip("@"),),
            fetch="one",
        )
        return dict(row) if row else None

    def count_users(self) -> int:
        row = self._execute("SELECT COUNT(*) AS c FROM users", fetch="one")
        return row["c"] if row else 0

    def list_users(self, limit: int = 10, offset: int = 0, search: str = None) -> list:
        """Most recently seen first. `search` matches user_id (exact) or
        username/first_name (partial, case-insensitive)."""
        if search:
            search = search.strip()
            like = f"%{search.lstrip('@')}%"
            if search.lstrip("-").isdigit():
                rows = self._execute(
                    """SELECT * FROM users
                       WHERE user_id = %s OR username ILIKE %s OR first_name ILIKE %s
                       ORDER BY last_seen_at DESC LIMIT %s OFFSET %s""",
                    (int(search), like, like, limit, offset),
                    fetch="all",
                )
            else:
                rows = self._execute(
                    """SELECT * FROM users
                       WHERE username ILIKE %s OR first_name ILIKE %s
                       ORDER BY last_seen_at DESC LIMIT %s OFFSET %s""",
                    (like, like, limit, offset),
                    fetch="all",
                )
        else:
            rows = self._execute(
                "SELECT * FROM users ORDER BY last_seen_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
                fetch="all",
            )
        return [dict(r) for r in rows]

    def set_banned(self, user_id, banned: bool) -> bool:
        row = self._execute(
            "UPDATE users SET is_banned = %s WHERE user_id = %s RETURNING user_id",
            (bool(banned), user_id),
            fetch="one",
        )
        return row is not None

    def is_banned(self, user_id) -> bool:
        row = self._execute("SELECT is_banned FROM users WHERE user_id = %s", (user_id,), fetch="one")
        return bool(row["is_banned"]) if row else False

    # ==================================================================
    # 2. WALLET
    # ==================================================================

    def _ensure_user_row(self, user_id):
        """Wallet/plan functions can be called for a user_id that hasn't
        gone through get_or_create_user yet (shouldn't normally happen since
        /start always registers first, but this keeps the FK constraints
        from ever blowing up a payment)."""
        self._execute(
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )

    def get_balance(self, user_id) -> int:
        row = self._execute("SELECT balance FROM users WHERE user_id = %s", (user_id,), fetch="one")
        return int(row["balance"]) if row else 0

    def update_balance(self, user_id, amount: int) -> int:
        """amount is signed (positive credits, negative debits). Returns new
        balance. Does NOT log a transaction - call add_transaction separately."""
        self._ensure_user_row(user_id)
        row = self._execute(
            "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance",
            (int(amount), user_id),
            fetch="one",
        )
        return int(row["balance"])

    def add_transaction(self, user_id, amount: int, type_: str, description: str = ""):
        self._ensure_user_row(user_id)
        self._execute(
            "INSERT INTO wallet_transactions (user_id, amount, type, description) VALUES (%s, %s, %s, %s)",
            (user_id, int(amount), type_, description or ""),
        )

    def get_transactions(self, user_id, limit: int = 10) -> list:
        rows = self._execute(
            "SELECT * FROM wallet_transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
            fetch="all",
        )
        out = []
        for r in rows or []:
            d = dict(r)
            d["at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S") if d.get("created_at") else ""
            out.append(d)
        return out

    # ==================================================================
    # 3. PLANS
    # ==================================================================

    def get_all_plans(self, active_only: bool = True) -> dict:
        if active_only:
            rows = self._execute(
                "SELECT * FROM plans WHERE enabled = TRUE ORDER BY plan_order ASC", fetch="all"
            )
        else:
            rows = self._execute("SELECT * FROM plans ORDER BY plan_order ASC", fetch="all")
        return {r["id"]: self._plan_dict(r) for r in (rows or [])}

    @staticmethod
    def _plan_dict(row) -> dict:
        d = dict(row)
        d["order"] = d.pop("plan_order", 0)
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        return d

    def get_plan(self, plan_id: str):
        row = self._execute("SELECT * FROM plans WHERE id = %s", (plan_id,), fetch="one")
        return self._plan_dict(row) if row else None

    def add_plan(self, name: str, price: int, days: int, max_servers: int, max_tabs: int, description: str = "") -> str:
        plan_id = uuid.uuid4().hex[:8]
        row = self._execute("SELECT COALESCE(MAX(plan_order), 0) AS m FROM plans", fetch="one")
        order = (row["m"] if row else 0) + 1
        self._execute(
            """INSERT INTO plans (id, name, description, price, days, max_servers, max_tabs, enabled, plan_order)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)""",
            (plan_id, name.strip(), (description or "").strip(), int(price), int(days),
             int(max_servers), int(max_tabs), order),
        )
        return plan_id

    def update_plan(self, plan_id: str, **kwargs) -> bool:
        allowed = {"name", "description", "price", "days", "max_servers", "max_tabs", "enabled"}
        fields, values = [], []
        for key, value in kwargs.items():
            if key == "order":
                key = "plan_order"
            elif key not in allowed:
                continue
            fields.append(f"{key} = %s")
            values.append(value)
        if not fields:
            return False
        values.append(plan_id)
        row = self._execute(
            f"UPDATE plans SET {', '.join(fields)} WHERE id = %s RETURNING id", tuple(values), fetch="one"
        )
        return row is not None

    def toggle_plan(self, plan_id: str) -> bool:
        row = self._execute(
            "UPDATE plans SET enabled = NOT enabled WHERE id = %s RETURNING id", (plan_id,), fetch="one"
        )
        return row is not None

    def delete_plan(self, plan_id: str) -> bool:
        row = self._execute("DELETE FROM plans WHERE id = %s RETURNING id", (plan_id,), fetch="one")
        return row is not None

    # ==================================================================
    # 4. SUBSCRIPTIONS
    # ==================================================================

    def get_subscription(self, user_id):
        row = self._execute("SELECT * FROM subscriptions WHERE user_id = %s", (user_id,), fetch="one")
        if not row:
            return None
        d = dict(row)
        d["granted_at"] = d["granted_at"].isoformat() if d.get("granted_at") else None
        d["expires_at"] = d["expires_at"].isoformat() if d.get("expires_at") else None
        return d

    def is_active(self, user_id) -> bool:
        sub = self.get_subscription(user_id)
        if not sub or not sub.get("expires_at"):
            return False
        try:
            return datetime.fromisoformat(sub["expires_at"]) > datetime.now()
        except Exception:
            return False

    def days_remaining(self, user_id) -> int:
        sub = self.get_subscription(user_id)
        if not sub or not sub.get("expires_at"):
            return 0
        try:
            delta = datetime.fromisoformat(sub["expires_at"]) - datetime.now()
        except Exception:
            return 0
        return max(0, delta.days + (1 if delta.seconds > 0 else 0))

    def get_limits(self, user_id):
        if not self.is_active(user_id):
            return 0, 0
        sub = self.get_subscription(user_id)
        return sub.get("max_servers", 0), sub.get("max_tabs", 0)

    def grant_subscription(self, user_id, plan: dict) -> dict:
        """Activates `plan` for the user. Remaining time on an existing
        active subscription is added on top of the new plan's `days`
        (renewal/top-up behaviour); enforced limits always switch to the
        plan just purchased."""
        self._ensure_user_row(user_id)
        now = datetime.now()
        base = now
        existing = self.get_subscription(user_id)
        if existing and existing.get("expires_at"):
            try:
                current_expiry = datetime.fromisoformat(existing["expires_at"])
                if current_expiry > now:
                    base = current_expiry
            except Exception:
                pass

        expires_at = base + timedelta(days=int(plan["days"]))

        self._execute(
            """
            INSERT INTO subscriptions (user_id, plan_id, plan_name, max_servers, max_tabs, granted_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                plan_id = EXCLUDED.plan_id,
                plan_name = EXCLUDED.plan_name,
                max_servers = EXCLUDED.max_servers,
                max_tabs = EXCLUDED.max_tabs,
                granted_at = EXCLUDED.granted_at,
                expires_at = EXCLUDED.expires_at
            """,
            (user_id, plan["id"], plan["name"], int(plan["max_servers"]), int(plan["max_tabs"]), now, expires_at),
        )
        return self.get_subscription(user_id)

    # ==================================================================
    # 5. PAYMENT REQUESTS (card-to-card, awaiting admin approval)
    # ==================================================================

    def create_payment_request(self, request_id: str, type_: str, user_id, amount: int, card_digits: str,
                                username: str = "", first_name: str = "", last_name: str = "", plan_id: str = None):
        self._execute(
            """INSERT INTO payment_requests
               (request_id, type, user_id, plan_id, amount, card_digits, username, first_name, last_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (request_id, type_, user_id, plan_id, int(amount), card_digits, username or "",
             first_name or "", last_name or ""),
        )

    def get_payment_request(self, request_id: str):
        row = self._execute(
            "SELECT * FROM payment_requests WHERE request_id = %s", (request_id,), fetch="one"
        )
        return dict(row) if row else None

    def pop_payment_request(self, request_id: str):
        """Fetch + delete atomically, mirroring dict.pop() on the old
        in-memory PENDING_PAYMENTS store."""
        row = self._execute(
            "DELETE FROM payment_requests WHERE request_id = %s RETURNING *", (request_id,), fetch="one"
        )
        return dict(row) if row else None
