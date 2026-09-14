import os
import io
import threading
import zipfile
from datetime import date

import requests
from flask import Flask, request, Response, render_template_string

from bot import handle, run_scan_job
from moex_downloader import fetch

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AUTO_SCAN_TOKEN = os.getenv("AUTO_SCAN_TOKEN", "").strip()

PUBLIC_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://aa-analitik-bot.onrender.com"
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


# -------------------------------------------------
# TELEGRAM
# -------------------------------------------------

@app.post(WEBHOOK_PATH)
def telegram():
    update = request.get_json(silent=True) or {}
    try:
        handle(update)
    except Exception as e:
        print("Update processing error:", repr(e), flush=True)
    return "ok", 200


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
            "webhook_info": data,
        }, 200
    return result, 500


# -------------------------------------------------
# MAIN
# -------------------------------------------------

@app.get("/")
def home():
    return {
        "ok": True,
        "service": "AA Analitik Bot",
        "webhook_url": WEBHOOK_URL,
        "moex_downloader": "/moex",
        "auto_scan": "/auto-scan",
    }, 200


@app.get("/health")
def health():
    return {"ok": True}, 200


# -------------------------------------------------
# AUTO SCAN
# -------------------------------------------------

@app.post("/auto-scan")
def auto_scan():
    supplied = request.headers.get("X-AA-AUTO-SCAN-TOKEN", "").strip()

    if not AUTO_SCAN_TOKEN or supplied != AUTO_SCAN_TOKEN:
        return {"ok": False, "error": "unauthorized"}, 401

    try:
        print("AUTO SCAN START", flush=True)

        result, events = run_scan_job(
            chat=None,
            notify_events=False,
        )

        meta = result.get("meta", {})

        print(
            f"AUTO SCAN FINISHED events={len(events)} "
            f"screened={meta.get('screened', 0)} "
            f"analyzed={meta.get('analyzed', 0)}",
            flush=True,
        )

        return {
            "ok": True,
            "events": len(events),
            "screened": meta.get("screened", 0),
            "analyzed": meta.get("analyzed", 0),
        }, 200

    except Exception as e:
        print("AUTO SCAN ERROR:", repr(e), flush=True)
        return {"ok": False, "error": str(e)[:1000]}, 500


# -------------------------------------------------
# MOEX DOWNLOADER
# -------------------------------------------------

MOEX_HTML = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AA Analitik — MOEX Data</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    background: radial-gradient(circle at top,#162447 0,#080d18 45%,#05070c 100%);
    color: #f5f7fb;
    font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    min-height: 100vh;
}
.container { max-width: 760px; margin:auto; padding:24px 18px 60px; }
.header { margin-bottom:24px; }
.logo { font-size:13px; letter-spacing:2px; color:#7dd3fc; font-weight:700; }
h1 { font-size:32px; margin:8px 0; }
.subtitle { color:#9ca9bc; line-height:1.5; }
.card {
    background:rgba(15,23,42,.86);
    border:1px solid rgba(148,163,184,.16);
    border-radius:20px;
    padding:20px;
    margin-top:18px;
    box-shadow:0 20px 60px rgba(0,0,0,.35);
}
label { display:block; margin:15px 0 7px; color:#cbd5e1; font-size:14px; }
select,input {
    width:100%; border:1px solid #26344b; background:#0b1220; color:white;
    border-radius:12px; padding:13px; font-size:16px; outline:none;
}
input:focus,select:focus { border-color:#38bdf8; }
button {
    width:100%; margin-top:20px; border:0; border-radius:13px; padding:15px;
    font-size:16px; font-weight:700; color:#03111c; background:#38bdf8; cursor:pointer;
}
button:active { transform:scale(.99); }
.info { margin-top:16px; color:#94a3b8; font-size:13px; line-height:1.5; }
.badge {
    display:inline-block; padding:5px 9px; border-radius:999px;
    background:rgba(56,189,248,.12); color:#7dd3fc; font-size:12px; margin-top:8px;
}
</style>
</head>
<body>
<div class="container">
<div class="header">
<div class="logo">AA ANALITIK</div>
<h1>MOEX Historical Data</h1>
<div class="subtitle">
Загрузка минутных данных MOEX для исторического тестирования протокола v1.3.
</div>
<div class="badge">M1 • MOEX ISS • Backtest</div>
</div>

<div class="card">
<form method="POST" action="/moex/download">

<label>Тип рынка</label>
<select name="market">
<option value="stock">Акции MOEX</option>
<option value="futures">Фьючерсы MOEX</option>
</select>

<label>SECID</label>
<input name="secids" placeholder="SBER, GAZP, LKOH" required>

<label>Дата начала</label>
<input type="date" name="start" required>

<label>Дата окончания</label>
<input type="date" name="end" required>

<button type="submit">Загрузить M1 данные</button>

<div class="info">
Для нескольких инструментов укажи SECID через запятую.
<br><br>
<b>SBER, GAZP, LKOH</b>
<br><br>
Результатом будет ZIP-архив с отдельным CSV для каждого инструмента.
</div>

</form>
</div>
</div>
</body>
</html>
"""


@app.get("/moex")
def moex():
    return render_template_string(MOEX_HTML)


@app.post("/moex/download")
def moex_download():
    market = request.form.get("market", "stock")

    secids_raw = request.form.get("secids", "")
    secids = [
        x.strip().upper()
        for x in secids_raw.split(",")
        if x.strip()
    ]

    if not secids:
        return "SECID is required", 400

    try:
        start = date.fromisoformat(request.form["start"])
        end = date.fromisoformat(request.form["end"])
    except Exception:
        return "Invalid dates", 400

    if start > end:
        return "Start date must be before end date", 400

    memory = io.BytesIO()

    try:
        with zipfile.ZipFile(
            memory,
            "w",
            zipfile.ZIP_DEFLATED
        ) as archive:

            for secid in secids:
                print(
                    f"Downloading {market} {secid} "
                    f"{start} -> {end}",
                    flush=True,
                )

                csv_data = fetch(
                    secid,
                    market,
                    start,
                    end,
                )

                archive.writestr(
                    f"{secid}_M1.csv",
                    csv_data,
                )

    except Exception as e:
        print("MOEX download error:", repr(e), flush=True)
        return f"MOEX download error: {e}", 500

    memory.seek(0)

    return Response(
        memory.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition":
                "attachment; filename=MOEX_M1_data.zip"
        },
    )


# -------------------------------------------------
# WEBHOOK STARTUP
# -------------------------------------------------

threading.Thread(
    target=set_webhook,
    daemon=True,
).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
