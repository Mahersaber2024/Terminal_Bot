import logging
import os
import sys

from telegram import Update, BotCommand, BotCommandScopeChat, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from admin import admin
import bot_settings
import config
import crypto_utils
import subscription
import logger_bot
import sponsor_gate
from db.database import get_db
from ServerManager import handlers as svm
from ServerManager import health as svm_health
from ServerManager import automation as svm_auto
import proxy_utils

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


HELP_TEXT = (
    "▣ *Terminal Bot*\n\n"
    "Register your own server(s) and run SSH commands on them right from this chat.\n\n"
    "• An active subscription is required to use Server Manager - tap "
    "*▭ Subscription* to buy or renew a plan.\n"
    "• Tap *▣ Server Manager* to add a server or open a live terminal session.\n"
    "• Inside a session, just type commands like you would in a normal terminal - "
    "output streams back live.\n"
    "• Use the *✕ Cancel* / *▪ End Session* button any time to stop.\n\n"
    "Your server credentials are stored encrypted and only you can see or use them."
)


def get_main_menu(user_id: int = None) -> ReplyKeyboardMarkup:
    keyboard = [[svm.MENU_BUTTON_TEXT, subscription.SUBSCRIPTION_BUTTON_TEXT]]
    if user_id is not None and admin.is_admin(user_id):
        keyboard.append([admin.ADMIN_MENU_BUTTON_TEXT, admin.OWNERSRV_MENU_BUTTON_TEXT])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update: Update, context):
    user = update.effective_user
    if user is not None:
        try:
            get_db().get_or_create_user(
                user.id,
                username=user.username or "",
                first_name=user.first_name or "",
                last_name=user.last_name or "",
            )
        except Exception as e:
            logger.error(f"Could not register user {user.id} in the database: {e}")
        try:
            subscription.ensure_default_plan(user.id)
        except Exception as e:
            logger.error(f"Could not grant default plan to user {user.id}: {e}")

    text = (
        "✦ Welcome to *Terminal Bot*!\n\n"
        "Add a server and run SSH commands on it, right here in the chat.\n"
        "Tap *▣ Server Manager* below to get started, or /help for details."
    )
    chat = update.effective_chat
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode="Markdown",
        reply_markup=get_main_menu(update.effective_user.id),
    )


async def help_command(update: Update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=get_main_menu(update.effective_user.id))


async def set_bot_commands(application):
    group_id = bot_settings.get_log_group_id()
    if group_id:
        logger_bot.init_logger(group_id)
        try:
            await logger_bot.create_all_topics(application.bot)
        except Exception as e:
            logger.warning(f"Could not verify/create log topics on startup: {e}")

    # Default menu for everyone.
    await application.bot.set_my_commands([
        BotCommand("start", "Start / main menu"),
        BotCommand("help", "How this bot works"),
    ])
    # Extra "/admin" entry, shown only in each admin's own command menu.
    for admin_id in config.ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Start / main menu"),
                    BotCommand("help", "How this bot works"),
                    BotCommand("admin", "Admin panel"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            logger.warning(f"Could not set admin commands for {admin_id}: {e}")


async def error_handler(update: object, context):
    logger.error("Unhandled exception while processing an update", exc_info=context.error)

    # Best-effort mirror of unexpected exceptions to the log group's "System Errors"
    # topic - never let a logging failure mask the original error.
    try:
        error_text = f"{type(context.error).__name__}: {context.error}" if context.error else "Unknown error"
        update_context = None
        if isinstance(update, Update):
            who = update.effective_user
            chat = update.effective_chat
            update_context = f"user={who.id if who else '-'} chat={chat.id if chat else '-'}"
        await logger_bot.log_system_error(context.bot, error_text, context=update_context)
    except Exception as e:
        logger.warning(f"Could not send system error to log group: {e}")


PROXY_WATCHDOG_INTERVAL = 120  # seconds
PROXY_WATCHDOG_FAIL_THRESHOLD = 3
_proxy_watchdog_failures = 0


async def proxy_watchdog_tick(context):
    global _proxy_watchdog_failures
    try:
        await context.bot.get_me()
        _proxy_watchdog_failures = 0
    except Exception as e:
        _proxy_watchdog_failures += 1
        logger.warning(
            f"▸ Proxy connectivity check failed "
            f"({_proxy_watchdog_failures}/{PROXY_WATCHDOG_FAIL_THRESHOLD}): {e}"
        )
        if _proxy_watchdog_failures >= PROXY_WATCHDOG_FAIL_THRESHOLD:
            logger.error(
                "▸ Proxy looks dead after repeated checks - exiting so the process "
                "manager restarts the bot and re-picks a working proxy."
            )
            os._exit(1)


def main():
    print("\n" + "=" * 60)
    print("→ Terminal Bot starting...")
    print("=" * 60)

    if not config.BOT_TOKEN:
        print("\n✕ BOT_TOKEN is missing. Set it in your environment or .env file.")
        return

    if not crypto_utils.IS_CONFIGURED:
        print(
            "\n✕ CRYPTO_SECRET is missing. Server Manager stores SSH passwords/private keys "
            "encrypted with it, and without a fixed secret every credential saved would become "
            "unreadable on the next restart. Set it in your .env, e.g.:\n"
            '   python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then run the bot again."
        )
        return

    try:
        get_db()
        print("✓ Connected to PostgreSQL and verified tables (users, plans, subscriptions, ...).")
    except Exception as e:
        print(f"\n✕ Could not connect to PostgreSQL: {e}")
        print("   Check DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD in your .env, then run:")
        print("   python3 setup_db.py")
        return

    channels = bot_settings.get_sponsor_channels()
    if channels and bot_settings.is_membership_required():
        print(f"⚿ Sponsor gate enabled - {len(channels)} required channel(s):")
        for ch in channels:
            print(f"   • {ch['title']} ({ch['id']})")
        print("   Make sure the bot is an ADMIN in each of them, or membership checks will fail.")
        print("   Manage this any time from /admin without restarting the bot.")
    else:
        print("ℹ Sponsor gate is disabled - the bot is open to everyone. Enable it from /admin.")

    if not config.ADMIN_IDS:
        print("⚠ ADMIN_IDS is empty - nobody will be able to open /admin. Set it in your .env.")

    # Wire ServerManager's / Admin panel's / payments' "back to main menu" buttons to our keyboard.
    svm.set_get_main_menu(get_main_menu)
    admin.set_get_main_menu(get_main_menu)
    subscription.set_get_main_menu(get_main_menu)
    svm_auto.set_get_main_menu(get_main_menu)

    try:
        proxy_url = proxy_utils.resolve_proxy()
        builder = Application.builder().token(config.BOT_TOKEN).concurrent_updates(True)
        if proxy_url:
            builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
        application = builder.build()
        if proxy_url:
            # Don't print credentials to logs/console.
            safe = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
            print(f"▸ Using proxy for Telegram connectivity: {safe}")
        print("✓ Bot initialized successfully!")
    except Exception as e:
        print(f"\n✕ Failed to initialize bot: {e}")
        return

    application.post_init = set_bot_commands
    application.add_error_handler(error_handler)

    application.add_handler(MessageHandler(filters.ALL, admin.ban_gate), group=-2)
    application.add_handler(CallbackQueryHandler(admin.ban_gate), group=-2)
    application.add_handler(MessageHandler(filters.ALL, sponsor_gate.gate), group=-1)
    application.add_handler(CallbackQueryHandler(sponsor_gate.gate), group=-1)
    application.add_handler(
        CallbackQueryHandler(sponsor_gate.sponsor_check_callback, pattern=f"^{sponsor_gate.SPONSOR_CHECK_CALLBACK}$")
    )

    # ====================== Core commands ======================
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # ====================== Admin panel ======================
    application.add_handler(CommandHandler("admin", admin.admin_panel))
    application.add_handler(MessageHandler(filters.Regex(f"^{admin.ADMIN_MENU_BUTTON_TEXT}$"), admin.admin_panel))
    application.add_handler(
        MessageHandler(filters.Regex(f"^{admin.OWNERSRV_MENU_BUTTON_TEXT}$"), admin.admin_ownersrv_button)
    )
    application.add_handler(CallbackQueryHandler(admin.admin_back_to_main, pattern="^admin_back_to_main$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_channel_settings_menu, pattern="^admin_channel_settings$")
    )
    application.add_handler(CallbackQueryHandler(admin.admin_channel_toggle, pattern="^admin_channel_toggle$"))
    application.add_handler(CallbackQueryHandler(admin.admin_channel_remove_menu, pattern="^admin_channel_remove$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_channel_remove_confirm, pattern=r"^admin_channel_remove_\d+$")
    )

    # Matches the "✕ Cancel" reply-keyboard button shown while the "Add
    # Channel" form is waiting for text input.
    admin_cancel_button = MessageHandler(filters.Regex(f"^{admin.CANCEL_BUTTON_TEXT}$"), admin.admin_cancel)
    admin_ownersrv_files_cancel_button = MessageHandler(
        filters.Regex(f"^{admin.CANCEL_BUTTON_TEXT}$"), admin.admin_ownersrv_files_cancel
    )

    admin_channel_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_channel_add_start, pattern="^admin_channel_add$")],
        states={
            admin.ADMIN_CHANNEL_ADD_ID: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_channel_add_id_input),
            ],
            admin.ADMIN_CHANNEL_ADD_TITLE: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_channel_add_title_input),
            ],
            admin.ADMIN_CHANNEL_ADD_LINK: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_channel_add_link_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_channel_add_conv)

    # ====================== Admin: Log Group (button-driven, replaces /setloggroup) ======================
    application.add_handler(CallbackQueryHandler(admin.admin_loggroup_menu, pattern="^admin_loggroup_menu$"))
    admin_loggroup_set_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_loggroup_set_start, pattern="^admin_loggroup_set$")],
        states={
            admin.ADMIN_LOGGROUP_SET: [
                admin_cancel_button,
                MessageHandler(filters.ALL & ~filters.COMMAND, admin.admin_loggroup_set_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_loggroup_set_conv)

    admin_botname_set_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_botname_set_start, pattern="^admin_botname_set$")],
        states={
            admin.ADMIN_BOTNAME_SET: [
                admin_cancel_button,
                MessageHandler(filters.ALL & ~filters.COMMAND, admin.admin_botname_set_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_botname_set_conv)

    # ====================== Admin: Manage Users ======================
    application.add_handler(CallbackQueryHandler(admin.admin_users_menu, pattern="^admin_users_menu$"))
    application.add_handler(CallbackQueryHandler(admin.admin_users_page, pattern=r"^admin_users_page_\d+$"))
    application.add_handler(CallbackQueryHandler(admin.admin_user_view, pattern=r"^admin_user_view_-?\d+$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_user_delete_confirm, pattern=fr"^{admin.ADMIN_USER_DELETE_CONFIRM_CB_PREFIX}-?\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_user_delete_execute, pattern=fr"^{admin.ADMIN_USER_DELETE_EXECUTE_CB_PREFIX}-?\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_user_ban_toggle, pattern=r"^admin_user_(un)?ban_-?\d+$")
    )

    admin_user_search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_user_search_start, pattern="^admin_user_search_start$")],
        states={
            admin.ADMIN_USER_SEARCH: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_user_search_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_user_search_conv)

    admin_user_balance_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_user_balance_start, pattern=r"^admin_user_balance_-?\d+$")],
        states={
            admin.ADMIN_USER_BALANCE_ADJUST: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_user_balance_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_user_balance_conv)

    # ====================== Admin: Manage Plans ======================
    application.add_handler(CallbackQueryHandler(admin.admin_plans_menu, pattern="^admin_plans_menu$"))
    application.add_handler(CallbackQueryHandler(admin.admin_plan_toggle, pattern=r"^admin_plan_toggle_"))
    application.add_handler(CallbackQueryHandler(admin.admin_plan_delete, pattern=r"^admin_plan_delete_"))
    application.add_handler(CallbackQueryHandler(admin.admin_plan_edit_menu, pattern=r"^admin_plan_edit_"))

    admin_plan_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_plan_edit_field_start, pattern=r"^admin_plan_ef_")],
        states={
            admin.ADMIN_PLAN_EDIT_VALUE: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_edit_value_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_plan_edit_conv)

    admin_plan_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_plan_add_start, pattern="^admin_plan_add$")],
        states={
            admin.ADMIN_PLAN_ADD_NAME: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_name_input),
            ],
            admin.ADMIN_PLAN_ADD_PRICE: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_price_input),
            ],
            admin.ADMIN_PLAN_ADD_DAYS: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_days_input),
            ],
            admin.ADMIN_PLAN_ADD_MAXSERVERS: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_maxservers_input),
            ],
            admin.ADMIN_PLAN_ADD_MAXTABS: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_maxtabs_input),
            ],
            admin.ADMIN_PLAN_ADD_SFTP: [
                CallbackQueryHandler(admin.admin_plan_add_sftp_choice, pattern="^admin_plan_add_sftp_(yes|no)$"),
            ],
            admin.ADMIN_PLAN_ADD_TIMEOUT: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_timeout_input),
            ],
            admin.ADMIN_PLAN_ADD_MAXAUTO: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_maxauto_input),
            ],
            admin.ADMIN_PLAN_ADD_ADVTOOLS: [
                CallbackQueryHandler(admin.admin_plan_add_advtools_choice, pattern="^admin_plan_add_advtools_(yes|no)$"),
            ],
            admin.ADMIN_PLAN_ADD_DESC: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_plan_add_desc_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_plan_add_conv)

    # ====================== Admin: Payment Settings ======================
    application.add_handler(CallbackQueryHandler(admin.admin_payment_settings_menu, pattern="^admin_payment_settings$"))

    admin_card_set_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_card_set_start, pattern="^admin_card_set$")],
        states={
            admin.ADMIN_CARD_NUMBER: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_card_number_input),
            ],
            admin.ADMIN_CARD_HOLDER: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_card_holder_input),
            ],
            admin.ADMIN_CARD_BANK: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_card_bank_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_card_set_conv)

    # ====================== Admin: Monitoring Settings ======================
    application.add_handler(
        CallbackQueryHandler(admin.admin_monitoring_settings_menu, pattern="^admin_monitoring_settings$")
    )

    admin_mon_interval_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_interval_start, pattern="^admin_mon_interval$")],
        states={
            admin.ADMIN_MON_INTERVAL: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_interval_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_interval_conv)

    admin_mon_timeout_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_timeout_start, pattern="^admin_mon_timeout$")],
        states={
            admin.ADMIN_MON_TIMEOUT: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_timeout_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_timeout_conv)

    admin_mon_diskpct_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_diskpct_start, pattern="^admin_mon_diskpct$")],
        states={
            admin.ADMIN_MON_DISKPCT: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_diskpct_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_diskpct_conv)

    admin_mon_hysteresis_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_hysteresis_start, pattern="^admin_mon_hysteresis$")],
        states={
            admin.ADMIN_MON_HYSTERESIS: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_hysteresis_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_hysteresis_conv)

    admin_mon_cpupct_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_cpupct_start, pattern="^admin_mon_cpupct$")],
        states={
            admin.ADMIN_MON_CPUPCT: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_cpupct_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_cpupct_conv)

    admin_mon_rampct_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_mon_rampct_start, pattern="^admin_mon_rampct$")],
        states={
            admin.ADMIN_MON_RAMPCT: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_mon_rampct_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_mon_rampct_conv)

    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_menu, pattern="^admin_ownersrv_menu$"))
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_toggle, pattern="^admin_ownersrv_toggle$"))

    admin_ownersrv_tz_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_ownersrv_tz_start, pattern="^admin_ownersrv_tz$")],
        states={
            admin.ADMIN_OWNERSRV_TZ: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_tz_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_ownersrv_tz_conv)
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_top_cpu, pattern="^admin_ownersrv_top_cpu$"))
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_top_ram, pattern="^admin_ownersrv_top_ram$"))
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_threads, pattern="^admin_ownersrv_threads$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_restart_confirm_prompt, pattern="^admin_ownersrv_restart$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_restart_execute, pattern="^admin_ownersrv_restartok$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_cleanup_execute, pattern="^admin_ownersrv_cleanup$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_reboot_confirm_prompt, pattern="^admin_ownersrv_reboot$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_reboot_execute, pattern="^admin_ownersrv_rebootok$")
    )
    application.add_handler(CallbackQueryHandler(admin.admin_noop, pattern="^admin_noop$"))

    # ---- Owner Server: process manager (kill) ----
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_kill_prompt, pattern=f"^{admin.OWNERSRV_KILL_PROMPT_CB_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_kill_term, pattern=f"^{admin.OWNERSRV_KILL_TERM_CB_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_kill_force, pattern=f"^{admin.OWNERSRV_KILL_FORCE_CB_PREFIX}\\d+$")
    )

    # ---- Owner Server: systemd services ----
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_svc_menu, pattern=f"^{admin.OWNERSRV_SVC_MENU_CB}$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_svc_view, pattern=f"^{admin.OWNERSRV_SVC_VIEW_CB_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.admin_ownersrv_svc_action,
            pattern=f"^{admin.OWNERSRV_SVC_ACTION_CB_PREFIX}\\d+_(start|stop|restart|enable|disable)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.admin_ownersrv_svc_filter,
            pattern=f"^{admin.OWNERSRV_SVC_FILTER_CB_PREFIX}(all|custom|system)$",
        )
    )
    # ---- Owner Server: systemd services - live log (journalctl -f) ----
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_svc_log_start, pattern=f"^{admin.OWNERSRV_SVC_LOG_CB_PREFIX}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_ownersrv_svc_log_stop, pattern=f"^{admin.OWNERSRV_SVC_LOGSTOP_CB}$")
    )
    # ---- Owner Server: systemd services - remove (stop + disable + delete unit file) ----
    application.add_handler(
        CallbackQueryHandler(
            admin.admin_ownersrv_svc_remove_prompt, pattern=f"^{admin.OWNERSRV_SVC_REMOVE_PROMPT_CB_PREFIX}\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.admin_ownersrv_svc_remove_confirm, pattern=f"^{admin.OWNERSRV_SVC_REMOVE_CONFIRM_CB_PREFIX}\\d+$"
        )
    )

    # ---- Owner Server: crontab editor ----
    admin_ownersrv_cron_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_ownersrv_cron_edit_prompt, pattern=f"^{admin.OWNERSRV_CRON_EDIT_CB}$")],
        states={
            admin.ADMIN_OWNERSRV_CRON: [
                admin_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_cron_save),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin.admin_cancel), admin_cancel_button],
    )
    application.add_handler(admin_ownersrv_cron_conv)
    application.add_handler(CallbackQueryHandler(admin.admin_ownersrv_cron_menu, pattern=f"^{admin.OWNERSRV_CRON_MENU_CB}$"))

    admin_ownersrv_terminal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_ownersrv_terminal_start, pattern="^admin_ownersrv_terminal$")],
        states={
            admin.ADMIN_OWNERSRV_CMD: [
                CallbackQueryHandler(admin.admin_ownersrv_terminal_exit, pattern=f"^{admin.OWNERSRV_TERMINAL_EXIT_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_terminal_cancel, pattern=f"^{admin.OWNERSRV_TERMINAL_CANCEL_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_terminal_enter, pattern=f"^{admin.OWNERSRV_TERMINAL_ENTER_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_terminal_yes, pattern=f"^{admin.OWNERSRV_TERMINAL_YES_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_terminal_no, pattern=f"^{admin.OWNERSRV_TERMINAL_NO_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_terminal_download, pattern=f"^{admin.OWNERSRV_TERMINAL_DOWNLOAD_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_tab_new, pattern=f"^{admin.OWNERSRV_TAB_NEW_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_tab_switch, pattern=f"^{admin.OWNERSRV_TAB_SWITCH_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_tab_close, pattern=f"^{admin.OWNERSRV_TAB_CLOSE_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_noop, pattern=f"^{admin.OWNERSRV_NOOP_CB}$"),
                # Quick Commands (⚡) and Resize (⇄) - see admin.py's
                # OWNERSRV_QUICK_*_CB / OWNERSRV_RESIZE_CB_PREFIX constants.
                CallbackQueryHandler(admin.admin_ownersrv_quick_menu_open, pattern=f"^{admin.OWNERSRV_QUICK_MENU_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_quick_menu_close, pattern=f"^{admin.OWNERSRV_QUICK_CLOSE_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_quick_run, pattern=f"^{admin.OWNERSRV_QUICK_RUN_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_resize, pattern=f"^{admin.OWNERSRV_RESIZE_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_quick_add_start, pattern=f"^{admin.OWNERSRV_QUICK_ADD_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_quick_manage_open, pattern=f"^{admin.OWNERSRV_QUICK_MANAGE_CB_PREFIX}.+$"),
                CallbackQueryHandler(admin.admin_ownersrv_quick_del, pattern=f"^{admin.OWNERSRV_QUICK_DEL_CB_PREFIX}.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_terminal_input),
            ],
            admin.ADMIN_OWNERSRV_QUICKADD_LABEL: [
                MessageHandler(filters.Regex(f"^{admin.CANCEL_BUTTON_TEXT}$"), admin.admin_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_quick_add_label_input),
            ],
            admin.ADMIN_OWNERSRV_QUICKADD_CMD: [
                MessageHandler(filters.Regex(f"^{admin.CANCEL_BUTTON_TEXT}$"), admin.admin_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_quick_add_cmd_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin.admin_cancel),
            admin_cancel_button,
            CallbackQueryHandler(admin.admin_ownersrv_terminal_exit, pattern=f"^{admin.OWNERSRV_TERMINAL_EXIT_CB}$"),
        ],
    )
    application.add_handler(admin_ownersrv_terminal_conv)

    # ---- Owner Server: Files (local file browser - the SFTP equivalent for the host) ----
    admin_ownersrv_files_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin.admin_ownersrv_files_start, pattern=f"^{admin.OWNERSRV_FILES_START_CB}$")],
        states={
            admin.ADMIN_OWNERSRV_FILES: [
                CallbackQueryHandler(admin.admin_ownersrv_files_nav, pattern=f"^{admin.OWNERSRV_FILES_NAV_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_up, pattern=f"^{admin.OWNERSRV_FILES_UP_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_refresh, pattern=f"^{admin.OWNERSRV_FILES_REFRESH_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_download, pattern=f"^{admin.OWNERSRV_FILES_DL_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_goto_prompt, pattern=f"^{admin.OWNERSRV_FILES_GOTO_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_upload_prompt, pattern=f"^{admin.OWNERSRV_FILES_UPLOAD_HERE_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_urldl_prompt, pattern=f"^{admin.OWNERSRV_FILES_URLDL_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_mkdir_prompt, pattern=f"^{admin.OWNERSRV_FILES_MKDIR_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_actions_menu, pattern=f"^{admin.OWNERSRV_FILES_ACTIONS_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_rename_prompt, pattern=f"^{admin.OWNERSRV_FILES_RENAME_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_edit_open, pattern=f"^{admin.OWNERSRV_FILES_EDIT_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_chmod_prompt, pattern=f"^{admin.OWNERSRV_FILES_CHMOD_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_chown_prompt, pattern=f"^{admin.OWNERSRV_FILES_CHOWN_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_extract, pattern=f"^{admin.OWNERSRV_FILES_EXTRACT_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_compress, pattern=f"^{admin.OWNERSRV_FILES_COMPRESS_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_tail_start, pattern=f"^{admin.OWNERSRV_FILES_TAIL_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_tail_stop, pattern=f"^{admin.OWNERSRV_FILES_TAILSTOP_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_delete_confirm, pattern=f"^{admin.OWNERSRV_FILES_DELCONFIRM_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_delete_execute, pattern=f"^{admin.OWNERSRV_FILES_DELOK_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_selmode_on, pattern=f"^{admin.OWNERSRV_FILES_SELMODE_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_selmode_off, pattern=f"^{admin.OWNERSRV_FILES_SELDONE_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_seltoggle, pattern=f"^{admin.OWNERSRV_FILES_SELTOGGLE_CB_PREFIX}\\d+$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_seldelconfirm, pattern=f"^{admin.OWNERSRV_FILES_SELDELCONFIRM_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_seldelexecute, pattern=f"^{admin.OWNERSRV_FILES_SELDELOK_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_selcompress, pattern=f"^{admin.OWNERSRV_FILES_SELCOMPRESS_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_back, pattern=f"^{admin.OWNERSRV_FILES_BACK_CB}$"),
                CallbackQueryHandler(admin.admin_ownersrv_files_close, pattern=f"^{admin.OWNERSRV_FILES_CLOSE_CB}$"),
                MessageHandler(filters.Document.ALL, admin.admin_ownersrv_files_upload_received),
                admin_ownersrv_files_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin.admin_ownersrv_files_text_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin.admin_ownersrv_files_cancel),
            admin_ownersrv_files_cancel_button,
        ],
    )
    application.add_handler(admin_ownersrv_files_conv)

    # ====================== Admin: payment approval (DM'd to every id in ADMIN_IDS) ======================
    application.add_handler(CallbackQueryHandler(subscription.admin_approve_payment, pattern=r"^adminpay_approve_"))
    application.add_handler(CallbackQueryHandler(subscription.admin_reject_payment, pattern=r"^adminpay_reject_"))

    # ====================== Subscription / Wallet (public feature - every user) ======================
    application.add_handler(
        MessageHandler(filters.Regex(f"^{subscription.SUBSCRIPTION_BUTTON_TEXT}$"), subscription.subscription_menu)
    )
    application.add_handler(CallbackQueryHandler(subscription.sub_back_to_status, pattern="^sub_back_status$"))
    application.add_handler(CallbackQueryHandler(subscription.sub_buy_menu, pattern="^sub_buy_menu$"))
    application.add_handler(CallbackQueryHandler(subscription.sub_plan_detail, pattern=r"^sub_plan_"))
    application.add_handler(CallbackQueryHandler(subscription.sub_pay_wallet, pattern=r"^sub_paywallet_"))

    payments_cancel_button = MessageHandler(filters.Regex(f"^{subscription.CANCEL_BUTTON_TEXT}$"), subscription.payments_cancel)

    sub_pay_card_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(subscription.sub_pay_card_start, pattern=r"^sub_paycard_")],
        states={
            subscription.PAY_CARD_DIGITS: [
                payments_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscription.sub_pay_card_digits_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", subscription.payments_cancel), payments_cancel_button],
    )
    application.add_handler(sub_pay_card_conv)

    sub_topup_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(subscription.sub_topup_start, pattern="^sub_topup_start$")],
        states={
            subscription.TOPUP_AMOUNT: [
                payments_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscription.sub_topup_amount_input),
            ],
            subscription.TOPUP_CARD_DIGITS: [
                payments_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, subscription.sub_topup_card_digits_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", subscription.payments_cancel), payments_cancel_button],
    )
    application.add_handler(sub_topup_conv)

    # ====================== Server Manager ======================
    svm.register_handlers(application)

    servermgr_cancel_button = MessageHandler(
        filters.Regex(f"^{svm.CANCEL_BUTTON_TEXT}$|^{svm.CMD_DONE_TEXT}$"), svm.servermgr_cancel
    )

    servermgr_quickping_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_quickping_start, pattern="^servermgr_quickping_start$")],
        states={
            svm.SERVERMGR_QUICKPING_INPUT: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_quickping_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_quickping_conv)

    servermgr_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_srv_add_start, pattern="^servermgr_srv_add$")],
        states={
            svm.SERVERMGR_ADD_HOST: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_host_input),
            ],
            svm.SERVERMGR_ADD_PORT: [
                servermgr_cancel_button,
                CallbackQueryHandler(svm.servermgr_add_port_default_button, pattern=f"^{svm.ADD_PORT_DEFAULT_CB}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_port_input),
            ],
            svm.SERVERMGR_ADD_USER: [
                servermgr_cancel_button,
                CallbackQueryHandler(svm.servermgr_add_user_default_button, pattern=f"^{svm.ADD_USER_DEFAULT_CB}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_user_input),
            ],
            svm.SERVERMGR_ADD_AUTHTYPE: [
                servermgr_cancel_button,
                CallbackQueryHandler(
                    svm.servermgr_add_authtype_button,
                    pattern=f"^({svm.ADD_AUTH_PASS_CB}|{svm.ADD_AUTH_KEY_CB})$",
                ),
            ],
            svm.SERVERMGR_ADD_SECRET: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_secret_input),
            ],
            svm.SERVERMGR_ADD_PASSPHRASE: [
                servermgr_cancel_button,
                CallbackQueryHandler(svm.servermgr_add_passphrase_none_button, pattern=f"^{svm.ADD_PASSPHRASE_NONE_CB}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_passphrase_input),
            ],
            svm.SERVERMGR_ADD_LABEL: [
                servermgr_cancel_button,
                CallbackQueryHandler(svm.servermgr_add_label_default_button, pattern=f"^{svm.ADD_LABEL_DEFAULT_CB}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_add_label_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_add_conv)

    servermgr_ssh_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_ssh_start, pattern="^servermgr_ssh_start_")],
        states={
            svm.SERVERMGR_CMD_INPUT: [
                CallbackQueryHandler(svm.servermgr_ssh_start, pattern="^servermgr_ssh_start_"),
                CallbackQueryHandler(svm.servermgr_switch_tab, pattern="^servermgr_switch_"),
                CallbackQueryHandler(svm.servermgr_closetab, pattern="^servermgr_closetab_"),
                CallbackQueryHandler(svm.servermgr_tabsbar_switch, pattern=f"^{svm.TAB_SWITCH_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_tabsbar_close, pattern=f"^{svm.TAB_CLOSE_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_tabsbar_newtab, pattern=f"^{svm.TAB_NEWTAB_CB}$"),
                CallbackQueryHandler(svm.servermgr_cmd_cancel_button, pattern=f"^{svm.CMD_CANCEL_CALLBACK}_"),
                CallbackQueryHandler(svm.servermgr_cmd_enter_button, pattern=f"^{svm.CMD_ENTER_CALLBACK}_"),
                CallbackQueryHandler(svm.servermgr_cmd_yes_button, pattern=f"^{svm.CMD_YES_CALLBACK}_"),
                CallbackQueryHandler(svm.servermgr_cmd_no_button, pattern=f"^{svm.CMD_NO_CALLBACK}_"),
                # ---- Quick commands (⚡) inside a live SSH terminal tab ----
                # Was missing entirely, so tapping "⚡ Quick" (and everything
                # under it) silently did nothing for regular users, even
                # though the Owner Server's equivalent menu works fine.
                CallbackQueryHandler(svm.servermgr_quick_menu_open, pattern=f"^{svm.QUICK_MENU_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_quick_menu_close, pattern=f"^{svm.QUICK_CLOSE_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_quick_manage_open, pattern=f"^{svm.QUICK_MANAGE_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_quick_del, pattern=f"^{svm.QUICK_DEL_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_quick_add_start, pattern=f"^{svm.QUICK_ADD_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_quick_run, pattern=f"^{svm.QUICK_RUN_CB_PREFIX}"),
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_cmd_input),
            ],
            # ---- "+ Add command" text prompts (label, then shell command) ----
            # Also missing: servermgr_quick_add_start() returns these states,
            # but with no entry here the ConversationHandler had no state to
            # move into, so the label/cmd prompts would never be handled.
            svm.SERVERMGR_QUICKADD_LABEL: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_quick_add_label_input),
            ],
            svm.SERVERMGR_QUICKADD_CMD: [
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_quick_add_cmd_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_ssh_conv)

    servermgr_files_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_files_start, pattern=f"^{svm.FILES_START_CB_PREFIX}")],
        states={
            svm.SERVERMGR_FILES_BROWSE: [
                CallbackQueryHandler(svm.servermgr_files_start, pattern=f"^{svm.FILES_START_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_nav, pattern=f"^{svm.FILES_NAV_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_download, pattern=f"^{svm.FILES_DL_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_up, pattern=f"^{svm.FILES_UP_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_refresh, pattern=f"^{svm.FILES_REFRESH_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_upload_prompt, pattern=f"^{svm.FILES_UPLOAD_HERE_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_goto_prompt, pattern=f"^{svm.FILES_GOTO_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_urldl_prompt, pattern=f"^{svm.FILES_URLDL_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_mkdir_prompt, pattern=f"^{svm.FILES_MKDIR_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_close, pattern=f"^{svm.FILES_CLOSE_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_actions_menu, pattern=f"^{svm.FILES_ACTIONS_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_edit_open, pattern=f"^{svm.FILES_EDIT_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_rename_prompt, pattern=f"^{svm.FILES_RENAME_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_delete_confirm, pattern=f"^{svm.FILES_DELCONFIRM_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_delete_execute, pattern=f"^{svm.FILES_DELOK_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_back, pattern=f"^{svm.FILES_BACK_CB}$"),
                CallbackQueryHandler(svm.servermgr_noop, pattern="^servermgr_noop$"),
                servermgr_cancel_button,
                MessageHandler(filters.Document.ALL, svm.servermgr_files_upload_received),
                MessageHandler(
                    filters.COMMAND & ~filters.Regex(r"(?i)^/cancel(@\w+)?(\s|$)"),
                    svm.servermgr_files_text_input,
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_files_text_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_files_conv)

    servermgr_adv_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_adv_start, pattern=f"^{svm.ADV_START_CB_PREFIX}")],
        states={
            svm.SERVERMGR_ADV_BROWSE: [
                CallbackQueryHandler(svm.servermgr_adv_start, pattern=f"^{svm.ADV_START_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_menu, pattern=f"^{svm.ADV_MENU_CB}$"),
                CallbackQueryHandler(svm.servermgr_adv_close, pattern=f"^{svm.ADV_CLOSE_CB}$"),
                CallbackQueryHandler(svm.servermgr_adv_kill9, pattern=f"^{svm.ADV_PROC_KILL9_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_kill, pattern=f"^{svm.ADV_PROC_KILL_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_processes, pattern=f"^{svm.ADV_PROC_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_svc_filter_mode, pattern=f"^{svm.ADV_SVC_FILTERMODE_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_noop, pattern="^servermgr_noop$"),
                CallbackQueryHandler(svm.servermgr_adv_svc_log_stop, pattern=f"^{svm.ADV_SVC_LOGSTOP_CB}$"),
                CallbackQueryHandler(svm.servermgr_adv_svc_log_start, pattern=f"^{svm.ADV_SVC_LOG_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_svc_remove_confirm, pattern=f"^{svm.ADV_SVC_RM_CONFIRM_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_svc_remove_execute, pattern=f"^{svm.ADV_SVC_RM_OK_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_svc_action, pattern=f"^{svm.ADV_SVC_ACTION_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_svc_detail, pattern=f"^{svm.ADV_SVC_DETAIL_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_adv_services, pattern=f"^{svm.ADV_SVC_CB}$"),
                CallbackQueryHandler(svm.servermgr_adv_cron_edit_prompt, pattern=f"^{svm.ADV_CRON_EDIT_CB}$"),
                CallbackQueryHandler(svm.servermgr_adv_cron_view, pattern=f"^{svm.ADV_CRON_VIEW_CB}$"),
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_adv_text_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_adv_conv)

    automation_cancel_button = MessageHandler(
        filters.Regex(f"^{svm_auto.CANCEL_BUTTON_TEXT}$"), svm_auto.automation_cancel
    )

    application.add_handler(CallbackQueryHandler(svm_auto.automation_menu, pattern="^svauto_menu_"))
    application.add_handler(CallbackQueryHandler(svm_auto.history_view, pattern="^svauto_hist_"))

    automation_tag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm_auto.tag_start, pattern="^svauto_tag_start_")],
        states={
            svm_auto.AUTO_TAG_INPUT: [
                automation_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm_auto.tag_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm_auto.automation_cancel), automation_cancel_button],
    )
    application.add_handler(automation_tag_conv)

    automation_qc_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm_auto.qc_add_start, pattern="^svauto_qc_add_")],
        states={
            svm_auto.AUTO_QC_ADD_LABEL: [
                automation_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm_auto.qc_add_label),
            ],
            svm_auto.AUTO_QC_ADD_COMMAND: [
                automation_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm_auto.qc_add_command),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm_auto.automation_cancel), automation_cancel_button],
    )
    application.add_handler(automation_qc_add_conv)
    application.add_handler(CallbackQueryHandler(svm_auto.qc_run, pattern="^svauto_qc_run_"))
    application.add_handler(CallbackQueryHandler(svm_auto.qc_delete, pattern="^svauto_qc_del_"))
    application.add_handler(CallbackQueryHandler(svm_auto.qc_menu, pattern="^svauto_qc_"))

    automation_sched_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm_auto.sched_add_start, pattern="^svauto_sched_add_")],
        states={
            svm_auto.AUTO_SCHED_ADD_COMMAND: [
                automation_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm_auto.sched_add_command),
            ],
            svm_auto.AUTO_SCHED_ADD_TIME: [
                automation_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm_auto.sched_add_time),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm_auto.automation_cancel), automation_cancel_button],
    )
    application.add_handler(automation_sched_add_conv)
    application.add_handler(CallbackQueryHandler(svm_auto.sched_toggle, pattern="^svauto_sched_toggle_"))
    application.add_handler(CallbackQueryHandler(svm_auto.sched_delete, pattern="^svauto_sched_del_"))
    application.add_handler(CallbackQueryHandler(svm_auto.sched_menu, pattern="^svauto_sched_"))

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            svm_health.health_monitor_tick,
            interval=svm_health.get_interval_seconds(),
            first=20,
            name=svm_health.JOB_NAME,
        )
        print(
            f"✚ Health monitoring active - checking every {svm_health.get_interval_seconds() / 3600:.1f}h, "
            f"disk alert at {svm_health.get_disk_alert_percent()}%. Toggle per-server from ◉ Alerts, "
            f"or tune these from /admin → ✚ Monitoring Settings."
        )
        svm_auto.register_all_jobs(application.job_queue)

        proxy_candidates = proxy_utils.get_proxy_candidates()
        if proxy_candidates:
            application.job_queue.run_repeating(
                proxy_watchdog_tick,
                interval=PROXY_WATCHDOG_INTERVAL,
                first=PROXY_WATCHDOG_INTERVAL,
                name="proxy_watchdog",
            )
            print(
                f"▸ Proxy watchdog active - {len(proxy_candidates)} candidate(s) configured, "
                f"checking every {PROXY_WATCHDOG_INTERVAL}s, restarts after "
                f"{PROXY_WATCHDOG_FAIL_THRESHOLD} consecutive failures."
            )
    else:
        print(
            "⚠ JobQueue is unavailable - health monitoring disabled. Install the extra:\n"
            '   pip install "python-telegram-bot[job-queue]"\n'
            "   then restart the bot."
        )

    print("✓ Handlers registered. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()