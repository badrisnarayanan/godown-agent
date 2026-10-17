"""Live dashboard: POST /api/order streams pipeline stages (SSE). Run: uvicorn dashboard:app --port 8020"""
import base64, hmac, json, os, re, sqlite3, time
from collections import defaultdict, deque
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "my_agent" / ".env")
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from my_agent.agent import root_agent

ROOT = Path(__file__).parent
DB, LESSONS = ROOT / "mcp_server" / "warehouse.db", ROOT / "mcp_server" / "lessons.json"
import sys
sys.path.insert(0, str(ROOT / "mcp_server"))
import warehouse_server as W  # reuse GODOWNS + geocoder for tracking positions

UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)
INDEX = UPLOADS / "index.json"  # tracking_id -> uploaded source file
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "audio/wav": "wav", "audio/mpeg": "mp3",
       "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/webm": "webm"}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_FILES = 200
MAGIC = {  # mime -> byte prefixes: reject files whose content does not match their claimed type
    "image/png": [b"\x89PNG"], "image/jpeg": [b"\xff\xd8\xff"], "image/webp": [b"RIFF"],
    "audio/wav": [b"RIFF"], "audio/mpeg": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"], "audio/ogg": [b"OggS"],
    "audio/mp4": [b"\x00\x00\x00"], "audio/webm": [b"\x1a\x45\xdf\xa3"],
}
_hits = defaultdict(deque)


def client_ip(req: Request):
    return (req.headers.get("x-forwarded-for") or (req.client.host if req.client else "?")).split(",")[0].strip()


def limit(bucket: str, per_minute: int):
    """Sliding-window rate limit per client IP. Protects LLM and Maps spend."""
    def dep(req: Request):
        q, now = _hits[(bucket, client_ip(req))], time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            raise HTTPException(429, f"Too many requests. Limit is {per_minute} per minute.")
        q.append(now)
    return dep


def is_admin(req: Request):
    tok = os.environ.get("ADMIN_TOKEN")
    return (not tok) or hmac.compare_digest(req.headers.get("x-admin-token", ""), tok)


def admin(req: Request):
    """Write endpoints need the admin token when ADMIN_TOKEN is set (always set it on a public deployment)."""
    if not is_admin(req):
        raise HTTPException(401, "Admin token required")


def check_upload(b64: str, mime: str):
    if mime not in EXT or mime not in MAGIC:
        raise HTTPException(415, "Unsupported file type. Use PNG, JPEG, WebP, WAV, MP3, OGG or M4A.")
    if len(b64) > MAX_UPLOAD_BYTES * 4 // 3 + 16:
        raise HTTPException(413, "File too large. Limit is 5 MB.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(400, "File is not valid base64")
    if not any(raw.startswith(m) for m in MAGIC[mime]):
        raise HTTPException(415, "File content does not match its type")
    return raw


def save_upload(raw: bytes, mime: str, prefix=""):
    name = f"{prefix}{int(time.time())}-{os.urandom(3).hex()}.{EXT[mime]}"
    (UPLOADS / name).write_bytes(raw)
    files = sorted(UPLOADS.glob("*.*"), key=lambda f: f.stat().st_mtime)
    for old in [f for f in files if f.name != "index.json"][:-MAX_UPLOAD_FILES]:
        old.unlink(missing_ok=True)  # keep the folder bounded
    return name


app = FastAPI()


@app.middleware("http")
async def guard(req: Request, call_next):
    if int(req.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    resp = await call_next(req)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' data: blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    return resp
runner = InMemoryRunner(agent=root_agent, app_name="dash")
from my_agent.inventory_agent import inventory_agent
inv_runner = InMemoryRunner(agent=inventory_agent, app_name="inv")


@app.get("/")
def index():
    return FileResponse(ROOT / "dashboard.html")


@app.get("/uploads/{name}")
def uploaded(name: str):
    f = UPLOADS / Path(name).name
    return FileResponse(f) if f.exists() else JSONResponse({"error": "not found"}, status_code=404)


@app.get("/samples/{name}")
def sample(name: str):
    return FileResponse(ROOT / "samples" / Path(name).name)


@app.get("/api/state")
def state(req: Request):
    stock, ships = [], []
    if DB.exists():
        c = sqlite3.connect(DB)
        stock = [dict(godown=g, sku=s, qty=q) for g, s, q in c.execute("SELECT godown,sku,qty FROM stock ORDER BY sku,godown")]
        ships = [json.loads(d) for (d,) in c.execute("SELECT data FROM shipments ORDER BY created DESC LIMIT 5")]
        c.close()
    lessons = json.loads(LESSONS.read_text()) if LESSONS.exists() else []
    allx = []
    if DB.exists():
        c = sqlite3.connect(DB)
        allx = [json.loads(d) for (d,) in c.execute("SELECT data FROM shipments ORDER BY created")]
        c.close()
    fin = {"orders": len(allx), "units": sum(x["qty"] for x in allx),
           "revenue": sum(x.get("revenue", 0) for x in allx), "product_cost": sum(x.get("product_cost", 0) for x in allx),
           "shipping": sum(x.get("shipping_cost", 0) for x in allx), "packaging": sum(x.get("packaging_cost", 0) for x in allx),
           "by_sku": {}}
    for x in allx:
        b = fin["by_sku"].setdefault(x["sku"], {"units": 0, "revenue": 0})
        b["units"] += x["qty"]; b["revenue"] += x.get("revenue", 0)
    fin["margin"] = fin["revenue"] - fin["product_cost"] - fin["shipping"] - fin["packaging"]
    if not is_admin(req):  # public viewers get tracking IDs only, no customer names or addresses
        ships = [{"tracking_id": x["tracking_id"]} for x in ships]
    return {"stock": stock, "lessons": lessons[-12:], "shipments": ships, "dispatched": len(allx), "lesson_count": len(lessons), "finance": fin}


@app.get("/api/config")
def config():
    return {"maps": bool(os.environ.get("GOOGLE_MAPS_API_KEY")) and os.environ.get("USE_REAL_MAPS") == "1"}


@app.post("/api/staticmap", dependencies=[Depends(limit("map", 30))])
async def staticmap(req: Request):
    """Server-side proxy for Google Static Maps so the API key never reaches the browser."""
    import urllib.parse, urllib.request
    from fastapi.responses import Response
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return JSONResponse({"error": "no key"}, status_code=404)
    b = await req.json()
    try:
        f, t = [float(x) for x in b["from"]][:2], [float(x) for x in b["to"]][:2]
        assert all(-90 <= v[0] <= 90 and -180 <= v[1] <= 180 for v in (f, t))
        poly = b.get("poly") or ""
        assert len(poly) <= 6000 and all(63 <= ord(ch) <= 126 for ch in poly)
        b["poly"] = poly
    except Exception:
        return JSONResponse({"error": "invalid map request"}, status_code=400)
    path = f"color:0x12806Fff|weight:5|enc:{b['poly']}" if b.get("poly") else f"color:0x12806Fff|weight:5|{f[0]},{f[1]}|{t[0]},{t[1]}"
    q = [("size", "640x420"), ("scale", "2"), ("maptype", "roadmap"), ("path", path),
         ("markers", f"color:0x13202B|label:G|{f[0]},{f[1]}"), ("markers", f"color:0xF2B01E|label:D|{t[0]},{t[1]}"), ("key", key)]
    try:
        with urllib.request.urlopen("https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q), timeout=6) as r:
            return Response(r.read(), media_type=r.headers.get_content_type())
    except Exception as e:
        return JSONResponse({"error": "map unavailable"}, status_code=502)


DEMO_SECONDS_PER_HOUR = 5  # demo clock: 1 hour of ETA plays out in 5 seconds


@app.get("/api/track/{tid}", dependencies=[Depends(limit("track", 60))])
def track(tid: str):
    if not DB.exists():
        return JSONResponse({"error": "unknown tracking id"}, status_code=404)
    c = sqlite3.connect(DB)
    row = c.execute("SELECT data, created FROM shipments WHERE id=?", (tid.strip().upper(),)).fetchone()
    c.close()
    if not row:
        return JSONResponse({"error": "unknown tracking id"}, status_code=404)
    d, created = json.loads(row[0]), row[1]
    total = max(25, d["eta_hours"] * DEMO_SECONDS_PER_HOUR)
    frac = min(1.0, (time.time() - created) / total)
    gname, glat, glon = W.GODOWNS[d["godown"]]
    dest = W._geocode(d["destination"])[0] or (glat, glon)
    move = max(0.0, min(1.0, (frac - 0.2) / 0.7))
    steps = [("Order received", 0), (f"Packed at {gname}", 0.08), ("Picked up by " + d["carrier"], 0.2),
             ("In transit", 0.35), ("Out for delivery", 0.9), ("Delivered", 1.0)]
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    return {"source": idx.get(d["tracking_id"]), "shipment": d, "progress": round(frac, 3), "remaining_hours": round(d["eta_hours"] * (1 - frac), 1),
            "delivered": frac >= 1, "from": [glat, glon], "to": list(dest),
            "position": [glat + (dest[0] - glat) * move, glon + (dest[1] - glon) * move],
            "steps": [{"label": l, "done": frac >= t} for l, t in steps]}


@app.get("/api/catalog")
def catalog():
    return W.list_catalog()


@app.post("/api/restock", dependencies=[Depends(admin)])
async def api_restock(req: Request):
    b = await req.json()
    return W.restock(b.get("godown", ""), b.get("sku", ""), int(b.get("qty", 0) or 0))


@app.post("/api/item", dependencies=[Depends(admin)])
async def api_item(req: Request):
    b = await req.json()
    try:
        return W.add_item(b["sku"], b["name"], float(b["weight_kg"]), float(b["volume_l"]), float(b["price"]),
                          float(b["cost"]), b.get("godown", "GD-BLR"), int(b.get("qty", 0) or 0))
    except (KeyError, ValueError) as e:
        return {"error": f"invalid input: {e}"}


@app.post("/api/inventory-agent", dependencies=[Depends(admin), Depends(limit("inv", 12))])
async def inventory_chat(req: Request):
    """Chat with the inventory manager agent. A client-held session id keeps confirmations in context."""
    b = await req.json()
    sid = b.get("session") or "default"
    if not await inv_runner.session_service.get_session(app_name="inv", user_id="u", session_id=sid):
        await inv_runner.session_service.create_session(app_name="inv", user_id="u", session_id=sid)
    parts = []
    if b.get("file_b64"):
        raw = check_upload(b["file_b64"], str(b.get("mime", "")))
        save_upload(raw, b["mime"], "inv-")
        parts.append(types.Part.from_bytes(data=raw, mime_type=b["mime"]))
    parts.append(types.Part(text=str(b.get("text") or "Process the attached document.")[:2000]))
    actions, reply = [], ""
    t0 = time.time()
    try:
        async for e in inv_runner.run_async(user_id="u", session_id=sid, new_message=types.Content(role="user", parts=parts)):
            for fc in e.get_function_calls() or []:
                actions.append({"tool": fc.name, "args": dict(fc.args)})
            for fr in e.get_function_responses() or []:
                try:
                    out = json.loads(fr.response["content"][0]["text"])
                except Exception:
                    out = {}
                if actions:
                    actions[-1]["error"] = out.get("error")
                    if out.get("pending_confirmation"):
                        actions[-1]["pending"] = {"id": out["pending_confirmation"], "summary": out.get("summary")}
            if e.is_final_response() and e.content and e.content.parts and e.content.parts[0].text:
                reply = e.content.parts[0].text
    except Exception as ex:
        print("inventory agent error:", repr(ex))
        reply = "The inventory assistant hit an error. Please try again."
    return {"reply": reply, "actions": actions, "seconds": round(time.time() - t0, 1)}


@app.get("/api/pending", dependencies=[Depends(admin)])
def pending():
    return W.list_pending()


@app.post("/api/confirm/{pid}", dependencies=[Depends(admin)])
def confirm(pid: str):
    return W.execute_pending(pid, True)


@app.post("/api/cancel/{pid}", dependencies=[Depends(admin)])
def cancel(pid: str):
    return W.execute_pending(pid, False)


@app.post("/api/reset", dependencies=[Depends(admin)])
def reset():
    for f in (DB, LESSONS, Path(str(DB) + ".fail")):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/order", dependencies=[Depends(limit("order", 10))])
async def order(req: Request):
    body = await req.json()
    body["text"] = str(body.get("text") or "")[:2000]
    parts, saved = [], None
    if body.get("file_b64"):
        raw = check_upload(body["file_b64"], str(body.get("mime", "")))
        saved = save_upload(raw, body["mime"])  # keep the original order document / voice note
        parts.append(types.Part.from_bytes(data=raw, mime_type=body["mime"]))
        parts.append(types.Part(text=body.get("text") or "Process this order."))
    else:
        parts.append(types.Part(text=body.get("text", "")))

    async def gen():
        t0 = time.time()
        sid = str(body.get("session") or "")
        sid = sid if re.fullmatch(r"[\w-]{1,40}", sid) else None
        s = await runner.session_service.get_session(app_name="dash", user_id="u", session_id=sid) if sid else None
        if not s:  # first message of an order, or the previous one finished
            s = await runner.session_service.create_session(app_name="dash", user_id="u", session_id=sid,
                                                            state={"FLAKY_CARRIER": "1" if body.get("chaos", True) else "0"})
        send = lambda d: f"data: {json.dumps(d)}\n\n"
        if saved:
            yield send({"kind": "saved", "url": f"/uploads/{saved}", "mime": body["mime"], "t": 0})
        try:
            async for e in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=parts)):
                el = round(time.time() - t0, 2)
                txt = e.content.parts[0].text if e.content and e.content.parts and e.content.parts[0].text else ""
                if e.author == "intake" and txt:
                    yield send({"kind": "stage", "stage": "intake", "detail": txt, "t": el})
                elif e.custom_metadata:
                    yield send({**e.custom_metadata, "t": el})
                if e.actions and e.actions.state_delta and "result" in e.actions.state_delta:
                    res = e.actions.state_delta["result"]
                    if saved and res.get("tracking_id"):
                        idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
                        idx[res["tracking_id"]] = {"url": f"/uploads/{saved}", "mime": body["mime"]}
                        INDEX.write_text(json.dumps(idx))
                    yield send({"kind": "result", "result": res, "text": txt, "t": el})
        except Exception as ex:
            print("order error:", repr(ex))  # details stay in the server log, not the browser
            yield send({"kind": "error", "detail": "The order could not be processed. Please try again."})
        yield send({"kind": "end", "t": round(time.time() - t0, 2)})

    return StreamingResponse(gen(), media_type="text/event-stream")
