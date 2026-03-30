import os
import logging
import datetime
import pytz
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from google import genai
from google.genai import types

import database

# --- Enable Logging ---
# Logs to both console and file
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))

# Initialize Database Migration
database.init_db()

# Initialize Gemini
client = genai.Client()
grounding_tool = types.Tool(google_search=types.GoogleSearch())

CATEGORIES = {
    1: "Top Stories (Trending / Breaking)",
    2: "World News",
    3: "India (National News)",
    4: "Indian Politics",
    5: "Sports",
    6: "Business & Economy",
    7: "Science and Technology",
    8: "Entertainment",
    9: "Cricket and IPL"
}

GLOBAL_NEWS_CACHE = {}

# v2: Store previous headlines to avoid sending duplicate stories in the evening
GLOBAL_RECENT_HEADLINES = {cat_id: [] for cat_id in CATEGORIES.keys()}

# Load SQLite users into memory cache
registered_users = database.get_all_users()

# --- Alerts Engine ---
async def send_admin_alert(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Sends a private crash/error alert to the bot owner."""
    logger.error(f"ADMIN ALERT: {message}")
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ **NewsPulse Error Alert**\n{message}")
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")

# --- Gemini News Fetcher ---
async def fetch_category_news(category_id, time_context, current_date_str, recent_headlines_list, context):
    category_name = CATEGORIES[category_id]
    
    # Deduplication context
    avoid_context = ""
    if recent_headlines_list:
        avoid_context = "CRITICAL: Do NOT cover the exact same stories from the previous briefing. Avoid these previous headlines if possible:\n" + "\n".join([f"- {h}" for h in recent_headlines_list])

    prompt = f"""
    Today's exact date is {current_date_str}. 
    You MUST use the Google Search tool to fetch the most recent and factual news for this specific date.
    Provide a {time_context}. I need the top 3 headlines and their details in brief for the following category: {category_name}.

    {avoid_context}

    **QUALITY CONTROL AND SOURCE RANKING:**
    Prioritize high-quality, verified sources like Reuters, BBC, The Hindu, Indian Express, and Bloomberg. Avoid random blogs or unverified tabloids.

    Format strictly for Telegram. Use plain text, spacing, and emojis.
    CRITICAL INSTRUCTION FOR LINKS: Do NOT use Markdown link formatting like [URL](URL). You must just output the raw, plain text URL directly after "URL: ".
    Include the source name. 3 sentences max per headline.
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[grounding_tool], temperature=0.3)
        )
        clean_text = response.text.replace("**", "").replace("*", "")
        
        # Save to memory to deduplicate later (just grab the first few lines to approximate the headlines)
        # It's a rough approximation for Gemini
        headlines = [line for line in clean_text.split("\n") if len(line) > 10 and not line.startswith("http")]
        GLOBAL_RECENT_HEADLINES[category_id] = headlines[:5]

        return category_id, f"📌 {category_name.upper()}\n{clean_text}"
    except Exception as e:
        error_msg = f"Error fetching {category_name}: {e}"
        logger.error(error_msg)
        if context:
            await send_admin_alert(context, error_msg)
        return category_id, f"📌 {category_name.upper()}\nCould not fetch news at this time."

async def prefetch_all_news(context: ContextTypes.DEFAULT_TYPE = None):
    """Prefetches news for all categories and stores in GLOBAL_NEWS_CACHE."""
    global GLOBAL_NEWS_CACHE, GLOBAL_RECENT_HEADLINES
    logger.info("Starting prefetch of all news categories...")
    
    ist_now = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
    current_hour = ist_now.hour
    current_date_str = ist_now.strftime("%A, %B %d, %Y")
    time_context = "morning briefing and overnight developments" if current_hour < 12 else "evening roundup of today's events"

    # If it's a new day (morning fetch), we clear the deduplication cache safely
    if current_hour < 12:
         GLOBAL_RECENT_HEADLINES = {cat_id: [] for cat_id in CATEGORIES.keys()}

    # Instead of pulling all 10 at exactly the same microsecond (which hits Google's 15 RPM free-tier limit),
    # we fetch them one by one with a 6-second cooldown. This takes exactly 60 seconds total and is 100% safe.
    results = []
    for cat_id in CATEGORIES.keys():
        recent_headlines = GLOBAL_RECENT_HEADLINES.get(cat_id, [])
        res = await fetch_category_news(cat_id, time_context, current_date_str, recent_headlines, context)
        results.append(res)
        await asyncio.sleep(6)
    
    # Clear the old cache and populate new
    GLOBAL_NEWS_CACHE.clear()
    for cat_id, text in results:
        GLOBAL_NEWS_CACHE[cat_id] = text
        
    logger.info("Prefetch complete! Cache updated.")

# --- Keyboard Utilities ---
def get_category_keyboard(selected_categories):
    """Generates the inline keyboard with toggleable selection."""
    keyboard = []
    for cat_id, cat_name in CATEGORIES.items():
        text = f"✅ {cat_name}" if cat_id in selected_categories else cat_name
        keyboard.append([InlineKeyboardButton(text, callback_data=f"toggle_{cat_id}")])
    
    keyboard.append([
        InlineKeyboardButton("Select All", callback_data="select_all"),
        InlineKeyboardButton("Clear All", callback_data="clear_all")
    ])
    keyboard.append([InlineKeyboardButton("💾 Save Preferences", callback_data="save_prefs")])
    return InlineKeyboardMarkup(keyboard)

# --- Telegram Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command and registers the user."""
    chat_id = str(update.effective_chat.id)
    
    if chat_id not in registered_users:
        all_cats = list(CATEGORIES.keys())
        registered_users[chat_id] = all_cats
        database.save_user_categories(chat_id, all_cats)
        logger.info(f"New subscriber added: {chat_id}")
    
    welcome_text = (
        "⚡ Welcome to NewsPulse AI!\n"
        "Your intelligent briefing has been initialized. I'll deliver the world's top headlines straight to your inbox every day at 9:00 AM and 5:30 PM IST.\n\n"
        "👇 Customize your feed: Select the categories you care about below.\n"
        "(Use /change_category anytime to update this list, or /stop to pause)"
    )
    
    selected_cats = registered_users.get(chat_id, [])
    reply_markup = get_category_keyboard(selected_cats)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def change_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to change category subscriptions."""
    chat_id = str(update.effective_chat.id)
    if chat_id not in registered_users:
        await update.message.reply_text("You are not currently subscribed. Type /start first!")
        return
        
    selected_cats = registered_users.get(chat_id, [])
    reply_markup = get_category_keyboard(selected_cats)
    await update.message.reply_text("Update your news preferences:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard button presses."""
    query = update.callback_query
    await query.answer()
    
    chat_id = str(query.message.chat_id)
    data = query.data
    
    if chat_id not in registered_users:
        registered_users[chat_id] = []
        
    selected_cats = registered_users[chat_id]
    
    if data.startswith("toggle_"):
        cat_id = int(data.split("_")[1])
        if cat_id in selected_cats:
            selected_cats.remove(cat_id)
        else:
            selected_cats.append(cat_id)
            
    elif data == "select_all":
        selected_cats = list(CATEGORIES.keys())
    elif data == "clear_all":
        selected_cats = []
    elif data == "save_prefs":
        # Save directly to SQLite
        database.save_user_categories(chat_id, registered_users[chat_id])
        await query.edit_message_text(f"✅ Preferences saved! You will receive news for {len(selected_cats)} categories.")
        return

    registered_users[chat_id] = selected_cats
    reply_markup = get_category_keyboard(selected_cats)
    
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    except Exception:
        pass

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /stop command and unregisters the user."""
    chat_id = str(update.effective_chat.id)
    
    if chat_id in registered_users:
        del registered_users[chat_id]
        database.remove_user(chat_id)
        await update.message.reply_text("You have been unsubscribed. You will no longer receive daily news. Type /start if you ever want to return! 👋")
        logger.info(f"User unsubscribed: {chat_id}")
    else:
        await update.message.reply_text("You are not currently subscribed.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """An admin-only command to check the subscriber count."""
    chat_id = update.effective_chat.id
    
    if chat_id == ADMIN_CHAT_ID:
        user_count = len(registered_users)
        await update.message.reply_text(f"📊 **Bot Analytics**\n\nTotal Active Subscribers: {user_count}\nDatabase engine: SQLite")
    else:
        await update.message.reply_text("You do not have permission to use this command.")

async def broadcast_news(context: ContextTypes.DEFAULT_TYPE):
    """Broadcasts pre-fetched news to all registered users based on their preferences."""
    if not registered_users:
        logger.info("No subscribers yet. Skipping broadcast.")
        return

    if not GLOBAL_NEWS_CACHE:
        logger.warning("GLOBAL_NEWS_CACHE is empty! Run prefetch first.")
        await prefetch_all_news(context)
        
    if not GLOBAL_NEWS_CACHE:
        logger.error("Failed to fetch news. Aborting broadcast.")
        await send_admin_alert(context, "Broadcast failed because GLOBAL_NEWS_CACHE is completely empty after prefetch.")
        return

    logger.info(f"Broadcasting to {len(registered_users)} users...")
    MAX_LENGTH = 4000 
    
    for chat_id_str, selected_cats in registered_users.items():
        if not selected_cats:
            continue
            
        parts = []
        for cat_id in CATEGORIES.keys():
            if cat_id in selected_cats and cat_id in GLOBAL_NEWS_CACHE:
                parts.append(GLOBAL_NEWS_CACHE[cat_id])
                
        if not parts:
            continue
            
        try:
            current_chunk = ""
            separator = "\n\n----------\n\n"
            
            for part in parts:
                if not current_chunk:
                    current_chunk = part
                elif len(current_chunk) + len(separator) + len(part) > MAX_LENGTH:
                    # Dispatch chunk sequentially, with a harsh fallback segmenter just in case 
                    # a single Gemini category wildly exceeded the 4000 char threshold.
                    for i in range(0, len(current_chunk), MAX_LENGTH):
                        await context.bot.send_message(
                            chat_id=int(chat_id_str), 
                            text=current_chunk[i:i + MAX_LENGTH], 
                            disable_web_page_preview=True 
                        )
                    current_chunk = part
                else:
                    # Append strictly safely
                    current_chunk += separator + part
            
            # Send whatever is remaining
            if current_chunk:
                for i in range(0, len(current_chunk), MAX_LENGTH):
                    await context.bot.send_message(
                        chat_id=int(chat_id_str), 
                        text=current_chunk[i:i + MAX_LENGTH], 
                        disable_web_page_preview=True 
                    )
        except Exception as e:
            logger.error(f"Failed to send to {chat_id_str}: {e}")
            
    logger.info("Broadcast complete!")

async def prefetch_manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger prefetch."""
    if update.effective_chat.id == ADMIN_CHAT_ID:
        await update.message.reply_text("Starting manual prefetch... Check console for logs.")
        try:
            await prefetch_all_news(context)
            await update.message.reply_text("Prefetch complete! Cache populated.")
        except Exception as e:
            await update.message.reply_text(f"Prefetch crashed: {e}")
    else:
         await update.message.reply_text("You do not have permission to use this command.")

async def broadcast_manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger broadcast."""
    if update.effective_chat.id == ADMIN_CHAT_ID:
        await update.message.reply_text("Starting manual broadcast...")
        await broadcast_news(context)
        await update.message.reply_text("Broadcast complete!")
    else:
         await update.message.reply_text("You do not have permission to use this command.")

# --- Main Application Setup ---
if __name__ == "__main__":
    logger.info("Starting NewsPulse AI (v2 Production Ready)...")
    
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .build()
    )
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("change_category", change_category_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    app.add_handler(CommandHandler("prefetch", prefetch_manual_command))
    app.add_handler(CommandHandler("broadcast", broadcast_manual_command))
    
    ist_tz = pytz.timezone('Asia/Kolkata')
    
    app.job_queue.run_daily(prefetch_all_news, time=datetime.time(hour=8, minute=30, tzinfo=ist_tz))
    app.job_queue.run_daily(prefetch_all_news, time=datetime.time(hour=17, minute=00, tzinfo=ist_tz))
    
    app.job_queue.run_daily(broadcast_news, time=datetime.time(hour=9, minute=0, tzinfo=ist_tz))
    app.job_queue.run_daily(broadcast_news, time=datetime.time(hour=17, minute=30, tzinfo=ist_tz))
    
    logger.info("Bot is polling. Press Ctrl+C to stop.")
    app.run_polling()
