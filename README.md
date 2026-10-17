# godown-agent: autonomous logistics fulfilment (Agent Wars)

Photo / voice / text order in, dispatched shipment with tracking ID out, with no human in the loop.

## Architecture: agents only where reasoning is needed
```
image | voice | text ─► [Intake agent: Gemini multimodal]  (1 LLM call)
                              │ structured order
                              ▼
                 [Fulfilment engine: deterministic, talks to our MCP server]
        godown ─► ( packaging ‖ routing ) ─► carrier quotes ‖ ─► dispatch ─► audit
                              │ unrecoverable failure only
                              ▼
        [Exception healer agent: Jev (TypeSafe System One), Gemini fallback]
```
- **MCP**: `mcp_server/warehouse_server.py` exposes stock, reservations, boxes, routing, carriers, shipments and lessons as MCP tools (SQLite).
- **Multimodal**: purchase-order photos and voice notes handled by Gemini in the intake step.
- **APIs**: Google Maps geocoding (`USE_REAL_MAPS=1` + `GOOGLE_MAPS_API_KEY`, offline fallback otherwise), carrier quote APIs, Jev decisions API.
- **Self-healing**: flaky carrier retry then exclusion, out-of-stock godown rerouting, split orders, bad-address repair, backorder decisions.
- **Learning loop**: every fix is saved to `lessons.json`; later runs read it and apply it (learned address aliases skip the healer call; carriers that flaked get hedged requests). `eval_suite.py` has a case proving run 2 is cheaper than run 1.
- **Speed**: 1 LLM call on the happy path; parallel packaging/routing and carrier quotes. ~3s end to end.

## Run
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp my_agent/.env.example my_agent/.env   # add GOOGLE_API_KEY (+ JEV_API_KEY optional)
uvicorn dashboard:app --port 8020        # live dashboard: stages, healing log, stock, lessons memory
.venv/bin/adk web .                      # raw ADK dev UI
.venv/bin/python run_pipeline.py samples/order1.png
.venv/bin/python eval_suite.py           # 10 cases: happy path, reroute, split, shortfall, bad address, voice, image...
```
Deploy: `./deploy.sh <gcp-project-id>`
