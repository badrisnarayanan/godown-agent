# Vegam Logistics: autonomous order-to-doorstep agents

**Live demo: https://godown-agent-1090536656180.asia-south1.run.app**
(Order desk, tracking and dashboards are open. Inventory edits and reset are locked behind an admin token on the public URL.)

Send an order as a **photo, a voice note or a chat message**. Vegam finds the stock across godowns, packs it, routes it, books the fastest carrier, dispatches it and texts the customer a tracking link, with nobody in between. If anything is missing it asks, like a person would. A second agent manages inventory in plain language.

![Chat: Vegam says it does not stock mobiles, offers alternatives, then dispatches the follow-up order](docs/chat.jpg)

*A real conversation: it says it does not stock mobiles, offers what it has, then ships the follow-up using the details from earlier in the chat.*

![Google Maps route: Bengaluru godown to Madurai by road, real distance from Google Routes, carrier quotes compared](docs/orders.jpg)

*Dispatched in 5.4 s: the Google Maps route from the godown to the customer, real distance and drive time from the Routes API, the three carrier quotes, and the stock that was reserved.*

![Tracking: the truck on the real road, with the status timeline](docs/track.jpg)

*Tracking: the truck moves along the actual road on the Google map while the timeline advances.*

## How this maps to the judging criteria

| Criterion | What we built | Where to see it |
|---|---|---|
| **MCP integration** | Our own MCP server (`mcp_server/warehouse_server.py`) with 14 tools: `check_stock`, `reserve_stock`, `select_box`, `get_route`, `get_carrier_quote`, `create_shipment`, `list_catalog`, `restock`, `add_item`, `update_item`, `remove_item`, `remove_stock`, `save_lesson`, `get_lessons`. Both agents act only through these tools. | Pipeline strip on the Orders view names the tool behind each step |
| **Multimodal agents** | Orders arrive as a **photo** (purchase order, handwritten slip), a **voice note** (recorded in the browser or uploaded) or **text**. Gemini reads all three. Uploads are saved and linked to the shipment. The inventory agent also reads receipt photos. | "Order by photo or voice" scenarios, **Speak** and **Attach photo** buttons |
| **API calls** | Google **Geocoding** (full street addresses and `lat,lng`), **Routes** (real driving distance and time), **Static Maps** (route image, via a server-side proxy so the key never reaches the browser), **Gemini**, **Jev (TypeSafe System One)**, three carrier quote tools. | Order details panel shows "GOOGLE ROUTES"; Google Maps tab |
| **Conversational, audio or automation** | All three. The order desk and the inventory assistant are chat agents with memory; voice is supported; the fulfilment itself is fully automated. | Orders and Inventory views |
| **Fastest end to end** | One LLM call on the happy path. Everything after intake is deterministic code over MCP, with packaging and routing in parallel and all three carrier quotes in parallel. Typically **3 to 5 s** from message to tracking ID (the engine itself takes about 0.4 s; the rest is the one Gemini call). We have seen up to about 12 s when the model is slow. | Banner shows "finished in X s" |
| **A to Z, it must work** | Chat, read order, stock, reserve, pack, route, carrier, dispatch, customer text, tracking with a moving truck, finance, inventory. 11 behaviour evals pass. | `eval_suite.py`, live URL |
| **Feedback loop, self-healing** | See the next section. Lessons are written **and read back**; a dedicated eval proves run 2 is cheaper than run 1. | "Problems Vegam solved on its own" log and "What Vegam has learned" panel |
| **Hosted, private repo** | Private GitHub repo, deployed to Google Cloud Run from the included `Dockerfile` and `deploy.sh`. | Link at the top |
| **Problem statement** | Godown to packaging to shipping, fully automated. Three godowns, box sizes, three carriers, stock, prices and customers; Google APIs for locations. Business rules enforced in code: electronics only, India only. | Orders and Business views |

## Technical design

```
photo | voice | chat ──► Intake agent (Gemini, multimodal, the ONLY LLM step on the happy path)
                              │ validated order JSON, or a question back to the customer
                              ▼
              Fulfilment engine (deterministic Python, calls the MCP server)
   find stock ─► reserve ─► ( pack ‖ route ) ─► ( 3 carrier quotes ‖ ) ─► dispatch ─► audit
        │                                                                      │
        │ only when code-level recovery is exhausted                           ▼
        ▼                                                         tracking, finance, customer text
   Exception healer: Jev (TypeSafe System One) typed decisions, Gemini fallback

   Inventory agent (Gemini + MCP tools): add, restock, edit, write off, remove, from text or receipt photo
```

**Agents only where reasoning is needed.** An early version used five LLM agents per order and took 30 s to 140 s. Moving stock lookup, packaging, routing and carrier choice into deterministic code cut it to 3 to 5 s with identical results. The LLMs read untrusted human input (intake), decide edge cases (healer) and translate free-form inventory requests into tool calls (inventory agent).

**Self-healing, in code.**
- Carrier returns 503: retry, then exclude it from selection.
- Nearest godown is empty: reroute to the next nearest with stock.
- Order larger than any one godown: split it across godowns.
- Address does not geocode: Google Geocoding, then coordinates, then the Jev healer picks the nearest known city.
- More than all stock: partial shipment, backorder the rest, healer decides the wording.
- Tool crashes or returns garbage: surfaced as an error the engine retries instead of crashing (this fixed a race between hedged requests that we found by running the demo).
- Incomplete or non-order messages: the agent asks only for what is missing and continues the same conversation.

**Learning loop.** Every fix is written to `lessons.json` and read back before the next run. A learned address alias skips the healer call entirely. A carrier that flaked before gets hedged (two parallel requests) instead of a sequential retry. `eval_suite.py` includes `learns-across-runs`, which asserts run 2 makes fewer LLM calls than run 1.

**Jev as the fast decision model.** The healer asks typed Choice questions (which city, which action, which product) and reads the confidence score. Low confidence, an error or a missing key falls back to Gemini.

**Safety by design: the model may propose, code and people decide.**
- The intake model has no tools. Its JSON is re-validated (positive whole quantities, bounded sizes, markup stripped) before any tool sees it.
- Tool arguments are validated again server-side (negative or huge quantities, SKU format, text cleaning).
- **India-only delivery** is enforced in code: every destination is geocoded and the country Google resolves it to must be India (coordinates are reverse-geocoded), so Paris, London, Colombo or `40.7128, -74.0060` are refused with the reason, and the shipment tool checks again before creating a record.
- **Electronics-only policy** is enforced inside the tools, not just the prompt: a deny list, an allow list, then a Jev classification that fails closed. "Add rats", "add puppies", "rename the mouse to rats" are all refused.
- The inventory agent can only **queue** destructive actions (delete a product, remove more than 25 units). A person must click Confirm. Text inside a photo cannot authorise anything.
- Write endpoints need `ADMIN_TOKEN`; rate limits per client; upload size, type and magic-byte checks; security headers; the map proxy validates its input.

## Run it locally

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp my_agent/.env.example my_agent/.env     # GOOGLE_API_KEY, JEV_API_KEY, GOOGLE_MAPS_API_KEY, USE_REAL_MAPS=1
.venv/bin/uvicorn dashboard:app --port 8020   # open http://localhost:8020
.venv/bin/python eval_suite.py                # 11 behaviour evals
```
Deploy: `ADMIN_TOKEN=<secret> ./deploy.sh <gcp-project-id>`

## Dashboard

- **Orders:** chat order desk, Google Maps by default (schematic view available), live step banner ("Step 2 of 5: Finding stock"), map (schematic or Google), pipeline with the MCP tool behind each step, the problems it solved on its own.
- **Track:** tracking ID lookup, timeline, a truck moving along the route (demo clock: 1 hour of ETA plays out in 5 s), and the original order photo or voice note.
- **Inventory:** product table, stock bars by godown, the inventory assistant chat, manual forms, and a panel of actions waiting for your confirmation.
- **Business:** orders, units, revenue, product cost, shipping and packaging cost, profit and margin, units sold per product.

## Evaluation and tests

- `eval_suite.py` (11 cases, isolated databases, run in parallel): happy path, nearest-has-stock, reroute when empty, split order, shortfall, bad address, fuzzy product name, flaky carrier, voice, photo, and learns-across-runs. All pass. Evals pin the offline geocoder so address-healing paths stay deterministic.
- `security_suite.py` (34 adversarial checks against a running server): the first full run passed 31 of 34. Two failures were a real bug (the policy also classified SKU text, wrongly refusing valid SKUs) and one was an over-broad test assertion. Both were fixed afterwards; **the suite has not been rerun on the fixed code**.
