import os
import threading
import traceback
import requests
from flask import Flask, request

from bot import handle

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://aa-analitik-bot.onrender.com"
).rstrip("/")

WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = PUBLIC_URL + WEBHOOK_PATH


def telegram_api(method, payload=None):
    if not TOKEN:
        return {
            "ok": False,
            "description": "TELEGRAM_BOT_TOKEN is not set"
        }

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            json=payload or {},
            timeout=20
        )
        return r.json()
    except Exception as e:
        return {
            "ok": False,
            "description": repr(e)
        }


def set_webhook():
    result = telegram_api(
        "setWebhook",
        {"url": WEBHOOK_URL}
    )
    print("Webhook setup:", result, flush=True)
    return result


@app.get("/")
def home():
    return {
        "ok": True,
        "service": "AA Analitik Bot",
        "webhook": "active"
    }, 200


@app.get("/health")
def health():
    return {"ok": True}, 200


@app.get("/setup")
def setup():
    result = set_webhook()
    return result, 200 if result.get("ok") else 500


@app.get("/telegram-status")
def telegram_status():
    result = telegram_api("getWebhookInfo")

    if result.get("ok"):
        data = result.get("result", {})
        data.pop("url", None)

        return {
            "ok": True,
            "webhook_expected": WEBHOOK_URL,
            "webhook_info": data
        }, 200

    return result, 500


@app.post(WEBHOOK_PATH)
def telegram():
    update = request.get_json(silent=True) or {}

    print(
        "TELEGRAM UPDATE RECEIVED:",
        {
            "update_id": update.get("update_id"),
            "text": (update.get("message") or {}).get("text"),
            "chat_id": (update.get("message") or {}).get("chat", {}).get("id")
        },
        flush=True
    )

    # IMPORTANT:
    # Process the Telegram update before returning HTTP 200.
    # This prevents Render from killing a daemon thread immediately
    # after the webhook request finishes.
    try:
        handle(update)
        print("TELEGRAM UPDATE PROCESSED", flush=True)
    except Exception as e:
        print("TELEGRAM UPDATE ERROR:", repr(e), flush=True)
        traceback.print_exc()

    return "ok", 200


# Configure webhook once when the process starts.
# Render health checks never call setWebhook because / is harmless.
threading.Thread(
    target=set_webhook,
    daemon=True
).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
