"""Real A2A (Agent2Agent protocol) agents served by this app: each has a public agent card and a JSON-RPC message/send endpoint.
- Guardian agent: masks personal and financial identifiers before any model sees the text.
- Carrier agents (FastFreight, BlueDart-Mock, EcoMove): each quotes independently, like a separate company would."""
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import warehouse_server as W
from my_agent.guardian import mask

router = APIRouter()
CARRIERS = ["FastFreight", "BlueDart-Mock", "EcoMove"]


def _card(req: Request, path, name, desc, skill_id, skill_name, skill_desc):
    base = str(req.base_url).rstrip("/")
    return {"protocolVersion": "0.3.0", "name": name, "description": desc, "url": f"{base}{path}",
            "provider": {"organization": "Vegam Logistics", "url": base},
            "preferredTransport": "JSONRPC", "version": "1.0.0", "capabilities": {"streaming": False},
            "defaultInputModes": ["application/json", "text/plain"], "defaultOutputModes": ["application/json"],
            "skills": [{"id": skill_id, "name": skill_name, "description": skill_desc, "tags": ["logistics"]}]}


def _reply(rid, data):
    return {"jsonrpc": "2.0", "id": rid, "result": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                                                     "parts": [{"kind": "data", "data": data}]}}


def _rpc_error(rid, code, msg):
    # JSON-RPC errors travel inside a normal HTTP 200 response
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}})


async def _read(req: Request):
    try:
        body = await req.json()
        if body.get("method") != "message/send":
            return body.get("id"), {"__method_not_found__": body.get("method")}
        parts = body["params"]["message"]["parts"]
        data = next((p.get("data") for p in parts if p.get("kind") == "data"), None)
        if data is None:
            data = {"text": " ".join(p.get("text", "") for p in parts if p.get("kind") == "text")}
        return body.get("id"), data
    except Exception:
        return None, None


def guardian_skill(data):
    out = mask(str(data.get("text", ""))[:4000])
    return {"masked": out["masked"], "findings": out["findings"]}


def carrier_skill(name, data):
    km, kg = float(data.get("distance_km", 0)), float(data.get("weight_kg", 0))
    if data.get("action") == "counter":  # negotiation round: respond to the orchestrator's counter-offer
        return W.carrier_counter(name, km, kg, float(data.get("offer", 0)))
    return W.quote_carrier(name, km, kg, bool(data.get("flaky", True)), int(data.get("boxes", 1) or 1))


@router.get("/a2a/guardian/.well-known/agent-card.json")
def guardian_card(req: Request):
    return _card(req, "/a2a/guardian", "Vegam Guardian agent",
                 "Responsible-AI gatekeeper: masks Aadhaar, PAN, card, bank, UPI, email and phone data before any model sees it.",
                 "mask_pii", "Mask personal data", "Returns the masked text and what was found.")


@router.post("/a2a/guardian")
async def guardian_rpc(req: Request):
    rid, data = await _read(req)
    if data is None:
        return _rpc_error(rid, -32600, "Invalid request")
    if "__method_not_found__" in data:
        return _rpc_error(rid, -32601, f"Method not found: {data['__method_not_found__']}")
    return _reply(rid, guardian_skill(data))


@router.get("/a2a/carrier/{name}/.well-known/agent-card.json")
def carrier_card(name: str, req: Request):
    if name not in CARRIERS:
        return JSONResponse({"error": "unknown carrier"}, status_code=404)
    return _card(req, f"/a2a/carrier/{name}", f"{name} carrier agent", f"Quotes price and ETA for {name} shipments.",
                 "bid", "Bid and negotiate", "action=bid: distance_km, weight_kg -> price, eta_hours and live metrics (or decline). "
                 "action=counter: offer -> accept, counter at floor, or reject.")


@router.post("/a2a/carrier/{name}")
async def carrier_rpc(name: str, req: Request):
    rid, data = await _read(req)
    if data is None or name not in CARRIERS:
        return _rpc_error(rid, -32600, "Invalid request")
    if "__method_not_found__" in data:
        return _rpc_error(rid, -32601, f"Method not found: {data['__method_not_found__']}")
    return _reply(rid, carrier_skill(name, data))


@router.get("/a2a/agents")
def list_agents(req: Request):
    base = str(req.base_url).rstrip("/")
    return {"agents": [f"{base}/a2a/guardian/.well-known/agent-card.json"] +
                      [f"{base}/a2a/carrier/{c}/.well-known/agent-card.json" for c in CARRIERS]}
