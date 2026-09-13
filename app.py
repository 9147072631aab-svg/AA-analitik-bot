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


def telegram_api(method, payload=None):
    if not TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN is not set"}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            json=payload or {},
            timeout=20,
        )
        return r.json()
    except Exception as e:
        return {"ok": False, "description": repr(e)}


def set_webhook():
    result = telegram_api("setWebhook", {"url": WEBHOOK_URL})
    print("Webhook setup:", result, flush=True)
    return result


@app.get("/")
def home():
    # Opening the Render URL also refreshes the Telegram webhook.
    result = set_webhook()
    return {
        "ok": True,
        "service": "AA Analitik Bot",
        "webhook_url": WEBHOOK_URL,
        "webhook_setup": result.get("ok", False),
    }, 200


@app.get("/health")
def health():
    return {"ok": True}, 200


@app.get("/setup")
def setup():
    # Open https://aa-analitik-bot.onrender.com/setup once after deploy.
    result = set_webhook()
    return result, 200 if result.get("ok") else 500


@app.get("/telegram-status")
def telegram_status():
    # Diagnostics without exposing the bot token.
    result = telegram_api("getWebhookInfo")
    if result.get("ok"):
        data = result.get("result", {})
        data.pop("url", None)
        return {
            "ok": True,
            "webhook_expected": WEBHOOK_URL,
            "webhook_info": data,
        }, 200
    return result, 500


def process_update(update):
    try:
        handle(update)
    except Exception as e:
        print("Update processing error:", repr(e), flush=True)


@app.post(WEBHOOK_PATH)
def telegram():
    update = request.get_json(silent=True) or {}

    # Telegram receives HTTP 200 immediately.
    # /scan can continue in the background without blocking the webhook.
    threading.Thread(
        target=process_update,
        args=(update,),
        daemon=True,
    ).start()

    return "ok", 200


# Register automatically when Render starts the process.
threading.Thread(target=set_webhook, daemon=True).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
