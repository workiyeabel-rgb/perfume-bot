import logging
import sqlite3
from datetime import date
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)

# ------------------ 1. ኮንፊገሬሽን (የግል መረጃዎች) ------------------
# BotFather የሰጠህን BOT TOKEN እዚህ ቦታ ላይ ተካ
BOT_TOKEN = "8229134590:AAHq9xub04wJef4RUFVzTDrnmbgb5gQ5L7I"

# የነጋዴዋን (የአድሚኑን) የቴሌግራም Chat ID እዚህ ተካ (በ @userinfobot ማወቅ ይቻላል)
ADMIN_CHAT_ID = 359999840

# የውይይት ደረጃዎች (Conversation States)
PHONE, ADDRESS, SCREENSHOT = range(3)

# አዲስ ሽቶ ለመጨመር የሚሆኑ ደረጃዎች
ADD_NAME, ADD_ML, ADD_PRICE, ADD_STOCK, ADD_PHOTO = range(3, 8)

# ሎግ ማስተካከያ (ለስህተት መከታተያ)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ------------------ 2. የዳታቤዝ ስራዎች ------------------
def init_db():
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()

    # የሽቶዎች ሰንጠረዥ (Products Table) - stock, size, photo_id ዓምዶች ታክለዋል
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT,
            stock INTEGER NOT NULL DEFAULT 0,
            size TEXT,
            photo_id TEXT
        )
    ''')

    # አስቀድሞ የተፈጠረ ዳታቤዝ ካለ እና አዳዲስ ዓምዶች ከሌሉ መጨመር (backward compatibility)
    cursor.execute("PRAGMA table_info(products)")
    columns = [col[1] for col in cursor.fetchall()]
    if "stock" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 0")
    if "size" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN size TEXT")
    if "photo_id" not in columns:
        cursor.execute("ALTER TABLE products ADD COLUMN photo_id TEXT")

    # የትዕዛዞች ሰንጠረዥ (Orders Table)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_name TEXT,
            price INTEGER,
            phone TEXT,
            address TEXT,
            order_date DATE
        )
    ''')

    # ዳታቤዙ ባዶ ከሆነ ናሙና ሽቶዎችን ይጨምራል (ከ stock ብዛት ጋር)
    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] == 0:
        sample_products = [
            ("Dior Sauvage (100ml)", 4500, "ለወንዶች የሚሆን የሚስብ መዓዛ ያለው", 10),
            ("Bleu De Chanel (100ml)", 5200, "ቆንጆና የማይቀየር ምርጥ ሽቶ", 8),
            ("Tom Ford Black Orchid", 6000, "ለየት ያለ ማራኪ ጠረን", 5),
            ("Victoria's Secret Bombshell", 3800, "ለሴቶች የሚሆን በጣም ተወዳጅ ሽቶ", 0)
        ]
        cursor.executemany(
            "INSERT INTO products (name, price, description, stock) VALUES (?, ?, ?, ?)",
            sample_products
        )

    conn.commit()
    conn.close()

# ------------------ 3. ለደንበኞች የሚሆኑ የቦት ተግባራት ------------------

# /start ሲባል የሚሰራ
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = (
        f"ሰላም {user_name}! 👋\n\n"
        "እንኳን ወደ እኛ የሽቶ መደብር በደህና መጡ! 🌸\n"
        "የምንፈልገውን ሽቶ መርጠው በቀላሉ ማዘዝ ይችላሉ።\n\n"
        "ሽቶዎችን ለማየት ከታች ያለውን ቁልፍ ይጫኑ፦"
    )
    
    keyboard = [[InlineKeyboardButton("🛍️ የሽቶዎች ዝርዝር (Catalog)", callback_data="show_catalog")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

# የሽቶዎችን ካታሎግ ማሳያ (ከስቶክ መረጃ ጋር)
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, price, description, stock, size, photo_id FROM products")
    products = cursor.fetchall()
    conn.close()
    
    await query.edit_message_text("👇 የሚፈልጉትን ሽቶ ይምረጡ፦")
    
    for item in products:
        p_id, name, price, desc, stock, size, photo_id = item

        display_name = f"{name} ({size})" if size else name

        if stock > 0:
            stock_line = f"📦 **ያለው ብዛት:** {stock} ጠርሙስ"
            button_text = f"🛒 {display_name} ግዛ"
            callback = f"buy_{p_id}"
        else:
            stock_line = "❌ **ያለቀ (Out of Stock)**"
            button_text = f"🚫 {display_name} - አልቋል"
            callback = "out_of_stock"

        text = (
            f"✨ **ሽቶ:** {display_name}\n"
            f"💰 **ዋጋ:** {price} ETB\n"
            f"📝 **መግለጫ:** {desc or '-'}\n"
            f"{stock_line}"
        )
        keyboard = [[InlineKeyboardButton(button_text, callback_data=callback)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if photo_id:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_id,
                caption=text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)

# ያለቀ እቃ ሲጫን የሚያሳውቅ
async def out_of_stock_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("ይቅርታ፣ ይህ ሽቶ በአሁኑ ሰዓት አልቋል።", show_alert=True)

# ሽቶ ሲመረጥ - ስቶክ ማረጋገጥ እና ስልክ ቁጥር መጠየቅ
async def select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    product_id = query.data.split("_")[1]
    
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, price, stock FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    conn.close()

    if product is None:
        await query.answer("ይህ ሽቶ አልተገኘም።", show_alert=True)
        return ConversationHandler.END

    name, price, stock = product

    # ስቶክ ከዜሮ በታች ወይም ዜሮ ከሆነ ግዢውን ማስቆም (ሌላ ሰው ቀድሞ የገዛው ሊሆን ስለሚችል ድጋሚ ማረጋገጥ)
    if stock <= 0:
        await query.answer("ይቅርታ፣ ይህ ሽቶ በአሁኑ ሰዓት አልቋል።", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    context.user_data['selected_product_id'] = product_id
    context.user_data['selected_product'] = name
    context.user_data['selected_price'] = price
    
    # ስልክ ቁጥር አጋራ የሚል ቁልፍ ማዘጋጀት
    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ስልክ ቁጥሬን አጋራ", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await query.message.reply_text(
        f"🎯 **የመረጡት ሽቶ:** {name}\n💰 **ዋጋ:** {price} ETB\n\n"
        "እባክዎን ትዕዛዝዎን ለማጠናቀቅ **የስልክ ቁጥርዎን** ያስገቡ (ወይም 'ስልክ ቁጥሬን አጋራ' የሚለውን ይጫኑ)፦",
        parse_mode="Markdown",
        reply_markup=contact_keyboard
    )
    return PHONE

# ስልክ ቁጥር ሲቀበል - አድራሻ መጠየቅ
async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text
        
    context.user_data['phone'] = phone
    
    await update.message.reply_text(
        "እሺ አግኝተነዋል! 👍\nበመቀጠል **እቃው የሚደርስበትን አድራሻ** (ምሳሌ፦ ቦሌ፣ አትላስ) ፅፈው ይላኩ፦",
        reply_markup=ReplyKeyboardRemove()
    )
    return ADDRESS

# አድራሻ ሲቀበል - የ 500 ብር ስክሪንሻት መጠየቅ
async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text
    context.user_data['address'] = address
    
    payment_instruction = (
        "💳 **የቅድመ-ክፍያ ማረጋገጫ (Advance Payment)**\n\n"
        "ትዕዛዝዎን ለማረጋገጥ እባክዎን **500 ETB** በ Telebirr ወይም በባንክ ሂሳባችን ገቢ ያድርጉ።\n\n"
        "📲 **Telebirr / CBE Accounts:**\n"
        "• Telebirr: 0912345678 (ስም)\n"
        "• CBE Bank: 1000123456789 (ስም)\n\n"
        "📌 ክፍያውን እንደፈፀሙ **የክፍያውን ደረሰኝ (Screenshot/ፎቶ)** እዚህ ይላኩልን።"
    )
    
    await update.message.reply_text(payment_instruction, parse_mode="Markdown")
    return SCREENSHOT

# ስክሪንሻት (ፎቶ) ሲቀበል - ትዕዛዝ ማጠናቀቅ፣ ስቶክ መቀነስ እና ለአድሚን ማሳወቅ
async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id  # የደረሰኙ ፎቶ ID
    
    user_id = update.effective_user.id
    product_id = context.user_data.get('selected_product_id')
    product_name = context.user_data.get('selected_product')
    price = context.user_data.get('selected_price')
    phone = context.user_data.get('phone')
    address = context.user_data.get('address')
    today = date.today().isoformat()
    
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()

    # ስቶኩ አሁንም ካለ ደግሞ ማረጋገጥ (race condition ለማስቀረት)
    cursor.execute("SELECT stock FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()
    current_stock = row[0] if row else 0

    if current_stock <= 0:
        conn.close()
        await update.message.reply_text(
            "ይቅርታ፣ ይህ ሽቶ ገቢ ሲደረግልዎት ተጠናቅቆ ነበር። እባክዎ ሌላ ሽቶ ይምረጡ ወይም ድጋፍ ያግኙ።"
        )
        return ConversationHandler.END

    # 1. ትዕዛዙን መመዝገብ
    cursor.execute(
        "INSERT INTO orders (user_id, product_name, price, phone, address, order_date) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, product_name, price, phone, address, today)
    )

    # 2. ስቶኩን በ 1 መቀነስ
    cursor.execute("UPDATE products SET stock = stock - 1 WHERE id = ? AND stock > 0", (product_id,))
    cursor.execute("SELECT stock FROM products WHERE id = ?", (product_id,))
    remaining_stock = cursor.fetchone()[0]

    conn.commit()
    conn.close()
    
    # 3. ለደንበኛው ማረጋገጫ መስጠት
    await update.message.reply_text(
        "ወዲያውኑ ደርሶናል! 🙏\n\n"
        "ትዕዛዝዎ በስኬት ተመዝግቧል። የላኩትን ደረሰኝ አይተን በቅርብ ደቂቃዎች ውስጥ በስልክ ቁጥርዎ እንደውላለን!\n"
        "ስለመረጡን እናመሰግናለን! ✨"
    )
    
    # 4. ለአድሚኑ (ለነጋዴዋ) የትዕዛዝ መረጃውን፣ የቀረውን ስቶክ እና የደረሰኙን ፎቶ መላክ
    admin_notification = (
        "🚨 **አዲስ ትዕዛዝ ገብቷል!** 🚨\n\n"
        f"🛍️ **የተመረጠው ሽቶ:** {product_name}\n"
        f"💰 **ሙሉ ዋጋ:** {price} ETB\n"
        f"📱 **ስልክ ቁጥር:** {phone}\n"
        f"📍 **ማድረሻ አድራሻ:** {address}\n"
        f"💵 **ቅድመ ክፍያ:** 500 ETB (ደረሰኝ ከታች ተያይዟል)\n"
        f"📦 **የቀረ ስቶክ:** {remaining_stock} ጠርሙስ"
    )

    if remaining_stock <= 0:
        admin_notification += "\n\n⚠️ **ይህ ሽቶ አሁን ሙሉ በሙሉ አልቋል! እባክዎ ስቶክ ያድሱ።**"
    elif remaining_stock <= 2:
        admin_notification += f"\n\n⚠️ **ትኩረት፦ ስቶኩ እያለቀ ነው ({remaining_stock} ቀርቷል)።**"
    
    # ለአድሚኑ ፎቶውን ከሙሉ መረጃው ጋር ይልካል
    await context.bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=photo_file_id,
        caption=admin_notification,
        parse_mode="Markdown"
    )
    
    return ConversationHandler.END

# ውይይት ለማቋረጥ (Cancel)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ትዕዛዙ ተሰርዟል። እንደገና ለማዘዝ /start ይበሉ።", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ------------------ 4. ለአድሚኑ ብቻ የሚሆኑ የቦት ተግባራት ------------------

# /report - የቀኑን አጠቃላይ ሽያጭ ማወቂያ
async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # አድሚን መሆኑን ማረጋገጥ
    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return
        
    today = date.today().isoformat()
    
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    
    # የዛሬ ትዕዛዞች ብዛት እና የአጠቃላይ ሽቶዎች ዋጋ ድምር
    cursor.execute("SELECT COUNT(*), SUM(price) FROM orders WHERE order_date = ?", (today,))
    result = cursor.fetchone()
    
    count = result[0] or 0
    total_sales = result[1] or 0
    advance_payments = count * 500  # የእያንዳንዱ ትዕዛዝ 500 ብር ቅድመ-ክፍያ
    
    # የዛሬ ሽቶዎች ዝርዝር
    cursor.execute("SELECT product_name, phone FROM orders WHERE order_date = ?", (today,))
    items = cursor.fetchall()
    conn.close()
    
    report_text = (
        f"📊 **የዛሬው የቀኑ መጨረሻ የሽያጭ ሪፖርት ({today})**\n\n"
        f"📦 **አጠቃላይ የትዕዛዝ ብዛት:** {count} እቃ\n"
        f"💵 **የተሰበሰበ ቅድመ-ክፍያ (500 ETB x {count}):** {advance_payments} ETB\n"
        f"💰 **የተሸጡ እቃዎች አጠቃላይ ዋጋ:** {total_sales} ETB\n\n"
        "📝 **የዛሬ የተሸጡ ሽቶዎች ዝርዝር፦**\n"
    )
    
    if items:
        for idx, item in enumerate(items, 1):
            report_text += f"{idx}. {item[0]} (ደዋይ: {item[1]})\n"
    else:
        report_text += "ዛሬ እስካሁን ምንም የተሸጠ እቃ የለም።"
        
    await update.message.reply_text(report_text, parse_mode="Markdown")

# /stock - የአሁኑን የስቶክ ሁኔታ ለአድሚኑ ማሳያ
async def stock_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return

    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, stock FROM products ORDER BY id")
    products = cursor.fetchall()
    conn.close()

    text = "📦 **የስቶክ ሁኔታ (Inventory Status)**\n\n"
    for p_id, name, stock in products:
        flag = "❌ አልቋል" if stock <= 0 else ("⚠️ እያለቀ ነው" if stock <= 2 else "✅")
        text += f"#{p_id} - {name}: **{stock}** {flag}\n"

    text += (
        "\nስቶክ ለማደስ፦ /restock <product_id> <quantity>\nምሳሌ፦ /restock 1 10"
        "\nአዲስ ሽቶ ለመጨመር፦ /addproduct"
    )

    await update.message.reply_text(text, parse_mode="Markdown")

# /restock <product_id> <quantity> - ስቶክ መጨመሪያ
async def restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return

    args = context.args
    if len(args) != 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await update.message.reply_text(
            "❗ አጠቃቀም፦ /restock <product_id> <quantity>\nምሳሌ፦ /restock 1 10"
        )
        return

    product_id = int(args[0])
    quantity = int(args[1])

    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, stock FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if product is None:
        conn.close()
        await update.message.reply_text("ይህ የምርት ID አልተገኘም።")
        return

    name, old_stock = product
    new_stock = max(0, old_stock + quantity)
    cursor.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, product_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ ስቶክ ታድሷል!\n🛍️ ሽቶ: {name}\n📦 ከ {old_stock} → **{new_stock}**"
    )


# --- አዲስ ሽቶ በ ፎቶ፣ size (ml) እና ዋጋ መጨመሪያ (ለአድሚን ብቻ) ---

async def start_add_perfume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ይህ command ወይም inline button ተጭኖ ሊጀመር ይችላል
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        reply_target = query.message
    else:
        user_id = update.effective_user.id
        reply_target = update.message

    if user_id != ADMIN_CHAT_ID:
        if update.callback_query:
            return ConversationHandler.END
        await update.message.reply_text("ይህ ትእዛዝ ለአድሚን ብቻ የተፈቀደ ነው!")
        return ConversationHandler.END

    await reply_target.reply_text("1️⃣ **እባክዎ የሽቶውን ስም አስገቡ** (ለምሳሌ፦ Dior Sauvage)፦", parse_mode="Markdown")
    return ADD_NAME

async def get_perfume_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['perfume_name'] = update.message.text.strip()
    await update.message.reply_text("2️⃣ **ስንት ml እንደሆነ አስገቡ** (ለምሳሌ፦ 100ml ወይም 50ml)፦", parse_mode="Markdown")
    return ADD_ML

async def get_perfume_ml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['perfume_ml'] = update.message.text.strip()
    await update.message.reply_text("3️⃣ **የሽቶውን ዋጋ በብር አስገቡ** (ለምሳሌ፦ 4500)፦", parse_mode="Markdown")
    return ADD_PRICE

async def get_perfume_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price_text = update.message.text.strip()
    if not price_text.isdigit():
        await update.message.reply_text("❗ እባክዎ ዋጋውን በቁጥር ብቻ ያስገቡ (ለምሳሌ፦ 4500)፦")
        return ADD_PRICE

    context.user_data['perfume_price'] = int(price_text)
    await update.message.reply_text("4️⃣ **የመነሻ ስቶክ ብዛት አስገቡ** (ለምሳሌ፦ 10)፦", parse_mode="Markdown")
    return ADD_STOCK

async def get_perfume_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock_text = update.message.text.strip()
    if not stock_text.isdigit():
        await update.message.reply_text("❗ እባክዎ ስቶኩን በቁጥር ብቻ ያስገቡ (ለምሳሌ፦ 10)፦")
        return ADD_STOCK

    context.user_data['perfume_stock'] = int(stock_text)
    await update.message.reply_text("5️⃣ **አሁን የሽቶውን ፎቶ ላኩልኝ** 📸፦")
    return ADD_PHOTO

async def get_perfume_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # የፎቶውን Telegram File ID መያዝ
    photo_file_id = update.message.photo[-1].file_id

    name = context.user_data['perfume_name']
    ml = context.user_data['perfume_ml']
    price = context.user_data['perfume_price']
    stock = context.user_data['perfume_stock']

    # ዳታቤዝ (SQLite) ውስጥ ማስገባት - ከነባሩ products ሰንጠረዥ ጋር
    conn = sqlite3.connect("perfumes_shop.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (name, price, description, stock, size, photo_id) VALUES (?, ?, ?, ?, ?, ?)",
        (name, price, "", stock, ml, photo_file_id)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **ሽቶው በትክክል ተመዝግቧል!**\n\n"
        f"🛍️ **ስም፦** {name}\n"
        f"📏 **መጠን፦** {ml}\n"
        f"💰 **ዋጋ፦** {price} ETB\n"
        f"📦 **የመነሻ ስቶክ፦** {stock}\n"
        f"📸 **ፎቶ፦** ተያይዟል"
    )
    return ConversationHandler.END

async def cancel_add_perfume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("የሽቶ መጨመር ሂደቱ ተሰርዟል።")
    return ConversationHandler.END


# ------------------ 5. ቦቱን ማስነሳት (Main Runner) ------------------
def main():
    # ዳታቤዝ ማዘጋጀት
    init_db()
    
    # ቦት አፕሊኬሽን መገንባት
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # የትዕዛዝ መቀበያ ውይይት (ConversationHandler)
    order_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_product, pattern="^buy_")],
        states={
            PHONE: [MessageHandler(filters.TEXT | filters.CONTACT, get_phone)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            SCREENSHOT: [MessageHandler(filters.PHOTO, get_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    # አዲስ ሽቶ መጨመሪያ ውይይት (ለአድሚን ብቻ)
    add_product_handler = ConversationHandler(
        entry_points=[
            CommandHandler("addproduct", start_add_perfume),
            CallbackQueryHandler(start_add_perfume, pattern="^add_perfume$"),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_name)],
            ADD_ML: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_ml)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_price)],
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_perfume_stock)],
            ADD_PHOTO: [MessageHandler(filters.PHOTO, get_perfume_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add_perfume)]
    )
    
    # Command & Callback Handlers ማገናኘት
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", daily_report))
    app.add_handler(CommandHandler("stock", stock_status))
    app.add_handler(CommandHandler("restock", restock))
    app.add_handler(CallbackQueryHandler(show_catalog, pattern="^show_catalog$"))
    app.add_handler(CallbackQueryHandler(out_of_stock_notice, pattern="^out_of_stock$"))
    app.add_handler(order_handler)
    app.add_handler(add_product_handler)
    
    print("🤖 የሽቶ መሸጫ ቴሌግራም ቦት ስራ ጀምሯል...")
    app.run_polling()

if __name__ == "__main__":
    main()
