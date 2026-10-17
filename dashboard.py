"""Live dashboard: POST /api/order streams pipeline stages (SSE). Run: uvicorn dashboard:app --port 8020"""
import asyncio, base64, hmac, json, os, re, sqlite3, time
from collections import defaultdict, deque
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "my_agent" / ".env")
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from my_agent.agent import root_agent
from my_agent.a2a_client import a2a_base, a2a_send
from my_agent.grounding import SAFE_REPLY, verify
from my_agent.guardian import mask as guardian_mask

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
        g = _hits[(bucket, "*")]  # global cap across all clients, so rotating IPs cannot run up LLM or Maps cost
        while g and now - g[0] > 60:
            g.popleft()
        if len(q) >= per_minute or len(g) >= per_minute * 6:
            raise HTTPException(429, f"Too many requests. Limit is {per_minute} per minute.")
        q.append(now)
        g.append(now)
    return dep


def is_admin(req: Request):
    tok = os.environ.get("ADMIN_TOKEN")
    return (not tok) or hmac.compare_digest(req.headers.get("x-admin-token", "").encode("utf-8", "ignore"), tok.encode())


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
    W.db().close()  # make sure the tables exist (recreated after a reset)
    if DB.exists():
        c = sqlite3.connect(DB)
        stock = [dict(godown=g, sku=s, qty=q) for g, s, q in c.execute("SELECT godown,sku,qty FROM stock WHERE sku IN (SELECT sku FROM catalog) ORDER BY sku,godown")]
        ships = [json.loads(d) for (d,) in c.execute("SELECT data FROM shipments ORDER BY created DESC LIMIT 5")]
        c.close()
    try:
        lessons = json.loads(LESSONS.read_text()) if LESSONS.exists() else []
    except Exception:
        lessons = []
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
        lessons = [{**l, "problem": re.sub(r"'[^']*'", "'...'", l["problem"]), "fix": re.sub(r"'[^']*'", "'...'", l["fix"])} for l in lessons]
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

    def ll(v):
        x = [float(c) for c in v][:2]
        assert -90 <= x[0] <= 90 and -180 <= x[1] <= 180
        return x
    try:
        gods = [ll(g) for g in (b.get("godowns") or [])][:5]
        f = ll(b["from"]) if b.get("from") else None
        t = ll(b["to"]) if b.get("to") else None
        truck = ll(b["truck"]) if b.get("truck") else None
        poly = b.get("poly") or ""
        assert len(poly) <= 60000 and all(63 <= ord(ch) <= 126 for ch in poly)
        assert gods or (f and t)
    except Exception:
        return JSONResponse({"error": "invalid map request"}, status_code=400)
    if len(poly) > 3000:  # keep the URL under Google's limit: thin the path to ~120 points
        pts = decode_poly(poly)
        step = max(1, len(pts) // 120)
        poly = encode_poly(pts[::step] + [pts[-1]])
    q = [("size", "640x420"), ("scale", "2"), ("maptype", "roadmap"), ("key", key)]
    if f and t:
        q.append(("path", f"color:0x12806Fff|weight:5|enc:{poly}" if poly else f"color:0x12806Fff|weight:5|{f[0]},{f[1]}|{t[0]},{t[1]}"))
        q.append(("markers", f"color:0x13202B|label:G|{f[0]},{f[1]}"))
        q.append(("markers", f"color:0xF2B01E|label:D|{t[0]},{t[1]}"))
    else:
        for g in gods:
            q.append(("markers", f"color:0x13202B|size:mid|{g[0]},{g[1]}"))
    if truck:
        q.append(("markers", f"color:0xD6402B|label:T|{truck[0]},{truck[1]}"))
    try:
        with urllib.request.urlopen("https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q), timeout=6) as r:
            return Response(r.read(), media_type=r.headers.get_content_type())
    except Exception:
        return JSONResponse({"error": "map unavailable"}, status_code=502)


def decode_poly(enc):
    """Google encoded polyline -> [(lat, lon)]."""
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(enc):
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(enc[i]) - 63; i += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0: lat += d
            else: lon += d
        pts.append((lat / 1e5, lon / 1e5))
    return pts


def encode_poly(pts):
    out, plat, plon = [], 0, 0
    for la, lo in pts:
        for v, prev in ((round(la * 1e5), plat), (round(lo * 1e5), plon)):
            d = v - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1f)) + 63)); d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(la * 1e5), round(lo * 1e5)
    return "".join(out)


def point_along(pts, frac):
    """Position `frac` (0..1) of the way along the polyline, by distance."""
    import math
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    want, acc = sum(seg) * max(0.0, min(1.0, frac)), 0.0
    for i, d in enumerate(seg):
        if acc + d >= want and d > 0:
            t = (want - acc) / d
            return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t]
        acc += d
    return list(pts[-1])


_ROUTE_CACHE = {}  # (godown, destination) -> encoded road polyline


DEMO_SECONDS_PER_HOUR = 5  # demo clock: 1 hour of ETA plays out in 5 seconds


@app.get("/api/track/{tid}", dependencies=[Depends(limit("track", 60))])
def track(tid: str, req: Request):
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
    key = (d["godown"], d["destination"])
    if key not in _ROUTE_CACHE:
        rt = W._routes_api((glat, glon), dest)
        _ROUTE_CACHE[key] = rt["polyline"] if rt else ""
    poly = _ROUTE_CACHE[key]
    on_road = point_along(decode_poly(poly), move) if poly else None
    steps = [("Order received", 0), (f"Packed at {gname}", 0.08), ("Picked up by " + d["carrier"], 0.2),
             ("In transit", 0.35), ("Out for delivery", 0.9), ("Delivered", 1.0)]
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    return {"source": idx.get(d["tracking_id"]), "shipment": (d if is_admin(req) else {k: v for k, v in d.items() if k != "customer"}), "progress": round(frac, 3), "remaining_hours": round(d["eta_hours"] * (1 - frac), 1),
            "delivered": frac >= 1, "from": [glat, glon], "to": list(dest),
            "position": on_road or [glat + (dest[0] - glat) * move, glon + (dest[1] - glon) * move], "poly": poly,
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
    W._GEO_CACHE.clear()
    W.db().close()  # recreate tables and seed data right away
    return {"ok": True}


@app.post("/api/order", dependencies=[Depends(limit("order", 10))])
async def order(req: Request):
    body = await req.json()
    body["text"] = str(body.get("text") or "")[:2000]
    raw_file, saved = None, None
    if body.get("file_b64"):
        raw_file = check_upload(body["file_b64"], str(body.get("mime", "")))
        saved = save_upload(raw_file, body["mime"])  # keep the original order document / voice note

    async def gen():
        t0 = time.time()
        send = lambda d: f"data: {json.dumps(d)}\n\n"
        el = lambda: round(time.time() - t0, 2)
        yield send({"kind": "agent", "agent": "orchestrator", "state": "working", "protocol": "Orchestrator", "t": 0,
                    "detail": "Plan: Guardian (A2A) masks personal data -> Intake (Gemini) reads the order -> Fulfilment (MCP tools) "
                              "-> Carrier agents (A2A) bid and negotiate -> Judge verifies the reply"})
        # 1. GUARDIAN over A2A: personal and financial identifiers are masked before any model sees the text
        try:
            g, ms = await asyncio.to_thread(a2a_send, f"{a2a_base()}/a2a/guardian", {"text": body["text"]})
            via = "A2A"
        except Exception:
            g, ms, via = guardian_mask(body["text"]), 0, "in-process (A2A unreachable, self-healed)"
        seen, findings = g["masked"], g["findings"]
        types_found = sorted({f["type"] for f in findings})
        yield send({"kind": "agent", "agent": "guardian", "state": "done", "protocol": via, "ms": ms, "t": el(),
                    "detail": (f"Masked {len(findings)} item(s): {', '.join(types_found)}. The model only sees placeholders."
                               if findings else "No personal or financial identifiers found."),
                    "seen": seen, "findings": findings,
                    "attachment": ("Attachment passed to the model as-is (image and voice redaction not enabled)" if raw_file else None)})
        # 2. Tracking questions are answered from the database, never by a model (no invented statuses)
        m = re.search(r"\bTRK[0-9A-Fa-f]{8}\b", seen)
        if m and not raw_file:
            tid = m.group(0).upper()
            row = None
            if DB.exists():
                c = sqlite3.connect(DB)
                row = c.execute("SELECT data FROM shipments WHERE id=?", (tid,)).fetchone()
                c.close()
            if row:
                d = json.loads(row[0])
                reply = (f"{tid}: {d['qty']} x {d['sku']} from {d['godown']} to {d['destination']} via {d['carrier']}, "
                         f"status {d['status']}, ETA about {d['eta_hours']} h.")
                evidence = d
            else:
                reply, evidence = f"I can't find a shipment with tracking ID {tid}. Please check the ID and try again.", {"tid": tid}
            v = verify(reply, evidence, seen)
            yield send({"kind": "agent", "agent": "judge", "state": "done", "protocol": "Sub-agent", "t": el(),
                        "detail": f"{v['verified']}/{v['total']} claims verified against the shipment database", "claims": v["claims"]})
            yield send({"kind": "result", "result": {"status": "TRACKING", "reply": reply, "llm_calls": 0}, "text": reply, "t": el()})
            yield send({"kind": "end", "t": el(), "tokens": {"in": 0, "out": 0}, "llm_calls": 0, "zero_token_steps": 3})
            return
        parts = []
        if raw_file:
            parts.append(types.Part.from_bytes(data=raw_file, mime_type=body["mime"]))
        parts.append(types.Part(text=seen or "Process this order."))
        sid = str(body.get("session") or "")
        sid = sid if re.fullmatch(r"[\w-]{1,40}", sid) else None
        s = await runner.session_service.get_session(app_name="dash", user_id="u", session_id=sid) if sid else None
        if not s:  # first message of an order, or the previous one finished
            s = await runner.session_service.create_session(app_name="dash", user_id="u", session_id=sid,
                                                            state={"FLAKY_CARRIER": "1" if body.get("chaos", True) else "0"})
        if saved:
            yield send({"kind": "saved", "url": f"/uploads/{saved}", "mime": body["mime"], "t": 0})
        tok = {"in": 0, "out": 0, "cached": 0}
        llm_calls, zero_steps = 0, 0
        try:
            async for e in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=parts)):
                um = getattr(e, "usage_metadata", None)
                if um and (um.prompt_token_count or um.candidates_token_count):
                    tok["in"] += um.prompt_token_count or 0
                    tok["out"] += um.candidates_token_count or 0
                    tok["cached"] += getattr(um, "cached_content_token_count", 0) or 0
                    llm_calls += 1
                if e.custom_metadata and e.custom_metadata.get("kind") in ("stage", "agent"):
                    zero_steps += 1
                txt = e.content.parts[0].text if e.content and e.content.parts and e.content.parts[0].text else ""
                if e.author == "intake" and txt:
                    yield send({"kind": "stage", "stage": "intake", "detail": guardian_mask(txt)["masked"], "t": el()})
                elif e.custom_metadata:
                    yield send({**e.custom_metadata, "t": el()})
                if e.actions and e.actions.state_delta and "result" in e.actions.state_delta:
                    res = dict(e.actions.state_delta["result"])
                    if saved and res.get("tracking_id"):
                        idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
                        idx[res["tracking_id"]] = {"url": f"/uploads/{saved}", "mime": body["mime"]}
                        INDEX.write_text(json.dumps(idx))
                    # 3. JUDGE: the customer-facing text must be grounded in tool results; output is masked too
                    if res.get("status") == "NEEDS_INFO":
                        claim_text, evidence = res.get("question", ""), W.list_catalog()
                    elif res.get("tracking_id"):
                        claim_text, evidence = (txt.split(" | ")[-1] if " | " in txt else txt), res
                    else:
                        claim_text, evidence = res.get("reason") or txt, res.get("reason") or txt
                    v = verify(claim_text, evidence, seen)
                    blocked = bool(v["flagged"]) and res.get("status") == "NEEDS_INFO"
                    if blocked:
                        res["question"], txt = SAFE_REPLY, SAFE_REPLY
                    out = guardian_mask(txt)
                    if res.get("question"):
                        res["question"] = guardian_mask(res["question"])["masked"]
                    yield send({"kind": "agent", "agent": "judge", "state": "flagged" if blocked else "done", "protocol": "Sub-agent",
                                "t": el(), "claims": v["claims"], "output_masked": len(out["findings"]),
                                "detail": (f"Blocked an unverified claim ({', '.join(v['flagged'][:3])}) and replied safely"
                                           if blocked else f"{v['verified']}/{v['total']} claims verified against tool results")})
                    yield send({"kind": "result", "result": res, "text": out["masked"], "t": el()})
        except Exception as ex:
            print("order error:", repr(ex))  # details stay in the server log, not the browser
            yield send({"kind": "error", "detail": "The order could not be processed. Please try again."})
        yield send({"kind": "end", "t": el(), "tokens": tok, "llm_calls": llm_calls, "zero_token_steps": zero_steps + 2})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/eval-results")
def eval_results():
    f = ROOT / "docs" / "eval_results.json"
    return json.loads(f.read_text()) if f.exists() else {"generated": None, "sections": []}


from a2a_agents import router as a2a_router  # noqa: E402  (needs warehouse_server on sys.path, set up above)
app.include_router(a2a_router)
