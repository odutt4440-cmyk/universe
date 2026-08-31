import asyncio
import io
import os
import logging
import time
import qrcode
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FreshResetAuthorisationForbiddenError
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId

# ==================== ENVIRONMENT CONFIGURATION ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

LOG_CHANNEL_ID = "tg_selling_group"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
UPI_ID_TEXT = os.getenv("UPI_ID_TEXT", "yourupi@bank")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Account Store")

MIN_DEPOSIT = 10.0
MIN_WITHDRAW = 50.0

logging.basicConfig(level=logging.INFO)

# ==================== MONGO DB INITIALIZATION ====================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["universe_shop_db"]

users_col = db["users"]
accounts_col = db["accounts"]
sudo_col = db["sudo_users"]
requests_col = db["requests"]
settings_col = db["settings"]

SUDO_USERS = set()

async def init_db():
    global SUDO_USERS
    await sudo_col.update_one({"user_id": OWNER_ID}, {"$set": {"user_id": OWNER_ID}}, upsert=True)
    sudo_docs = await sudo_col.find().to_list(length=1000)
    SUDO_USERS = {doc["user_id"] for doc in sudo_docs}
    SUDO_USERS.add(OWNER_ID)
    
    m_doc = await settings_col.find_one({"key": "maintenance"})
    if not m_doc:
        await settings_col.insert_one({
            "key": "maintenance",
            "is_active": False,
            "reason": "Routine system maintenance and performance upgrades in progress."
        })

# ==================== BOT CLIENT SETUP ====================
app = Client("ShopBotGUI", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user_states = {}
temp_data = {}

# ==================== HELPER DB FUNCTIONS ====================
async def get_maintenance_status() -> tuple[bool, str]:
    m_doc = await settings_col.find_one({"key": "maintenance"})
    if not m_doc:
        return False, "Routine system maintenance and performance upgrades in progress."
    return m_doc.get("is_active", False), m_doc.get("reason", "Routine system maintenance and performance upgrades in progress.")

async def set_maintenance_status(is_active: bool, reason: str = None):
    update_data = {"is_active": is_active}
    if reason is not None:
        update_data["reason"] = reason
    await settings_col.update_one({"key": "maintenance"}, {"$set": update_data}, upsert=True)

def mask_phone_number(phone: str) -> str:
    if len(phone) > 5:
        return phone[:5] + "X" * (len(phone) - 5)
    return phone

async def get_user_data(user_id: int):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "balance": 0.0,
            "profile_cashback": 0.0,
            "is_banned": False,
            "ban_reason": ""
        }
        await users_col.insert_one(user)
    return user

async def get_user_balance(user_id: int) -> float:
    user = await get_user_data(user_id)
    return user.get("balance", 0.0)

async def is_banned(user_id: int) -> tuple[bool, str]:
    user = await get_user_data(user_id)
    return user.get("is_banned", False), user.get("ban_reason", "")

async def set_user_balance(user_id: int, new_balance: float):
    await users_col.update_one({"user_id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)

async def update_balance(user_id: int, amount: float):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"balance": amount}}, upsert=True)

async def update_profile_cashback(user_id: int, amount: float):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"profile_cashback": amount}}, upsert=True)

async def add_sudo_user(user_id: int):
    SUDO_USERS.add(user_id)
    await sudo_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

async def remove_sudo_user(user_id: int):
    if user_id == OWNER_ID:
        return
    SUDO_USERS.discard(user_id)
    await sudo_col.delete_one({"user_id": user_id})

async def log_to_channel(text: str, reply_markup=None):
    if LOG_CHANNEL_ID:
        try:
            await app.send_message(LOG_CHANNEL_ID, text, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Log Channel Error: {e}")

def get_buy_now_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url="https://t.me/Jhsggsgbot")]
    ])

def generate_upi_qr(upi_id: str, name: str, amount: float = None) -> io.BytesIO:
    name_encoded = name.replace(" ", "%20")
    if amount and amount > 0:
        upi_url = f"upi://pay?pa={upi_id}&pn={name_encoded}&am={amount:.2f}&cu=INR"
    else:
        upi_url = f"upi://pay?pa={upi_id}&pn={name_encoded}&cu=INR"

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    bio = io.BytesIO()
    bio.name = 'qr.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

def get_account_options_keyboard(acc_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Re-fetch OTP", callback_data=f"refetch_otp_{acc_id}")],
        [InlineKeyboardButton("📱 Manage Devices", callback_data=f"manage_devs_{acc_id}")],
        [InlineKeyboardButton("🚪 Finish & Logout Bot", callback_data=f"logout_bot_{acc_id}")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="user_main_menu")]
    ])

# ==================== OTP LISTENER ENGINE ====================
async def fetch_latest_otp(user_id: int, acc_id: str, is_manual: bool = False):
    acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})

    if not acc:
        await app.send_message(user_id, "❌ **Account session record not found!**")
        return

    phone_number = acc["phone_number"]
    session_string = acc["session_string"]
    two_fa = acc["two_fa"]
    category = acc.get("category", "General")
    country = acc.get("country", "Global")
    year = acc.get("year", "N/A")
    
    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await app.send_message(user_id, f"⚠️ **Account Session Expired or Closed:** `{phone_number}`")
            return

        latest_otp = None
        async for message in t_client.iter_messages(777000, limit=5):
            if message and message.text:
                latest_otp = message.text
                break

        await t_client.disconnect()

        if latest_otp:
            await app.send_message(
                user_id,
                f"📲 **NEW LOGIN OTP RECEIVED!**\n\n"
                f"**Phone:** `{phone_number}`\n"
                f"**OTP Details:**\n`{latest_otp}`\n\n"
                f"**2FA Password:** `{two_fa}`\n\n"
                f"⚠️ *Note: We are not responsible for any issues after receiving the OTP.*",
                reply_markup=get_account_options_keyboard(acc_id)
            )
            
            masked_phone = mask_phone_number(phone_number)
            log_text = (
                f"✅ **LOGIN OTP RECEIVED!**\n\n"
                f"👤 **Buyer ID:** `{user_id}`\n"
                f"📁 **Category:** {category}\n"
                f"🌍 **Country & Year:** {country} ({year})\n"
                f"📞 **Phone Number:** `{masked_phone}`\n\n"
                f"📌 **Status:** Login Code Delivered"
            )
            await log_to_channel(log_text, reply_markup=get_buy_now_keyboard())

        elif is_manual:
            await app.send_message(
                user_id,
                f"⌛ **No fresh OTP found yet for** `{phone_number}`. Re-send OTP in app and click again.",
                reply_markup=get_account_options_keyboard(acc_id)
            )

    except Exception as e:
        logging.error(f"OTP Fetch Error: {e}")

async def listen_for_otp(user_id: int, phone_number: str, session_string: str, two_fa: str, acc_id: str):
    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await app.send_message(user_id, f"⚠️ **Account Session Expired:** `{phone_number}`")
            return

        buy_time = time.time()
        for _ in range(30):
            await asyncio.sleep(5)
            async for message in t_client.iter_messages(777000, limit=1):
                if message and message.date:
                    msg_timestamp = message.date.timestamp()
                    if msg_timestamp >= buy_time - 5 and message.text:
                        await t_client.disconnect()
                        await fetch_latest_otp(user_id, acc_id, is_manual=False)
                        return

        await t_client.disconnect()
    except Exception as e:
        logging.error(f"OTP Listener Error: {e}")

# ==================== MAIN MENUS ====================
def get_main_menu_keyboard(user_id: int):
    buttons = [
        [InlineKeyboardButton("🛒 Buy Accounts", callback_data="user_buy_menu"), InlineKeyboardButton("💳 Deposit Money", callback_data="user_deposit_menu")],
        [InlineKeyboardButton("💸 Withdraw Cashback", callback_data="user_withdraw_menu")],
        [InlineKeyboardButton("👤 Profile", callback_data="user_profile"), InlineKeyboardButton("👨‍💻 Support", url="https://t.me/storeacc_hub")]
    ]
    if user_id in SUDO_USERS:
        buttons.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard(user_id: int):
    row_1 = [
        InlineKeyboardButton("➕ Add Account Stock", callback_data="admin_add_acc"),
        InlineKeyboardButton("🗑️ Remove Stock", callback_data="admin_remove_stock")
    ]
    
    row_2 = []
    if user_id == OWNER_ID:
        row_2.append(InlineKeyboardButton("🏷️ Change Price (Owner)", callback_data="admin_change_price"))

    buttons = [row_1]
    if row_2:
        buttons.append(row_2)
    
    if user_id == OWNER_ID:
        buttons.append([InlineKeyboardButton("✏️ Edit User Balance", callback_data="admin_edit_bal")])
        
    buttons.append([
        InlineKeyboardButton("📊 Stats & Revenue", callback_data="admin_stats"),
        InlineKeyboardButton("ℹ️ User History & Info", callback_data="admin_user_history")
    ])

    buttons.append([
        InlineKeyboardButton("🛠️ Maintenance Mode", callback_data="admin_maint_panel"),
        InlineKeyboardButton("📢 Broadcast DM", callback_data="admin_broadcast")
    ])
    
    buttons.append([InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user"), InlineKeyboardButton("🟢 Unban User", callback_data="admin_unban_user")])
    
    if user_id == OWNER_ID:
        buttons.append([InlineKeyboardButton("👥 Manage Admins (Owner Only)", callback_data="admin_manage_sudo")])
    
    buttons.append([InlineKeyboardButton("🔙 Exit Admin Panel", callback_data="user_main_menu")])
    return InlineKeyboardMarkup(buttons)

async def get_maintenance_panel_keyboard():
    is_active, _ = await get_maintenance_status()
    toggle_text = "🔴 Turn OFF Maintenance" if is_active else "🟢 Turn ON Maintenance"
    
    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="adm_toggle_maint")],
        [InlineKeyboardButton("✏️ Change Maintenance Reason", callback_data="adm_change_maint_reason")],
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(buttons)

async def get_manage_sudo_keyboard():
    buttons = [
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm_add_sudo_btn")]
    ]
    sudo_docs = await sudo_col.find({"user_id": {"$ne": OWNER_ID}}).to_list(length=100)
    for doc in sudo_docs:
        s_id = doc["user_id"]
        buttons.append([InlineKeyboardButton(f"❌ Remove {s_id}", callback_data=f"adm_rem_sudo_{s_id}")])
    
    buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

# ==================== COMMAND HANDLERS ====================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS:
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}\n\n*All bot operations are temporarily paused. Please try again later.*")
        return

    bal = await get_user_balance(user_id)
    text = f"👋 **Welcome to the Account Store Bot!**\n\n🆔 **User ID:** `{user_id}`\n💰 **Wallet Balance:** ₹{bal:.2f}"
    await message.reply_text(text, reply_markup=get_main_menu_keyboard(user_id))

@app.on_message(filters.command("admin") & filters.private)
async def admin_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    if user_id not in SUDO_USERS:
        await message.reply_text("🚫 **Unauthorized.** This command is restricted to admins.")
        return
    await message.reply_text("⚙️ **Welcome to the Admin Dashboard**", reply_markup=get_admin_panel_keyboard(user_id))

# ==================== CALLBACK ROUTER ====================
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    data = query.data

    banned, reason = await is_banned(user_id)
    if banned:
        await query.answer(f"🚫 You are banned! Reason: {reason}", show_alert=True)
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS and not data.startswith("admin_"):
        await query.answer(f"🚧 Bot is under maintenance!\nReason: {maint_reason}", show_alert=True)
        return

    if data == "user_main_menu":
        user_states.pop(user_id, None)
        temp_data.pop(user_id, None)
        bal = await get_user_balance(user_id)
        await query.message.edit_text(f"👋 **Main Menu**\n\n💰 **Balance:** ₹{bal:.2f}", reply_markup=get_main_menu_keyboard(user_id))

    elif data == "user_profile":
        u_data = await get_user_data(user_id)
        await query.message.edit_text(
            f"👤 **Your Profile**\n\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"💵 **Wallet Balance:** ₹{u_data.get('balance', 0.0):.2f}\n"
            f"🎁 **Purchasing Profile Cashback:** ₹{u_data.get('profile_cashback', 0.0):.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    elif data == "user_deposit_menu":
        user_states[user_id] = "WAIT_DEPOSIT_AMOUNT_INPUT"
        await query.message.edit_text(
            f"💳 **DEPOSIT MONEY**\n\n"
            f"⚠️ **Minimum Deposit Amount:** ₹{MIN_DEPOSIT:.2f}\n\n"
            f"🔢 **Enter the amount you wish to deposit (in ₹):**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    elif data == "user_buy_menu":
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price",
                    "cashback": "$cashback"
                },
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.answer("❌ Currently Out of Stock!", show_alert=True)
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            cat = info.get("category", "General")
            country = info["country"]
            year = info["year"]
            price = info["price"]
            cb = info["cashback"]
            count = s["count"]

            cb_status = f"🎁 CB: ₹{cb}" if cb > 0 else "❌ No CB"
            btn_label = f"📁 {cat} | {country} ({year}) | ₹{price} | {cb_status} | 📦 Stock: {count}"
            
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"buy_cat_{cat}_{country}_{year}_{price}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")])
        await query.message.edit_text("🌍 **Select an Account Category:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("buy_cat_"):
        parts = data.split("_")
        category, country, year, price = parts[2], parts[3], parts[4], float(parts[5])

        acc = await accounts_col.find_one_and_update(
            {"category": category, "country": country, "year": year, "price": price, "status": "AVAILABLE"},
            {"$set": {"status": "SOLD", "sold_to": user_id}}
        )

        if not acc:
            await query.answer("❌ Item is out of stock!", show_alert=True)
            return

        acc_id = str(acc["_id"])
        phone = acc["phone_number"]
        session_str = acc["session_string"]
        two_fa = acc["two_fa"]
        cashback = acc.get("cashback", 0.0)

        bal = await get_user_balance(user_id)
        if bal < price:
            await accounts_col.update_one({"_id": acc["_id"]}, {"$set": {"status": "AVAILABLE", "sold_to": None}})
            await query.answer(f"❌ Insufficient Balance! Required: ₹{price}, Available: ₹{bal:.2f}", show_alert=True)
            return

        await update_balance(user_id, -price)

        msg = f"⚡ **OTP Live Monitoring Started!**\n\n" \
              f"📞 **Phone:** `{phone}`\n" \
              f"🔑 **2FA Password:** `{two_fa}`\n" \
              f"💵 **Price Paid:** ₹{price:.2f}\n"

        if cashback > 0:
            temp_data[user_id] = {"pending_cashback": cashback, "acc_id": acc_id}
            msg += f"\n🎁 **Cashback Earned:** ₹{cashback:.2f}! *(Options available after Finish & Logout process)*\n"

        msg += "\n_Enter phone number in Telegram app. Auto-checking OTP..._"

        await query.message.edit_text(msg, reply_markup=get_account_options_keyboard(acc_id))

        masked_phone = mask_phone_number(phone)
        log_text = (
            f"🛒 **NEW NUMBER PURCHASED!**\n\n"
            f"👤 **Buyer ID:** `{user_id}`\n"
            f"📂 **Category:** {category}\n"
            f"🌍 **Country & Year:** {country} ({year})\n"
            f"📞 **Phone Number:** `{masked_phone}`\n"
            f"💵 **Price Paid:** ₹{price:.2f}\n"
            f"🎁 **Cashback:** ₹{cashback:.2f}\n\n"
            f"📌 **Status:** Live Monitoring OTP..."
        )
        await log_to_channel(log_text, reply_markup=get_buy_now_keyboard())

        asyncio.create_task(listen_for_otp(user_id, phone, session_str, two_fa, acc_id))

    elif data.startswith("refetch_otp_"):
        acc_id = data.split("_")[2]
        await query.answer("🔄 Re-fetching latest OTP...", show_alert=False)
        await fetch_latest_otp(user_id, acc_id, is_manual=True)

    elif data.startswith("manage_devs_"):
        acc_id = data.split("_")[2]
        await query.answer("📱 Fetching Active Devices...", show_alert=False)
        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})
        
        if not acc:
            await query.message.reply_text("❌ **Account session record not found!**")
            return

        try:
            t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
            await t_client.connect()

            if not await t_client.is_user_authorized():
                await query.message.reply_text(f"⚠️ **Account Session Expired or Closed:** `{acc['phone_number']}`")
                return

            authorizations = await t_client(GetAuthorizationsRequest())
            await t_client.disconnect()

            dev_text = f"📱 **Active Devices List for** `{acc['phone_number']}`:\n\n"
            buttons = []

            for idx, auth in enumerate(authorizations.authorizations, 1):
                is_curr = " (Current Session)" if auth.current else ""
                dev_text += (
                    f"**{idx}. {auth.device_model}**{is_curr}\n"
                    f"▫️ **App:** {auth.app_name} ({auth.app_version})\n"
                    f"▫️ **System:** {auth.platform} ({auth.system_version})\n"
                    f"▫️ **IP:** `{auth.ip}` ({auth.country})\n\n"
                )
                
                # Dynamic terminate buttons for each device session
                btn_label = f"❌ Terminate {auth.device_model}" + (" (Current)" if auth.current else "")
                buttons.append([InlineKeyboardButton(btn_label, callback_data=f"term_hash_{acc_id}_{auth.hash}")])

            buttons.append([InlineKeyboardButton("🔙 Back to Options", callback_data=f"refetch_otp_{acc_id}")])
            await query.message.reply_text(dev_text, reply_markup=InlineKeyboardMarkup(buttons))

        except Exception as e:
            await query.message.reply_text(f"❌ Error fetching active devices: `{e}`")

    elif data.startswith("term_hash_"):
        parts = data.split("_")
        acc_id, hash_val = parts[2], int(parts[3])
        
        await query.answer("🛠️ Terminating selected session...", show_alert=False)
        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})
        
        if not acc:
            await query.message.reply_text("❌ **Account session record not found!**")
            return

        try:
            t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
            await t_client.connect()

            if not await t_client.is_user_authorized():
                await query.message.reply_text(f"⚠️ **Account Session Expired or Closed:** `{acc['phone_number']}`")
                return

            try:
                await t_client(ResetAuthorizationRequest(hash=hash_val))
                await query.message.reply_text("✅ **Device session terminated successfully!**")
            except FreshResetAuthorisationForbiddenError:
                await query.message.reply_text("⚠️ **Telegram Security Restriction:** New sessions cannot terminate other devices within 24 hours.")
            except Exception as err:
                await query.message.reply_text(f"❌ Failed to terminate device session: `{err}`")

            await t_client.disconnect()

        except Exception as e:
            await query.message.reply_text(f"❌ Error connecting to session: `{e}`")

    elif data.startswith("logout_bot_"):
        acc_id = data.split("_")[2]
        await query.answer("🚪 Logging out bot session...", show_alert=True)

        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})
        if acc:
            try:
                t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
                await t_client.connect()
                await t_client.log_out()
                await query.message.reply_text("🚪 **Finish & Logout Complete! Bot session deleted.**")
            except Exception as e:
                await query.message.reply_text(f"⚠️ Session notice: `{e}`")

        cb_info = temp_data.get(user_id)
        if cb_info and cb_info.get("pending_cashback", 0) > 0:
            cb_val = cb_info["pending_cashback"]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Add to Main Wallet", callback_data=f"cb_claim_wallet_{cb_val}")],
                [InlineKeyboardButton("👤 Add to Purchasing Profile", callback_data=f"cb_claim_profile_{cb_val}")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="user_main_menu")]
            ])
            await app.send_message(
                user_id,
                f"🎉 **You have earned cashback on your purchase!**\n\n"
                f"🎁 **Cashback Amount:** ₹{cb_val:.2f}\n"
                f"Where would you like to add your cashback rewards?",
                reply_markup=kb
            )

    elif data.startswith("cb_claim_wallet_"):
        amount = float(data.split("_")[3])
        await update_balance(user_id, amount)
        temp_data.pop(user_id, None)
        await query.message.edit_text(f"✅ **₹{amount:.2f} Cashback credited to your Main Wallet!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]]))

    elif data.startswith("cb_claim_profile_"):
        amount = float(data.split("_")[3])
        await update_profile_cashback(user_id, amount)
        temp_data.pop(user_id, None)
        await query.message.edit_text(f"✅ **₹{amount:.2f} Cashback saved to your Purchasing Profile!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]]))

    elif data == "user_withdraw_menu":
        bal = await get_user_balance(user_id)
        if bal < MIN_WITHDRAW:
            await query.answer(f"❌ Minimum ₹{MIN_WITHDRAW:.2f} required to withdraw. Your balance: ₹{bal:.2f}", show_alert=True)
            return

        user_states[user_id] = "WAIT_WITHDRAW_AMOUNT"
        await query.message.edit_text(
            f"💸 **CASHBACK / WALLET WITHDRAWAL**\n\n"
            f"💰 **Available Balance:** ₹{bal:.2f}\n"
            f"⚠️ **Minimum Withdrawal:** ₹{MIN_WITHDRAW:.2f}\n\n"
            f"🔢 **Enter amount to withdraw:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    # ==================== ADMIN PANEL HANDLERS ====================
    elif data == "admin_panel":
        if user_id not in SUDO_USERS: return
        user_states.pop(user_id, None)
        await query.message.edit_text("⚙️ **Admin Dashboard**", reply_markup=get_admin_panel_keyboard(user_id))

    elif data == "admin_maint_panel":
        if user_id not in SUDO_USERS: return
        user_states.pop(user_id, None)
        is_active, reason = await get_maintenance_status()
        status_text = "🟢 **ONLINE (Active)**" if not is_active else "🔴 **MAINTENANCE MODE (Paused)**"
        kb = await get_maintenance_panel_keyboard()
        await query.message.edit_text(
            f"🛠️ **MAINTENANCE CONTROL PANEL**\n\n"
            f"📊 **Current Status:** {status_text}\n"
            f"📝 **Reason / Message:**\n`{reason}`\n\n"
            f"Toggle maintenance status or modify the maintenance message using options below:",
            reply_markup=kb
        )

    elif data == "adm_toggle_maint":
        if user_id not in SUDO_USERS: return
        is_active, reason = await get_maintenance_status()
        new_state = not is_active
        await set_maintenance_status(new_state)
        
        state_str = "ENABLED" if new_state else "DISABLED"
        await query.answer(f"✅ Maintenance Mode {state_str}!", show_alert=True)
        
        status_text = "🟢 **ONLINE (Active)**" if not new_state else "🔴 **MAINTENANCE MODE (Paused)**"
        kb = await get_maintenance_panel_keyboard()
        await query.message.edit_text(
            f"🛠️ **MAINTENANCE CONTROL PANEL**\n\n"
            f"📊 **Current Status:** {status_text}\n"
            f"📝 **Reason / Message:**\n`{reason}`\n\n"
            f"Toggle maintenance status or modify the maintenance message using options below:",
            reply_markup=kb
        )

    elif data == "adm_change_maint_reason":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_MAINT_REASON"
        await query.message.edit_text(
            "✏️ **SET MAINTENANCE REASON / TEXT**\n\n"
            "Send the text or reason to show users when maintenance mode is active:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Maintenance Panel", callback_data="admin_maint_panel")]])
        )

    elif data == "admin_stats":
        if user_id not in SUDO_USERS: return
        await query.answer("📊 Calculating revenue & stats...", show_alert=False)
        
        total_users = await users_col.count_documents({})
        banned_users = await users_col.count_documents({"is_banned": True})
        
        available_stock = await accounts_col.count_documents({"status": "AVAILABLE"})
        sold_stock = await accounts_col.count_documents({"status": "SOLD"})
        
        revenue_pipeline = [
            {"$match": {"status": "SOLD"}},
            {"$group": {
                "_id": None,
                "total_rev": {"$sum": "$price"},
                "total_cb": {"$sum": "$cashback"}
            }}
        ]
        rev_res = await accounts_col.aggregate(revenue_pipeline).to_list(length=1)
        
        total_revenue = rev_res[0]["total_rev"] if rev_res else 0.0
        total_cashback_issued = rev_res[0]["total_cb"] if rev_res else 0.0

        user_cb_pipeline = [
            {"$group": {
                "_id": None,
                "total_claimed_cb": {"$sum": "$profile_cashback"}
            }}
        ]
        user_cb_res = await users_col.aggregate(user_cb_pipeline).to_list(length=1)
        profile_cb_claimed = user_cb_res[0]["total_claimed_cb"] if user_cb_res else 0.0

        stats_text = (
            f"📊 **BOT STATISTICS & REVENUE METRICS**\n\n"
            f"💰 **Total Revenue:** ₹{total_revenue:.2f}\n"
            f"🎁 **Total Cashback Issued:** ₹{total_cashback_issued:.2f}\n"
            f"👤 **Profile Cashback Claimed:** ₹{profile_cb_claimed:.2f}\n\n"
            f"📦 **Available Stock:** {available_stock} accounts\n"
            f"🛍️ **Total Accounts Sold:** {sold_stock} accounts\n\n"
            f"👥 **Total Registered Users:** {total_users}\n"
            f"🚫 **Banned Users:** {banned_users}\n"
            f"👨‍💻 **Total Admins:** {len(SUDO_USERS)}"
        )
        await query.message.edit_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_user_history":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_GET_USER_HISTORY"
        await query.message.edit_text(
            "ℹ️ **FETCH USER DETAILS & HISTORY**\n\n"
            "Send the **User ID** of the user whose complete information and history you want to check:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_add_acc":
        if user_id not in SUDO_USERS: return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Temporary Spam", callback_data="adm_cat_Temporary Spam")],
            [InlineKeyboardButton("🚫 Permanent Spam", callback_data="adm_cat_Permanent Spam")],
            [InlineKeyboardButton("✨ Fresh Account", callback_data="adm_cat_Fresh Account")],
            [InlineKeyboardButton("📜 Old Account", callback_data="adm_cat_Old Account")],
            [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
        ])
        await query.message.edit_text("📂 **Select Account Category:**", reply_markup=kb)

    elif data == "admin_remove_stock":
        if user_id not in SUDO_USERS: return
        
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price"
                },
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.answer("❌ No active stock currently available!", show_alert=True)
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            cat = info.get("category", "General")
            country = info["country"]
            year = info["year"]
            price = info["price"]
            count = s["count"]

            btn_label = f"🗑️ Delete [{cat}] {country} ({year}) | ₹{price} | 📦 Count: {count}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_rmstock_confirm_{cat}_{country}_{year}_{price}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
        await query.message.edit_text("🗑️ **Select Stock Item to Remove/Delete:**\n\n*(Clicking a button will immediately remove the stock item from availability)*", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_rmstock_confirm_"):
        if user_id not in SUDO_USERS: return
        parts = data.split("_")
        cat, country, year, price = parts[3], parts[4], parts[5], float(parts[6])

        res = await accounts_col.delete_many(
            {"category": cat, "country": country, "year": year, "price": price, "status": "AVAILABLE"}
        )

        await query.answer(f"✅ Removed {res.deleted_count} items from stock!", show_alert=True)

        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price"
                },
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.message.edit_text(
                "✅ **All active stock items have been cleared.**",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
            )
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            cat_n = info.get("category", "General")
            c_n = info["country"]
            y_n = info["year"]
            p_n = info["price"]
            count_n = s["count"]

            btn_label = f"🗑️ Delete [{cat_n}] {c_n} ({y_n}) | ₹{p_n} | 📦 Count: {count_n}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_rmstock_confirm_{cat_n}_{c_n}_{y_n}_{p_n}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
        await query.message.edit_text(
            f"✅ **Stock removed successfully! ({res.deleted_count} items deleted)**\n\nSelect another stock to remove:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "admin_change_price":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can change stock prices!", show_alert=True)
            return

        user_states.pop(user_id, None)
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price"
                },
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.answer("❌ No Stock Available to change price!", show_alert=True)
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            cat = info.get("category", "General")
            country = info["country"]
            year = info["year"]
            price = info["price"]
            count = s["count"]

            btn_label = f"📁 [{cat}] {country} ({year}) - Current: ₹{price} | Stock: {count}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_chgprice_sel_{cat}_{country}_{year}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
        await query.message.edit_text("🏷️ **Select Stock Item to Change Price:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_chgprice_sel_"):
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can change stock prices!", show_alert=True)
            return

        parts = data.split("_")
        cat, country, year = parts[3], parts[4], parts[5]
        
        temp_data[user_id] = {"chg_cat": cat, "chg_country": country, "chg_year": year}
        user_states[user_id] = "ADM_STEP_WAIT_NEW_PRICE"

        await query.message.edit_text(
            f"🏷️ **Changing Price for:**\n"
            f"📁 Category: `{cat}`\n"
            f"🌍 Item: `{country} ({year})`\n\n"
            f"🔢 **Enter the new Price (in ₹):**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_edit_bal":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can edit balance!", show_alert=True)
            return
        user_states[user_id] = "ADM_STEP_EDIT_BAL"
        await query.message.edit_text(
            "✏️ **ADD USER BALANCE**\n\n"
            "Send User ID and Balance to Add separated by space.\n"
            "Format: `UserID BalanceToAdd`\n"
            "Example: `123456789 5`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_manage_sudo":
        if user_id != OWNER_ID:
            await query.answer("🚫 Owner Only Access!", show_alert=True)
            return
        user_states.pop(user_id, None)
        kb = await get_manage_sudo_keyboard()
        await query.message.edit_text("👥 **MANAGE ADMINS**\n\nClick **➕ Add Admin** or click on any existing Admin ID to **Remove** them:", reply_markup=kb)

    elif data == "adm_add_sudo_btn":
        if user_id != OWNER_ID: return
        user_states[user_id] = "ADM_STEP_INPUT_ADD_SUDO"
        await query.message.edit_text(
            "➕ **ADD NEW ADMIN**\n\nSend the **Telegram User ID** of the person you want to make Admin:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Manage Admins", callback_data="admin_manage_sudo")]])
        )

    elif data.startswith("adm_rem_sudo_"):
        if user_id != OWNER_ID: return
        target_id = int(data.split("_")[3])
        await remove_sudo_user(target_id)
        await query.answer(f"🗑️ Removed Admin {target_id}", show_alert=True)
        kb = await get_manage_sudo_keyboard()
        await query.message.edit_text("👥 **MANAGE ADMINS**\n\nClick **➕ Add Admin** or click on any existing Admin ID to **Remove** them:", reply_markup=kb)

    elif data.startswith("adm_cat_"):
        if user_id not in SUDO_USERS: return
        cat = data.split("_")[2]
        temp_data[user_id] = {"category": cat}
        user_states[user_id] = "ADM_STEP_COUNTRY"
        await query.message.edit_text(
            f" Selected Category: **{cat}**\n\n📝 **Step 1:** Enter Country Name (e.g. `India`, `USA`):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_broadcast":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BROADCAST"
        await query.message.edit_text(
            "📢 **Send the message you want to Broadcast in DM to all users:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_ban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BAN_ID"
        await query.message.edit_text(
            "🚫 **Enter User ID or @Username to Ban:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_unban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_UNBAN_ID"
        await query.message.edit_text(
            "🟢 **Enter User ID or @Username to Unban:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data.startswith("adm_app_dep_"):
        if user_id not in SUDO_USERS: return
        _, _, _, dep_user_id, amount, req_id = data.split("_")
        dep_user_id = int(dep_user_id)
        amount = float(amount)

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "APPROVED"}}
        )

        if not res:
            await query.answer("⚠️ Already processed by another admin!", show_alert=True)
            return

        await update_balance(dep_user_id, amount)
        admin_mention = query.from_user.mention
        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ **APPROVED (+₹{amount:.2f})** by {admin_mention}")
        await app.send_message(dep_user_id, f"🎉 **Deposit Approved!** ₹{amount:.2f} credited to your wallet.")
        
        log_text = (
            f"💳 **NEW DEPOSIT APPROVED!**\n\n"
            f"👤 **User ID:** `{dep_user_id}`\n"
            f"💰 **Amount Credited:** ₹{amount:.2f}\n"
            f"👨‍💻 **Approved By Admin:** {admin_mention}\n\n"
            f"📌 **Status:** Wallet Balance Added"
        )
        await log_to_channel(log_text, reply_markup=get_buy_now_keyboard())

    elif data.startswith("adm_rej_dep_"):
        if user_id not in SUDO_USERS: return
        _, _, _, dep_user_id, req_id = data.split("_")
        dep_user_id = int(dep_user_id)

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "REJECTED"}}
        )

        if not res:
            await query.answer("⚠️ Already processed by another admin!", show_alert=True)
            return

        admin_mention = query.from_user.mention
        await query.message.edit_caption(caption=query.message.caption + f"\n\n❌ **REJECTED** by {admin_mention}")
        await app.send_message(dep_user_id, "❌ Your deposit request was rejected by Admin.")

    elif data.startswith("own_app_wth_"):
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can process withdrawals!", show_alert=True)
            return
        _, _, _, w_user_id, amount, req_id = data.split("_")
        w_user_id = int(w_user_id)
        amount = float(amount)

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "APPROVED"}}
        )

        if not res:
            await query.answer("⚠️ Request already actioned!", show_alert=True)
            return

        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ **WITHDRAW SUCCESSFUL! Money Sent.**")
        await app.send_message(w_user_id, f"🎉 **Withdrawal Successful!** ₹{amount:.2f} has been transferred to your QR code.")

        log_text = (
            f"💸 **NEW WITHDRAWAL PROCESSED!**\n\n"
            f"👤 **User ID:** `{w_user_id}`\n"
            f"💵 **Amount Paid:** ₹{amount:.2f}\n\n"
            f"📌 **Status:** Withdrawal Completed"
        )
        await log_to_channel(log_text, reply_markup=get_buy_now_keyboard())

# ==================== PHOTO RECEIVER ====================
@app.on_message(filters.photo & filters.private)
async def photo_receiver(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS:
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}")
        return

    if state == "WAIT_DEPOSIT_PHOTO":
        temp_data[user_id]["photo_id"] = message.photo.file_id
        user_states[user_id] = "WAIT_DEPOSIT_TXN_ID"
        await message.reply_text("🧾 **Now enter the Transaction ID / UTR Number:**")

    elif state == "WAIT_WITHDRAW_QR":
        qr_photo_id = message.photo.file_id
        amount = temp_data[user_id]["withdraw_amount"]
        user_states.pop(user_id, None)

        await update_balance(user_id, -amount)

        req_doc = {"type": "WITHDRAW", "status": "PENDING"}
        req_res = await requests_col.insert_one(req_doc)
        req_id = str(req_res.inserted_id)

        await message.reply_text("⏳ **Withdrawal request submitted! Sent to Owner for payment processing.**")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Send Money & Approve", callback_data=f"own_app_wth_{user_id}_{amount}_{req_id}")]
        ])

        caption = (
            f"💸 **NEW WITHDRAWAL REQUEST (OWNER ONLY)**\n\n"
            f"👤 **User:** {message.from_user.mention} (`{user_id}`)\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"📌 **Status:** Pending payment to QR below."
        )

        try:
            await app.send_photo(chat_id=OWNER_ID, photo=qr_photo_id, caption=caption, reply_markup=kb)
        except Exception as e:
            logging.error(f"Failed to send withdraw req to Owner: {e}")

# ==================== STEP-BY-STEP INPUT ROUTER ====================
@app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "admin"]))
async def text_router(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS and not state.startswith("ADM_"):
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}")
        return

    if not state:
        return

    if state == "ADM_STEP_GET_USER_HISTORY":
        if user_id not in SUDO_USERS: return
        try:
            target_id = int(message.text.strip())
            u_data = await users_col.find_one({"user_id": target_id})
            
            if not u_data:
                await message.reply_text(
                    f"❌ **User ID `{target_id}` not found in Database!**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
                )
                user_states.pop(user_id, None)
                return

            purchased_accs = await accounts_col.find({"sold_to": target_id, "status": "SOLD"}).to_list(length=100)
            
            history_text = ""
            if purchased_accs:
                history_text = "\n\n📦 **Purchased Accounts History:**\n"
                for idx, acc in enumerate(purchased_accs, 1):
                    history_text += f"{idx}. `{acc.get('phone_number')}` | {acc.get('category')} ({acc.get('country')} {acc.get('year')}) | ₹{acc.get('price', 0.0):.2f}\n"
            else:
                history_text = "\n\n📦 **Purchased Accounts History:** No accounts purchased yet."

            status_ban = "🚫 Banned" if u_data.get("is_banned", False) else "🟢 Active"
            ban_reason = f"\n⚠️ **Ban Reason:** {u_data.get('ban_reason')}" if u_data.get("is_banned", False) else ""

            info_msg = (
                f"👤 **USER DETAILED INFO & HISTORY**\n\n"
                f"🆔 **User ID:** `{target_id}`\n"
                f"📌 **Account Status:** {status_ban}{ban_reason}\n"
                f"💵 **Wallet Balance:** ₹{u_data.get('balance', 0.0):.2f}\n"
                f"🎁 **Profile Cashback:** ₹{u_data.get('profile_cashback', 0.0):.2f}\n"
                f"🛍️ **Total Accounts Bought:** {len(purchased_accs)}"
                f"{history_text}"
            )

            user_states.pop(user_id, None)
            await message.reply_text(info_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]]))
            
        except ValueError:
            await message.reply_text("❌ Invalid User ID! Please enter numeric User ID only:")

    elif state == "ADM_STEP_MAINT_REASON":
        if user_id not in SUDO_USERS: return
        new_reason = message.text.strip()
        is_active, _ = await get_maintenance_status()
        await set_maintenance_status(is_active, new_reason)
        user_states.pop(user_id, None)
        kb = await get_maintenance_panel_keyboard()
        await message.reply_text(f"✅ **Maintenance Reason Updated!**\n\n`{new_reason}`", reply_markup=kb)

    elif state == "WAIT_WITHDRAW_AMOUNT":
        try:
            amount = float(message.text.strip())
            bal = await get_user_balance(user_id)

            if amount < MIN_WITHDRAW:
                await message.reply_text(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAW:.2f}:")
                return

            if amount > bal:
                await message.reply_text(f"❌ Insufficient wallet balance! Available: ₹{bal:.2f}:")
                return

            temp_data[user_id] = {"withdraw_amount": amount}
            user_states[user_id] = "WAIT_WITHDRAW_QR"
            await message.reply_text("📸 **Now send your Payment QR Code Photo:**")

        except ValueError:
            await message.reply_text("❌ Enter numbers only:")

    elif state == "WAIT_DEPOSIT_AMOUNT_INPUT":
        try:
            amount = float(message.text.strip())
            if amount < MIN_DEPOSIT:
                await message.reply_text(f"❌ **Minimum Deposit limit is ₹{MIN_DEPOSIT:.2f}.**")
                return

            temp_data[user_id] = {"amount": amount}
            user_states[user_id] = "WAIT_DEPOSIT_PHOTO"
            
            qr_image = generate_upi_qr(UPI_ID_TEXT, PAYEE_NAME, amount)
            await app.send_photo(
                chat_id=user_id,
                photo=qr_image,
                caption=f"💳 **Send ₹{amount:.2f} to UPI ID:** `{UPI_ID_TEXT}`\n\nSend payment screenshot here."
            )
        except ValueError:
            await message.reply_text("❌ Invalid input!")

    elif state == "WAIT_DEPOSIT_TXN_ID":
        txn_id = message.text.strip()
        data = temp_data[user_id]
        photo_id = data["photo_id"]
        amount = data["amount"]
        user_states.pop(user_id, None)

        req_doc = {"type": "DEPOSIT", "status": "PENDING"}
        req_res = await requests_col.insert_one(req_doc)
        req_id = str(req_res.inserted_id)

        await message.reply_text("⏳ **Deposit proof submitted! Admins are verifying your payment.**")
        
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_dep_{user_id}_{amount}_{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_dep_{user_id}_{req_id}")
            ]
        ])

        deposit_caption = (
            f"📥 **NEW DEPOSIT VERIFICATION REQUEST**\n\n"
            f"👤 **User:** {message.from_user.mention} (`{user_id}`)\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"🧾 **Transaction ID / UTR:** `{txn_id}`"
        )

        for sudo_id in SUDO_USERS:
            try:
                await app.send_photo(chat_id=sudo_id, photo=photo_id, caption=deposit_caption, reply_markup=kb)
            except Exception as e:
                logging.error(f"Failed sending DM to Admin {sudo_id}: {e}")

    elif state == "ADM_STEP_WAIT_NEW_PRICE":
        if user_id != OWNER_ID:
            await message.reply_text("🚫 Only Owner can change prices!")
            return

        try:
            new_price = float(message.text.strip())
            c_info = temp_data[user_id]
            cat, country, year = c_info["chg_cat"], c_info["chg_country"], c_info["chg_year"]

            res = await accounts_col.update_many(
                {"category": cat, "country": country, "year": year, "status": "AVAILABLE"},
                {"$set": {"price": new_price}}
            )

            user_states.pop(user_id, None)
            temp_data.pop(user_id, None)

            await message.reply_text(
                f"✅ **Price Updated!**\n\nUpdated price for `{cat}` ({country} {year}) to **₹{new_price:.2f}** ({res.modified_count} accounts affected).",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Price must be a valid number! Try again:")

    elif state == "ADM_STEP_EDIT_BAL":
        if user_id != OWNER_ID:
            await message.reply_text("🚫 Only Owner can edit balance!")
            return

        try:
            parts = message.text.strip().split()
            if len(parts) != 2:
                await message.reply_text("❌ Invalid Format! Use: `UserID BalanceToAdd`")
                return

            t_user_id = int(parts[0])
            add_amount = float(parts[1])

            await update_balance(t_user_id, add_amount)
            new_total = await get_user_balance(t_user_id)
            
            user_states.pop(user_id, None)
            await message.reply_text(
                f"✅ **Balance Added Successfully!**\n\n"
                f"👤 User ID: `{t_user_id}`\n"
                f"➕ Added Amount: ₹{add_amount:.2f}\n"
                f"💰 New Balance: ₹{new_total:.2f}",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Check your input numbers!")

    elif state == "ADM_STEP_INPUT_ADD_SUDO":
        if user_id != OWNER_ID: return
        try:
            target_id = int(message.text.strip())
            await add_sudo_user(target_id)
            user_states.pop(user_id, None)
            kb = await get_manage_sudo_keyboard()
            await message.reply_text(f"✅ User `{target_id}` added to Admins!", reply_markup=kb)
        except ValueError:
            await message.reply_text("❌ Invalid User ID! Enter numbers only:")

    elif state == "ADM_STEP_BROADCAST":
        user_states.pop(user_id, None)
        broadcast_msg = message.text
        cursor = users_col.find({"is_banned": False})
        users = await cursor.to_list(length=10000)

        success = 0
        failed = 0
        await message.reply_text(f"⏳ **Starting broadcast to {len(users)} users...**")

        for u in users:
            try:
                await app.send_message(u["user_id"], broadcast_msg)
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await message.reply_text(f"✅ **Broadcast Completed!**\n\n🟢 Delivered: {success}\n🔴 Failed: {failed}", reply_markup=get_admin_panel_keyboard(user_id))

    elif state == "ADM_STEP_BAN_ID":
        try:
            target_user = (await client.get_users(message.text.strip())).id

            if target_user == OWNER_ID or target_user in SUDO_USERS:
                await message.reply_text("❌ You cannot ban yourself or another admin.")
                user_states.pop(user_id, None)
                return

            temp_data[user_id] = {"target_ban_user": target_user}
            user_states[user_id] = "ADM_STEP_BAN_REASON"
            await message.reply_text(f"👤 Target User ID: `{target_user}`\n\n📝 **Enter Reason for Ban:**")
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

    elif state == "ADM_STEP_BAN_REASON":
        reason = message.text.strip()
        target_user = temp_data[user_id]["target_ban_user"]
        user_states.pop(user_id, None)

        await users_col.update_one({"user_id": target_user}, {"$set": {"is_banned": True, "ban_reason": reason}}, upsert=True)
        await message.reply_text(f"🚫 User `{target_user}` banned.\n**Reason:** {reason}", reply_markup=get_admin_panel_keyboard(user_id))
        
        try:
            await app.send_message(target_user, f"🚫 **You have been banned from the bot.**\n\n**Reason:** {reason}")
        except Exception:
            pass

    elif state == "ADM_STEP_UNBAN_ID":
        try:
            target_user = (await client.get_users(message.text.strip())).id
            user_states.pop(user_id, None)
            await users_col.update_one({"user_id": target_user}, {"$set": {"is_banned": False, "ban_reason": ""}})
            await message.reply_text(f"🟢 User `{target_user}` is now Unbanned.", reply_markup=get_admin_panel_keyboard(user_id))
            try:
                await app.send_message(target_user, "🎉 **Your account has been unbanned by Admin!** You can use the bot again.")
            except Exception:
                pass
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

    elif state == "ADM_STEP_COUNTRY":
        temp_data[user_id]["country"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_YEAR"
        await message.reply_text("📅 **Step 2:** Enter Account Creation Year (e.g. `2022`, `2024`):")

    elif state == "ADM_STEP_YEAR":
        temp_data[user_id]["year"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_PRICE"
        await message.reply_text("💵 **Step 3:** Enter Account Price (₹):")

    elif state == "ADM_STEP_PRICE":
        try:
            temp_data[user_id]["price"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_CASHBACK_VAL"
            await message.reply_text("🎁 **Enter Cashback Amount (If none, type `0`):**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_CASHBACK_VAL":
        try:
            temp_data[user_id]["cashback"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_PHONE"
            await message.reply_text("📞 **Enter Account Phone Number (with Country Code e.g. `+1234567890`):**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_PHONE":
        temp_data[user_id]["phone"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_2FA"
        await message.reply_text("🔑 **Enter 2FA Password (If none, type `None`):**")

    elif state == "ADM_STEP_2FA":
        temp_data[user_id]["two_fa"] = message.text.strip()
        phone = temp_data[user_id]["phone"]

        await message.reply_text(f"⏳ Triggering Telegram OTP request to `{phone}`...")
        t_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await t_client.connect()
        await t_client.send_code_request(phone)

        temp_data[user_id]["client"] = t_client
        user_states[user_id] = "ADM_STEP_OTP"
        await message.reply_text("📲 **Enter Telegram OTP code received:**")

    elif state == "ADM_STEP_OTP":
        otp = message.text.strip()
        data = temp_data[user_id]
        t_client = data["client"]
        
        try:
            try:
                await t_client.sign_in(data["phone"], otp)
            except SessionPasswordNeededError:
                if data["two_fa"] and data["two_fa"] != "None":
                    await t_client.sign_in(password=data["two_fa"])
                else:
                    await message.reply_text("❌ **2FA Password Required!**")
                    await t_client.disconnect()
                    return

            session_str = t_client.session.save()
            await t_client.disconnect()

            acc_doc = {
                "category": data["category"],
                "country": data["country"],
                "year": data["year"],
                "price": data["price"],
                "cashback": data["cashback"],
                "phone_number": data["phone"],
                "session_string": session_str,
                "two_fa": data["two_fa"],
                "status": "AVAILABLE",
                "sold_to": None
            }
            await accounts_col.insert_one(acc_doc)

            user_states.pop(user_id, None)
            await message.reply_text(
                f"✅ **Account Added to MongoDB Stock!**\n\n"
                f"📂 **Category:** {data['category']}\n"
                f"🌍 **Location:** {data['country']} ({data['year']})\n"
                f"📞 **Phone:** `{data['phone']}`",
                reply_markup=get_admin_panel_keyboard(user_id)
            )

        except Exception as e:
            await message.reply_text(f"❌ Error during sign in: `{e}`")

# ==================== START SERVER ====================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    print("🚀 Mongo Engine Activated!")
    app.run()
