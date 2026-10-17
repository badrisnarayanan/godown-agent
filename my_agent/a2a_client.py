"""Minimal A2A (Agent2Agent) JSON-RPC client: message/send with a data part, as defined by the A2A protocol."""
import json
import os
import time
import urllib.request
import uuid


def a2a_base():
    return os.environ.get("A2A_BASE") or f"http://127.0.0.1:{os.environ.get('PORT', '8020')}"


def a2a_send(url: str, data: dict, timeout: float = 6.0):
    """Blocking call (run it in a thread from async code). Returns (result_data, milliseconds)."""
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
            "params": {"message": {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex,
                                   "parts": [{"kind": "data", "data": data}]}}}
    t0 = time.time()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", "a2a error"))
    parts = resp["result"]["parts"]
    out = next((p["data"] for p in parts if p.get("kind") == "data"), {})
    return out, round((time.time() - t0) * 1000)
