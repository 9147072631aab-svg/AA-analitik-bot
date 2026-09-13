from flask import Flask,request
from bot import handle
import os
app=Flask(__name__)
@app.get("/")
def home(): return "AA Analitik Bot is running",200
@app.get("/health")
def health(): return {"ok":True},200
@app.post("/telegram")
def telegram():
    handle(request.get_json(force=True,silent=True) or {}); return "ok",200
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
