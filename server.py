"""Trusted-owner Telegram Mini App control plane.

The server never executes operating-system commands: it queues a small,
explicit command set for an authenticated local agent to validate and execute.
"""
from __future__ import annotations

import asyncio, base64, hashlib, hmac, json, os, secrets, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
OWNER_API_KEY = os.environ.get("OWNER_API_KEY", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.environ.get("DATABASE_PATH", "data/control.db")
TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "45"))

if not OWNER_ID or not OWNER_API_KEY:
    raise RuntimeError("OWNER_TELEGRAM_ID and OWNER_API_KEY must be set in .env")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Trusted PC Control")
app.mount("/static", StaticFiles(directory="static"), name="static")

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db() -> None:
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, os TEXT NOT NULL,
          secret TEXT NOT NULL, paired_at INTEGER NOT NULL, last_seen INTEGER,
          last_ip TEXT, metrics TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS pairings (code_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, created_at INTEGER NOT NULL, finished_at INTEGER, result TEXT, FOREIGN KEY(device_id) REFERENCES devices(id));
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, action TEXT NOT NULL, ok INTEGER NOT NULL, details TEXT, created_at INTEGER NOT NULL);
        """)
init_db()

async def telegram_message(text: str) -> None:
    if not BOT_TOKEN: return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": OWNER_ID, "text": text})

async def telegram_photo(image_b64: str, caption: str) -> None:
    if not BOT_TOKEN: return
    image = base64.b64decode(image_b64, validate=True)
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data={"chat_id": str(OWNER_ID), "caption": caption}, files={"photo": ("screenshot.png", image, "image/png")})

async def status_watcher() -> None:
    """Persist transitions so restart does not repeatedly announce the same state."""
    states: dict[str, bool] = {}
    while True:
        try:
            with db() as con:
                rows = con.execute("SELECT id,name,last_seen FROM devices").fetchall()
            for row in rows:
                online = bool(row["last_seen"] and now() - row["last_seen"] <= TIMEOUT)
                old = states.setdefault(row["id"], online)
                if old != online:
                    states[row["id"]] = online
                    label = "появился Online" if online else "ушёл Offline"
                    with db() as con: con.execute("INSERT INTO events(device_id,action,ok,details,created_at) VALUES(?,?,?,?,?)", (row["id"], "status", 1, label, now()))
                    await telegram_message(f"🖥 {row['name']} {label}")
        except Exception:
            pass
        await asyncio.sleep(10)

@app.on_event("startup")
async def start_watcher():
    asyncio.create_task(status_watcher())

def fail(status: int, message: str): raise HTTPException(status, message)
def now() -> int: return int(time.time())
def code_hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()

def require_admin(authorization: str | None):
    if not authorization or not hmac.compare_digest(authorization.removeprefix("Bearer "), OWNER_API_KEY): fail(401, "Invalid administrator key")

def validate_telegram_init(init_data: str | None) -> None:
    """Telegram's documented WebApp initData HMAC validation; no dev bypass."""
    if not BOT_TOKEN or not init_data: fail(401, "Telegram authorization required")
    pairs = dict(x.split("=", 1) for x in init_data.split("&") if "=" in x)
    supplied = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied): fail(401, "Invalid Telegram authorization")
    try: user = json.loads(unquote_plus(pairs["user"]))
    except Exception: fail(401, "Missing Telegram user")
    if int(user.get("id", 0)) != OWNER_ID: fail(403, "This account is not authorized")

def agent_device(device_id: str, timestamp: str | None, signature: str | None, raw: bytes):
    if not timestamp or not signature or abs(now() - int(timestamp)) > 300: fail(401, "Expired agent request")
    with db() as con: row = con.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not row: fail(401, "Unknown device")
    signed = timestamp.encode() + b"." + raw
    expected = hmac.new(row["secret"].encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature): fail(401, "Invalid agent signature")
    return row

class PairRequest(BaseModel): code: str; device_id: str = Field(pattern=r"^[a-f0-9-]{16,64}$"); name: str = Field(min_length=1,max_length=80); os: str = Field(min_length=1,max_length=120)
class Heartbeat(BaseModel): metrics: dict[str, Any]
class CommandResult(BaseModel): command_id: str; ok: bool; result: dict[str, Any] = {}
class QueueCommand(BaseModel): action: str; payload: dict[str, Any] = {}

@app.get("/health")
def health(): return {"ok": True}

@app.post("/api/admin/pairings")
def make_pairing(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    code = secrets.token_urlsafe(18)
    with db() as con: con.execute("INSERT INTO pairings VALUES (?, ?, 0)", (code_hash(code), now()+900))
    return {"pairing_code": code, "expires_in_seconds": 900}

@app.post("/api/agent/pair")
def pair(req: PairRequest, request: Request):
    with db() as con:
        item = con.execute("SELECT * FROM pairings WHERE code_hash=?", (code_hash(req.code),)).fetchone()
        if not item or item["used"] or item["expires_at"] < now(): fail(401, "Invalid or expired pairing code")
        if con.execute("SELECT 1 FROM devices WHERE id=?", (req.device_id,)).fetchone(): fail(409, "Device already paired")
        secret = secrets.token_urlsafe(32)
        con.execute("UPDATE pairings SET used=1 WHERE code_hash=?", (code_hash(req.code),))
        con.execute("INSERT INTO devices(id,name,os,secret,paired_at,last_seen,last_ip) VALUES(?,?,?,?,?,?,?)", (req.device_id,req.name,req.os,secret,now(),now(),request.client.host))
        con.execute("INSERT INTO events(device_id,action,ok,details,created_at) VALUES(?,?,?,?,?)", (req.device_id,"paired",1,"Device paired",now()))
    return {"device_secret": secret}

@app.post("/api/agent/{device_id}/heartbeat")
async def heartbeat(device_id: str, request: Request, x_agent_timestamp: str | None = Header(default=None), x_agent_signature: str | None = Header(default=None)):
    raw = await request.body(); agent_device(device_id,x_agent_timestamp,x_agent_signature,raw); data=Heartbeat.model_validate_json(raw)
    restarted = bool(data.metrics.pop("agent_started", False))
    with db() as con:
        con.execute("UPDATE devices SET last_seen=?,last_ip=?,metrics=? WHERE id=?", (now(),request.client.host,json.dumps(data.metrics),device_id))
        if restarted:
            name=con.execute("SELECT name FROM devices WHERE id=?",(device_id,)).fetchone()["name"]
            con.execute("INSERT INTO events(device_id,action,ok,details,created_at) VALUES(?,?,?,?,?)", (device_id,"agent_started",1,"Client restarted",now()))
    if restarted: await telegram_message(f"🖥 Клиент {name} был перезапущен")
    return {"ok":True}

@app.get("/api/agent/{device_id}/commands")
async def poll(device_id: str, request: Request, x_agent_timestamp: str | None = Header(default=None), x_agent_signature: str | None = Header(default=None)):
    raw=await request.body(); agent_device(device_id,x_agent_timestamp,x_agent_signature,raw)
    with db() as con:
        row=con.execute("SELECT * FROM commands WHERE device_id=? AND status='queued' ORDER BY created_at LIMIT 1",(device_id,)).fetchone()
        if row: con.execute("UPDATE commands SET status='running' WHERE id=?",(row["id"],)); return {"command":{"id":row["id"],"action":row["action"],"payload":json.loads(row["payload"])}}
    return {"command":None}

@app.post("/api/agent/{device_id}/result")
async def result(device_id: str, request: Request, x_agent_timestamp: str | None = Header(default=None), x_agent_signature: str | None = Header(default=None)):
    raw=await request.body(); agent_device(device_id,x_agent_timestamp,x_agent_signature,raw); data=CommandResult.model_validate_json(raw)
    screenshot = data.result.pop("screenshot_b64", None)
    with db() as con:
        command=con.execute("SELECT * FROM commands WHERE id=? AND device_id=?",(data.command_id,device_id)).fetchone()
        if not command: fail(404,"Command not found")
        status="completed" if data.ok else "failed"; details=json.dumps(data.result)
        con.execute("UPDATE commands SET status=?,finished_at=?,result=? WHERE id=?",(status,now(),details,data.command_id))
        con.execute("INSERT INTO events(device_id,action,ok,details,created_at) VALUES(?,?,?,?,?)",(device_id,command["action"],data.ok,details,now()))
        device_name=con.execute("SELECT name FROM devices WHERE id=?",(device_id,)).fetchone()["name"]
    if not data.ok:
        await telegram_message(f"⚠️ Команда {command['action']} на {device_name} завершилась ошибкой: {data.result.get('error', 'unknown')}")
    elif screenshot:
        try: await telegram_photo(screenshot, f"Скриншот: {device_name}")
        except (ValueError, httpx.HTTPError): await telegram_message(f"⚠️ Не удалось доставить скриншот с {device_name}")
    return {"ok":True}

@app.get("/api/devices")
def devices(x_telegram_init_data: str | None = Header(default=None)):
    validate_telegram_init(x_telegram_init_data)
    with db() as con: rows=con.execute("SELECT id,name,os,last_seen,last_ip,metrics FROM devices ORDER BY name").fetchall()
    return [{**dict(r),"online": bool(r["last_seen"] and now()-r["last_seen"]<=TIMEOUT),"metrics":json.loads(r["metrics"])} for r in rows]

@app.post("/api/devices/{device_id}/commands")
def queue(device_id: str, item: QueueCommand, x_telegram_init_data: str | None = Header(default=None)):
    validate_telegram_init(x_telegram_init_data)
    allowed={"screenshot","lock","shutdown","restart","list_apps","launch_app","close_app"}
    if item.action not in allowed: fail(400,"Unsupported action")
    with db() as con:
        if not con.execute("SELECT 1 FROM devices WHERE id=?",(device_id,)).fetchone(): fail(404,"Device not found")
        command_id=secrets.token_urlsafe(12); con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,NULL,NULL)",(command_id,device_id,item.action,json.dumps(item.payload),"queued",now()))
    return {"command_id":command_id,"status":"queued"}

@app.get("/api/events")
def events(x_telegram_init_data: str | None = Header(default=None)):
    validate_telegram_init(x_telegram_init_data)
    with db() as con: return [dict(r) for r in con.execute("SELECT e.*,d.name device_name FROM events e LEFT JOIN devices d ON d.id=e.device_id ORDER BY e.created_at DESC LIMIT 30")]

@app.get("/")
def index(): return FileResponse("static/index.html")

@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    """Minimal bot entry: only the configured owner receives Mini App button."""
    msg=update.get("message",{}); chat=msg.get("chat",{}); text=msg.get("text","")
    if chat.get("id") != OWNER_ID or text != "/start" or not BOT_TOKEN: return {"ok":True}
    base=os.environ.get("PUBLIC_BASE_URL","").rstrip("/")
    if not base: return {"ok":True}
    payload={"chat_id":OWNER_ID,"text":"Откройте панель управления:","reply_markup":{"keyboard":[[{"text":"🖥 Панель ПК","web_app":{"url":base}}]],"resize_keyboard":True}}
    async with httpx.AsyncClient(timeout=10) as client: await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json=payload)
    return {"ok":True}
