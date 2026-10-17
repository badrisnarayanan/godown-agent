"""Eval suite: isolated DB per case, run in parallel. Usage: python eval_suite.py"""
import asyncio, os, sys, tempfile, time, json
from dotenv import load_dotenv
load_dotenv("my_agent/.env")
from google.adk.runners import InMemoryRunner
from google.genai import types
from my_agent.agent import root_agent

# (name, order text, expectations on result dict)
CASES = [
    ("happy-path",        "PO ORD-1 Priya: 3 laptops to Madurai",                  dict(status="OK", godown="GD-BLR")),
    ("nearest-has-stock", "PO ORD-2 Asha: 5 monitors to Chennai",                  dict(status="OK", godown="GD-CHN", healed=False)),
    ("reroute-oos",       "PO ORD-3 Ravi: 3 laptops to Chennai",                   dict(status="OK", godown="GD-BLR", healed=True)),
    ("split-order",       "PO ORD-4 Meena: 70 laptops to Hyderabad",               dict(status="OK", healed=True)),
    ("shortfall",         "PO ORD-5 Kumar: 500 laptops to Pune",                   dict(status="PARTIAL", healed=True)),
    ("bad-address",       "PO ORD-6 Anu: 2 keyboards to Kovai (Peelamedu)",        dict(status="OK", healed=True, destination="Coimbatore")),
    ("fuzzy-product",     "PO ORD-7 Dev: 4 notebook computers to Mumbai",          dict(status="OK", sku="LAPTOP-15")),
    ("flaky-carrier",     "PO ORD-8 Sia: 10 wireless mice to Bengaluru",           dict(status="OK", healed=True)),
    ("voice-order",      "samples/voice1.wav",                                    dict(status="OK", sku="KEYBOARD-MX", destination_has="Kochi")),
    ("image-order",       "samples/order1.png",                                    dict(status="OK", sku="LAPTOP-15", destination_has="Madurai")),
]

async def run_case(name, text, exp):
    d = tempfile.mkdtemp()
    state = {"WAREHOUSE_DB": f"{d}/w.db", "LESSONS_FILE": f"{d}/l.json", "FLAKY_CARRIER": "1" if name == "flaky-carrier" else "0"}
    r = InMemoryRunner(agent=root_agent, app_name="ev")
    s = await r.session_service.create_session(app_name="ev", user_id="u", state=state)
    if os.path.exists(text):
        import mimetypes
        parts = [types.Part.from_bytes(data=open(text, "rb").read(), mime_type=mimetypes.guess_type(text)[0]), types.Part(text="Process this order.")]
    else:
        parts = [types.Part(text=text)]
    t0 = time.time(); err = None
    try:
        async for _ in r.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=parts)):
            pass
    except Exception as e:
        err = repr(e)[:120]
    dt = time.time() - t0
    res = (await r.session_service.get_session(app_name="ev", user_id="u", session_id=s.id)).state.get("result", {})
    fails = []
    for k, v in exp.items():
        if k == "healed": ok = bool(res.get("healed")) == v
        elif k == "destination_has": ok = v.lower() in str(res.get("destination", "")).lower()
        else: ok = res.get(k) == v
        if not ok: fails.append(f"{k}: want {v!r} got {res.get('healed') if k=='healed' else res.get(k)!r}")
    return dict(name=name, ok=not fails and not err, secs=round(dt, 1), llm=res.get("llm_calls"), fails=fails, err=err,
                healed=res.get("healed"))

async def learning_case():
    """Run the same bad-address order twice with a shared lessons file: run 2 must apply the lesson, skip the healer."""
    d = tempfile.mkdtemp(); outs = []
    for i in range(2):
        r = InMemoryRunner(agent=root_agent, app_name="ev")
        s = await r.session_service.create_session(app_name="ev", user_id="u", state={
            "WAREHOUSE_DB": f"{d}/w{i}.db", "LESSONS_FILE": f"{d}/l.json", "FLAKY_CARRIER": "1"})
        async for _ in r.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="PO ORD-9 Anu: 2 keyboards to Kovai (Peelamedu)")])):
            pass
        outs.append((await r.session_service.get_session(app_name="ev", user_id="u", session_id=s.id)).state.get("result", {}))
    ok = outs[0].get("llm_calls") == 2 and outs[1].get("llm_calls") == 1 and bool(outs[1].get("learned"))
    return dict(name="learns-across-runs", ok=ok, secs=outs[1].get("engine_s"), llm=f"{outs[0].get('llm_calls')}->{outs[1].get('llm_calls')}",
                fails=[] if ok else [f"run1={outs[0].get('llm_calls')} run2={outs[1].get('llm_calls')} learned={outs[1].get('learned')}"], err=None, healed=outs[1].get("learned"))

async def main():
    t0 = time.time()
    out = await asyncio.gather(learning_case(), *[run_case(*c) for c in CASES])
    print(f"\n{'case':<18}{'result':<7}{'secs':>6}{'llm':>5}  notes")
    for o in out:
        print(f"{o['name']:<18}{'PASS' if o['ok'] else 'FAIL':<7}{o['secs']:>6}{str(o['llm']):>5}  {'; '.join(o['fails']) or o['err'] or ''} {o['healed'] or ''}")
    n = sum(o["ok"] for o in out)
    print(f"\n{n}/{len(out)} passed | wall {time.time()-t0:.1f}s | avg {sum(o['secs'] for o in out)/len(out):.1f}s/case")
asyncio.run(main())
