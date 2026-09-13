import os
import threading
import requests
from flask import Flask, request

from bot import handle

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://aa-analitik-bot.onrender.com",
).rstrip("/")
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = PUBLIC_URL + WEBHOOK_PATH


@app.get("/")
def home():
    return "AA Analitik Bot is running", 200


@app.get("/health")
def health():
    return {"ok": True}, 200


def set_webhook():
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": WEBHOOK_URL},
            timeout=20,
        )
        print("Webhook:", r.status_code, r.text)
    except Exception as e:
        print("Webhook setup error:", repr(e))


def process_update(update):
    try:
        handle(update)
    except Exception as e:
        print("Update processing error:", repr(e))


@app.post(WEBHOOK_PATH)
def telegram():
    update = request.get_json(silent=True) or {}

    # Telegram gets 200 immediately; /scan continues in background.
    threading.Thread(
        target=process_update,
        args=(update,),
        daemon=True,
    ).start()

    return "ok", 200


# Register webhook after the application starts.
threading.Thread(target=set_webhook, daemon=True).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
