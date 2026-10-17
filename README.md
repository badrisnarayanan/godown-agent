# Vegam Logistics: autonomous order-to-doorstep agents

Send an order as a **photo, voice note or text**. Vegam finds the stock, packs it, routes it, books the fastest carrier, dispatches it and texts the customer a tracking link, with nobody in between. If details are missing, it asks.

![Vegam dashboard: a bad address healed, rerouted to Chennai, real Google Routes distance, customer text](docs/orders.jpg)

*An order with an address nobody can geocode: healed, rerouted to the godown that has stock, routed with Google Routes, dispatched in 3.6 s with the customer text.*

## Run it
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp my_agent/.env.example my_agent/.env     # GOOGLE_API_KEY (+ JEV_API_KEY, GOOGLE_MAPS_API_KEY, USE_REAL_MAPS=1)
.venv/bin/uvicorn dashboard:app --port 8020   # open http://localhost:8020
.venv/bin/python eval_suite.py                # 11 behaviour evals
```
Deploy: `./deploy.sh <gcp-project-id>` (set `ADMIN_TOKEN` first on any public URL).

## Two agents, used only where reasoning is needed
- **Order agent**: one LLM call (Gemini, multimodal) reads the order; a deterministic engine does the rest over our **MCP warehouse server** (stock, reserve, box, route, carriers, shipment). Packaging and routing, and the three carrier quotes, run in parallel. About 3 s end to end.
- **Inventory agent**: an LLM with MCP tools to add, restock, edit, write off and remove products from plain text or a receipt photo.
- **Exception healer**: **Jev (TypeSafe System One)** makes fast typed decisions when code-level recovery runs out, with a Gemini fallback.

## Failure handling and learning
Flaky carrier (retry, then exclude), empty nearest godown (reroute), order bigger than one godown (split), unknown address (Google geocoding, coordinates, then healer), shortfall (backorder), incomplete order (asks a question, continues the same conversation). Every fix is saved to `lessons.json` and **read back on later runs**: learned addresses skip the healer, carriers that flaked get hedged requests. `eval_suite.py` includes a case proving run 2 is cheaper than run 1.

## Dashboard
Orders (live step banner, map, pipeline with MCP tool names), Track (timeline and moving truck), Inventory (assistant plus manual forms), Business (revenue, costs, profit). Uploaded photos and voice notes are saved and linked to the shipment. Google Maps is used for geocoding (full addresses or `lat,lng`), Routes API distance and time, and a Static Maps image.

## Security design
The LLM may propose, code and people decide: tool arguments are validated (positive bounded quantities, SKU format, markup stripped); the intake model has no tools and its JSON is re-validated; the inventory agent can only **queue** destructive actions, which need a human click; write endpoints need `ADMIN_TOKEN`; rate limits, upload size/type/magic-byte checks, security headers, and a validated map proxy protect cost and data. `security_suite.py` attacks a running server; its first admin and validation checks pass, but the full run is not yet complete (see Status).

## Status
Tested: evals (11/11), end-to-end text, photo and voice orders, tracking, finance, inventory agent in the UI, real Google geocoding and Routes. Fixed late: a race between hedged carrier requests that could crash an order (found by running the demo, now covered by a stress run of 6 orders). Not yet tested end to end: the complete adversarial suite, the follow-up-question flow through the UI, mic recording, and the Cloud Run build of the latest code.
