"""Live dashboard: POST /api/order streams pipeline stages (SSE). Run: uvicorn dashboard:app --port 8020"""
import base64, json, os, sqlite3, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "my_agent" / ".env")
from fastapi import FastAPI, Request
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

app = FastAPI()
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
def state():
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
    return {"stock": stock, "lessons": lessons[-12:], "shipments": ships, "dispatched": len(allx), "lesson_count": len(lessons), "finance": fin}


@app.get("/api/config")
def config():
    return {"maps": bool(os.environ.get("GOOGLE_MAPS_API_KEY")) and os.environ.get("USE_REAL_MAPS") == "1"}


@app.post("/api/staticmap")
async def staticmap(req: Request):
    """Server-side proxy for Google Static Maps so the API key never reaches the browser."""
    import urllib.parse, urllib.request
    from fastapi.responses import Response
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return JSONResponse({"error": "no key"}, status_code=404)
    b = await req.json()
    f, t = b["from"], b["to"]
    path = f"color:0x12806Fff|weight:5|enc:{b['poly']}" if b.get("poly") else f"color:0x12806Fff|weight:5|{f[0]},{f[1]}|{t[0]},{t[1]}"
    q = [("size", "640x420"), ("scale", "2"), ("maptype", "roadmap"), ("path", path),
         ("markers", f"color:0x13202B|label:G|{f[0]},{f[1]}"), ("markers", f"color:0xF2B01E|label:D|{t[0]},{t[1]}"), ("key", key)]
    try:
        with urllib.request.urlopen("https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q), timeout=6) as r:
            return Response(r.read(), media_type=r.headers.get_content_type())
    except Exception as e:
        return JSONResponse({"error": str(e)[:120]}, status_code=502)


DEMO_SECONDS_PER_HOUR = 5  # demo clock: 1 hour of ETA plays out in 5 seconds


@app.get("/api/track/{tid}")
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


@app.post("/api/restock")
async def api_restock(req: Request):
    b = await req.json()
    return W.restock(b.get("godown", ""), b.get("sku", ""), int(b.get("qty", 0) or 0))


@app.post("/api/item")
async def api_item(req: Request):
    b = await req.json()
    try:
        return W.add_item(b["sku"], b["name"], float(b["weight_kg"]), float(b["volume_l"]), float(b["price"]),
                          float(b["cost"]), b.get("godown", "GD-BLR"), int(b.get("qty", 0) or 0))
    except (KeyError, ValueError) as e:
        return {"error": f"invalid input: {e}"}


@app.post("/api/inventory-agent")
async def inventory_chat(req: Request):
    """Chat with the inventory manager agent. A client-held session id keeps confirmations in context."""
    b = await req.json()
    sid = b.get("session") or "default"
    if not await inv_runner.session_service.get_session(app_name="inv", user_id="u", session_id=sid):
        await inv_runner.session_service.create_session(app_name="inv", user_id="u", session_id=sid)
    parts = []
    if b.get("file_b64"):
        raw = base64.b64decode(b["file_b64"])
        name = f"inv-{int(time.time())}-{os.urandom(3).hex()}.{EXT.get(b['mime'], 'bin')}"
        (UPLOADS / name).write_bytes(raw)
        parts.append(types.Part.from_bytes(data=raw, mime_type=b["mime"]))
    parts.append(types.Part(text=b.get("text") or "Process the attached document."))
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
            if e.is_final_response() and e.content and e.content.parts and e.content.parts[0].text:
                reply = e.content.parts[0].text
    except Exception as ex:
        reply = f"The inventory agent hit an error: {repr(ex)[:160]}"
    return {"reply": reply, "actions": actions, "seconds": round(time.time() - t0, 1)}


@app.post("/api/reset")
def reset():
    for f in (DB, LESSONS, Path(str(DB) + ".fail")):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/order")
async def order(req: Request):
    body = await req.json()
    parts, saved = [], None
    if body.get("file_b64"):
        raw = base64.b64decode(body["file_b64"])
        saved = f"{int(time.time())}-{os.urandom(3).hex()}.{EXT.get(body['mime'], 'bin')}"
        (UPLOADS / saved).write_bytes(raw)  # keep the original order document / voice note
        parts.append(types.Part.from_bytes(data=raw, mime_type=body["mime"]))
        parts.append(types.Part(text=body.get("text") or "Process this order."))
    else:
        parts.append(types.Part(text=body.get("text", "")))

    async def gen():
        t0 = time.time()
        s = await runner.session_service.create_session(app_name="dash", user_id="u", state={"FLAKY_CARRIER": "1" if body.get("chaos", True) else "0"})
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
            yield send({"kind": "error", "detail": repr(ex)[:200]})
        yield send({"kind": "end", "t": round(time.time() - t0, 2)})

    return StreamingResponse(gen(), media_type="text/event-stream")
