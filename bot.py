import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ============================================================
# 1. CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "359999840"))
DB_PATH = Path(
    os.environ.get(
        "DATABASE_PATH",
        str(Path(__file__).resolve().with_name("perfumes_shop.db")),
    )
)
ADVANCE_PAYMENT = 500  # ETB required as pre-payment to confirm an order
LOW_STOCK_THRESHOLD = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# httpx includes the full Telegram API URL (and bot token) in INFO messages.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============================================================
# 2. CONVERSATION STATES
# (kept in clearly separate numeric ranges so the two flows
#  can never be confused with one another)
# ============================================================
# --- Customer ordering flow ---
PHONE, ADDRESS, SCREENSHOT = range(0, 3)

# --- Admin: add new perfume wizard ---
ADD_NAME, ADD_SIZE, ADD_PRICE, ADD_STOCK, ADD_DESC, ADD_PHOTO, ADD_MORE = range(3, 10)

# --- Admin: quick edit price/stock ---
EDIT_VALUE = 10


# ============================================================
# 3. DATABASE
# ============================================================
def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT,
            stock INTEGER NOT NULL DEFAULT 0,
            size TEXT,
            photo_id TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            product_name TEXT,
            price REAL,
            phone TEXT,
            address TEXT,
            order_date DATE,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    ''')

    # --- Auto-migration for older DB files created by earlier bot versions ---
    cursor.execute("PRAGMA table_info(products)")
    prod_cols = [c[1] for c in cursor.fetchall()]
    if "stock" not in prod_cols:
        cursor.execute("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 0")
    if "size" not in prod_cols:
        cursor.execute("ALTER TABLE products ADD COLUMN size TEXT")
    if "photo_id" not in prod_cols:
        cursor.execute("ALTER TABLE products ADD COLUMN photo_id TEXT")
    if "is_active" not in prod_cols:
        cursor.execute("ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")

    cursor.execute("PRAGMA table_info(orders)")
    order_cols = [c[1] for c in cursor.fetchall()]
    if "product_id" not in order_cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN product_id INTEGER")
    if "status" not in order_cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")

    # Seed sample data only on a brand new, empty database
    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] == 0:
        sample_products = [
            ("Dior Sauvage", 4500, "ለወንዶች የሚሆን የሚስብ መዓዛ ያለው", 10, "100ml", None, 1),
            ("Bleu De Chanel", 5200, "ቆንጆና የማይቀየር ምርጥ ሽቶ", 8, "100ml", None, 1),
            ("Tom Ford Black Orchid", 6000, "ለየት ያለ ማራኪ ጠረን", 5, "50ml", None, 1),
            ("Victoria's Secret Bombshell", 3800, "ለሴቶች የሚሆን በጣም ተወዳጅ ሽቶ", 0, "100ml", None, 1),
        ]
        cursor.executemany(
            "INSERT INTO products (name, price, description, stock, size, photo_id, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            sample_products,
        )

    conn.commit()
    conn.close()


# ============================================================
# 4. HELPERS
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


def format_price(value) -> str:
    """Show whole numbers without a trailing .0, keep decimals otherwise."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def parse_number(text: str):
    """Return a float if text is a valid positive number, else None."""
    text = text.strip().replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Add New Perfume", callback_data="admin_add_perfume")],
        [InlineKeyboardButton("💰 Edit Prices & Stock", callback_data="admin_edit_menu")],
        [InlineKeyboardButton("🛍️ View Catalog", callback_data="admin_view_catalog")],
        [InlineKeyboardButton("🗑️ Delete / Manage Inventory", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📊 Sales & Order Summary", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_admin_menu(chat_id, context: ContextTypes.DEFAULT_TYPE, text=None):
    await context.bot.send_message(
        chat_id=chat_id,
        text=text or "⚙️ **Admin Control Panel**\n\nምን መስራት ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=admin_main_menu_keyboard(),
    )


# ============================================================
# 5. CUSTOMER-FACING HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user

    if is_admin(user.id):
        await update.message.reply_text(f"ሰላም አድሚን {user.first_name}! 👋")
        await send_admin_menu(update.effective_chat.id, context)
        return

    welcome_text = (
        f"ሰላም {user.first_name}! 👋\n\n"
        "እንኳን ወደ እኛ የሽቶ መደብር በደህና መጡ! 🌸\n"
        "የምንፈልገውን ሽቶ መርጠው በቀላሉ ማዘዝ ይችላሉ።\n\n"
        "ሽቶዎችን ለማየት ከታች ያለውን ቁልፍ ይጫኑ፦"
    )
    keyboard = [[InlineKeyboardButton("🛍️ የሽቶዎች ዝርዝር (Catalog)", callback_data="show_catalog")]]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, price, description, stock, size, photo_id "
        "FROM products WHERE is_active = 1 ORDER BY id"
    )
    products = cursor.fetchall()
    conn.close()

    await query.edit_message_text("👇 የሚፈልጉትን ሽቶ ይምረጡ፦")

    if not products:
        await context.bot.send_message(chat_id=query.message.chat_id, text="በአሁኑ ሰዓት ምንም ሽቶ አልተመዘገበም።")
        return

    for p_id, name, price, desc, stock, size, photo_id in products:
        display_name = f"{name} ({size})" if size else name

        if stock > 0:
            stock_line = f"📦 **ያለው ብዛት:** {stock}"
            button_text = "🛒 አዘዝ (Order Now)"
            callback = f"buy_{p_id}"
        else:
            stock_line = "❌ **ያለቀ (Out of Stock)**"
            button_text = "🚫 አልቋል (Out of Stock)"
            callback = "out_of_stock"

        caption = (
            f"✨ **{display_name}**\n"
            f"💰 **ዋጋ:** {format_price(price)} ETB\n"
            f"📝 {desc or '-'}\n"
            f"{stock_line}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data=callback)]])

        if photo_id:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )


async def out_of_stock_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("ይቅርታ፣ ይህ ሽቶ በአሁኑ ሰዓት አልቋል።", show_alert=True)


async def select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    product_id = query.data.split("_")[1]

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, price, stock, size FROM products WHERE id = ? AND is_active = 1",
        (product_id,),
    )
    product = cursor.fetchone()
    conn.close()

    if product is None:
        await query.answer("ይህ ሽቶ አልተገኘም።", show_alert=True)
        return ConversationHandler.END

    name, price, stock, size = product
    if stock <= 0:
        await query.answer("ይቅርታ፣ ይህ ሽቶ በአሁኑ ሰዓት አልቋል።", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    # Namespaced keys so this never collides with the admin add-product flow's user_data
    context.user_data["order_product_id"] = product_id
    context.user_data["order_product_name"] = f"{name} ({size})" if size else name
    context.user_data["order_price"] = price

    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ስልክ ቁጥሬን አጋራ", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await query.message.reply_text(
        f"🎯 **የመረጡት ሽቶ:** {context.user_data['order_product_name']}\n"
        f"💰 **ዋጋ:** {format_price(price)} ETB\n\n"
        "እባክዎን ትዕዛዝዎን ለማጠናቀቅ **የስልክ ቁጥርዎን** ያስገቡ (ወይም 'ስልክ ቁጥሬን አጋራ' የሚለውን ይጫኑ)፦",
        parse_mode="Markdown",
        reply_markup=contact_keyboard,
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text

    context.user_data["order_phone"] = phone

    await update.message.reply_text(
        "እሺ አግኝተነዋል! 👍\nበመቀጠል **እቃው የሚደርስበትን አድራሻ** (ምሳሌ፦ ቦሌ፣ አትላስ) ፅፈው ይላኩ፦",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADDRESS


async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order_address"] = update.message.text

    payment_instruction = (
        "💳 **የቅድመ-ክፍያ ማረጋገጫ (Advance Payment)**\n\n"
        f"ትዕዛዝዎን ለማረጋገጥ እባክዎን **{ADVANCE_PAYMENT} ETB** በ Telebirr ወይም በባንክ ሂሳባችን ገቢ ያድርጉ።\n\n"
        "📲 **Telebirr / CBE Accounts:**\n"
        "• Telebirr: 0912345678 (ስም)\n"
        "• CBE Bank: 1000123456789 (ስም)\n\n"
        "📌 ክፍያውን እንደፈፀሙ **የክፍያውን ደረሰኝ (Screenshot/ፎቶ)** እዚህ ይላኩልን።"
    )
    await update.message.reply_text(payment_instruction, parse_mode="Markdown")
    return SCREENSHOT


async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id

    user_id = update.effective_user.id
    product_id = context.user_data.get("order_product_id")
    product_name = context.user_data.get("order_product_name")
    price = context.user_data.get("order_price")
    phone = context.user_data.get("order_phone")
    address = context.user_data.get("order_address")
    today = date.today().isoformat()

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT stock FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    current_stock = row[0] if row else 0

    if current_stock <= 0:
        conn.close()
        await update.message.reply_text(
            "ይቅርታ፣ ይህ ሽቶ ገቢ ሲደረግልዎት ተጠናቅቆ ነበር። እባክዎ ሌላ ሽቶ ይምረጡ ወይም ድጋፍ ያግኙ።"
        )
        context.user_data.clear()
        return ConversationHandler.END

    cursor.execute(
        "INSERT INTO orders (user_id, product_id, product_name, price, phone, address, order_date, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (user_id, product_id, product_name, price, phone, address, today),
    )
    cursor.execute("UPDATE products SET stock = stock - 1 WHERE id = ? AND stock > 0", (product_id,))
    cursor.execute("SELECT stock FROM products WHERE id = ?", (product_id,))
    remaining_stock = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🙏 <b>እናመሰግናለን!</b>\n\n"
        "✅ <b>ትዕዛዝዎ በስኬት ደርሶናል!</b>\n"
        "📦 እቃዎ <b>በነገው ዕለት</b> የሚደርስዎት ሲሆን፣ የዴሊቨሪ አጋራችን ከመምጣቱ በፊት በስልክ ቁጥርዎ ይደውልልዎታል።",
        parse_mode="HTML",
    )

    admin_notification = (
        "🚨 **አዲስ ትዕዛዝ ገብቷል!** 🚨\n\n"
        f"🛍️ **የተመረጠው ሽቶ:** {product_name}\n"
        f"💰 **ሙሉ ዋጋ:** {format_price(price)} ETB\n"
        f"📱 **ስልክ ቁጥር:** {phone}\n"
        f"📍 **ማድረሻ አድራሻ:** {address}\n"
        f"💵 **ቅድመ ክፍያ:** {ADVANCE_PAYMENT} ETB (ደረሰኝ ከታች ተያይዟል)\n"
        f"📦 **የቀረ ስቶክ:** {remaining_stock}"
    )
    if remaining_stock <= 0:
        admin_notification += "\n\n⚠️ **ይህ ሽቶ አሁን ሙሉ በሙሉ አልቋል! እባክዎ ስቶክ ያድሱ።**"
    elif remaining_stock <= LOW_STOCK_THRESHOLD:
        admin_notification += f"\n\n⚠️ **ትኩረት፦ ስቶኩ እያለቀ ነው ({remaining_stock} ቀርቷል)።**"

    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=photo_file_id,
        caption=admin_notification,
        parse_mode="Markdown",
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Critical fix: always wipe state so nothing leaks into the next flow
    context.user_data.clear()
    await update.message.reply_text(
        "ትዕዛዙ/ሂደቱ ተሰርዟል። እንደገና ለማዘዝ /start ይበሉ።",
        reply_markup=ReplyKeyboardRemove(),
    )
    if is_admin(update.effective_user.id):
        await send_admin_menu(update.effective_chat.id, context)
    return ConversationHandler.END


# ============================================================
# 6. ADMIN PANEL NAVIGATION
# ============================================================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return
    await send_admin_menu(update.effective_chat.id, context)


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    if not is_admin(query.from_user.id):
        return
    await query.edit_message_text("⚙️ **Admin Control Panel**", parse_mode="Markdown")
    await send_admin_menu(query.message.chat_id, context, text="ምን መስራት ይፈልጋሉ?")


async def admin_view_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets the admin preview exactly what customers see, including hidden/out-of-stock items."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, price, description, stock, size, photo_id, is_active FROM products ORDER BY id"
    )
    products = cursor.fetchall()
    conn.close()

    if not products:
        await context.bot.send_message(chat_id=query.message.chat_id, text="ምንም ምርት አልተመዘገበም።")
        return

    for p_id, name, price, desc, stock, size, photo_id, is_active in products:
        display_name = f"{name} ({size})" if size else name
        if not is_active:
            status = "🙈 **ተደብቋል (Hidden)**"
        elif stock > 0:
            status = f"📦 **ያለው ብዛት:** {stock}"
        else:
            status = "❌ **ያለቀ (Out of Stock)**"

        caption = (
            f"#{p_id} ✨ **{display_name}**\n"
            f"💰 {format_price(price)} ETB\n"
            f"📝 {desc or '-'}\n"
            f"{status}"
        )
        if photo_id:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_id, caption=caption, parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=caption, parse_mode="Markdown")

    await send_admin_menu(query.message.chat_id, context, text="⬆️ ይህ ደንበኞች የሚያዩት ካታሎግ ነው።")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return
    await send_stats(update.effective_chat.id, context)


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    await send_stats(query.message.chat_id, context)


async def send_stats(chat_id, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM orders")
    total_orders, total_revenue = cursor.fetchone()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM orders WHERE order_date = ?", (today,))
    today_orders, today_revenue = cursor.fetchone()

    cursor.execute(
        "SELECT name, stock FROM products WHERE is_active = 1 AND stock <= ? ORDER BY stock ASC",
        (LOW_STOCK_THRESHOLD,),
    )
    low_stock = cursor.fetchall()
    conn.close()

    text = (
        "📊 **Sales & Order Summary**\n\n"
        f"🗓️ **ዛሬ ({today}):** {today_orders} ትዕዛዝ | {format_price(today_revenue)} ETB\n"
        f"📦 **አጠቃላይ ትዕዛዝ (ከጅምሩ):** {total_orders}\n"
        f"💰 **አጠቃላይ ገቢ (ከጅምሩ):** {format_price(total_revenue)} ETB\n"
        f"💵 **የተሰበሰበ ቅድመ ክፍያ (ከጅምሩ):** {format_price(total_orders * ADVANCE_PAYMENT)} ETB\n\n"
    )

    if low_stock:
        text += "⚠️ **ዝቅተኛ/ያለቀ ስቶክ፦**\n"
        for name, stock in low_stock:
            flag = "❌ አልቋል" if stock <= 0 else f"⚠️ {stock} ቀርቷል"
            text += f"• {name} — {flag}\n"
    else:
        text += "✅ ሁሉም ምርቶች በቂ ስቶክ አላቸው።"

    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    await send_admin_menu(chat_id, context)


# ============================================================
# 7. ADMIN: ADD NEW PERFUME (isolated ConversationHandler)
# ============================================================
async def start_add_perfume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /addproduct command AND the '➕ Add New Perfume' button."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    # Bug fix: wipe any leftover state (e.g. an abandoned customer order) before starting
    context.user_data.clear()

    if not is_admin(user_id):
        if update.message:
            await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return ConversationHandler.END

    context.user_data["new_product"] = {}
    await context.bot.send_message(
        chat_id=chat_id,
        text="1️⃣ **እባክዎ የሽቶውን ስም አስገቡ** (ለምሳሌ፦ Valentino Donna)፦\n\n/cancel ብለው ማቆም ይችላሉ።",
        parse_mode="Markdown",
    )
    return ADD_NAME


async def get_perfume_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["name"] = update.message.text.strip()
    await update.message.reply_text("2️⃣ **ስንት ml እንደሆነ አስገቡ** (ለምሳሌ፦ 100ml)፦", parse_mode="Markdown")
    return ADD_SIZE


async def get_perfume_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["size"] = update.message.text.strip()
    await update.message.reply_text("3️⃣ **የሽቶውን ዋጋ በብር አስገቡ** (ለምሳሌ፦ 4500 ወይም 4500.50)፦", parse_mode="Markdown")
    return ADD_PRICE


async def get_perfume_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = parse_number(update.message.text)
    if price is None:
        await update.message.reply_text("❗ ትክክለኛ ዋጋ በቁጥር ያስገቡ (ለምሳሌ፦ 4500)፦")
        return ADD_PRICE

    context.user_data["new_product"]["price"] = price
    await update.message.reply_text("4️⃣ **የመነሻ ስቶክ ብዛት አስገቡ** (ለምሳሌ፦ 10)፦", parse_mode="Markdown")
    return ADD_STOCK


async def get_perfume_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_text = update.message.text.strip()
    if not stock_text.isdigit():
        await update.message.reply_text("❗ ስቶኩን በዜሮ ወይም አዎንታዊ ቁጥር ብቻ ያስገቡ (ለምሳሌ፦ 10)፦")
        return ADD_STOCK

    context.user_data["new_product"]["stock"] = int(stock_text)
    await update.message.reply_text("5️⃣ **የሽቶውን መግለጫ (Description) አስገቡ**፦", parse_mode="Markdown")
    return ADD_DESC


async def get_perfume_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text("6️⃣ **አሁን የሽቶውን ፎቶ ላኩልኝ** 📸፦")
    return ADD_PHOTO


async def get_perfume_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❗ እባክዎ የሽቶውን ፎቶ (ምስል) ላኩ፦")
        return ADD_PHOTO

    photo_file_id = update.message.photo[-1].file_id
    data = context.user_data.get("new_product", {})

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (name, price, description, stock, size, photo_id, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (data.get("name"), data.get("price"), data.get("description", ""), data.get("stock"), data.get("size"), photo_file_id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **ሽቶው በትክክል ተመዝግቧል!**\n\n"
        f"🛍️ **ስም፦** {data.get('name')}\n"
        f"📏 **መጠን፦** {data.get('size')}\n"
        f"💰 **ዋጋ፦** {format_price(data.get('price'))} ETB\n"
        f"📦 **የመነሻ ስቶክ፦** {data.get('stock')}\n"
        f"📝 **መግለጫ፦** {data.get('description')}\n"
        f"📸 **ፎቶ፦** ተያይዟል",
        parse_mode="Markdown",
    )

    context.user_data.pop("new_product", None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Another Perfume", callback_data="add_another")],
        [InlineKeyboardButton("⚙️ Admin Main Menu", callback_data="admin_menu")],
    ])
    await update.message.reply_text("ቀጣይ ምን ማድረግ ይፈልጋሉ?", reply_markup=keyboard)
    return ADD_MORE


async def add_another_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Loops back to step 1 without leaving the conversation
    return await start_add_perfume(update, context)


async def admin_menu_from_add_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Used only inside the ADD_MORE state so the conversation is properly ended."""
    await admin_menu_callback(update, context)
    return ConversationHandler.END


async def cancel_add_perfume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("የሽቶ መጨመር ሂደቱ ተሰርዟል።")
    if is_admin(update.effective_user.id):
        await send_admin_menu(update.effective_chat.id, context)
    return ConversationHandler.END


# ============================================================
# 8. ADMIN: QUICK PRICE & RESTOCK MANAGER (isolated ConversationHandler)
# ============================================================
async def admin_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, size, price, stock FROM products ORDER BY id")
    products = cursor.fetchall()
    conn.close()

    if not products:
        await query.edit_message_text("ምንም ምርት አልተመዘገበም።")
        return

    keyboard = []
    for p_id, name, size, price, stock in products:
        label = f"{name} ({size}) — {format_price(price)} ETB | ስቶክ: {stock}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_edit_select_{p_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])

    await query.edit_message_text(
        "💰 **Edit Prices & Stock**\n\nየትኛውን ምርት ማስተካከል ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = query.data.split("_")[-1]
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name, size, price, stock FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    conn.close()

    if product is None:
        await query.edit_message_text("ይህ ምርት አልተገኘም።")
        return

    name, size, price, stock = product
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Price", callback_data=f"admin_edit_field_price_{product_id}")],
        [InlineKeyboardButton("📦 Add / Adjust Stock", callback_data=f"admin_edit_field_stock_{product_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_edit_menu")],
    ]
    await query.edit_message_text(
        f"🛍️ **{name} ({size})**\n💰 ዋጋ: {format_price(price)} ETB\n📦 ስቶክ: {stock}\n\nምን ማስተካከል ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_edit_field_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    # callback_data format: admin_edit_field_price_<id>  or  admin_edit_field_stock_<id>
    parts = query.data.split("_")
    field = parts[3]          # "price" or "stock"
    product_id = parts[4]

    context.user_data["edit_product_id"] = product_id
    context.user_data["edit_field"] = field

    if field == "price":
        prompt = "💰 **አዲሱን ዋጋ በብር ያስገቡ** (ለምሳሌ፦ 4700)፦"
    else:
        prompt = (
            "📦 **ምን ያክል ወደ ስቶክ መጨመር ወይም መቀነስ ይፈልጋሉ?**\n"
            "አዎንታዊ ቁጥር ለመጨመር (ለምሳሌ፦ 10), አሉታዊ ቁጥር ለመቀነስ (ለምሳሌ፦ -3)፦"
        )

    await query.message.reply_text(prompt + "\n\n/cancel ብለው ማቆም ይችላሉ።", parse_mode="Markdown")
    return EDIT_VALUE


async def admin_save_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    product_id = context.user_data.get("edit_product_id")
    text = update.message.text.strip()

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name, price, stock FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if product is None:
        conn.close()
        await update.message.reply_text("ይህ ምርት አልተገኘም።")
        context.user_data.clear()
        return ConversationHandler.END

    name, price, stock = product

    if field == "price":
        new_price = parse_number(text)
        if new_price is None:
            await update.message.reply_text("❗ ትክክለኛ ዋጋ በቁጥር ያስገቡ፦")
            conn.close()
            return EDIT_VALUE
        cursor.execute("UPDATE products SET price = ? WHERE id = ?", (new_price, product_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ የ **{name}** ዋጋ ወደ **{format_price(new_price)} ETB** ተቀይሯል።", parse_mode="Markdown")
    else:
        if not (text.lstrip("-").isdigit()):
            await update.message.reply_text("❗ ትክክለኛ ቁጥር ያስገቡ (ለምሳሌ፦ 10 ወይም -3)፦")
            conn.close()
            return EDIT_VALUE
        delta = int(text)
        new_stock = max(0, stock + delta)
        cursor.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, product_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ የ **{name}** ስቶክ ከ {stock} ወደ **{new_stock}** ተቀይሯል።", parse_mode="Markdown")

    context.user_data.clear()
    await send_admin_menu(update.effective_chat.id, context)
    return ConversationHandler.END


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("ማስተካከያው ተሰርዟል።")
    if is_admin(update.effective_user.id):
        await send_admin_menu(update.effective_chat.id, context)
    return ConversationHandler.END


# ============================================================
# 9. ADMIN: DELETE / HIDE INVENTORY (plain callbacks, no conversation needed)
# ============================================================
async def admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, size, is_active, stock FROM products ORDER BY id")
    products = cursor.fetchall()
    conn.close()

    if not products:
        await query.edit_message_text("ምንም ምርት አልተመዘገበም።")
        return

    keyboard = []
    for p_id, name, size, is_active, stock in products:
        label = f"{name} ({size})" if size else name
        toggle_label = "🙈 Hide" if is_active else "👁️ Unhide"
        keyboard.append([
            InlineKeyboardButton(f"🗑️ {label}", callback_data=f"admin_delete_{p_id}"),
            InlineKeyboardButton(toggle_label, callback_data=f"admin_toggle_{p_id}"),
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])

    await query.edit_message_text(
        "🗑️ **Delete / Manage Inventory**\n\n"
        "🗑️ = ሙሉ በሙሉ ማጥፋት (ሊመለስ አይችልም)\n"
        "🙈/👁️ = ከካታሎግ መደበቅ/ማሳየት (ለወደፊት መልሶ መጠቀም ይቻላል)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return

    product_id = query.data.split("_")[-1]
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        await query.answer(f"🗑️ {row[0]} ተሰርዟል።", show_alert=True)
    else:
        await query.answer("ይህ ምርት አልተገኘም።", show_alert=True)
    conn.close()

    await admin_delete_menu(update, context)


async def admin_toggle_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return

    product_id = query.data.split("_")[-1]
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name, is_active FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    if row:
        name, is_active = row
        new_state = 0 if is_active else 1
        cursor.execute("UPDATE products SET is_active = ? WHERE id = ?", (new_state, product_id))
        conn.commit()
        msg = f"🙈 {name} ከካታሎግ ተደብቋል።" if new_state == 0 else f"👁️ {name} ወደ ካታሎግ ተመልሷል።"
        await query.answer(msg, show_alert=True)
    else:
        await query.answer("ይህ ምርት አልተገኘም።", show_alert=True)
    conn.close()

    await admin_delete_menu(update, context)


# ============================================================
# 10. MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- Admin: Add New Perfume wizard ---
    add_product_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addproduct", start_add_perfume),
            CallbackQueryHandler(start_add_perfume, pattern="^admin_add_perfume$"),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_name)],
            ADD_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_size)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_price)],
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_stock)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_description)],
            ADD_PHOTO: [MessageHandler(filters.PHOTO, get_perfume_photo)],
            ADD_MORE: [
                CallbackQueryHandler(add_another_callback, pattern="^add_another$"),
                CallbackQueryHandler(admin_menu_from_add_flow, pattern="^admin_menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add_perfume)],
        name="add_product_conv",
        persistent=False,
    )

    # --- Admin: Quick price/stock editor ---
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_edit_field_prompt, pattern=r"^admin_edit_field_(price|stock)_\d+$"),
        ],
        states={
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
        name="edit_conv",
        persistent=False,
    )

    # --- Customer ordering flow ---
    order_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_product, pattern="^buy_")],
        states={
            PHONE: [MessageHandler(filters.TEXT | filters.CONTACT, get_phone)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            SCREENSHOT: [MessageHandler(filters.PHOTO, get_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="order_conv",
        persistent=False,
    )

    # --------------------------------------------------------
    # Registration order matters: admin conversation handlers
    # are registered FIRST so they take priority over any
    # (potentially stale) customer conversation state for the
    # same admin chat. Combined with context.user_data.clear()
    # calls throughout, this prevents the two flows from ever
    # cross-triggering each other.
    # --------------------------------------------------------
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(add_product_conv)
    app.add_handler(edit_conv)

    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern="^admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_view_catalog, pattern="^admin_view_catalog$"))
    app.add_handler(CallbackQueryHandler(admin_stats_callback, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_edit_menu, pattern="^admin_edit_menu$"))
    app.add_handler(CallbackQueryHandler(admin_edit_select, pattern=r"^admin_edit_select_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_menu, pattern="^admin_delete_menu$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern=r"^admin_delete_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_active, pattern=r"^admin_toggle_\d+$"))

    app.add_handler(CallbackQueryHandler(show_catalog, pattern="^show_catalog$"))
    app.add_handler(CallbackQueryHandler(out_of_stock_notice, pattern="^out_of_stock$"))
    app.add_handler(order_handler)

    # Standalone /cancel for when no conversation is currently active
    app.add_handler(CommandHandler("cancel", cancel))

    print("🤖 የሽቶ መሸጫ ቴሌግራም ቦት ስራ ጀምሯል...")
    app.run_polling()


if __name__ == "__main__":
    main()
