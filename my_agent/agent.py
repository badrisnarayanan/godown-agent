"""Autonomous logistics pipeline: order image -> godown -> (packaging || routing) -> dispatch -> audit/learn."""
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from google.adk.agents import Agent, BaseAgent, ParallelAgent, SequentialAgent
from google.adk.events import Event, EventActions
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "gemini-flash-latest"
FAST = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0), temperature=0)
ROOT = Path(__file__).parent.parent
LESSONS = ROOT / "mcp_server" / "lessons.json"
SERVER = str(ROOT / "mcp_server" / "warehouse_server.py")


def _j(v):
    """Parse JSON (tolerates ```json fences)."""
    try:
        return json.loads(str(v).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except Exception:
        return {}


intake = Agent(
    name="intake", model=MODEL, output_key="order", generate_content_config=FAST,
    instruction=(
        "You are the order-intake agent (the ONLY LLM step). The user supplies a purchase order as an IMAGE "
        "(photo/scan/handwritten), a VOICE RECORDING (transcribe it mentally and extract), or text. Output ONLY compact JSON: "
        '{"order_id","customer","destination","sku","qty"}. '
        "Valid SKUs: LAPTOP-15, MOUSE-WL, KEYBOARD-MX, MONITOR-24 - map product names to the closest SKU. "
        "If order_id is missing invent 'ORD-' + 4 digits."))

CARRIERS = ["FastFreight", "BlueDart-Mock", "EcoMove"]


SKU_LIST = ["LAPTOP-15", "MOUSE-WL", "KEYBOARD-MX", "MONITOR-24"]


CITIES = ["Chennai", "Bengaluru", "Hyderabad", "Mumbai", "Delhi", "Kolkata", "Coimbatore", "Madurai", "Pune", "Kochi"]
SKU_DESC = {"LAPTOP-15": "laptops, notebooks, computers", "MOUSE-WL": "mice, pointing devices, cursors",
            "KEYBOARD-MX": "keyboards, typing devices", "MONITOR-24": "monitors, screens, displays"}
JEV_MIN_CONF = 0.45


def _jev_sync(state, questions):
    key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("no JEV_API_KEY")
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)["answers"]


async def _gemini_json(prompt):
    from google import genai
    r = await genai.Client().aio.models.generate_content(
        model=MODEL, contents=prompt, config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0), temperature=0,
            response_mime_type="application/json"))
    return _j(r.text)


async def heal(kind, context):
    """EXCEPTION-HEALER AGENT, called only when deterministic recovery is exhausted.
    Primary brain: Jev (TypeSafe System One) - fast typed Choice decisions with confidence.
    Confidence-gated: low confidence / error / no key -> falls back to Gemini. Returns a decision dict."""
    try:
        state = json.dumps(context)
        if kind == "geocode_failed":
            a = await asyncio.to_thread(_jev_sync, state, {"city": {
                "type": "choice", "instructions": "Which city is the delivery address in or nearest to?",
                "criteria": {c: f"Address is in or near {c}" for c in CITIES}}})
            if a["city"]["confidence"] >= JEV_MIN_CONF:
                return {"action": "fix_address", "city": a["city"]["choice"], "via": "jev"}
        elif kind in ("unknown_sku", "stock_shortfall"):
            a = await asyncio.to_thread(_jev_sync, state, {
                "sku": {"type": "choice", "instructions": "Which catalog product best matches the ordered item?",
                        "criteria": dict(SKU_DESC)},
                "action": {"type": "choice", "instructions": "Best way to handle this stock exception",
                           "criteria": {"substitute": "Item name is unknown/unmatched: map to a catalog product",
                                        "backorder": "Item is a valid product but stock is short: backorder the rest"}}})
            if min(a["sku"]["confidence"], a["action"]["confidence"]) >= JEV_MIN_CONF:
                act = "substitute" if kind == "unknown_sku" else a["action"]["choice"]
                return {"action": act, "sku": a["sku"]["choice"], "reason": f"jev {act} (conf {a['action']['confidence']:.2f})", "via": "jev"}
        elif kind == "all_carriers_down":
            a = await asyncio.to_thread(_jev_sync, state, {"action": {
                "type": "choice", "instructions": "All carrier APIs are failing. What should happen?",
                "criteria": {"backorder": "Hold the order and retry dispatch later", "escalate": "Escalate to a human operator"}}})
            return {"action": a["action"]["choice"], "reason": f"jev: {a['action']['choice']}", "via": "jev"}
    except Exception as e:  # self-heal the healer: fall back to Gemini
        context = {**context, "_jev_error": str(e)[:80]}
    prompt = (
        f"You are the exception-healer of a logistics system. Failure: {kind}. Context: {json.dumps(context)}\n"
        "Valid SKUs: LAPTOP-15, MOUSE-WL, KEYBOARD-MX, MONITOR-24. Reply ONLY JSON, one of: "
        '{"action":"fix_address","city":"<city>"} | {"action":"substitute","sku":"<valid sku>","reason":"..."} | '
        '{"action":"backorder","reason":"..."} | {"action":"escalate","reason":"..."}')
    d = await _gemini_json(prompt)
    d["via"] = "gemini-fallback"
    return d


class FulfilmentEngine(BaseAgent):
    """Deterministic A-Z fulfilment over MCP tools: godown -> (pack || route) -> carrier -> dispatch -> audit.
    Self-healing + learning loop are explicit code paths. The healer LLM is called only for unrecoverable failures."""

    async def _run_async_impl(self, ctx):
        t0 = time.time()
        state = ctx.session.state
        order = _j(state.get("order"))
        trace, healed, llm_calls = [], [], 1  # intake counts as 1
        res = {"status": "FAILED", "healed": healed, "llm_calls": 1}
        env = {k: state[k] for k in ("WAREHOUSE_DB", "LESSONS_FILE", "FLAKY_CARRIER") if state.get(k)}
        env |= {k: os.environ[k] for k in ("GOOGLE_MAPS_API_KEY", "USE_REAL_MAPS") if os.environ.get(k)}
        try:
            sku, qty, dest = order["sku"], int(order["qty"]), order["destination"]
        except Exception:
            yield self._done(res | {"status": "NO_ORDER", "reason": f"intake produced no valid order: {state.get('order')}"},
                             "I'm the autonomous logistics agent. Send me an order and I'll pick the godown, pack it, "
                             "choose the fastest carrier and dispatch it - no human steps.\n"
                             "Send it as a photo of a purchase order, a voice note, or text, e.g.:\n"
                             "  \"3 laptops to Chennai for Ravi\"  |  \"2 keyboards to Kovai for Anu\"")
            return
        params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env or None)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as mcp:
            await mcp.initialize()

            async def call(name, **kw):
                out = await mcp.call_tool(name, kw)
                return json.loads(out.content[0].text)

            async def past(stage):
                out = await mcp.call_tool("get_lessons", {"stage": stage})
                return [json.loads(c.text) for c in out.content]

            # LEARNING LOOP: read what earlier runs learned and apply it before acting
            gl, dl = await asyncio.gather(past("godown"), past("dispatch"))
            aliases = {}
            for l in gl:
                m1, m2 = re.search(r"address '(.+)' failed geocoding", l["problem"]), re.search(r"normalised to '(.+)'", l["fix"])
                if m1 and m2:
                    aliases[m1.group(1).lower()] = m2.group(1)
            hot = {c for c in CARRIERS if any(f"{c} returned 503" in l["problem"] for l in dl)}
            learned = []
            if dest.lower() in aliases:
                learned.append(f"address '{dest}' -> '{aliases[dest.lower()]}' (no healer call needed)")
                dest = aliases[dest.lower()]

            sent = {"h": 0, "l": 0}

            def flush():
                evs = [self._prog("heal", h, "heal") for h in healed[sent["h"]:]]
                evs += [self._prog("learned", l, "learned") for l in learned[sent["l"]:]]
                sent["h"], sent["l"] = len(healed), len(learned)
                return evs

            async def lesson(stage, problem, fix):
                healed.append(fix)
                await call("save_lesson", stage=stage, problem=problem, fix=fix)

            # 1. GODOWN ------------------------------------------------------------------
            stock = await call("check_stock", sku=sku, qty=qty, destination=dest)
            if "error" in stock and "unknown sku" in stock["error"]:
                llm_calls += 1
                d = await heal("unknown_sku", {"sku": sku, "order": order})
                if d.get("action") == "substitute" and d.get("sku") in SKU_LIST:
                    await lesson("godown", f"unknown sku {sku}", f"healer({d.get('via')}) substituted {d['sku']} ({d.get('reason')})")
                    sku = d["sku"]
                    stock = await call("check_stock", sku=sku, qty=qty, destination=dest)
            if "error" in stock and "geocode" in stock["error"]:
                llm_calls += 1
                d = await heal("geocode_failed", {"address": dest})
                if d.get("city"):
                    await lesson("godown", f"address '{dest}' failed geocoding", f"healer({d.get('via')}) normalised to '{d['city']}'")
                    dest = d["city"]
                    stock = await call("check_stock", sku=sku, qty=qty, destination=dest)
            if "error" in stock:
                yield self._done(res | {"reason": stock["error"], "llm_calls": llm_calls},
                                 f"AUDIT FAILED: {stock['error']}")
                return
            ranked = stock["ranked"]
            if ranked[0]["available"] < qty and stock["recommended"]:
                await lesson("godown", f"nearest godown {ranked[0]['godown']} short of {sku}",
                             f"rerouted to {stock['recommended']}")
            use = ([g for g in ranked if g["godown"] == stock["recommended"]] if stock["recommended"] else []) \
                + [g for g in ranked if g["godown"] != stock["recommended"]]
            need, picks = qty, []
            for g in use:
                take = min(need, g["available"])
                if take <= 0:
                    continue
                out = await call("reserve_stock", godown=g["godown"], sku=sku, qty=take)
                if "error" not in out:
                    picks.append((g["godown"], take))
                    need -= take
                if need <= 0:
                    break
            if len(picks) > 1:
                await lesson("godown", f"no single godown had {qty} x {sku}", f"split across {picks}")
            if need > 0:  # unrecoverable stock shortfall -> healer decides
                llm_calls += 1
                d = await heal("stock_shortfall", {"sku": sku, "qty": qty, "short_by": need, "ranked": ranked})
                if d.get("action") == "substitute" and d.get("sku") in SKU_LIST and d["sku"] != sku:
                    # keep it simple and safe: report the substitution proposal, ship what we have
                    await lesson("godown", f"{sku} short by {need}", f"healer proposed {d['sku']}: {d.get('reason')}")
                else:
                    await lesson("godown", f"{sku} short by {need}", f"backorder {need}: healer({d.get('via')}) {str(d.get('reason'))[:60]}")
            if not picks:
                yield self._done(res | {"reason": f"no stock for {sku}", "llm_calls": llm_calls},
                                 f"AUDIT FAILED: no stock anywhere for {sku}. {healed[-1] if healed else ''}")
                return
            shipped = qty - need
            primary = picks[0][0]
            trace.append(f"godown {primary} reserved {shipped}/{qty} ({time.time() - t0:.1f}s)")
            yield self._prog("godown", f"{primary} reserved {shipped}/{qty} x {sku} for {dest}", primary=primary,
                             stocks={g["godown"]: g["available"] for g in ranked}, need=qty)
            for ev in flush():
                yield ev

            # 2. PACKAGING || ROUTING ----------------------------------------------------
            box, route = await asyncio.gather(
                call("select_box", sku=sku, qty=shipped), call("get_route", godown=primary, destination=dest))
            if "error" in route:
                yield self._done(res | {"reason": route["error"], "llm_calls": llm_calls}, f"AUDIT FAILED: {route['error']}")
                return
            trace.append(f"box {box['box']} x{box['boxes']} | route {route['distance_km']}km ({time.time() - t0:.1f}s)")
            yield self._prog("packroute", f"box {box['box']} x{box['boxes']} ({box['total_weight_kg']}kg) | route {route['distance_km']}km, ~{route['drive_hours']}h drive",
                             geo={"from": route.get("from"), "to": route.get("to"), "godown": primary, "poly": route.get("polyline")},
                             box=box, route={k: route.get(k) for k in ("distance_km", "drive_hours", "source")})

            # 3. CARRIERS in parallel; heal by retry, then exclude ----------------------
            async def quote(c):
                if c in hot:  # learned: this carrier flakes, so hedge with two parallel requests
                    rs = await asyncio.gather(*[call("get_carrier_quote", carrier=c, distance_km=route["distance_km"],
                                                     weight_kg=box["total_weight_kg"]) for _ in range(2)])
                    good = [q for q in rs if "error" not in q]
                    if good:
                        learned.append(f"{c} flaked before -> hedged request, no retry round-trip")
                        return good[0]
                for attempt in range(3):
                    q = await call("get_carrier_quote", carrier=c, distance_km=route["distance_km"],
                                   weight_kg=box["total_weight_kg"])
                    if "error" not in q:
                        if attempt:
                            await lesson("dispatch", f"{c} returned 503", f"retry #{attempt} succeeded")
                        return q
                await lesson("dispatch", f"{c} unavailable after 3 tries", "excluded from selection")

            quotes = [q for q in await asyncio.gather(*[quote(c) for c in CARRIERS]) if q]
            if not quotes:
                llm_calls += 1
                d = await heal("all_carriers_down", {"order": order})
                yield self._done(res | {"reason": "all carriers down", "llm_calls": llm_calls},
                                 f"AUDIT FAILED: all carriers down. Healer: {d.get('reason')}")
                return
            best = min(quotes, key=lambda q: (q["eta_hours"], q["price"]))  # speed first
            trace.append(f"carrier {best['carrier']} {best['eta_hours']}h from {len(quotes)} quotes ({time.time() - t0:.1f}s)")
            yield self._prog("carrier", f"{best['carrier']} chosen: {best['eta_hours']}h, Rs {best['price']} (from {len(quotes)} quotes: "
                             + ", ".join(f"{q['carrier']} {q['eta_hours']}h" for q in quotes) + ")",
                             quotes=quotes, chosen=best["carrier"])
            for ev in flush():
                yield ev

            # 4. DISPATCH ------------------------------------------------------------------
            ship = await call("create_shipment", order_id=order["order_id"], customer=order["customer"],
                              destination=dest, godown=primary, sku=sku, qty=shipped, box=box["box"],
                              carrier=best["carrier"], price=best["price"], eta_hours=best["eta_hours"],
                              box_cost=box.get("box_cost", 0))

        status = "OK" if ship.get("tracking_id") and need == 0 else f"PARTIAL (backorder {need})"
        heal_txt = ("Self-healed: " + "; ".join(healed) if healed else "Clean run") + \
            ("\nApplied lessons: " + "; ".join(learned) if learned else "")
        out = res | {"status": "OK" if need == 0 else "PARTIAL", "godown": primary, "sku": sku, "shipped": shipped,
                     "learned": learned, "carrier": best["carrier"], "tracking_id": ship.get("tracking_id"), "llm_calls": llm_calls,
                     "engine_s": round(time.time() - t0, 2), "destination": dest}
        yield self._done(out, f"AUDIT {status} - engine {time.time() - t0:.1f}s (LLM calls: {llm_calls})\n"
                         + "\n".join("  - " + t for t in trace)
                         + f"\n{heal_txt}\nTracking {ship.get('tracking_id')} | {ship.get('notification', '')}")

    def _prog(self, stage, detail, kind="stage", **extra):
        """Progress event (consumed live by the dashboard via custom_metadata; readable in ADK UI too)."""
        return Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=f"[{stage}] {detail}")]),
                     custom_metadata={"kind": kind, "stage": stage, "detail": detail, "t": round(time.time(), 2), **extra})

    def _done(self, result, text):
        return Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=text)]),
                     actions=EventActions(state_delta={"result": result}))


_t = {}


def _start(ctx):
    _t["t0"] = time.time()


def _end(ctx):
    print(f"\n[PIPELINE] end-to-end: {time.time() - _t.get('t0', time.time()):.1f}s")


root_agent = SequentialAgent(
    name="logistics_pipeline",
    description="Fully automated order-to-dispatch logistics agent (1 LLM call, deterministic MCP fulfilment).",
    sub_agents=[intake, FulfilmentEngine(name="fulfilment")],
    before_agent_callback=_start,
    after_agent_callback=_end,
)
