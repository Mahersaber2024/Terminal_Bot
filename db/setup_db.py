"""
setup_db.py
===========
Run this once (and again after pulling updates that add new tables) to
create/verify the PostgreSQL schema used by Terminal Bot. Reads connection
settings from .env via db/config.py (DB_HOST, DB_PORT, DB_NAME, DB_USER,
DB_PASSWORD) - see db/config.py for the full list.

Usage:
    python3 setup_db.py            # interactive
    python3 setup_db.py --auto     # skip the closing prompt (used by install.sh)
"""
import sys

import psycopg2

from db.config import config

DB_CONFIG = config.get_db_config()


def test_connection() -> bool:
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("SELECT version()")
        version = cursor.fetchone()
        print(f"✅ Connected to PostgreSQL: {version[0][:50]}...")
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\n📌 Please check:")
        print("  1. Is PostgreSQL installed and running?")
        print("  2. Are DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD in .env correct?")
        print("  3. Has the database/role already been created (e.g. by install.sh)?")
        return False


def create_tables() -> bool:
    """Delegates to db.database.Database, which creates every table this
    bot needs (users, plans, subscriptions, wallet_transactions,
    payment_requests) with CREATE TABLE IF NOT EXISTS, so this is always
    safe to re-run."""
    try:
        from db.database import Database
        print("\n📊 Creating/verifying tables...")
        Database(DB_CONFIG)  # __init__ connects and calls _create_tables()
        print("✅ users, plans, subscriptions, wallet_transactions, payment_requests ready")
        return True
    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        return False


def show_tables():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """)
        tables = cursor.fetchall()

        if tables:
            print("\n📋 Tables in database:")
            print("-" * 30)
            for table in tables:
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table[0]}"')
                    count = cursor.fetchone()[0]
                    print(f"  • {table[0]} ({count} rows)")
                except Exception:
                    print(f"  • {table[0]}")
        else:
            print("\n📭 No tables found in database.")

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error showing tables: {e}")


def drop_all_tables():
    """Drop all tables (use with caution!)"""
    confirm = input("⚠️ Are you sure you want to drop ALL tables? (yes/no): ")
    if confirm.lower() != "yes":
        print("❌ Operation cancelled.")
        return False
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        for table in ("payment_requests", "wallet_transactions", "subscriptions", "plans", "users"):
            cursor.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ All tables dropped successfully!")
        return True
    except Exception as e:
        print(f"❌ Error dropping tables: {e}")
        return False


def main():
    auto_mode = "--auto" in sys.argv

    print("🚀 Setting up PostgreSQL tables for Terminal Bot...")
    print("=" * 50)
    print(f"📊 Database: {DB_CONFIG['database']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    print(f"👤 User: {DB_CONFIG['user']}")
    print("=" * 50)

    print("\n🔌 Testing connection...")
    if not test_connection():
        print("\n❌ Cannot connect to database with the credentials from .env.")
        sys.exit(1)

    if not create_tables():
        print("❌ Failed to create tables.")
        sys.exit(1)

    show_tables()

    print("\n" + "=" * 50)
    print("✅ Database setup completed successfully!")

    if not auto_mode and "--drop" in sys.argv:
        drop_all_tables()


if __name__ == "__main__":
    main()
