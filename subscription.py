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
import logger_bot

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
    """Marks `plan_id` as the auto-granted default (unmarks any previous one)."""
    return get_db().set_default_plan(plan_id)


def add_plan(name: str, price: int, days: int, max_servers: int, max_tabs: int,
             description: str = "", is_default: bool = False, sftp_enabled: bool = True,
             session_timeout_minutes: int = None, max_automations: int = None,
             advanced_tools_enabled: bool = False) -> str:
    return get_db().add_plan(
        name, price, days, max_servers, max_tabs, description, is_default=is_default,
        sftp_enabled=sftp_enabled, session_timeout_minutes=session_timeout_minutes,
        max_automations=max_automations, advanced_tools_enabled=advanced_tools_enabled,
    )


def update_plan(plan_id: str, **kwargs) -> bool:
    return get_db().update_plan(plan_id, **kwargs)


def ensure_default_plan(user_id) -> bool:
    """Grants the default plan on /start if the user has no active subscription."""
    return get_db().grant_default_plan_if_needed(user_id)


async def log_new_user_join(bot, user, referred_by=None):
    """Call this from /start once the user record is created, to log the new membership."""
    await logger_bot.log_user_join(
        bot, user.id, username=user.username, first_name=user.first_name, last_name=user.last_name,
    )


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
    """Signed amount; returns new balance. Does not log a transaction (call add_transaction separately)."""
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
    """(max_servers, max_tabs) for the active plan, or (0, 0) if none."""
    return get_db().get_limits(user_id)


def get_capabilities(user_id) -> dict:
    """Plan-enforced capability dict (None = unlimited); zeroed if no active subscription."""
    return get_db().get_capabilities(user_id)


def grant_subscription(user_id, plan: dict, stack_remaining: bool = True) -> dict:
    """Activates `plan`. stack_remaining=True (default) adds any remaining
    time on the current subscription on top - renewal behaviour. Pass False
    when the unused value of the current plan was already credited toward
    this purchase (see _purchase_pricing) - the new plan's term then simply
    starts now instead of also stacking the old leftover days on top."""
    return get_db().grant_subscription(user_id, plan, stack_remaining=stack_remaining)


# ============================================================
# 4. PAYMENTS - user-facing buy / top-up flow + admin DM approval
# ============================================================
SUBSCRIPTION_BUTTON_TEXT = "◈ Subscription"
CANCEL_BUTTON_TEXT = "✕ Cancel"

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
    """Shared SFTP/timeout/automation/advanced-tools formatting; `d` is a
    subscription row or plan dict."""
    lines = []
    lines.append(
        "▤ SFTP (works alongside terminal): "
        + ("✓ included" if d.get("sftp_enabled", True) else "⚿ not included")
    )
    timeout = d.get("session_timeout_minutes")
    lines.append(f"⧗ SSH session length: {timeout} min" if timeout else "⧗ SSH session length: unlimited")
    max_auto = d.get("max_automations")
    lines.append(f"⚙ Automation jobs: up to {max_auto}" if max_auto is not None else "⚙ Automation jobs: unlimited")
    lines.append(
        "⚡ Advanced Tools (processes/services/cron/logs): "
        + ("✓ included" if d.get("advanced_tools_enabled") else "⚿ not included")
    )
    return lines


def _purchase_pricing(user_id, plan: dict) -> dict:
    """What a user would actually pay for `plan` right now.

    Buying the exact plan the user already has active is blocked outright
    (that's a renewal, not a purchase - offer it again once the plan has
    expired) - flagged via "same_plan".

    Switching to a *different* plan while one is still active credits the
    pro-rated unused value of the current plan (price/day * days remaining,
    capped at the new plan's price) toward the new plan's price, so
    upgrading isn't paying for the same days twice. When a credit applies,
    "stack_remaining" is False - the new plan's full term starts now rather
    than also stacking the old leftover days on top of it.

    Returns {"price", "credit", "stack_remaining", "same_plan"}."""
    price = int(plan["price"])
    result = {"price": price, "credit": 0, "stack_remaining": True, "same_plan": False}
    if not is_active(user_id):
        return result

    sub = get_subscription(user_id)
    if sub.get("plan_id") == plan["id"]:
        result["same_plan"] = True
        return result

    current_plan = get_plan(sub["plan_id"]) if sub.get("plan_id") else None
    remaining = days_remaining(user_id)
    if current_plan and remaining > 0:
        # Switching plans always starts the new term fresh - never stack the
        # old plan's leftover days (including a free/default plan's, which
        # has no price to credit but should still not carry over).
        result["stack_remaining"] = False
        if current_plan.get("days") and current_plan.get("price"):
            per_day = current_plan["price"] / current_plan["days"]
            credit = min(int(per_day * remaining), price)
            if credit > 0:
                result["credit"] = credit
                result["price"] = price - credit
    return result


def _status_text(user_id) -> str:
    balance = get_balance(user_id)
    lines = ["◈ *Subscription*\n"]
    if is_active(user_id):
        sub = get_subscription(user_id)
        lines.append(f"✓ Active plan: *{sub['plan_name']}*")
        days = days_remaining(user_id)
        lines.append(f"⧗ Expires in: {days} day(s)")
        lines.append(f"▣ Server limit: {sub['max_servers']}")
        lines.append(f"▥ Window limit: {sub['max_tabs']}")
        lines.extend(_capability_lines(sub))
    else:
        lines.append("✕ No active subscription.")
        lines.append("You need an active plan to use Server Manager.")
    lines.append(f"\n◈ Wallet balance: {balance:,}")
    return "\n".join(lines)


def _status_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("▨ Buy / Renew a plan", callback_data="sub_buy_menu")],
        [InlineKeyboardButton("◈ Top up wallet", callback_data="sub_topup_start")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: the "◈ Subscription" reply-keyboard button."""
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

def _purchasable_plans() -> dict:
    """Active plans minus the default (free) plan, which is auto-granted on /start
    rather than bought. The default plan is determined dynamically (cheapest
    enabled plan - see get_default_plan()), not via the unused is_default column."""
    default_plan = get_default_plan()
    default_id = default_plan.get("id") if default_plan else None
    return {
        plan_id: plan
        for plan_id, plan in get_all_plans(active_only=True).items()
        if plan_id != default_id
    }


def _plan_list_keyboard(user_id):
    current_plan_id = None
    if is_active(user_id):
        current_plan_id = get_subscription(user_id).get("plan_id")

    keyboard = []
    for plan_id, plan in _purchasable_plans().items():
        if plan_id == current_plan_id:
            label = f"✓ {plan['name']}"
        else:
            pricing = _purchase_pricing(user_id, plan)
            if pricing["credit"] > 0:
                label = f"{plan['name']} - {pricing['price']:,} / {plan['days']}d"
            else:
                label = f"{plan['name']} - {plan['price']:,} / {plan['days']}d"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"sub_plan_{plan_id}")])
    keyboard.append([InlineKeyboardButton("← Back", callback_data="sub_back_status")])
    return InlineKeyboardMarkup(keyboard)


async def sub_buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    available = _purchasable_plans()
    if not available:
        await query.edit_message_text(
            "No plans are available for purchase right now. Please check back later.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="sub_back_status")]]
            ),
        )
        return

    await query.edit_message_text("▨ Choose a plan:", reply_markup=_plan_list_keyboard(query.from_user.id))


def _plan_detail_text(plan: dict, pricing: dict = None) -> str:
    lines = [f"▨ *{plan['name']}*"]
    if plan.get("description"):
        lines.append(plan["description"])
    if pricing and pricing["credit"] > 0:
        lines.append(f"\n◈ Price: {plan['price']:,} → *{pricing['price']:,}*")
        lines.append(f"◈ Credit from your current plan's remaining time: {pricing['credit']:,}")
    else:
        lines.append(f"\n◈ Price: {plan['price']:,}")
    lines.append(f"⧗ Duration: {plan['days']} day(s)")
    lines.append(f"▣ Max servers: {plan['max_servers']}")
    lines.append(f"▥ Max concurrent windows: {plan['max_tabs']}")
    lines.extend(_capability_lines(plan))
    return "\n".join(lines)


async def sub_plan_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("✕ This plan is no longer available.", show_alert=True)
        return

    user_id = query.from_user.id
    pricing = _purchase_pricing(user_id, plan)
    if pricing["same_plan"]:
        await query.answer(
            f"⚠ You already have {plan['name']} active - pick a different plan below to switch/upgrade.",
            show_alert=True,
        )
        return

    keyboard = [
        [InlineKeyboardButton("◈ Pay with wallet", callback_data=f"sub_paywallet_{plan_id}")],
        [InlineKeyboardButton("◈ Pay with card", callback_data=f"sub_paycard_{plan_id}")],
        [InlineKeyboardButton("← Back", callback_data="sub_buy_menu")],
    ]
    await query.answer()
    await query.edit_message_text(
        _plan_detail_text(plan, pricing), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def sub_pay_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("✕ This plan is no longer available.", show_alert=True)
        return

    user_id = query.from_user.id
    pricing = _purchase_pricing(user_id, plan)
    if pricing["same_plan"]:
        await query.answer(
            f"⚠ You already have {plan['name']} active - pick a different plan to switch/upgrade.",
            show_alert=True,
        )
        return

    balance = get_balance(user_id)
    price = pricing["price"]

    if balance < price:
        await query.answer(
            f"✕ Insufficient wallet balance.\nBalance: {balance:,}\nNeeded: {price:,}\nShort by: {price - balance:,}",
            show_alert=True,
        )
        return

    update_balance(user_id, -price)
    desc = f"Subscription: {plan['name']}"
    if pricing["credit"] > 0:
        desc += f" (credit {pricing['credit']:,} applied)"
    add_transaction(user_id, -price, "purchase", desc)
    grant_subscription(user_id, plan, stack_remaining=pricing["stack_remaining"])

    await logger_bot.log_subscription_activated(
        context.bot, user_id, plan["name"], plan["days"], price, credit=pricing["credit"],
        payment_method="Wallet", username=query.from_user.username,
        first_name=query.from_user.first_name, last_name=query.from_user.last_name,
    )

    await query.answer("✓ Subscription activated!")
    await query.edit_message_text(
        f"✓ Payment successful!\n\n"
        f"▨ Plan: {plan['name']}\n"
        f"◈ Paid: {price:,}\n"
        f"⧗ Expires in: {days_remaining(user_id)} day(s)\n\n"
        f"You can now use Server Manager.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Back", callback_data="sub_back_status")]]
        ),
    )


async def sub_pay_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    plan_id = query.data.rsplit("_", 1)[-1]
    plan = get_plan(plan_id)
    if not plan or not plan.get("enabled", True):
        await query.answer("✕ This plan is no longer available.", show_alert=True)
        return ConversationHandler.END

    if not bot_settings.is_card_payment_configured():
        await query.answer("✕ Card payment isn't configured yet. Please contact an admin.", show_alert=True)
        return ConversationHandler.END

    user_id = query.from_user.id
    pricing = _purchase_pricing(user_id, plan)
    if pricing["same_plan"]:
        await query.answer(
            f"⚠ You already have {plan['name']} active - pick a different plan to switch/upgrade.",
            show_alert=True,
        )
        return ConversationHandler.END

    await query.answer()
    context.user_data["pay_plan_id"] = plan_id
    context.user_data["pay_amount"] = pricing["price"]
    price_line = f"({pricing['price']:,}, credit {pricing['credit']:,} applied)" if pricing["credit"] > 0 else f"({pricing['price']:,})"
    await query.edit_message_text(
        f"◈ Paying for *{plan['name']}* {price_line}.\n\n"
        f"Please send the last 4 digits of the card you'll pay from.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("↓ Tap below to cancel:", reply_markup=_cancel_kb())
    return PAY_CARD_DIGITS


async def sub_pay_card_digits_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = update.message.text.strip()
    if not digits.isdigit() or len(digits) != 4:
        await update.message.reply_text(
            "✕ Please send exactly 4 digits (e.g. 1234), or tap Cancel.", reply_markup=_cancel_kb()
        )
        return PAY_CARD_DIGITS

    plan_id = context.user_data.pop("pay_plan_id", None)
    amount = context.user_data.pop("pay_amount", None)
    plan = get_plan(plan_id) if plan_id else None
    if not plan or amount is None:
        await update.message.reply_text("✕ Something went wrong, please try again.", reply_markup=get_main_menu())
        return ConversationHandler.END

    user = update.effective_user
    request_id = uuid.uuid4().hex[:8]
    data = {
        "type": "subscription",
        "user_id": user.id,
        "plan_id": plan_id,
        "amount": amount,
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

    await logger_bot.log_invoice_issued(
        context.bot, user.id, plan["name"], amount, digits,
        username=user.username, first_name=user.first_name, last_name=user.last_name,
    )

    await update.message.reply_text(
        f"▤ Invoice\n\n"
        f"▨ Plan: {plan['name']}\n"
        f"◈ Amount to pay: {amount:,}\n\n"
        f"◈ Transfer to:\n"
        f"Card: {bot_settings.get_card_number()}\n"
        f"Holder: {bot_settings.get_card_holder()}\n"
        f"Bank: {bot_settings.get_card_bank()}\n\n"
        f"⚠ Please pay from a card ending in {digits} and send the exact amount.\n"
        f"No receipt photo needed - your subscription activates automatically once an admin confirms.",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ---- wallet top-up ----

async def sub_topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not bot_settings.is_card_payment_configured():
        await query.answer("✕ Card payment isn't configured yet. Please contact an admin.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text("◈ How much would you like to add to your wallet? Send a number.")
    await query.message.reply_text("↓ Tap below to cancel:", reply_markup=_cancel_kb())
    return TOPUP_AMOUNT


async def sub_topup_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
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
            "✕ Please send exactly 4 digits (e.g. 1234), or tap Cancel.", reply_markup=_cancel_kb()
        )
        return TOPUP_CARD_DIGITS

    amount = context.user_data.pop("topup_amount", None)
    if not amount:
        await update.message.reply_text("✕ Something went wrong, please try again.", reply_markup=get_main_menu())
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

    await logger_bot.log_invoice_issued(
        context.bot, user.id, "Wallet Top-up", amount, digits,
        username=user.username, first_name=user.first_name, last_name=user.last_name,
    )

    await update.message.reply_text(
        f"▤ Invoice\n\n"
        f"◈ Amount: {amount:,}\n\n"
        f"◈ Transfer to:\n"
        f"Card: {bot_settings.get_card_number()}\n"
        f"Holder: {bot_settings.get_card_holder()}\n"
        f"Bank: {bot_settings.get_card_bank()}\n\n"
        f"⚠ Please pay from a card ending in {digits} and send the exact amount.\n"
        f"No receipt photo needed - your wallet will be credited automatically once an admin confirms.",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


# ---- cancel ----

async def payments_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pay_plan_id", None)
    context.user_data.pop("topup_amount", None)
    await update.message.reply_text("✕ Cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END


# ---- admin DM approval ----

async def _send_admin_approval_request(bot, request_id: str, data: dict, plan_name: str = None):
    if not config.ADMIN_IDS:
        logger.warning("No ADMIN_IDS configured; payment approval request has nowhere to go.")
        return

    who = data.get("first_name") or data.get("username") or str(data["user_id"])
    if data["type"] == "subscription":
        desc = f"▨ Plan: {plan_name}"
    else:
        desc = "◈ Wallet top-up"

    text = (
        f"▤ Payment awaiting approval\n\n"
        f"• {who} (id: {data['user_id']})\n"
        f"{desc}\n"
        f"◈ Amount: {data['amount']:,}\n"
        f"# Last 4 digits: {data['card_digits']}\n\n"
        f"Request id: {request_id}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Approve", callback_data=f"adminpay_approve_{request_id}"),
            InlineKeyboardButton("✕ Reject", callback_data=f"adminpay_reject_{request_id}"),
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
        await query.answer("⊘ You don't have admin access.", show_alert=True)
        return

    request_id = query.data.replace("adminpay_approve_", "")
    data = get_db().pop_payment_request(request_id)
    if not data:
        await query.answer("✕ This request is no longer valid (already handled).", show_alert=True)
        return

    await query.answer("⧗ Processing...")
    bot = context.bot
    user_id = data["user_id"]

    try:
        if data["type"] == "subscription":
            plan = get_plan(data["plan_id"])
            if not plan:
                await bot.send_message(chat_id=user_id, text="✕ That plan no longer exists. Please contact support.")
            else:
                pricing = _purchase_pricing(user_id, plan)
                stack_remaining = True if pricing["same_plan"] else pricing["stack_remaining"]
                grant_subscription(user_id, plan, stack_remaining=stack_remaining)
                await logger_bot.log_subscription_activated(
                    bot, user_id, plan["name"], plan["days"], data["amount"], credit=pricing.get("credit", 0),
                    payment_method="Card (admin approved)", username=data.get("username"),
                    first_name=data.get("first_name"), last_name=data.get("last_name"),
                )
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✓ Your payment was approved!\n\n"
                        f"▨ Plan: {plan['name']}\n"
                        f"⧗ Expires in: {days_remaining(user_id)} day(s)\n\n"
                        f"You can now use Server Manager."
                    ),
                )
        else:  # topup
            new_balance = update_balance(user_id, data["amount"])
            add_transaction(user_id, data["amount"], "topup", "Wallet top-up (card, admin approved)")
            await logger_bot.log_balance_change(
                bot, user_id, data["amount"], new_balance, "Wallet top-up (card, admin approved)",
                username=data.get("username"), first_name=data.get("first_name"), last_name=data.get("last_name"),
            )
            await bot.send_message(
                chat_id=user_id,
                text=f"✓ Your wallet was topped up!\n\n◈ Amount: {data['amount']:,}\n◈ New balance: {new_balance:,}",
            )

        try:
            await query.edit_message_text(query.message.text + f"\n\n✓ Approved by {admin.first_name or admin.id}")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error approving payment {request_id}: {e}")
        await query.answer("✕ An error occurred; check the bot logs.", show_alert=True)


async def admin_reject_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user

    if admin.id not in config.ADMIN_IDS:
        await query.answer("⊘ You don't have admin access.", show_alert=True)
        return

    request_id = query.data.replace("adminpay_reject_", "")
    data = get_db().pop_payment_request(request_id)
    if not data:
        await query.answer("✕ This request is no longer valid (already handled).", show_alert=True)
        return

    await query.answer("Rejected")
    try:
        await context.bot.send_message(
            chat_id=data["user_id"],
            text="✕ Your payment could not be confirmed. This is usually due to a mismatched amount or card. Please contact support.",
        )
    except Exception as e:
        logger.error(f"Error notifying user of rejection: {e}")

    await logger_bot.log_admin_action(
        context.bot, admin.id, "Rejected payment", target_user_id=data["user_id"],
        details=f"type={data['type']}", amount=data.get("amount"),
        username=admin.username, first_name=admin.first_name, last_name=admin.last_name,
        target_username=data.get("username"), target_first_name=data.get("first_name"),
        target_last_name=data.get("last_name"),
    )

    try:
        await query.edit_message_text(query.message.text + f"\n\n✕ Rejected by {admin.first_name or admin.id}")
    except Exception:
        pass
