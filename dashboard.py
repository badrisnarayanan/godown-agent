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
app = FastAPI()
runner = InMemoryRunner(agent=root_agent, app_name="dash")


@app.get("/")
def index():
    return FileResponse(ROOT / "dashboard.html")


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
    return {"stock": stock, "lessons": lessons[-12:], "shipments": ships}


@app.post("/api/reset")
def reset():
    for f in (DB, LESSONS, Path(str(DB) + ".fail")):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/order")
async def order(req: Request):
    body = await req.json()
    parts = []
    if body.get("file_b64"):
        parts.append(types.Part.from_bytes(data=base64.b64decode(body["file_b64"]), mime_type=body["mime"]))
        parts.append(types.Part(text=body.get("text") or "Process this order."))
    else:
        parts.append(types.Part(text=body.get("text", "")))

    async def gen():
        t0 = time.time()
        s = await runner.session_service.create_session(app_name="dash", user_id="u")
        send = lambda d: f"data: {json.dumps(d)}\n\n"
        try:
            async for e in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=parts)):
                el = round(time.time() - t0, 2)
                txt = e.content.parts[0].text if e.content and e.content.parts and e.content.parts[0].text else ""
                if e.author == "intake" and txt:
                    yield send({"kind": "stage", "stage": "intake", "detail": txt, "t": el})
                elif e.custom_metadata:
                    yield send({**e.custom_metadata, "t": el})
                if e.actions and e.actions.state_delta and "result" in e.actions.state_delta:
                    yield send({"kind": "result", "result": e.actions.state_delta["result"], "text": txt, "t": el})
        except Exception as ex:
            yield send({"kind": "error", "detail": repr(ex)[:200]})
        yield send({"kind": "end", "t": round(time.time() - t0, 2)})

    return StreamingResponse(gen(), media_type="text/event-stream")
