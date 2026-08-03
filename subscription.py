"""
subscription.py
"""
import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import bot_settings
import config
from db.database import get_db

logger = logging.getLogger(__name__)


# ============================================================
# 1. PLANS - admin-managed, nothing auto-seeded
# ============================================================

def get_all_plans(active_only: bool = True) -> dict:
    """{plan_id: plan_dict}, sorted by 'order'."""
    return get_db().get_all_plans(active_only=active_only)


def get_plan(plan_id: str):
    return get_db().get_plan(plan_id)


def get_default_plan():
    """The plan (if any) currently marked to be auto-granted to new users."""
    return get_db().get_default_plan()


def set_default_plan(plan_id: str) -> bool:
    """Marks `plan_id` as the one auto-granted to new users; unmarks any
    previous default (only one plan can be default at a time)."""
    return get_db().set_default_plan(plan_id)


def add_plan(name: str, price: int, days: int, max_servers: int, max_tabs: int,
             description: str = "", is_default: bool = False, sftp_enabled: bool = True,
             session_timeout_minutes: int = None, max_automations: int = None) -> str:
    return get_db().add_plan(
        name, price, days, max_servers, max_tabs, description, is_default=is_default,
        sftp_enabled=sftp_enabled, session_timeout_minutes=session_timeout_minutes,
        max_automations=max_automations,
    )


def update_plan(plan_id: str, **kwargs) -> bool:
    return get_db().update_plan(plan_id, **kwargs)


def ensure_default_plan(user_id) -> bool:
    """Call this after registering/refreshing a user (e.g. on /start). Grants
    the configured default (free) plan if the user doesn't currently have an
    active subscription. Returns True if a plan was just granted."""
    return get_db().grant_default_plan_if_needed(user_id)


def toggle_plan(plan_id: str) -> bool:
    return get_db().toggle_plan(plan_id)


def delete_plan(plan_id: str) -> bool:
    return get_db().delete_plan(plan_id)


# ============================================================
# 2. WALLET - per-user balance + transaction history
# ============================================================

def get_balance(user_id) -> int:
    return get_db().get_balance(user_id)


def add_transaction(user_id, amount: int, type_: str, description: str = ""):
    """amount is signed: positive = credit, negative = debit."""
    get_db().add_transaction(user_id, amount, type_, description)


def update_balance(user_id, amount: int) -> int:
    """amount is signed (positive credits, negative debits). Returns new balance.
    Does NOT log a transaction - call add_transaction separately so callers can
    control the description."""
    return get_db().update_balance(user_id, amount)


def get_transactions(user_id, limit: int = 10) -> list:
    return get_db().get_transactions(user_id, limit=limit)


# ============================================================
# 3. SUBSCRIPTIONS - which plan each user has active, and until when
# ============================================================

def get_subscription(user_id) -> dict:
    """Raw stored record, or None if the user never bought a plan."""
    return get_db().get_subscription(user_id)


def is_active(user_id) -> bool:
    return get_db().is_active(user_id)


def days_remaining(user_id) -> int:
    return get_db().days_remaining(user_id)


def get_limits(user_id):
    """(max_servers, max_tabs) for the user's currently active plan, or
    (0, 0) if they have no active subscription."""
    return get_db().get_limits(user_id)


def get_capabilities(user_id) -> dict:
    """Full plan-enforced capability dict for the user's active plan:
    {max_servers, max_tabs, sftp_enabled, session_timeout_minutes, max_automations}.
    session_timeout_minutes/max_automations of None mean unlimited. Every
    value is closed/zeroed if the user has no active subscription."""
    return get_db().get_capabilities(user_id)


def grant_subscription(user_id, plan: dict) -> dict:
    """Activates `plan` for the user. If they already have time left on a
    current subscription, that remaining time is added on top of the new
    plan's `days` (renewal/top-up behaviour) rather than being discarded;
    the enforced limits always switch to the plan just purchased."""
    return get_db().grant_subscription(user_id, plan)


# ============================================================
# 4. PAYMENTS - user-facing buy / top-up flow + admin DM approval
# ============================================================
SUBSCRIPTION_BUTTON_TEXT = "💳 Subscription"
CANCEL_BUTTON_TEXT = "❌ Cancel"

# Conversation states
PAY_CARD_DIGITS = 701
TOPUP_AMOUNT = 702
TOPUP_CARD_DIGITS = 703

# ---- shared main-menu wiring (same pattern as admin.py) ----
_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu():
    if _get_main_menu_func:
        return _get_main_menu_func()
    return None


def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True)


def _uid(update: Update) -> int:
    return update.effective_user.id


# ---- subscription status menu ----

def _capability_lines(d: dict) -> list:
    """Shared formatting for the SFTP / session-timeout / automation flags,
    used by both the subscription status screen and a plan's buy-detail
    screen. `d` can be a subscription row or a plan dict - both use the
    same key names (sftp_enabled, session_timeout_minutes, max_automations)."""
    lines = []
    lines.append("🗄 SFTP file browser: " + ("✅ included" if d.get("sftp_enabled", True) else "🔒 not included"))
    timeout = d.get("session_timeout_minutes")
    lines.append(f"⏱ SSH session length: {timeout} min" if timeout else "⏱ SSH session length: unlimited")
    max_auto = d.get("max_automations")
    lines.append(f"⚙️ Automation jobs: up to {max_auto}" if max_auto is not None else "⚙️ Automation jobs: unlimited")
    return lines


def _status_text(user_id) -> str:
    balance = get_balance(user_id)
    lines = ["💳 *Subscription*\n"]
    if is_active(user_id):
        sub = get_subscription(user_id)
        days = days_remaining(user_id)
        lines.append(f"✅ Active plan: *{sub['plan_name']}*")
        lines.append(f"⏳ Expires in: {days} day(s)")
        lines.append(f"🖥 Server limit: {sub['max_servers']}")
        lines.append(f"📑 Tab limit: {sub['max_tabs']}")
        lines.extend(_capability_lines(sub))
    else:
        lines.append("❌ No active subscription.")
        lines.append("You need an active plan to use Server Manager.")
    lines.append(f"\n💰 Wallet balance: {balance:,}")
    return "\n".join(lines)


def _status_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🛒 Buy / Renew a plan", callback_data="sub_buy_menu")],
        [InlineKeyboardButton("💰 Top up wallet", callback_data="sub_topup_start")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: the "💳 Subscription" reply-keyboard button."""
    user_id = _uid(update)
    await update.message.reply_text(
        _status_text(user_id), parse_mode="Markdown", reply_markup=_status_keyboard()
    )


async def sub_back_to_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await query.edit_message_text(
        _status_text(user_id), parse_mode="Markdown", reply_markup=_status_keyboard()
    )
    return ConversationHandler.END


# ---- buy / renew ----

def _plan_list_keyboard():
    keyboard = []
    for plan_id, plan in get_all_plans(active_only=True).items():
        label = f"{plan['name']} - {plan['price']:,} / {plan['days']}d"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"sub_plan_{plan_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="sub_back_status")])
    return InlineKeyboardMarkup(keyboard)


async def sub_buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    available = get_all_plans(active_only=True)
    if not available:
        await query.edit_message_text(
            "No plans are available for purchase right now. Please check back later.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="sub_back_status")]]
            ),
        )
        return

    await query.edit_message_text("🛒 Choose a plan:", reply_markup=_plan_list_keyboard())


def _plan_detail_text(plan: dict) -> str:
    lines = [f"📦 *{plan['name']}*"]
    if plan.get("description"):
        lines.append(plan["description"])
    lines.append(f"\n💰 Price: {plan['price']:,}")
    lines.append(f"⏳ Duration: {plan['days']} day(s)")
    lines.append(f"🖥 Max servers: {plan['max_servers']}")
    lines.append(f"📑 Max concurrent tabs: {plan['max_tabs']}")
    lines.extend(_capability_lines(plan))
    return "\n".join(lines)


async def sub_plan_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("❌ This plan is no longer available.", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("💰 Pay with wallet", callback_data=f"sub_paywallet_{plan_id}")],
        [InlineKeyboardButton("💳 Pay with card", callback_data=f"sub_paycard_{plan_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="sub_buy_menu")],
    ]
    await query.edit_message_text(
        _plan_detail_text(plan), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def sub_pay_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("❌ This plan is no longer available.", show_alert=True)
        return

    user_id = query.from_user.id
    balance = get_balance(user_id)
    price = plan["price"]

    if balance < price:
        await query.answer(
            f"❌ Insufficient wallet balance.\nBalance: {balance:,}\nNeeded: {price:,}\nShort by: {price - balance:,}",
            show_alert=True,
        )
        return

    update_balance(user_id, -price)
    add_transaction(user_id, -price, "purchase", f"Subscription: {plan['name']}")
    grant_subscription(user_id, plan)

    await query.answer("✅ Subscription activated!")
    await query.edit_message_text(
        f"✅ Payment successful!\n\n"
        f"📦 Plan: {plan['name']}\n"
        f"💰 Paid: {price:,}\n"
        f"⏳ Expires in: {days_remaining(user_id)} day(s)\n\n"
        f"You can now use Server Manager.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Back", callback_data="sub_back_status")]]
        ),
    )


async def sub_pay_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("❌ This plan is no longer available.", show_alert=True)
        return ConversationHandler.END

    if not bot_settings.is_card_payment_configured():
        await query.answer("❌ Card payment isn't configured yet. Please contact an admin.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["pay_plan_id"] = plan_id
    await query.edit_message_text(
        f"💳 Paying for *{plan['name']}* ({plan['price']:,}).\n\n"
        f"Please send the last 4 digits of the card you'll pay from.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("👇 Tap below to cancel:", reply_markup=_cancel_kb())
    return PAY_CARD_DIGITS


async def sub_pay_card_digits_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = update.message.text.strip()
    if not digits.isdigit() or len(digits) != 4:
        await update.message.reply_text(
            "❌ Please send exactly 4 digits (e.g. 1234), or tap Cancel.", reply_markup=_cancel_kb()
        )
        return PAY_CARD_DIGITS

    plan_id = context.user_data.pop("pay_plan_id", None)
    plan = get_plan(plan_id) if plan_id else None
    if not plan:
        await update.message.reply_text("❌ Something went wrong, please try again.", reply_markup=get_main_menu())
        return ConversationHandler.END

    user = update.effective_user
    request_id = uuid.uuid4().hex[:8]
    data = {
        "type": "subscription",
        "user_id": user.id,
        "plan_id": plan_id,
        "amount": plan["price"],
        "card_digits": digits,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    get_db().create_payment_request(
        request_id, data["type"], data["user_id"], data["amount"], data["card_digits"],
        username=data["username"] or "", first_name=data["first_name"] or "",
        last_name=data["last_name"] or "", plan_id=plan_id,
    )

    await _send_admin_approval_request(context.bot, request_id, data, plan_name=plan["name"])

    await update.message.reply_text(
        f"🧾 Invoice\n\n"
        f"📦 Plan: {plan['name']}\n"
        f"💰 Amount to pay: {plan['price']:,}\n\n"
        f"🏦 Transfer to:\n"
        f"Card: {bot_settings.get_card_number()}\n"
        f"Holder: {bot_settings.get_card_holder()}\n"
        f"Bank: {bot_settings.get_card_bank()}\n\n"
        f"⚠️ Please pay from a card ending in {digits} and send the exact amount.\n"
        f"No receipt photo needed - your subscription activates automatically once an admin confirms.",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ---- wallet top-up ----

async def sub_topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not bot_settings.is_card_payment_configured():
        await query.answer("❌ Card payment isn't configured yet. Please contact an admin.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text("💰 How much would you like to add to your wallet? Send a number.")
    await query.message.reply_text("👇 Tap below to cancel:", reply_markup=_cancel_kb())
    return TOPUP_AMOUNT


async def sub_topup_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return TOPUP_AMOUNT

    context.user_data["topup_amount"] = int(text)
    await update.message.reply_text(
        "Please send the last 4 digits of the card you'll pay from.", reply_markup=_cancel_kb()
    )
    return TOPUP_CARD_DIGITS


async def sub_topup_card_digits_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = update.message.text.strip()
    if not digits.isdigit() or len(digits) != 4:
        await update.message.reply_text(
            "❌ Please send exactly 4 digits (e.g. 1234), or tap Cancel.", reply_markup=_cancel_kb()
        )
        return TOPUP_CARD_DIGITS

    amount = context.user_data.pop("topup_amount", None)
    if not amount:
        await update.message.reply_text("❌ Something went wrong, please try again.", reply_markup=get_main_menu())
        return ConversationHandler.END

    user = update.effective_user
    request_id = uuid.uuid4().hex[:8]
    data = {
        "type": "topup",
        "user_id": user.id,
        "amount": amount,
        "card_digits": digits,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    get_db().create_payment_request(
        request_id, data["type"], data["user_id"], data["amount"], data["card_digits"],
        username=data["username"] or "", first_name=data["first_name"] or "",
        last_name=data["last_name"] or "",
    )

    await _send_admin_approval_request(context.bot, request_id, data)

    await update.message.reply_text(
        f"🧾 Invoice\n\n"
        f"💰 Amount: {amount:,}\n\n"
        f"🏦 Transfer to:\n"
        f"Card: {bot_settings.get_card_number()}\n"
        f"Holder: {bot_settings.get_card_holder()}\n"
        f"Bank: {bot_settings.get_card_bank()}\n\n"
        f"⚠️ Please pay from a card ending in {digits} and send the exact amount.\n"
        f"No receipt photo needed - your wallet will be credited automatically once an admin confirms.",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ---- cancel ----

async def payments_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pay_plan_id", None)
    context.user_data.pop("topup_amount", None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END


# ---- admin DM approval ----

async def _send_admin_approval_request(bot, request_id: str, data: dict, plan_name: str = None):
    if not config.ADMIN_IDS:
        logger.warning("No ADMIN_IDS configured; payment approval request has nowhere to go.")
        return

    who = data.get("first_name") or data.get("username") or str(data["user_id"])
    if data["type"] == "subscription":
        desc = f"📦 Plan: {plan_name}"
    else:
        desc = "💰 Wallet top-up"

    text = (
        f"🧾 Payment awaiting approval\n\n"
        f"👤 {who} (id: {data['user_id']})\n"
        f"{desc}\n"
        f"💰 Amount: {data['amount']:,}\n"
        f"🔢 Last 4 digits: {data['card_digits']}\n\n"
        f"Request id: {request_id}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adminpay_approve_{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adminpay_reject_{request_id}"),
        ]
    ])
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Could not DM admin {admin_id} about payment {request_id}: {e}")


async def admin_approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user

    if admin.id not in config.ADMIN_IDS:
        await query.answer("⛔️ You don't have admin access.", show_alert=True)
        return

    request_id = query.data.replace("adminpay_approve_", "")
    data = get_db().pop_payment_request(request_id)
    if not data:
        await query.answer("❌ This request is no longer valid (already handled).", show_alert=True)
        return

    await query.answer("⏳ Processing...")
    bot = context.bot
    user_id = data["user_id"]

    try:
        if data["type"] == "subscription":
            plan = get_plan(data["plan_id"])
            if not plan:
                await bot.send_message(chat_id=user_id, text="❌ That plan no longer exists. Please contact support.")
            else:
                grant_subscription(user_id, plan)
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ Your payment was approved!\n\n"
                        f"📦 Plan: {plan['name']}\n"
                        f"⏳ Expires in: {days_remaining(user_id)} day(s)\n\n"
                        f"You can now use Server Manager."
                    ),
                )
        else:  # topup
            new_balance = update_balance(user_id, data["amount"])
            add_transaction(user_id, data["amount"], "topup", "Wallet top-up (card, admin approved)")
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Your wallet was topped up!\n\n💰 Amount: {data['amount']:,}\n💳 New balance: {new_balance:,}",
            )

        try:
            await query.edit_message_text(query.message.text + f"\n\n✅ Approved by {admin.first_name or admin.id}")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error approving payment {request_id}: {e}")
        await query.answer("❌ An error occurred; check the bot logs.", show_alert=True)


async def admin_reject_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user

    if admin.id not in config.ADMIN_IDS:
        await query.answer("⛔️ You don't have admin access.", show_alert=True)
        return

    request_id = query.data.replace("adminpay_reject_", "")
    data = get_db().pop_payment_request(request_id)
    if not data:
        await query.answer("❌ This request is no longer valid (already handled).", show_alert=True)
        return

    await query.answer("Rejected")
    try:
        await context.bot.send_message(
            chat_id=data["user_id"],
            text="❌ Your payment could not be confirmed. This is usually due to a mismatched amount or card. Please contact support.",
        )
    except Exception as e:
        logger.error(f"Error notifying user of rejection: {e}")

    try:
        await query.edit_message_text(query.message.text + f"\n\n❌ Rejected by {admin.first_name or admin.id}")
    except Exception:
        pass
