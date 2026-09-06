#!/usr/bin/env python3
# logger_bot.py - send subscription/membership activity logs to a Telegram log group
import json
import logging
import asyncio
import os
from datetime import datetime
from typing import Optional, Dict, Any

import bot_settings

logger = logging.getLogger(__name__)

# ============ Constants ============
LOG_GROUP_ID = None  # e.g. -1001234567890

TOPICS_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics_state.json")


def init_logger(group_id: int):
    global LOG_GROUP_ID
    LOG_GROUP_ID = group_id
    logger.info(f"Logger initialized with group ID: {group_id}")


def _topic_display_name(topic_id: int) -> str:
    """Returns the topic name, suffixed with the admin-set bot name if any,
    e.g. '📢 New Users(Terminal Bot)'. The name is admin-configurable via
    bot_settings.get_bot_name() (see admin.py's "▤ Log Group" menu) - never
    hardcoded here."""
    base_name = TOPIC_NAMES.get(topic_id, str(topic_id))
    bot_name = bot_settings.get_bot_name()
    return f"{base_name}({bot_name})" if bot_name else base_name


# ============ Topic IDs ============
class Topics:
    """Topic IDs for log categories (starts at 2; thread_id 1 is the group's default "General" topic)."""
    USER_JOIN = 30          # New user starts the bot
    INVOICE = 31            # Invoice issued (card payment awaiting approval)
    SUBSCRIPTION = 32       # New subscription activated / renewed
    BALANCE_CHANGE = 33     # Wallet balance changes (top-ups, debits)
    ADMIN_ACTION = 34       # Admin actions (approve/reject payments, plan edits, etc.)
    SUBSCRIPTION_EXPIRE = 35  # Subscription expiry
    SYSTEM_ERROR = 36       # System errors
    USER_ACTIVITY = 37      # General user activity


TOPIC_NAMES = {
    Topics.USER_JOIN: "📢 New Users",
    Topics.INVOICE: "🧾 Invoices",
    Topics.SUBSCRIPTION: "🔑 Subscriptions",
    Topics.BALANCE_CHANGE: "💰 Balance Changes",
    Topics.ADMIN_ACTION: "🔧 Admin Actions",
    Topics.SUBSCRIPTION_EXPIRE: "⏰ Subscription Expiry",
    Topics.SYSTEM_ERROR: "💥 System Errors",
    Topics.USER_ACTIVITY: "🔄 User Activity",
}


# ============ Persisted Topic State ============
def _load_topics_state() -> Dict[str, int]:
    if not os.path.exists(TOPICS_STATE_FILE):
        return {}
    try:
        with open(TOPICS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading topics state file: {e}")
        return {}


def _save_topics_state(state: Dict[str, int]) -> None:
    try:
        with open(TOPICS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving topics state file: {e}")


_TOPIC_THREAD_MAP: Dict[int, int] = {}


def get_thread_id(topic_id: int) -> Optional[int]:
    return _TOPIC_THREAD_MAP.get(topic_id)


# ============ Create Topics ============
async def create_all_topics(bot) -> bool:
    """Creates any missing topics in the log group. Call once on startup."""
    global _TOPIC_THREAD_MAP

    if not LOG_GROUP_ID:
        logger.error("LOG_GROUP_ID is not set")
        return False

    try:
        state = _load_topics_state()
        _TOPIC_THREAD_MAP = {int(k): v for k, v in state.items()}
        created_count = 0

        for topic_id in TOPIC_NAMES:
            if topic_id in _TOPIC_THREAD_MAP:
                continue
            try:
                topic_name = _topic_display_name(topic_id)
                result = await bot.create_forum_topic(chat_id=LOG_GROUP_ID, name=topic_name, icon_color=0x6FB9F0)
                _TOPIC_THREAD_MAP[topic_id] = result.message_thread_id
                created_count += 1
                _save_topics_state({str(k): v for k, v in _TOPIC_THREAD_MAP.items()})
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Failed to create topic {topic_id}: {e}")

        logger.info(f"✅ Created {created_count} new topics")
        return True
    except Exception as e:
        logger.error(f"Error creating topics: {e}")
        return False


# ============ Send Message Function ============
async def send_log_message(topic_id: int, message: str, parse_mode: str = "HTML", bot=None):
    if not LOG_GROUP_ID:
        logger.warning("Logger not initialized. LOG_GROUP_ID is missing.")
        return None
    if bot is None:
        logger.warning("Bot instance not provided")
        return None

    thread_id = get_thread_id(topic_id)
    if thread_id is None:
        logger.warning(f"No thread_id found for logical topic {topic_id}; sending without topic")

    try:
        result = await bot.send_message(
            chat_id=LOG_GROUP_ID, text=message, parse_mode=parse_mode, message_thread_id=thread_id,
        )
        return result
    except Exception as e:
        logger.error(f"Error sending log message: {e}")
        return None


# ============ Format Helpers ============
def _full_name(first_name: str = None, last_name: str = None) -> str:
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    full = f"{first_name} {last_name}".strip()
    return full if full else "Unknown"


def format_user_full(user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> str:
    name = _full_name(first_name, last_name)
    username_text = f"@{username}" if username else "None"
    return f"📛 Name: {name}\n🔰 Username: {username_text}\n🆔 ID: `{user_id}`"


def _resolve_user_details(user_id: int, username: str = None,
                           first_name: str = None, last_name: str = None) -> Dict[str, Any]:
    """Falls back to a DB lookup for name/username so no log ever shows just a bare ID."""
    if username or first_name or last_name:
        return {"user_id": user_id, "username": username, "first_name": first_name, "last_name": last_name}
    try:
        from db.database import get_db
        user = get_db().get_user(user_id)
        if user:
            return {
                "user_id": user_id,
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
            }
    except Exception:
        pass
    return {"user_id": user_id, "username": username, "first_name": first_name, "last_name": last_name}


# ============ Log Functions ============
async def log_user_join(bot, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """Log when a new user starts the bot."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_info = format_user_full(user_id, username, first_name, last_name)

    message = f"""
📢 **New User Started The Bot**
🕐 Time: {timestamp}
{user_info}
    """.strip()

    await send_log_message(Topics.USER_JOIN, message, bot=bot)


async def log_invoice_issued(bot, user_id: int, plan_name: str, price: int, card_digits: str = None,
                              username: str = None, first_name: str = None, last_name: str = None):
    """Log when a card-payment invoice is issued (awaiting admin approval)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = _resolve_user_details(user_id, username, first_name, last_name)
    user_info = format_user_full(**details)

    message = f"""
🧾 **Invoice Issued**
🕐 Time: {timestamp}
{user_info}
▨ Item: {plan_name}
💰 Amount: {price:,}
{'🔢 Last 4 card digits: `' + card_digits + '`' if card_digits else ''}
📊 **Status:** ⏳ Awaiting admin approval
    """.strip()

    await send_log_message(Topics.INVOICE, message, bot=bot)


async def log_subscription_activated(bot, user_id: int, plan_name: str, days: int, price: int,
                                      credit: int = 0, payment_method: str = "Wallet", status: str = "success",
                                      username: str = None, first_name: str = None, last_name: str = None):
    """Log when a subscription plan is activated (wallet purchase or admin-approved card payment)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = _resolve_user_details(user_id, username, first_name, last_name)
    user_info = format_user_full(**details)
    status_text = "✅ Success" if status == "success" else "❌ Failed"

    message = f"""
🔑 **Subscription Activated**
🕐 Time: {timestamp}
{user_info}
▨ Plan: {plan_name}
⧗ Duration: {days} day(s)
💰 Paid: {price:,}
{'◈ Credit applied from previous plan: ' + f'{credit:,}' if credit > 0 else ''}
💳 Payment method: {payment_method}
📊 **Status:** {status_text}
    """.strip()

    await send_log_message(Topics.SUBSCRIPTION, message, bot=bot)


async def log_balance_change(bot, user_id: int, change: int, new_balance: int, reason: str,
                              username: str = None, first_name: str = None, last_name: str = None):
    """Log wallet balance changes (top-ups, debits)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    change_type = "Increase" if change > 0 else "Decrease"
    emoji = "📈" if change > 0 else "📉"
    details = _resolve_user_details(user_id, username, first_name, last_name)
    user_info = format_user_full(**details)

    message = f"""
{emoji} **Balance Change**
🕐 Time: {timestamp}
{user_info}
📊 Type: {change_type}
💰 Amount: {abs(change):,}
💳 New balance: {new_balance:,}
📝 Reason: {reason}
    """.strip()

    await send_log_message(Topics.BALANCE_CHANGE, message, bot=bot)


async def log_admin_action(bot, admin_id: int, action: str, target_user_id: int = None,
                            details: str = None, amount: int = None, username: str = None,
                            first_name: str = None, last_name: str = None,
                            target_username: str = None, target_first_name: str = None,
                            target_last_name: str = None):
    """Log admin actions (approve/reject payments, plan management, etc.)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    admin_details = _resolve_user_details(admin_id, username, first_name, last_name)
    admin_info = format_user_full(**admin_details)

    lines = ["🔧 **Admin Action**", f"🕐 Time: {timestamp}", "👤 Admin ->", admin_info]

    if target_user_id:
        target_details = _resolve_user_details(target_user_id, target_username, target_first_name, target_last_name)
        target_info = format_user_full(**target_details)
        lines.append("👤 Target user ->")
        lines.append(target_info)

    if amount:
        lines.append(f"💰 Amount: {amount:,}")

    lines.append("📝 Details:")
    lines.append(details or "No details")
    lines.append(f"📊 **Action:** {action}")

    await send_log_message(Topics.ADMIN_ACTION, "\n".join(lines), bot=bot)


async def log_subscription_expire(bot, user_id: int, plan_name: str,
                                   username: str = None, first_name: str = None, last_name: str = None):
    """Log when a subscription expires."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = _resolve_user_details(user_id, username, first_name, last_name)
    user_info = format_user_full(**details)

    message = f"""
⏰ **Subscription Expired**
🕐 Time: {timestamp}
{user_info}
▨ Plan: {plan_name}
📊 **Status:** ⏰ Expired
    """.strip()

    await send_log_message(Topics.SUBSCRIPTION_EXPIRE, message, bot=bot)


async def log_system_error(bot, error: str, context: str = None):
    """Log system errors."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"""
💥 **System Error**
🕐 Time: {timestamp}
{'📝 Context: ' + context if context else ''}
❌ **Error:**
<code>{error[:500]}</code>
    """.strip()

    await send_log_message(Topics.SYSTEM_ERROR, message, bot=bot)


async def log_user_activity(bot, user_id: int, action: str, details: str = None,
                             username: str = None, first_name: str = None, last_name: str = None):
    """Log general user activity."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_details = _resolve_user_details(user_id, username, first_name, last_name)
    user_info = format_user_full(**user_details)

    message = f"""
🔄 **User Activity**
🕐 Time: {timestamp}
{user_info}
📝 Activity: {action}
{'📋 Details: ' + details if details else ''}
    """.strip()

    await send_log_message(Topics.USER_ACTIVITY, message, bot=bot)
