import os
import logging
import base64
import asyncio
from flask import Flask, request
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- CONFIGURATION ---
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Essential Environment Variables
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
# Render provides the URL via RENDER_EXTERNAL_URL (e.g., https://app.onrender.com)
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

# --- eFOOTBALL LOGIC ---
SYSTEM_PROMPT = """You are an elite eFootball betting analyst. 
Extract data from the screenshot. If no 5% edge or 7/10 confidence exists, return ONLY: PASS.
Otherwise, provide the structured betting analysis."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 **Agent Chloe Pro: Vision Mode**\nSend a match screenshot to begin analysis.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("🔍 *Scanning patterns...*", parse_mode="Markdown")
    try:
        photo = await update.message.photo[-1].get_file()
        photo_bytes = await photo.download_as_bytearray()
        encoded = base64.b64encode(photo_bytes).decode('utf-8')

        response = client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Analyze this eFootball screenshot."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
                ]}
            ]
        )
        await status.edit_text(f"📊 *Analysis*\n\n{response.choices[0].message.content}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error: {e}")
        await status.edit_text("❌ Analysis failed. Ensure the screenshot is clear.")

# --- WEBHOOK AUTO-MANAGER ---
app = Flask(__name__)
ptb_app = ApplicationBuilder().token(TOKEN).build()

# Initialize handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
ptb_app.add_handler(MessageHandler(filters.ALL & ~filters.PHOTO, 
    lambda u, c: u.message.reply_text("⚠️ Please send a screenshot.")))

@app.before_serving
async def setup_webhook():
    """Automatic Webhook Manager: Configures Telegram on startup."""
    webhook_url = f"{BASE_URL}/webhook/{TOKEN}"
    current_info = await ptb_app.bot.get_webhook_info()
    
    if current_info.url != webhook_url:
        logger.info(f"Setting Webhook to: {webhook_url}")
        await ptb_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    
    # Start the PTB application context
    await ptb_app.initialize()
    await ptb_app.start()

@app.after_serving
async def cleanup():
    """Graceful shutdown logic."""
    await ptb_app.stop()
    await ptb_app.shutdown()

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
async def webhook_handler():
    """Main entry point for Telegram updates."""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        await ptb_app.process_update(update)
    return "OK", 200

@app.route("/health")
def health():
    return "Alive", 200

# --- RENDER ENTRY POINT ---
# Use 'hypercorn' or 'uvicorn' to run the async Flask app
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    # We use the Quart-like async behavior in Flask 2.0+
    uvicorn.run("main:app", host="0.0.0.0", port=port, loop="asyncio")
