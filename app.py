import os
from flask import Flask, request
from bot import handle_update
app=Flask(__name__)
@app.get("/")
def health(): return "AA Analitik Bot is running",200
@app.post("/telegram/webhook")
def webhook():
    handle_update(request.get_json(silent=True) or {})
    return {"ok":True}
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
