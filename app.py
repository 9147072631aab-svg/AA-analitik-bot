import os
import io
import threading
import zipfile
from datetime import date

import requests
from flask import Flask, request, Response, render_template_string

import bot
from bot import handle
import trade_sync
import scan_scheduler

app = Flask(__name__)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
AUTO_SCAN_TOKEN = os.getenv('AUTO_SCAN_TOKEN', '').strip()
PUBLIC_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://aa-analitik-bot.onrender.com').rstrip('/')
WEBHOOK_PATH = '/telegram'
WEBHOOK_URL = PUBLIC_URL + WEBHOOK_PATH

# Explicit scheduler startup; avoids relying on Python's sitecustomize import hook.
AUTO_SCAN_SCHEDULER = scan_scheduler.install(bot)


def telegram_api(method, payload=None, timeout=8):
    if not TOKEN:
        return {'ok': False, 'description': 'TELEGRAM_BOT_TOKEN is not set'}
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TOKEN}/{method}',
            json=payload or {},
            timeout=timeout,
        )
        return r.json()
    except Exception as e:
        return {'ok': False, 'description': repr(e)}


def set_webhook():
    result = telegram_api('setWebhook', {'url': WEBHOOK_URL})
    print('Webhook setup:', result, flush=True)
    return result


def _process_update(update):
    try:
        handle(update)
    except Exception as e:
        print('Update processing error:', repr(e), flush=True)


@app.post(WEBHOOK_PATH)
def telegram():
    update = request.get_json(silent=True) or {}
    # Return HTTP 200 immediately. Telegram updates are processed in a
    # background thread so outbound Telegram/market requests cannot block
    # the gunicorn sync worker.
    threading.Thread(
        target=_process_update,
        args=(update,),
        daemon=True,
        name='telegram-update-worker',
    ).start()
    return 'ok', 200


@app.get('/setup')
def setup():
    result = set_webhook()
    return result, 200 if result.get('ok') else 500


@app.get('/telegram-status')
def telegram_status():
    result = telegram_api('getWebhookInfo')
    if result.get('ok'):
        data = result.get('result', {})
        data.pop('url', None)
        return {'ok': True, 'webhook_expected': WEBHOOK_URL, 'webhook_info': data}, 200
    return result, 500


@app.get('/')
def home():
    return {
        'ok': True,
        'service': 'AA Analitik Bot',
        'version': '3.2.7',
        'webhook_url': WEBHOOK_URL,
        'moex_downloader': '/moex',
        'auto_scan': '/auto-scan',
        'active_signals': '/active-signals',
        'trades': '/trades',
    }, 200


@app.get('/health')
def health():
    return {'ok': True}, 200


@app.post('/auto-scan')
def auto_scan():
    supplied = request.headers.get('X-AA-AUTO-SCAN-TOKEN', '').strip()
    if not AUTO_SCAN_TOKEN or supplied != AUTO_SCAN_TOKEN:
        return {'ok': False, 'error': 'unauthorized'}, 401
    try:
        print('AUTO SCAN REQUEST ACCEPTED', flush=True)
        chat = bot.LAST_CHAT_ID
        if not bot.enqueue_scan(chat):
            print('AUTO SCAN ALREADY RUNNING', flush=True)
            return {'ok': False, 'error': 'scan already running'}, 409
        print(f'AUTO SCAN QUEUED chat={chat}', flush=True)
        return {'ok': True, 'queued': True, 'message': 'scan queued'}, 202
    except Exception as e:
        print('AUTO SCAN ERROR:', repr(e), flush=True)
        return {'ok': False, 'error': str(e)[:1000]}, 500
