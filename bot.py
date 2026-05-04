import asyncio
import logging
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from flight_scraper import FlightScraper
from cities_db import get_iata_code, get_city_name_ar

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
ORIGIN, DESTINATION, TRIP_TYPE, DATE, RETURN_DATE, SEARCHING = range(6)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation."""
    context.user_data.clear()
    
    welcome_text = (
        "✈️ *مرحباً بك في بوت البحث عن الطيران!*\n\n"
        "🌍 يمكنني مساعدتك في العثور على أفضل رحلات الطيران بأسعار مناسبة.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🏙️ *من أي مدينة تريد السفر؟*\n\n"
        "💡 _يمكنك كتابة اسم المدينة بالعربي أو الإنجليزي_\n"
        "مثال: الرياض، جدة، دبي، القاهرة..."
    )
    
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown"
    )
    return ORIGIN


async def get_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get origin city."""
    city_input = update.message.text.strip()
    iata_code = get_iata_code(city_input)
    
    if not iata_code:
        await update.message.reply_text(
            f"❌ *لم أتعرف على المدينة:* `{city_input}`\n\n"
            "🔍 يرجى المحاولة مرة أخرى بكتابة:\n"
            "• اسم المدينة بالعربي (مثال: الرياض)\n"
            "• اسم المدينة بالإنجليزي (مثال: Riyadh)\n"
            "• كود المطار IATA (مثال: RUH)",
            parse_mode="Markdown"
        )
        return ORIGIN
    
    context.user_data["origin"] = iata_code
    context.user_data["origin_name"] = get_city_name_ar(iata_code) or city_input
    
    await update.message.reply_text(
        f"✅ *مدينة المغادرة:* {context.user_data['origin_name']} ({iata_code})\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🏙️ *إلى أي مدينة تريد السفر؟*\n\n"
        "💡 _اكتب اسم المدينة الوجهة_",
        parse_mode="Markdown"
    )
    return DESTINATION


async def get_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get destination city."""
    city_input = update.message.text.strip()
    iata_code = get_iata_code(city_input)
    
    if not iata_code:
        await update.message.reply_text(
            f"❌ *لم أتعرف على المدينة:* `{city_input}`\n\n"
            "🔍 يرجى المحاولة مرة أخرى",
            parse_mode="Markdown"
        )
        return DESTINATION
    
    if iata_code == context.user_data.get("origin"):
        await update.message.reply_text(
            "⚠️ *مدينة الوجهة يجب أن تختلف عن مدينة المغادرة!*\n\n"
            "يرجى اختيار مدينة أخرى",
            parse_mode="Markdown"
        )
        return DESTINATION
    
    context.user_data["destination"] = iata_code
    context.user_data["destination_name"] = get_city_name_ar(iata_code) or city_input
    
    keyboard = [
        [
            InlineKeyboardButton("🔁 ذهاب فقط", callback_data="one_way"),
            InlineKeyboardButton("🔄 ذهاب وعودة", callback_data="round_trip"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ *مدينة الوجهة:* {context.user_data['destination_name']} ({iata_code})\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎫 *ما نوع الرحلة المطلوبة؟*",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return TRIP_TYPE


async def get_trip_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get trip type."""
    query = update.callback_query
    await query.answer()
    
    trip_type = query.data
    context.user_data["trip_type"] = trip_type
    
    trip_emoji = "🔁" if trip_type == "one_way" else "🔄"
    trip_name = "ذهاب فقط" if trip_type == "one_way" else "ذهاب وعودة"
    
    # Generate date buttons for next 30 days
    today = datetime.now()
    keyboard = []
    row = []
    
    for i in range(1, 31):
        date = today + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        date_display = date.strftime("%d/%m")
        day_name = get_arabic_day(date.weekday())
        
        row.append(InlineKeyboardButton(
            f"{day_name} {date_display}", 
            callback_data=f"date_{date_str}"
        ))
        
        if len(row) == 3:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *نوع الرحلة:* {trip_emoji} {trip_name}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📅 *اختر تاريخ السفر:*",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return DATE


async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get departure date."""
    query = update.callback_query
    await query.answer()
    
    date_str = query.data.replace("date_", "")
    context.user_data["departure_date"] = date_str
    
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    date_display = date_obj.strftime("%d/%m/%Y")
    day_name = get_arabic_day(date_obj.weekday())
    
    if context.user_data.get("trip_type") == "round_trip":
        # Generate return date buttons
        keyboard = []
        row = []
        
        for i in range(1, 31):
            ret_date = date_obj + timedelta(days=i)
            ret_date_str = ret_date.strftime("%Y-%m-%d")
            ret_date_display = ret_date.strftime("%d/%m")
            ret_day_name = get_arabic_day(ret_date.weekday())
            
            row.append(InlineKeyboardButton(
                f"{ret_day_name} {ret_date_display}",
                callback_data=f"return_{ret_date_str}"
            ))
            
            if len(row) == 3:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ *تاريخ الذهاب:* {day_name} {date_display}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📅 *اختر تاريخ العودة:*",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return RETURN_DATE
    else:
        return await start_search(query, context, date_display, day_name)


async def get_return_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get return date for round trips."""
    query = update.callback_query
    await query.answer()
    
    return_date_str = query.data.replace("return_", "")
    context.user_data["return_date"] = return_date_str
    
    ret_date_obj = datetime.strptime(return_date_str, "%Y-%m-%d")
    ret_date_display = ret_date_obj.strftime("%d/%m/%Y")
    ret_day_name = get_arabic_day(ret_date_obj.weekday())
    
    dep_date_obj = datetime.strptime(context.user_data["departure_date"], "%Y-%m-%d")
    dep_date_display = dep_date_obj.strftime("%d/%m/%Y")
    dep_day_name = get_arabic_day(dep_date_obj.weekday())
    
    return await start_search(query, context, dep_date_display, dep_day_name, ret_date_display, ret_day_name)


async def start_search(query, context, dep_date_display, dep_day_name, ret_date_display=None, ret_day_name=None):
    """Start the flight search."""
    data = context.user_data
    trip_name = "ذهاب فقط" if data.get("trip_type") == "one_way" else "ذهاب وعودة"
    
    summary = (
        "🔍 *جاري البحث عن الرحلات...*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛫 *من:* {data.get('origin_name')} ({data.get('origin')})\n"
        f"🛬 *إلى:* {data.get('destination_name')} ({data.get('destination')})\n"
        f"🎫 *النوع:* {trip_name}\n"
        f"📅 *الذهاب:* {dep_day_name} {dep_date_display}\n"
    )
    
    if ret_date_display:
        summary += f"📅 *العودة:* {ret_day_name} {ret_date_display}\n"
    
    summary += "\n⏳ _قد يستغرق البحث 30-60 ثانية..._"
    
    await query.edit_message_text(summary, parse_mode="Markdown")
    
    # Perform the search
    try:
        scraper = FlightScraper()
        flights = await scraper.search_flights(
            origin=data.get("origin"),
            destination=data.get("destination"),
            departure_date=data.get("departure_date"),
            return_date=data.get("return_date"),
            trip_type=data.get("trip_type")
        )
        
        if not flights:
            await query.message.reply_text(
                "😔 *لم يتم العثور على رحلات متاحة*\n\n"
                "💡 *اقتراحات:*\n"
                "• جرب تواريخ أخرى\n"
                "• تحقق من صحة أسماء المدن\n"
                "• حاول مرة أخرى لاحقاً\n\n"
                "🔄 اضغط /start للبدء من جديد",
                parse_mode="Markdown"
            )
        else:
            await send_flight_results(query.message, flights, data)
            
    except Exception as e:
        logger.error(f"Search error: {e}")
        await query.message.reply_text(
            "⚠️ *حدث خطأ أثناء البحث*\n\n"
            f"التفاصيل: `{str(e)[:100]}`\n\n"
            "🔄 اضغط /start للمحاولة مرة أخرى",
            parse_mode="Markdown"
        )
    
    return ConversationHandler.END


def format_boarding_pass(flight: dict, index: int, origin: str, destination: str, dep_date: str) -> str:
    """Format a single flight as a digital boarding pass."""
    airline      = flight.get("airline", "غير محدد")
    price        = flight.get("price", "—")
    dep_time     = flight.get("departure_time", "--:--")
    arr_time     = flight.get("arrival_time", "--:--")
    duration     = flight.get("duration", "")
    stops        = flight.get("stops", "مباشر")
    flight_no    = flight.get("flight_number", f"XX{100 + index}")

    stops_badge  = "🟢 مباشر" if stops in ["0", "مباشر", "direct", "Nonstop"] else f"🟡 {stops} توقف"
    dur_line     = f"\n│  ⏱  *المدة:*   {duration}" if duration else ""

    # Ticket serial number cosmetic
    serial = f"TKT-{dep_date.replace('-','')}-{index:03d}"

    pass_text = (
        f"```\n"
        f"┌─────────────────────────────┐\n"
        f"│        🎫 تذكرة سفر #{index}        │\n"
        f"└─────────────────────────────┘\n"
        f"```"
        f"\n"
        f"✈️  *{airline}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"│\n"
        f"│  🛫  *{origin}*  ──────►  🛬  *{destination}*\n"
        f"│\n"
        f"│  🕒  *الإقلاع:*  `{dep_time}`   →   *الهبوط:* `{arr_time}`\n"
        f"{dur_line}\n"
        f"│  📅  *التاريخ:*  `{dep_date}`\n"
        f"│  🛑  *التوقفات:* {stops_badge}\n"
        f"│\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"│  💰  *السعر:*  `{price} ريال سعودي`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖  `{serial}`"
    )
    return pass_text


async def send_flight_results(message, flights, data):
    """Send flight results formatted as digital boarding passes."""
    origin_name = data.get("origin_name", data.get("origin", ""))
    dest_name   = data.get("destination_name", data.get("destination", ""))
    dep_date    = data.get("departure_date", "")
    trip_type   = data.get("trip_type", "one_way")
    trip_label  = "🔁 ذهاب فقط" if trip_type == "one_way" else "🔄 ذهاب وعودة"

    # ── Header ────────────────────────────────────────────────────────────────
    header = (
        "🗂  *نتائج البحث عن الرحلات*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛫  *من:*   {origin_name}  ›  {dest_name}  :*إلى*  🛬\n"
        f"📅  *التاريخ:*  {dep_date}      {trip_label}\n"
        f"🎟  وجدنا *{len(flights)}* رحلة — اختر الأنسب لك!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(header, parse_mode="Markdown")

    # ── One boarding pass per flight ───────────────────────────────────────────
    for i, flight in enumerate(flights[:5], 1):
        booking_url = flight.get("booking_url", "")

        pass_text = format_boarding_pass(
            flight, i, origin_name, dest_name, dep_date
        )

        keyboard = []
        if booking_url:
            keyboard.append([
                InlineKeyboardButton("🔗 احجز هذه الرحلة مباشرةً ←", url=booking_url)
            ])
        keyboard.append([
            InlineKeyboardButton("🔍 بحث جديد", callback_data="new_search")
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await message.reply_text(
            pass_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    # ── Footer ─────────────────────────────────────────────────────────────────
    footer = (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *تلميح:* الأسعار قابلة للتغيير — احجز مبكراً!\n"
        "🔄 اضغط /start للبحث عن رحلة جديدة\n"
        "⭐ شكراً لاستخدامك بوت الطيران!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(footer, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ *تم إلغاء البحث*\n\n"
        "🔄 اضغط /start للبدء من جديد",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message."""
    help_text = (
        "ℹ️ *مساعدة بوت الطيران*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *كيفية الاستخدام:*\n\n"
        "1️⃣ اضغط /start لبدء البحث\n"
        "2️⃣ أدخل مدينة المغادرة\n"
        "3️⃣ أدخل مدينة الوجهة\n"
        "4️⃣ اختر نوع الرحلة\n"
        "5️⃣ اختر التاريخ\n"
        "6️⃣ انتظر النتائج!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🏙️ *المدن المدعومة:*\n"
        "السعودية، الإمارات، مصر، الكويت،\n"
        "قطر، البحرين، الأردن، لبنان،\n"
        "تركيا، المغرب، وأكثر من 100 مدينة!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📞 *الأوامر المتاحة:*\n"
        "/start - بدء البحث\n"
        "/cancel - إلغاء البحث\n"
        "/help - المساعدة"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


def get_arabic_day(weekday: int) -> str:
    """Convert weekday number to Arabic name."""
    days = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    return days[weekday]


def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_origin)],
            DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_destination)],
            TRIP_TYPE: [CallbackQueryHandler(get_trip_type, pattern="^(one_way|round_trip)$")],
            DATE: [CallbackQueryHandler(get_date, pattern="^date_")],
            RETURN_DATE: [CallbackQueryHandler(get_return_date, pattern="^return_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    
    logger.info("🚀 Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
