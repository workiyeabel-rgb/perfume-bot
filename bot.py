import asyncio
import logging
import math
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
from telegram.error import RetryAfter, TimedOut, BadRequest
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
ADVANCE_PAYMENT = 500  # ETB flat pre-payment/deposit required to confirm an order
LOW_STOCK_THRESHOLD = 2
MAX_QUANTITY = 5          # highest quantity a customer can pick per order
CATALOG_PAGE_SIZE = 3      # products per page in photo-heavy catalog views
EDIT_PAGE_SIZE = 8         # products per page in text/button-only admin lists
SEND_DELAY = 0.35          # small delay between sequential sends to avoid flood limits

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# httpx includes the full Telegram API URL (and bot token) in INFO messages.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============================================================
# 2. CONVERSATION STATES
# (kept in clearly separate numeric ranges so flows can never
#  be confused with one another)
# ============================================================
# --- Customer ordering flow ---
PHONE, ADDRESS, SCREENSHOT = range(0, 3)

# --- Admin: add new perfume wizard (name/desc/photo, then a repeatable
#     size -> price -> stock loop so each perfume can have many sizes) ---
(
    ADD_NAME,
    ADD_DESC,
    ADD_PHOTO,
    ADD_VARIANT_SIZE,
    ADD_VARIANT_PRICE,
    ADD_VARIANT_STOCK,
    ADD_VARIANT_MORE,
    ADD_MORE,
) = range(3, 11)

# --- Admin: quick edit price/stock of a single size variant ---
EDIT_VALUE = 11

# --- Admin: edit a single payment (Telebirr/CBE) field ---
PAYMENT_VALUE = 12


# ============================================================
# 3. DATABASE
# ============================================================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    cur = conn.cursor()

    # ---- Base schema (new installs get this shape immediately) ----
    cur.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            photo_id TEXT,
            photo_type TEXT NOT NULL DEFAULT 'photo',
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS product_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            size TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            variant_id INTEGER,
            product_name TEXT,
            size TEXT,
            unit_price REAL,
            quantity INTEGER NOT NULL DEFAULT 1,
            total_price REAL,
            phone TEXT,
            address TEXT,
            order_date DATE,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()

    # ---- Seed default payment settings (only fills in missing keys) ----
    default_settings = {
        "telebirr_number": "0912345678",
        "telebirr_name": "ስም",
        "cbe_number": "1000123456789",
        "cbe_name": "ስም",
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()

    # ---- Auto-migration for DB files created by earlier bot versions ----
    cur.execute("PRAGMA table_info(products)")
    prod_cols = [c[1] for c in cur.fetchall()]
    if "description" not in prod_cols:
        cur.execute("ALTER TABLE products ADD COLUMN description TEXT")
    if "photo_id" not in prod_cols:
        cur.execute("ALTER TABLE products ADD COLUMN photo_id TEXT")
    if "photo_type" not in prod_cols:
        cur.execute("ALTER TABLE products ADD COLUMN photo_type TEXT NOT NULL DEFAULT 'photo'")
    if "is_active" not in prod_cols:
        cur.execute("ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")

    cur.execute("PRAGMA table_info(orders)")
    order_cols = [c[1] for c in cur.fetchall()]
    if "product_id" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN product_id INTEGER")
    if "variant_id" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN variant_id INTEGER")
    if "size" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN size TEXT")
    if "unit_price" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN unit_price REAL")
    if "quantity" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
    if "total_price" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN total_price REAL")
    if "status" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")

    conn.commit()

    # ---- One-time data migration: old single-size product rows -> product_variants ----
    # Earlier bot versions stored price/stock/size directly on the products row.
    # prod_cols was captured BEFORE the ALTERs above, so this only fires for old DBs.
    if "price" in prod_cols:
        cur.execute("SELECT COUNT(*) FROM product_variants")
        if cur.fetchone()[0] == 0:
            cur.execute("SELECT id, price, stock, size FROM products")
            legacy_rows = cur.fetchall()
            for p_id, price, stock, size in legacy_rows:
                cur.execute(
                    "INSERT INTO product_variants (product_id, size, price, stock) VALUES (?, ?, ?, ?)",
                    (p_id, size or "Standard", price or 0, stock or 0),
                )
            conn.commit()
            if legacy_rows:
                logger.info("Migrated %d legacy product rows into product_variants.", len(legacy_rows))

    # ---- One-time data migration: old orders.price -> unit_price/total_price/quantity ----
    if "price" in order_cols:
        cur.execute(
            "UPDATE orders SET unit_price = price, total_price = price, quantity = 1 "
            "WHERE unit_price IS NULL AND price IS NOT NULL"
        )
        conn.commit()

    # ---- Seed sample data only on a brand new, empty database ----
    cur.execute("SELECT COUNT(*) FROM products")
    if cur.fetchone()[0] == 0:
        sample = [
            ("Dior Sauvage", "ለወንዶች የሚሆን የሚስብ መዓዛ ያለው", [("50ml", 3200, 6), ("100ml", 4500, 10)]),
            ("Bleu De Chanel", "ቆንጆና የማይቀየር ምርጥ ሽቶ", [("50ml", 3800, 5), ("100ml", 5200, 8)]),
            ("Tom Ford Black Orchid", "ለየት ያለ ማራኪ ጠረን", [("50ml", 6000, 5)]),
            ("Victoria's Secret Bombshell", "ለሴቶች የሚሆን በጣም ተወዳጅ ሽቶ", [("100ml", 3800, 0)]),
        ]
        for name, desc, variants in sample:
            cur.execute(
                "INSERT INTO products (name, description, photo_id, photo_type, is_active) "
                "VALUES (?, ?, NULL, 'photo', 1)",
                (name, desc),
            )
            pid = cur.lastrowid
            for size, price, stock in variants:
                cur.execute(
                    "INSERT INTO product_variants (product_id, size, price, stock) VALUES (?, ?, ?, ?)",
                    (pid, size, price, stock),
                )
        conn.commit()

    conn.close()


def fetch_products(active_only: bool, limit: int, offset: int):
    """Returns (rows, total_count).
    active_only=True  -> rows are (id, name, description, photo_id, photo_type)
    active_only=False -> rows are (id, name, description, photo_id, photo_type, is_active)
    """
    conn = get_conn()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT id, name, description, photo_id, photo_type FROM products "
            "WHERE is_active = 1 ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        )
    else:
        cur.execute("SELECT COUNT(*) FROM products")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT id, name, description, photo_id, photo_type, is_active FROM products "
            "ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        )
    rows = cur.fetchall()
    conn.close()
    return rows, total


def fetch_variants(product_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, size, price, stock FROM product_variants WHERE product_id = ? ORDER BY id",
        (product_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
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


def format_price_range(variants) -> str:
    prices = [v[2] for v in variants]
    if not prices:
        return "-"
    lo, hi = min(prices), max(prices)
    if lo == hi:
        return f"{format_price(lo)} ETB"
    return f"{format_price(lo)}-{format_price(hi)} ETB"


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


def escape_md(text) -> str:
    """Escape legacy-Markdown special characters in user/admin-supplied text
    so a stray *, _, ` or [ can never break formatting or trigger a
    'can't parse entities' error from Telegram."""
    if not text:
        return text
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def paginate(total: int, page: int, page_size: int):
    total_pages = max(1, math.ceil(total / page_size)) if page_size > 0 else 1
    page = max(0, min(page, total_pages - 1))
    return page, total_pages


async def clear_tracked_messages(chat_id, context: ContextTypes.DEFAULT_TYPE, key: str):
    """Deletes messages sent during a previous page render. Failures (message
    already gone, too old to delete, etc.) are swallowed so this can never
    freeze or crash the bot."""
    ids = context.user_data.pop(key, [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def safe_send_message(bot, chat_id, text, reply_markup=None, parse_mode="Markdown"):
    for _ in range(3):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TimedOut:
            await asyncio.sleep(1)
        except BadRequest as e:
            logger.warning("BadRequest sending message: %s", e)
            if parse_mode:
                parse_mode = None
                continue
            raise
    return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def safe_send_photo(bot, chat_id, photo, caption, reply_markup=None, parse_mode="Markdown"):
    for _ in range(3):
        try:
            return await bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TimedOut:
            await asyncio.sleep(1)
        except BadRequest as e:
            logger.warning("BadRequest sending photo: %s", e)
            if parse_mode:
                parse_mode = None
                continue
            raise
    return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, reply_markup=reply_markup)


async def safe_send_document(bot, chat_id, document, caption, reply_markup=None, parse_mode="Markdown"):
    for _ in range(3):
        try:
            return await bot.send_document(
                chat_id=chat_id, document=document, caption=caption,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TimedOut:
            await asyncio.sleep(1)
        except BadRequest as e:
            logger.warning("BadRequest sending document: %s", e)
            if parse_mode:
                parse_mode = None
                continue
            raise
    return await bot.send_document(chat_id=chat_id, document=document, caption=caption, reply_markup=reply_markup)


async def send_product_card(bot, chat_id, photo_id, photo_type, caption, keyboard):
    """Sends one product as a photo, an image-document, or plain text,
    whichever matches how it was originally uploaded."""
    if photo_id and photo_type == "document":
        return await safe_send_document(bot, chat_id, photo_id, caption, keyboard)
    elif photo_id:
        return await safe_send_photo(bot, chat_id, photo_id, caption, keyboard)
    else:
        return await safe_send_message(bot, chat_id, caption, keyboard)


def admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Add New Perfume", callback_data="admin_add_perfume")],
        [InlineKeyboardButton("💰 Edit Prices & Stock", callback_data="admin_edit_menu")],
        [InlineKeyboardButton("💳 የክፍያ አካውንት ማስተካከያ", callback_data="admin_payment_menu")],
        [InlineKeyboardButton("🛍️ View Catalog", callback_data="admin_view_catalog")],
        [InlineKeyboardButton("🗑️ Delete / Manage Inventory", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📊 Sales & Order Summary", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_admin_menu(chat_id, context: ContextTypes.DEFAULT_TYPE, text=None):
    await safe_send_message(
        context.bot, chat_id,
        text or "⚙️ *Admin Control Panel*\n\nምን መስራት ይፈልጋሉ?",
        reply_markup=admin_main_menu_keyboard(),
    )


def welcome_text(first_name: str) -> str:
    return (
        f"ሰላም {first_name}! 👋\n\n"
        "እንኳን ወደ እኛ የሽቶ መደብር በደህና መጡ! 🌸\n"
        "የምንፈልገውን ሽቶ መርጠው በቀላሉ ማዘዝ ይችላሉ።\n\n"
        "ሽቶዎችን ለማየት ከታች ያለውን ቁልፍ ይጫኑ፦"
    )


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛍️ የሽቶዎች ዝርዝር (Catalog)", callback_data="show_catalog")]]
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

    await update.message.reply_text(welcome_text(user.first_name), reply_markup=welcome_keyboard())


async def go_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await clear_tracked_messages(query.message.chat_id, context, "catalog_msg_ids")
    await safe_send_message(
        context.bot, query.message.chat_id,
        welcome_text(query.from_user.first_name),
        reply_markup=welcome_keyboard(),
    )


async def render_catalog_page(chat_id, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Renders one page (CATALOG_PAGE_SIZE products) of the customer catalog.
    Deletes the previous page's messages first so browsing many products
    never floods the chat or hits Telegram's rate limits."""
    products, total = fetch_products(active_only=True, limit=CATALOG_PAGE_SIZE, offset=page * CATALOG_PAGE_SIZE)
    page, total_pages = paginate(total, page, CATALOG_PAGE_SIZE)

    await clear_tracked_messages(chat_id, context, "catalog_msg_ids")

    if not products:
        msg = await safe_send_message(context.bot, chat_id, "በአሁኑ ሰዓት ምንም ሽቶ አልተመዘገበም።")
        context.user_data["catalog_msg_ids"] = [msg.message_id]
        return

    sent_ids = []
    for p_id, name, desc, photo_id, photo_type in products:
        variants = fetch_variants(p_id)
        in_stock = any(v[3] > 0 for v in variants)
        price_line = format_price_range(variants)

        caption = (
            f"✨ *{escape_md(name)}*\n"
            f"📝 {escape_md(desc) or '-'}\n"
            f"💰 {price_line}\n"
        )
        if in_stock:
            caption += "📦 ይገኛል"
            button = InlineKeyboardButton("🛒 ይምረጡ (Select)", callback_data=f"selectp_{p_id}")
        else:
            caption += "❌ *ያለቀ (Out of Stock)*"
            button = InlineKeyboardButton("🚫 አልቋል", callback_data="out_of_stock")

        keyboard = InlineKeyboardMarkup([[button]])
        msg = await send_product_card(context.bot, chat_id, photo_id, photo_type, caption, keyboard)
        sent_ids.append(msg.message_id)
        await asyncio.sleep(SEND_DELAY)

    nav_rows = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"catalogpage_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"catalogpage_{page + 1}"))
    if nav_row:
        nav_rows.append(nav_row)
    nav_rows.append([InlineKeyboardButton("🏠 ዋና ገፅ", callback_data="go_home")])

    nav_msg = await safe_send_message(
        context.bot, chat_id, f"📄 ገፅ {page + 1}/{total_pages}",
        reply_markup=InlineKeyboardMarkup(nav_rows),
    )
    sent_ids.append(nav_msg.message_id)
    context.user_data["catalog_msg_ids"] = sent_ids
    context.user_data["catalog_page"] = page


async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("👇 የሚፈልጉትን ሽቶ ይምረጡ፦ (ከታች ዝርዝሩ ይታያል)")
    except Exception:
        pass
    await render_catalog_page(query.message.chat_id, context, page=0)


async def catalog_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[1])
    await render_catalog_page(query.message.chat_id, context, page)


async def out_of_stock_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("ይቅርታ፣ ይህ ሽቶ በአሁኑ ሰዓት አልቋል።", show_alert=True)


async def variant_oos_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("ይቅርታ፣ ይህ መጠን በአሁኑ ሰዓት አልቋል።", show_alert=True)


async def select_product_variants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer picked a perfume from the catalog -> show its available sizes."""
    query = update.callback_query
    product_id = int(query.data.split("_")[1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM products WHERE id = ? AND is_active = 1", (product_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        await query.answer("ይህ ሽቶ አልተገኘም።", show_alert=True)
        return
    name = row[0]

    variants = fetch_variants(product_id)
    if not variants:
        await query.answer("ለዚህ ሽቶ ምንም አማራጭ መጠን የለም።", show_alert=True)
        return

    await query.answer()
    buttons = []
    for v_id, size, price, stock in variants:
        if stock > 0:
            label = f"{size} - {format_price(price)} ETB"
            buttons.append([InlineKeyboardButton(label, callback_data=f"selectv_{v_id}")])
        else:
            buttons.append([InlineKeyboardButton(f"❌ {size} - ያለቀ", callback_data="variant_oos")])

    back_page = context.user_data.get("catalog_page", 0)
    buttons.append([InlineKeyboardButton("⬅️ ተመለስ", callback_data=f"catalogpage_{back_page}")])

    await query.message.reply_text(
        f"🎯 *{escape_md(name)}*\nመጠን ይምረጡ፦",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def select_variant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer picked a size -> show quantity options."""
    query = update.callback_query
    variant_id = int(query.data.split("_")[1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pv.size, pv.price, pv.stock, p.name, p.id "
        "FROM product_variants pv JOIN products p ON p.id = pv.product_id "
        "WHERE pv.id = ?",
        (variant_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        await query.answer("ይህ አማራጭ አልተገኘም።", show_alert=True)
        return
    size, price, stock, name, product_id = row
    if stock <= 0:
        await query.answer("ይቅርታ፣ ይህ መጠን በአሁኑ ሰዓት አልቋል።", show_alert=True)
        return

    await query.answer()
    max_qty = min(stock, MAX_QUANTITY)
    qty_buttons = [
        InlineKeyboardButton(str(n), callback_data=f"qty_{variant_id}_{n}")
        for n in range(1, max_qty + 1)
    ]
    keyboard = [qty_buttons, [InlineKeyboardButton("⬅️ ተመለስ", callback_data=f"selectp_{product_id}")]]

    await query.message.reply_text(
        f"🎯 *{escape_md(name)} ({escape_md(size)})*\n💰 {format_price(price)} ETB\n\n"
        "ስንት ይፈልጋሉ? (ብዛት ይምረጡ)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def select_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer picked a quantity -> entry point into the order ConversationHandler."""
    query = update.callback_query
    parts = query.data.split("_")
    variant_id, qty = int(parts[1]), int(parts[2])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pv.size, pv.price, pv.stock, p.name, p.id "
        "FROM product_variants pv JOIN products p ON p.id = pv.product_id "
        "WHERE pv.id = ? AND p.is_active = 1",
        (variant_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        await query.answer("ይህ አማራጭ አልተገኘም።", show_alert=True)
        return ConversationHandler.END

    size, price, stock, name, product_id = row
    if stock < qty:
        await query.answer(f"ይቅርታ፣ አሁን ያለው ስቶክ {stock} ብቻ ነው።", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    # Namespaced keys so this never collides with the admin add-product flow's user_data
    context.user_data["order_variant_id"] = variant_id
    context.user_data["order_product_id"] = product_id
    context.user_data["order_size"] = size
    context.user_data["order_product_name"] = f"{name} ({size})"
    context.user_data["order_unit_price"] = price
    context.user_data["order_quantity"] = qty
    context.user_data["order_total_price"] = price * qty

    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ስልክ ቁጥሬን አጋራ", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await query.message.reply_text(
        f"🎯 *የመረጡት ሽቶ:* {escape_md(name)} ({escape_md(size)})\n"
        f"🔢 *ብዛት:* {qty}\n"
        f"💰 *ጠቅላላ ዋጋ:* {format_price(price * qty)} ETB\n\n"
        "እባክዎን ትዕዛዝዎን ለማጠናቀቅ *የስልክ ቁጥርዎን* ያስገቡ (ወይም 'ስልክ ቁጥሬን አጋራ' የሚለውን ይጫኑ)፦",
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
        "እሺ አግኝተነዋል! 👍\nበመቀጠል *እቃው የሚደርስበትን አድራሻ* (ምሳሌ፦ ቦሌ፣ አትላስ) ፅፈው ይላኩ፦",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADDRESS


async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order_address"] = update.message.text

    qty = context.user_data.get("order_quantity", 1)
    total = context.user_data.get("order_total_price", 0)

    telebirr_number = get_setting("telebirr_number", "-")
    telebirr_name = get_setting("telebirr_name", "-")
    cbe_number = get_setting("cbe_number", "-")
    cbe_name = get_setting("cbe_name", "-")

    payment_instruction = (
        "💳 *የቅድመ-ክፍያ ማረጋገጫ (Advance Payment)*\n\n"
        f"🔢 ብዛት: {qty}\n"
        f"💰 ጠቅላላ ዋጋ: {format_price(total)} ETB\n\n"
        f"ትዕዛዝዎን ለማረጋገጥ እባክዎን *{ADVANCE_PAYMENT} ETB* በ Telebirr ወይም በባንክ ሂሳባችን ገቢ ያድርጉ።\n\n"
        "📲 *Telebirr / CBE Accounts:*\n"
        f"• Telebirr: {escape_md(telebirr_number)} ({escape_md(telebirr_name)})\n"
        f"• CBE Bank: {escape_md(cbe_number)} ({escape_md(cbe_name)})\n\n"
        "📌 ክፍያውን እንደፈፀሙ *የክፍያውን ደረሰኝ (Screenshot/ፎቶ ወይም ፋይል)* እዚህ ይላኩልን።"
    )
    await update.message.reply_text(payment_instruction, parse_mode="Markdown")
    return SCREENSHOT


async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        media_file_id = update.message.photo[-1].file_id
        is_document = False
    elif update.message.document:
        media_file_id = update.message.document.file_id
        is_document = True
    else:
        await update.message.reply_text("❗ እባክዎ የክፍያ ደረሰኝ ፎቶ ወይም የምስል ፋይል ይላኩ፦")
        return SCREENSHOT

    user_id = update.effective_user.id
    variant_id = context.user_data.get("order_variant_id")
    product_id = context.user_data.get("order_product_id")
    product_name = context.user_data.get("order_product_name", "")
    size = context.user_data.get("order_size")
    unit_price = context.user_data.get("order_unit_price")
    quantity = context.user_data.get("order_quantity", 1)
    total_price = context.user_data.get("order_total_price")
    phone = context.user_data.get("order_phone", "")
    address = context.user_data.get("order_address", "")
    today = date.today().isoformat()

    conn = get_conn()
    conn.isolation_level = None  # manual transaction control for an atomic stock check
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")
        # Atomic decrement: only succeeds if enough stock is still available,
        # which prevents two simultaneous orders from overselling the same size.
        cur.execute(
            "UPDATE product_variants SET stock = stock - ? WHERE id = ? AND stock >= ?",
            (quantity, variant_id, quantity),
        )
        if cur.rowcount == 0:
            cur.execute("ROLLBACK")
            conn.close()
            await update.message.reply_text(
                "ይቅርታ፣ ስቶኩ በበቂ ሁኔታ ስላልቀረ ይህን ትዕዛዝ ማስኬድ አልተቻለም። እባክዎ ሌላ አማራጭ ይምረጡ ወይም ድጋፍ ያግኙ።"
            )
            context.user_data.clear()
            return ConversationHandler.END

        cur.execute(
            "INSERT INTO orders "
            "(user_id, product_id, variant_id, product_name, size, unit_price, quantity, "
            "total_price, phone, address, order_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (user_id, product_id, variant_id, product_name, size, unit_price, quantity,
             total_price, phone, address, today),
        )
        cur.execute("SELECT stock FROM product_variants WHERE id = ?", (variant_id,))
        remaining_stock = cur.fetchone()[0]
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        logger.exception("Failed to finalize order for variant_id=%s", variant_id)
        await update.message.reply_text(
            "❗ ይቅርታ፣ ትዕዛዝዎን ስናስኬድ ስህተት ተፈጥሯል። እባክዎ ቆይተው እንደገና ይሞክሩ ወይም ድጋፍ ያግኙ።"
        )
        context.user_data.clear()
        return ConversationHandler.END

    conn.close()

    await update.message.reply_text(
        "🙏 <b>እናመሰግናለን!</b>\n\n"
        "✅ <b>ትዕዛዝዎ በስኬት ደርሶናል!</b>\n"
        "📦 እቃዎ <b>በነገው ዕለት</b> የሚደርስዎት ሲሆን፣ የዴሊቨሪ አጋራችን ከመምጣቱ በፊት በስልክ ቁጥርዎ ይደውልልዎታል።",
        parse_mode="HTML",
    )

    admin_notification = (
        "🚨 *አዲስ ትዕዛዝ ገብቷል!* 🚨\n\n"
        f"🛍️ *የተመረጠው ሽቶ:* {escape_md(product_name)}\n"
        f"🔢 *ብዛት:* {quantity}\n"
        f"💰 *ነጠላ ዋጋ:* {format_price(unit_price)} ETB\n"
        f"💵 *ጠቅላላ ዋጋ:* {format_price(total_price)} ETB\n"
        f"📱 *ስልክ ቁጥር:* {escape_md(phone)}\n"
        f"📍 *ማድረሻ አድራሻ:* {escape_md(address)}\n"
        f"💵 *የተከፈለ ቅድመ ክፍያ:* {ADVANCE_PAYMENT} ETB (ደረሰኝ ከታች ተያይዟል)\n"
        f"📦 *የቀረ ስቶክ:* {remaining_stock}"
    )
    if remaining_stock <= 0:
        admin_notification += "\n\n⚠️ *ይህ መጠን አሁን ሙሉ በሙሉ አልቋል! እባክዎ ስቶክ ያድሱ።*"
    elif remaining_stock <= LOW_STOCK_THRESHOLD:
        admin_notification += f"\n\n⚠️ *ትኩረት፦ ስቶኩ እያለቀ ነው ({remaining_stock} ቀርቷል)።*"

    if is_document:
        await safe_send_document(context.bot, ADMIN_CHAT_ID, media_file_id, admin_notification)
    else:
        await safe_send_photo(context.bot, ADMIN_CHAT_ID, media_file_id, admin_notification)

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Always wipe state so nothing leaks into the next flow
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
    try:
        await query.edit_message_text("⚙️ *Admin Control Panel*", parse_mode="Markdown")
    except Exception:
        pass
    await send_admin_menu(query.message.chat_id, context, text="ምን መስራት ይፈልጋሉ?")


async def render_admin_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Lets the admin preview exactly what customers see, plus hidden/out-of-stock
    items and per-size stock, with the same pagination used for customers."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    chat_id = query.message.chat_id
    products, total = fetch_products(active_only=False, limit=CATALOG_PAGE_SIZE, offset=page * CATALOG_PAGE_SIZE)
    page, total_pages = paginate(total, page, CATALOG_PAGE_SIZE)

    await clear_tracked_messages(chat_id, context, "admin_catalog_msg_ids")

    if not products:
        msg = await safe_send_message(context.bot, chat_id, "ምንም ምርት አልተመዘገበም።")
        context.user_data["admin_catalog_msg_ids"] = [msg.message_id]
        return

    sent_ids = []
    for p_id, name, desc, photo_id, photo_type, is_active in products:
        variants = fetch_variants(p_id)
        if variants:
            variant_lines = "\n".join(
                f"  • {escape_md(size)}: {format_price(price)} ETB — ስቶክ {stock}"
                for _, size, price, stock in variants
            )
        else:
            variant_lines = "  (ምንም መጠን አልተመዘገበም)"
        status = "🙈 *ተደብቋል*" if not is_active else "👁️ *ይታያል*"

        caption = (
            f"#{p_id} ✨ *{escape_md(name)}*\n"
            f"📝 {escape_md(desc) or '-'}\n"
            f"{status}\n\n{variant_lines}"
        )
        msg = await send_product_card(context.bot, chat_id, photo_id, photo_type, caption, None)
        sent_ids.append(msg.message_id)
        await asyncio.sleep(SEND_DELAY)

    nav_rows = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admincatpage_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admincatpage_{page + 1}"))
    if nav_row:
        nav_rows.append(nav_row)
    nav_rows.append([InlineKeyboardButton("⚙️ Admin Main Menu", callback_data="admin_menu")])

    nav_msg = await safe_send_message(
        context.bot, chat_id, f"📄 ገፅ {page + 1}/{total_pages}",
        reply_markup=InlineKeyboardMarkup(nav_rows),
    )
    sent_ids.append(nav_msg.message_id)
    context.user_data["admin_catalog_msg_ids"] = sent_ids


async def admin_view_catalog_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_admin_catalog(update, context, page=0)


async def admin_view_catalog_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(update.callback_query.data.split("_")[1])
    await render_admin_catalog(update, context, page=page)


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
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*), COALESCE(SUM(total_price), 0) FROM orders")
    total_orders, total_revenue = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_price), 0) FROM orders WHERE order_date = ?", (today,)
    )
    today_orders, today_revenue = cur.fetchone()

    cur.execute(
        "SELECT p.name, pv.size, pv.stock FROM product_variants pv "
        "JOIN products p ON p.id = pv.product_id "
        "WHERE p.is_active = 1 AND pv.stock <= ? ORDER BY pv.stock ASC",
        (LOW_STOCK_THRESHOLD,),
    )
    low_stock = cur.fetchall()
    conn.close()

    text = (
        "📊 *Sales & Order Summary*\n\n"
        f"🗓️ *ዛሬ ({today}):* {today_orders} ትዕዛዝ | {format_price(today_revenue)} ETB\n"
        f"📦 *አጠቃላይ ትዕዛዝ (ከጅምሩ):* {total_orders}\n"
        f"💰 *አጠቃላይ ገቢ (ከጅምሩ):* {format_price(total_revenue)} ETB\n"
        f"💵 *የተሰበሰበ ቅድመ ክፍያ (ከጅምሩ):* {format_price(total_orders * ADVANCE_PAYMENT)} ETB\n\n"
    )

    if low_stock:
        text += "⚠️ *ዝቅተኛ/ያለቀ ስቶክ፦*\n"
        for name, size, stock in low_stock:
            flag = "❌ አልቋል" if stock <= 0 else f"⚠️ {stock} ቀርቷል"
            text += f"• {escape_md(name)} ({escape_md(size)}) — {flag}\n"
    else:
        text += "✅ ሁሉም ምርቶች በቂ ስቶክ አላቸው።"

    await safe_send_message(context.bot, chat_id, text)
    await send_admin_menu(chat_id, context)


# ============================================================
# 7. ADMIN: ADD NEW PERFUME (isolated ConversationHandler)
#    name -> description -> photo -> [size -> price -> stock]+ -> save
# ============================================================
async def start_add_perfume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /addproduct, the '➕ Add New Perfume' button, and the
    'Add Another Perfume' loop. Always wipes user_data first so unlimited
    perfumes can be added back-to-back without any state leaking between them."""
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    context.user_data.clear()

    if not is_admin(user_id):
        if update.message:
            await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return ConversationHandler.END

    context.user_data["new_product"] = {"variants": []}
    await context.bot.send_message(
        chat_id=chat_id,
        text="1️⃣ *እባክዎ የሽቶውን ስም አስገቡ* (ለምሳሌ፦ Valentino Donna)፦\n\n/cancel ብለው ማቆም ይችላሉ።",
        parse_mode="Markdown",
    )
    return ADD_NAME


async def get_perfume_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["name"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏩ ይለፍ / Skip Description", callback_data="desc_skip")]]
    )
    await update.message.reply_text(
        "2️⃣ *የሽቶውን ዝርዝር መግለጫ ያስገቡ*\n"
        "(ለምሳሌ፦ የመዓዛ ማስታወሻ/Fragrance Notes፣ የጠረን ዓይነት/Scent Profile፣ "
        "ለወንድ/ለሴት/ለሁለቱም (Unisex))\n\n"
        "በርካታ መስመሮች (multi-line) አድርገው መጻፍ ይችላሉ፣ ወይም መግለጫ ከሌለ ከታች ያለውን ይጫኑ፦",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ADD_DESC


async def prompt_for_photo(bot, chat_id):
    await bot.send_message(
        chat_id=chat_id,
        text="3️⃣ *አሁን የሽቶውን ፎቶ ላኩልኝ* 📸 (እንደ ፎቶ ወይም እንደ ምስል ፋይል/Document መላክ ይችላሉ)፦",
        parse_mode="Markdown",
    )


async def get_perfume_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # .strip() only trims leading/trailing whitespace, so internal line
    # breaks (multi-line notes/scent profile/etc.) are preserved as-is.
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await prompt_for_photo(context.bot, update.effective_chat.id)
    return ADD_PHOTO


async def skip_description_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.setdefault("new_product", {"variants": []})["description"] = ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await prompt_for_photo(context.bot, query.message.chat_id)
    return ADD_PHOTO


async def get_perfume_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
        photo_type = "photo"
    elif update.message.document:
        photo_id = update.message.document.file_id
        photo_type = "document"
    else:
        await update.message.reply_text("❗ እባክዎ የሽቶውን ፎቶ (ምስል) ወይም የምስል ፋይል ላኩ፦")
        return ADD_PHOTO

    data = context.user_data.setdefault("new_product", {"variants": []})
    data["photo_id"] = photo_id
    data["photo_type"] = photo_type

    await update.message.reply_text(
        "4️⃣ *የመጀመሪያውን መጠን (Size) አስገቡ* (ለምሳሌ፦ 30ml)፦", parse_mode="Markdown"
    )
    return ADD_VARIANT_SIZE


async def get_variant_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_product = context.user_data.setdefault("new_product", {"variants": []})
    new_product["current_variant"] = {"size": update.message.text.strip()}
    await update.message.reply_text(
        "💰 *የዚህን መጠን ዋጋ በብር አስገቡ* (ለምሳሌ፦ 1500)፦", parse_mode="Markdown"
    )
    return ADD_VARIANT_PRICE


async def get_variant_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = parse_number(update.message.text)
    if price is None:
        await update.message.reply_text("❗ ትክክለኛ ዋጋ በቁጥር ያስገቡ (ለምሳሌ፦ 1500)፦")
        return ADD_VARIANT_PRICE

    context.user_data["new_product"]["current_variant"]["price"] = price
    await update.message.reply_text(
        "📦 *የዚህን መጠን ስቶክ ብዛት አስገቡ* (ለምሳሌ፦ 10)፦", parse_mode="Markdown"
    )
    return ADD_VARIANT_STOCK


async def get_variant_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_text = update.message.text.strip()
    if not stock_text.isdigit():
        await update.message.reply_text("❗ ስቶኩን በዜሮ ወይም አዎንታዊ ቁጥር ብቻ ያስገቡ (ለምሳሌ፦ 10)፦")
        return ADD_VARIANT_STOCK

    new_product = context.user_data["new_product"]
    variant = new_product.pop("current_variant", {})
    variant["stock"] = int(stock_text)
    new_product.setdefault("variants", []).append(variant)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ሌላ መጠን ጨምር (Add another size)", callback_data="variant_add_more")],
        [InlineKeyboardButton("✅ ጨርሻለሁ (Done, save perfume)", callback_data="variant_done")],
    ])
    await update.message.reply_text(
        f"✅ *{escape_md(variant['size'])}* — {format_price(variant['price'])} ETB — "
        f"ስቶክ {variant['stock']} ተመዝግቧል።\n\nቀጣይ ምን ማድረግ ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ADD_VARIANT_MORE


async def variant_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="➕ *ቀጣዩን መጠን (Size) አስገቡ* (ለምሳሌ፦ 50ml)፦",
        parse_mode="Markdown",
    )
    return ADD_VARIANT_SIZE


async def variant_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = context.user_data.get("new_product", {})
    variants = data.get("variants", [])

    if not variants:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❗ ቢያንስ አንድ መጠን (Size) ማስመዝገብ አለብዎት።",
        )
        return ADD_VARIANT_MORE

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO products (name, description, photo_id, photo_type, is_active) VALUES (?, ?, ?, ?, 1)",
        (data.get("name"), data.get("description", ""), data.get("photo_id"), data.get("photo_type", "photo")),
    )
    product_id = cur.lastrowid
    for v in variants:
        cur.execute(
            "INSERT INTO product_variants (product_id, size, price, stock) VALUES (?, ?, ?, ?)",
            (product_id, v["size"], v["price"], v["stock"]),
        )
    conn.commit()
    conn.close()

    summary_lines = "\n".join(
        f"• {escape_md(v['size'])} — {format_price(v['price'])} ETB — ስቶክ {v['stock']}" for v in variants
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "✅ *ሽቶው በትክክል ተመዝግቧል!*\n\n"
            f"🛍️ *ስም፦* {escape_md(data.get('name'))}\n"
            f"📝 *መግለጫ፦* {escape_md(data.get('description'))}\n"
            "📸 *ፎቶ፦* ተያይዟል\n\n"
            f"📏 *የተመዘገቡ መጠኖች፦*\n{summary_lines}"
        ),
        parse_mode="Markdown",
    )

    context.user_data.pop("new_product", None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Another Perfume", callback_data="add_another")],
        [InlineKeyboardButton("⚙️ Admin Main Menu", callback_data="admin_menu")],
    ])
    await context.bot.send_message(chat_id=query.message.chat_id, text="ቀጣይ ምን ማድረግ ይፈልጋሉ?", reply_markup=keyboard)
    return ADD_MORE


async def add_another_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Loops back to step 1 without leaving the conversation, with fully wiped state
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
#    product list -> variant list -> edit price/stock -> save
# ============================================================
async def render_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    products, total = fetch_products(active_only=False, limit=EDIT_PAGE_SIZE, offset=page * EDIT_PAGE_SIZE)
    page, total_pages = paginate(total, page, EDIT_PAGE_SIZE)

    if not products:
        await query.edit_message_text("ምንም ምርት አልተመዘገበም።")
        return

    keyboard = []
    for row in products:
        p_id, name = row[0], row[1]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"admineditp_{p_id}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admineditlist_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admineditlist_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])

    await query.edit_message_text(
        f"💰 *Edit Prices & Stock* (ገፅ {page + 1}/{total_pages})\n\nየትኛውን ምርት ማስተካከል ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_edit_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_edit_menu(update, context, page=0)


async def admin_edit_menu_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(update.callback_query.data.split("_")[1])
    await render_edit_menu(update, context, page=page)


async def admin_edit_select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    product_id = int(query.data.split("_")[1])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        await query.edit_message_text("ይህ ምርት አልተገኘም።")
        return
    name = row[0]

    variants = fetch_variants(product_id)
    if not variants:
        await query.edit_message_text(f"*{escape_md(name)}* ላይ ምንም መጠን አልተመዘገበም።", parse_mode="Markdown")
        return

    keyboard = []
    for v_id, size, price, stock in variants:
        keyboard.append([InlineKeyboardButton(
            f"{size} | {format_price(price)} ETB | ስቶክ {stock}",
            callback_data=f"admineditv_{v_id}",
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_edit_menu")])

    await query.edit_message_text(
        f"🛍️ *{escape_md(name)}*\n\nየትኛውን መጠን ማስተካከል ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_edit_select_variant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    variant_id = int(query.data.split("_")[1])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pv.size, pv.price, pv.stock, p.name, p.id FROM product_variants pv "
        "JOIN products p ON p.id = pv.product_id WHERE pv.id = ?",
        (variant_id,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        await query.edit_message_text("ይህ አማራጭ አልተገኘም።")
        return
    size, price, stock, name, product_id = row

    keyboard = [
        [InlineKeyboardButton("✏️ Edit Price", callback_data=f"admineditfield_price_{variant_id}")],
        [InlineKeyboardButton("📦 Add / Adjust Stock", callback_data=f"admineditfield_stock_{variant_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"admineditp_{product_id}")],
    ]
    await query.edit_message_text(
        f"🛍️ *{escape_md(name)} ({escape_md(size)})*\n"
        f"💰 ዋጋ: {format_price(price)} ETB\n📦 ስቶክ: {stock}\n\nምን ማስተካከል ይፈልጋሉ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_edit_field_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    # callback_data format: admineditfield_price_<variant_id> or admineditfield_stock_<variant_id>
    parts = query.data.split("_")
    field = parts[1]           # "price" or "stock"
    variant_id = parts[2]

    context.user_data["edit_variant_id"] = variant_id
    context.user_data["edit_field"] = field

    if field == "price":
        prompt = "💰 *አዲሱን ዋጋ በብር ያስገቡ* (ለምሳሌ፦ 4700)፦"
    else:
        prompt = (
            "📦 *ምን ያክል ወደ ስቶክ መጨመር ወይም መቀነስ ይፈልጋሉ?*\n"
            "አዎንታዊ ቁጥር ለመጨመር (ለምሳሌ፦ 10), አሉታዊ ቁጥር ለመቀነስ (ለምሳሌ፦ -3)፦"
        )

    await query.message.reply_text(prompt + "\n\n/cancel ብለው ማቆም ይችላሉ።", parse_mode="Markdown")
    return EDIT_VALUE


async def admin_save_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    variant_id = context.user_data.get("edit_variant_id")
    text = update.message.text.strip()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pv.size, pv.price, pv.stock, p.name FROM product_variants pv "
        "JOIN products p ON p.id = pv.product_id WHERE pv.id = ?",
        (variant_id,),
    )
    row = cur.fetchone()

    if row is None:
        conn.close()
        await update.message.reply_text("ይህ አማራጭ አልተገኘም።")
        context.user_data.clear()
        return ConversationHandler.END

    size, price, stock, name = row

    if field == "price":
        new_price = parse_number(text)
        if new_price is None:
            await update.message.reply_text("❗ ትክክለኛ ዋጋ በቁጥር ያስገቡ፦")
            conn.close()
            return EDIT_VALUE
        cur.execute("UPDATE product_variants SET price = ? WHERE id = ?", (new_price, variant_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ የ *{escape_md(name)} ({escape_md(size)})* ዋጋ ወደ *{format_price(new_price)} ETB* ተቀይሯል።",
            parse_mode="Markdown",
        )
    else:
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❗ ትክክለኛ ቁጥር ያስገቡ (ለምሳሌ፦ 10 ወይም -3)፦")
            conn.close()
            return EDIT_VALUE
        delta = int(text)
        new_stock = max(0, stock + delta)
        cur.execute("UPDATE product_variants SET stock = ? WHERE id = ?", (new_stock, variant_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ የ *{escape_md(name)} ({escape_md(size)})* ስቶክ ከ {stock} ወደ *{new_stock}* ተቀይሯል።",
            parse_mode="Markdown",
        )

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
async def render_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    products, total = fetch_products(active_only=False, limit=EDIT_PAGE_SIZE, offset=page * EDIT_PAGE_SIZE)
    page, total_pages = paginate(total, page, EDIT_PAGE_SIZE)

    if not products:
        await query.edit_message_text("ምንም ምርት አልተመዘገበም።")
        return

    keyboard = []
    for row in products:
        p_id, name, is_active = row[0], row[1], row[5]
        toggle_label = "🙈 Hide" if is_active else "👁️ Unhide"
        keyboard.append([
            InlineKeyboardButton(f"🗑️ {name}", callback_data=f"admindel_{p_id}"),
            InlineKeyboardButton(toggle_label, callback_data=f"admintoggle_{p_id}"),
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admindellist_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admindellist_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])

    await query.edit_message_text(
        f"🗑️ *Delete / Manage Inventory* (ገፅ {page + 1}/{total_pages})\n\n"
        "🗑️ = ሙሉ በሙሉ ማጥፋት (ሊመለስ አይችልም፣ ሁሉንም መጠኖች ያካትታል)\n"
        "🙈/👁️ = ከካታሎግ መደበቅ/ማሳየት (ለወደፊት መልሶ መጠቀም ይቻላል)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_delete_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_delete_menu(update, context, page=0)


async def admin_delete_menu_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(update.callback_query.data.split("_")[1])
    await render_delete_menu(update, context, page=page)


async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return

    product_id = query.data.split("_")[-1]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM product_variants WHERE product_id = ?", (product_id,))
        cur.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        await query.answer(f"🗑️ {row[0]} ተሰርዟል።", show_alert=True)
    else:
        await query.answer("ይህ ምርት አልተገኘም።", show_alert=True)
    conn.close()

    await render_delete_menu(update, context, page=0)


async def admin_toggle_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return

    product_id = query.data.split("_")[-1]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, is_active FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    if row:
        name, is_active = row
        new_state = 0 if is_active else 1
        cur.execute("UPDATE products SET is_active = ? WHERE id = ?", (new_state, product_id))
        conn.commit()
        msg = f"🙈 {name} ከካታሎግ ተደብቋል።" if new_state == 0 else f"👁️ {name} ወደ ካታሎግ ተመልሷል።"
        await query.answer(msg, show_alert=True)
    else:
        await query.answer("ይህ ምርት አልተገኘም።", show_alert=True)
    conn.close()

    await render_delete_menu(update, context, page=0)


# ============================================================
# 10. ADMIN: DYNAMIC BANK & TELEBIRR ACCOUNT MANAGEMENT
#    Lets the admin change the payment details shown to customers
#    without touching any code.
# ============================================================
PAYMENT_FIELD_LABELS = {
    "telebirr_number": "የቴሌብር ስልክ ቁጥር (Telebirr Number)",
    "telebirr_name": "የቴሌብር ስም (Telebirr Name)",
    "cbe_number": "የ CBE ባንክ ሂሳብ ቁጥር (CBE Account Number)",
    "cbe_name": "የ CBE ባንክ ስም (CBE Account Name)",
}


def payment_menu_content():
    telebirr_number = get_setting("telebirr_number", "-")
    telebirr_name = get_setting("telebirr_name", "-")
    cbe_number = get_setting("cbe_number", "-")
    cbe_name = get_setting("cbe_name", "-")

    text = (
        "💳 *የክፍያ አካውንት ማስተካከያ (Manage Bank Accounts)*\n\n"
        f"📲 *Telebirr:* {escape_md(telebirr_number)} ({escape_md(telebirr_name)})\n"
        f"🏦 *CBE Bank:* {escape_md(cbe_number)} ({escape_md(cbe_name)})\n\n"
        "የትኛውን ማስተካከል ይፈልጋሉ?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Telebirr ቁጥር", callback_data="adminpayfield_telebirr_number")],
        [InlineKeyboardButton("✏️ Telebirr ስም", callback_data="adminpayfield_telebirr_name")],
        [InlineKeyboardButton("✏️ CBE ቁጥር", callback_data="adminpayfield_cbe_number")],
        [InlineKeyboardButton("✏️ CBE ስም", callback_data="adminpayfield_cbe_name")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")],
    ])
    return text, keyboard


async def render_payment_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = payment_menu_content()
    await safe_send_message(context.bot, chat_id, text, reply_markup=keyboard)


async def admin_payment_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    await render_payment_menu(query.message.chat_id, context)


async def admin_payment_field_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    # callback_data format: adminpayfield_<key>, e.g. adminpayfield_telebirr_number
    key = "_".join(query.data.split("_")[1:])
    context.user_data["payment_field_key"] = key
    label = PAYMENT_FIELD_LABELS.get(key, key)

    await query.message.reply_text(
        f"✏️ *አዲሱን {label} ያስገቡ*፦\n\n/cancel ብለው ማቆም ይችላሉ።",
        parse_mode="Markdown",
    )
    return PAYMENT_VALUE


async def admin_save_payment_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("payment_field_key")
    value = update.message.text.strip()

    if not key:
        await update.message.reply_text("❗ ስህተት ተፈጥሯል፣ እባክዎ ዳግም ይሞክሩ።")
        context.user_data.clear()
        return ConversationHandler.END

    if not value:
        await update.message.reply_text("❗ ትክክለኛ ዋጋ ያስገቡ፦")
        return PAYMENT_VALUE

    set_setting(key, value)
    context.user_data.clear()

    await update.message.reply_text("✅ ተቀምጧል! (Saved)")
    await render_payment_menu(update.effective_chat.id, context)
    return ConversationHandler.END


async def cancel_payment_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("ማስተካከያው ተሰርዟል።")
    if is_admin(update.effective_user.id):
        await send_admin_menu(update.effective_chat.id, context)
    return ConversationHandler.END


# ============================================================
# 11. GLOBAL ERROR HANDLER (so one bad update can never freeze the bot)
# ============================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)


# ============================================================
# 12. MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- Admin: Add New Perfume wizard (name -> desc -> photo -> sizes loop) ---
    add_product_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addproduct", start_add_perfume),
            CallbackQueryHandler(start_add_perfume, pattern="^admin_add_perfume$"),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_name)],
            ADD_DESC: [
                CallbackQueryHandler(skip_description_callback, pattern="^desc_skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_description),
            ],
            ADD_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, get_perfume_photo)],
            ADD_VARIANT_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_variant_size)],
            ADD_VARIANT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_variant_price)],
            ADD_VARIANT_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_variant_stock)],
            ADD_VARIANT_MORE: [
                CallbackQueryHandler(variant_more_callback, pattern="^variant_add_more$"),
                CallbackQueryHandler(variant_done_callback, pattern="^variant_done$"),
            ],
            ADD_MORE: [
                CallbackQueryHandler(add_another_callback, pattern="^add_another$"),
                CallbackQueryHandler(admin_menu_from_add_flow, pattern="^admin_menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add_perfume)],
        name="add_product_conv",
        persistent=False,
    )

    # --- Admin: Quick price/stock editor (per size variant) ---
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_edit_field_prompt, pattern=r"^admineditfield_(price|stock)_\d+$"),
        ],
        states={
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
        name="edit_conv",
        persistent=False,
    )

    # --- Admin: edit Telebirr/CBE payment details ---
    payment_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_payment_field_prompt,
                pattern=r"^adminpayfield_(telebirr_number|telebirr_name|cbe_number|cbe_name)$",
            ),
        ],
        states={
            PAYMENT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_payment_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_payment_edit)],
        name="payment_conv",
        persistent=False,
    )

    # --- Customer ordering flow (starts after size + quantity are picked) ---
    order_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_quantity, pattern=r"^qty_\d+_\d+$")],
        states={
            PHONE: [MessageHandler(filters.TEXT | filters.CONTACT, get_phone)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            SCREENSHOT: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, get_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="order_conv",
        persistent=False,
    )

    # --------------------------------------------------------
    # Registration order matters: ConversationHandlers are
    # registered FIRST so they take priority over any (potentially
    # stale) state for the same chat. Combined with the
    # context.user_data.clear() calls throughout, this prevents
    # the different flows from ever cross-triggering each other.
    # --------------------------------------------------------
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(add_product_conv)
    app.add_handler(edit_conv)
    app.add_handler(payment_conv)
    app.add_handler(order_handler)

    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern="^admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_payment_menu_entry, pattern="^admin_payment_menu$"))
    app.add_handler(CallbackQueryHandler(admin_view_catalog_entry, pattern="^admin_view_catalog$"))
    app.add_handler(CallbackQueryHandler(admin_view_catalog_page, pattern=r"^admincatpage_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_stats_callback, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_edit_menu_entry, pattern="^admin_edit_menu$"))
    app.add_handler(CallbackQueryHandler(admin_edit_menu_page, pattern=r"^admineditlist_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_select_product, pattern=r"^admineditp_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_select_variant, pattern=r"^admineditv_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_menu_entry, pattern="^admin_delete_menu$"))
    app.add_handler(CallbackQueryHandler(admin_delete_menu_page, pattern=r"^admindellist_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern=r"^admindel_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_active, pattern=r"^admintoggle_\d+$"))

    app.add_handler(CallbackQueryHandler(show_catalog, pattern="^show_catalog$"))
    app.add_handler(CallbackQueryHandler(catalog_page_nav, pattern=r"^catalogpage_\d+$"))
    app.add_handler(CallbackQueryHandler(go_home, pattern="^go_home$"))
    app.add_handler(CallbackQueryHandler(out_of_stock_notice, pattern="^out_of_stock$"))
    app.add_handler(CallbackQueryHandler(variant_oos_notice, pattern="^variant_oos$"))
    app.add_handler(CallbackQueryHandler(select_product_variants, pattern=r"^selectp_\d+$"))
    app.add_handler(CallbackQueryHandler(select_variant, pattern=r"^selectv_\d+$"))

    # Standalone /cancel for when no conversation is currently active
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_error_handler(error_handler)

    print("🤖 የሽቶ መሸጫ ቴሌግራም ቦት ስራ ጀምሯል...")
    app.run_polling()


if __name__ == "__main__":
    main()
