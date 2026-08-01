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
import sponsor_gate
from db import get_db
from ServerManager import handlers as svm
from ServerManager import health as svm_health
from ServerManager import automation as svm_auto

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ====================== Core commands: /start, /help, main menu ======================
# Small enough to live here rather than in their own module. ServerManager.
# server_manager_handlers calls get_main_menu() (wired up via set_get_main_menu
# below) whenever it needs to send the user "back" to this same keyboard.

HELP_TEXT = (
    "🤖 *Terminal Bot*\n\n"
    "Register your own server(s) and run SSH commands on them right from this chat.\n\n"
    "• An active subscription is required to use Server Manager - tap "
    "*💳 Subscription* to buy or renew a plan.\n"
    "• Tap *🖥 Server Manager* to add a server or open a live terminal session.\n"
    "• Inside a session, just type commands like you would in a normal terminal - "
    "output streams back live.\n"
    "• Use the *❌ Cancel* / *🔚 End Session* button any time to stop.\n\n"
    "Your server credentials are stored encrypted and only you can see or use them."
)


def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[svm.MENU_BUTTON_TEXT], [subscription.SUBSCRIPTION_BUTTON_TEXT]],
        resize_keyboard=True,
    )


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

    text = (
        "👋 Welcome to *Terminal Bot*!\n\n"
        "Add a server and run SSH commands on it, right here in the chat.\n"
        "Tap *🖥 Server Manager* below to get started, or /help for details."
    )
    chat = update.effective_chat
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


async def help_command(update: Update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=get_main_menu())


async def set_bot_commands(application):
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


def main():
    print("\n" + "=" * 60)
    print("🚀 Terminal Bot starting...")
    print("=" * 60)

    if not config.BOT_TOKEN:
        print("\n❌ BOT_TOKEN is missing. Set it in your environment or .env file.")
        return

    if not crypto_utils.IS_CONFIGURED:
        print(
            "\n❌ CRYPTO_SECRET is missing. Server Manager stores SSH passwords/private keys "
            "encrypted with it, and without a fixed secret every credential saved would become "
            "unreadable on the next restart. Set it in your .env, e.g.:\n"
            '   python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then run the bot again."
        )
        return

    try:
        get_db()
        print("✅ Connected to PostgreSQL and verified tables (users, plans, subscriptions, ...).")
    except Exception as e:
        print(f"\n❌ Could not connect to PostgreSQL: {e}")
        print("   Check DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD in your .env, then run:")
        print("   python3 setup_db.py")
        return

    channels = bot_settings.get_sponsor_channels()
    if channels and bot_settings.is_membership_required():
        print(f"🔒 Sponsor gate enabled - {len(channels)} required channel(s):")
        for ch in channels:
            print(f"   • {ch['title']} ({ch['id']})")
        print("   Make sure the bot is an ADMIN in each of them, or membership checks will fail.")
        print("   Manage this any time from /admin without restarting the bot.")
    else:
        print("ℹ️ Sponsor gate is disabled - the bot is open to everyone. Enable it from /admin.")

    if not config.ADMIN_IDS:
        print("⚠️ ADMIN_IDS is empty - nobody will be able to open /admin. Set it in your .env.")

    # Wire ServerManager's / Admin panel's / payments' "back to main menu" buttons to our keyboard.
    svm.set_get_main_menu(get_main_menu)
    admin.set_get_main_menu(get_main_menu)
    subscription.set_get_main_menu(get_main_menu)
    svm_auto.set_get_main_menu(get_main_menu)

    try:
        # concurrent_updates=True is required for Server Manager's live terminal:
        # the "❌ Cancel" button is a separate update from the one currently being
        # processed (the streaming command), so without this the button tap just
        # queues up and isn't handled until the command finishes on its own.
        application = Application.builder().token(config.BOT_TOKEN).concurrent_updates(True).build()
        print("✅ Bot initialized successfully!")
    except Exception as e:
        print(f"\n❌ Failed to initialize bot: {e}")
        return

    application.post_init = set_bot_commands
    application.add_error_handler(error_handler)

    # ====================== Sponsor channel gate ======================
    # group=-1 runs before every other handler below, for every update type.
    # See sponsor_gate.py for how it decides to let an update through or not.
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
    application.add_handler(CallbackQueryHandler(admin.admin_back_to_main, pattern="^admin_back_to_main$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_channel_settings_menu, pattern="^admin_channel_settings$")
    )
    application.add_handler(CallbackQueryHandler(admin.admin_channel_toggle, pattern="^admin_channel_toggle$"))
    application.add_handler(CallbackQueryHandler(admin.admin_channel_remove_menu, pattern="^admin_channel_remove$"))
    application.add_handler(
        CallbackQueryHandler(admin.admin_channel_remove_confirm, pattern=r"^admin_channel_remove_\d+$")
    )

    # Matches the "❌ Cancel" reply-keyboard button shown while the "Add
    # Channel" form is waiting for text input.
    admin_cancel_button = MessageHandler(filters.Regex(f"^{admin.CANCEL_BUTTON_TEXT}$"), admin.admin_cancel)

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

    # ====================== Admin: Manage Users ======================
    application.add_handler(CallbackQueryHandler(admin.admin_users_menu, pattern="^admin_users_menu$"))
    application.add_handler(CallbackQueryHandler(admin.admin_users_page, pattern=r"^admin_users_page_\d+$"))
    application.add_handler(CallbackQueryHandler(admin.admin_user_view, pattern=r"^admin_user_view_-?\d+$"))
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

    # Matches the "❌ Cancel" / "🔚 End Session" reply-keyboard buttons shown while
    # a Server Manager conversation (add-server form, or an open SSH command loop)
    # is waiting for text input.
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
                # Registering these here too (not just as entry_points) is what lets
                # the user open/switch/close additional tabs while already mid-session -
                # entry_points alone are skipped once a conversation is active for that user.
                CallbackQueryHandler(svm.servermgr_ssh_start, pattern="^servermgr_ssh_start_"),
                CallbackQueryHandler(svm.servermgr_switch_tab, pattern="^servermgr_switch_"),
                CallbackQueryHandler(svm.servermgr_closetab, pattern="^servermgr_closetab_"),
                CallbackQueryHandler(svm.servermgr_tabsbar_switch, pattern=f"^{svm.TAB_SWITCH_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_tabsbar_close, pattern=f"^{svm.TAB_CLOSE_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_tabsbar_newtab, pattern=f"^{svm.TAB_NEWTAB_CB}$"),
                CallbackQueryHandler(svm.servermgr_cmd_cancel_button, pattern=f"^{svm.CMD_CANCEL_CALLBACK}_"),
                servermgr_cancel_button,
                MessageHandler(filters.TEXT & ~filters.COMMAND, svm.servermgr_cmd_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", svm.servermgr_cancel), servermgr_cancel_button],
    )
    application.add_handler(servermgr_ssh_conv)

    servermgr_files_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(svm.servermgr_files_start, pattern=f"^{svm.FILES_START_CB_PREFIX}")],
        states={
            svm.SERVERMGR_FILES_BROWSE: [
                # Registered here too (not just as entry_points) so opening the file
                # browser for a different server while one is already open just
                # closes the old one and opens the new one, instead of being ignored.
                CallbackQueryHandler(svm.servermgr_files_start, pattern=f"^{svm.FILES_START_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_nav, pattern=f"^{svm.FILES_NAV_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_download, pattern=f"^{svm.FILES_DL_CB_PREFIX}"),
                CallbackQueryHandler(svm.servermgr_files_up, pattern=f"^{svm.FILES_UP_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_refresh, pattern=f"^{svm.FILES_REFRESH_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_upload_prompt, pattern=f"^{svm.FILES_UPLOAD_HERE_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_goto_prompt, pattern=f"^{svm.FILES_GOTO_CB}$"),
                CallbackQueryHandler(svm.servermgr_files_urldl_prompt, pattern=f"^{svm.FILES_URLDL_CB}$"),
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
                # "Go to path" / URL-download prompts expect free text that can
                # legitimately start with "/" (e.g. "/root", "/opt/terminal-bot").
                # Telegram tags any text starting with "/" as a command entity
                # regardless of whether it's a real registered command, so
                # filters.COMMAND matches it - without this handler that text
                # would just be silently dropped instead of reaching
                # servermgr_files_text_input. "/cancel" itself is excluded here
                # so it still falls through to the fallback CommandHandler below.
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

    # ====================== Server Manager: Automation (tags / quick commands / schedules) ======================
    # See ServerManager/automation.py - entry point is the "⚙️ Automation" button on the
    # server detail screen. Order matters below: each "_add_"/"_run_"/"_del_"/"_toggle_"
    # handler must be registered before the more general list-menu handler that shares
    # its prefix (e.g. svauto_qc_add_ before svauto_qc_), since CallbackQueryHandlers are
    # matched in registration order and the first match wins.
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

    # ====================== Server Manager: health monitoring ======================
    # Background sweep of every registered server's CPU/RAM/Disk/reachability -
    # see server_manager_health.py. Alerts DM each server's own owner, only on
    # a down<->up transition or a disk-full threshold crossing (not every tick).
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            svm_health.health_monitor_tick,
            interval=svm_health.get_interval_seconds(),
            first=20,
            name=svm_health.JOB_NAME,
        )
        print(
            f"🩺 Health monitoring active - checking every {svm_health.get_interval_seconds() / 3600:.1f}h, "
            f"disk alert at {svm_health.get_disk_alert_percent()}%. Toggle per-server from 🔔 Alerts, "
            f"or tune these from /admin → 🩺 Monitoring Settings."
        )
        svm_auto.register_all_jobs(application.job_queue)
    else:
        print(
            "⚠️ JobQueue is unavailable - health monitoring disabled. Install the extra:\n"
            '   pip install "python-telegram-bot[job-queue]"\n'
            "   then restart the bot."
        )

    print("✅ Handlers registered. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
