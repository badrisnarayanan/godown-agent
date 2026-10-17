"""Warehouse / logistics MCP server (SQLite-backed). Run over stdio by the ADK agents."""
import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).parent
DB = os.environ.get("WAREHOUSE_DB", str(ROOT / "warehouse.db"))
LESSONS = Path(os.environ.get("LESSONS_FILE", ROOT / "lessons.json"))
FAIL_STATE = Path(DB + ".fail")  # planted-failure bookkeeping (per-DB so evals are isolated)

mcp = MCPServer("warehouse")

# ---- input validation: tools are callable by LLMs, so never trust their arguments ----
SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,23}$")
MAX_QTY = 10_000


def _bad_qty(qty):
    return not isinstance(qty, int) or isinstance(qty, bool) or qty < 1 or qty > MAX_QTY


def _clean(text, n):
    """Strip control chars and angle brackets, cap length."""
    return re.sub(r"[\x00-\x1f<>]", "", str(text)).strip()[:n]


GODOWNS = {
    "GD-CHN": ("Chennai Central Godown", 13.0827, 80.2707),
    "GD-BLR": ("Bengaluru Hub Godown", 12.9716, 77.5946),
    "GD-HYD": ("Hyderabad Depot", 17.3850, 78.4867),
}
CITY = {  # fallback geocoder
    "chennai": (13.0827, 80.2707), "bengaluru": (12.9716, 77.5946), "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867), "mumbai": (19.0760, 72.8777), "delhi": (28.6139, 77.2090),
    "kolkata": (22.5726, 88.3639), "coimbatore": (11.0168, 76.9558), "madurai": (9.9252, 78.1198),
    "pune": (18.5204, 73.8567), "kochi": (9.9312, 76.2673),
}
STOCK = {
    "GD-CHN": {"LAPTOP-15": 0, "MOUSE-WL": 120, "KEYBOARD-MX": 40, "MONITOR-24": 15},
    "GD-BLR": {"LAPTOP-15": 25, "MOUSE-WL": 300, "KEYBOARD-MX": 0, "MONITOR-24": 30},
    "GD-HYD": {"LAPTOP-15": 60, "MOUSE-WL": 80, "KEYBOARD-MX": 90, "MONITOR-24": 0},
}
SKUS = {  # sku -> (name, weight_kg, volume_l)
    "LAPTOP-15": ("15in Laptop", 2.2, 6.0), "MOUSE-WL": ("Wireless Mouse", 0.15, 0.3),
    "KEYBOARD-MX": ("Mechanical Keyboard", 0.9, 3.0), "MONITOR-24": ("24in Monitor", 4.5, 28.0),
}
PRICES = {  # sku -> (selling price, unit cost) in INR
    "LAPTOP-15": (52000, 41000), "MOUSE-WL": (900, 520), "KEYBOARD-MX": (4200, 2800), "MONITOR-24": (11500, 8400),
}
BOXES = [  # id, max_kg, volume_l, cost
    ("BOX-S", 2, 5, 15), ("BOX-M", 8, 30, 40), ("BOX-L", 20, 80, 90), ("BOX-XL", 40, 200, 160),
]
CARRIERS = {  # name -> (base, per_kg, per_km, speed_kmph)
    "FastFreight": (60, 4.0, 0.9, 60), "BlueDart-Mock": (90, 5.5, 1.2, 80), "EcoMove": (40, 3.0, 0.6, 40),
}


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS stock(godown TEXT, sku TEXT, qty INT, PRIMARY KEY(godown, sku))")
    c.execute("CREATE TABLE IF NOT EXISTS shipments(id TEXT PRIMARY KEY, data TEXT, created REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, action TEXT, args TEXT, summary TEXT, created REAL, status TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS catalog(sku TEXT PRIMARY KEY, name TEXT, weight REAL, volume REAL, price REAL, cost REAL)")
    if not c.execute("SELECT 1 FROM catalog LIMIT 1").fetchone():
        for k, (n, w, v) in SKUS.items():
            c.execute("INSERT INTO catalog VALUES(?,?,?,?,?,?)", (k, n, w, v, *PRICES[k]))
    if not c.execute("SELECT 1 FROM stock LIMIT 1").fetchone():
        for g, items in STOCK.items():
            for s, q in items.items():
                c.execute("INSERT INTO stock VALUES(?,?,?)", (g, s, q))
    c.commit()
    c.close()


_init()


BIG_REMOVAL = 25  # removing more units than this needs a human click


def _propose(action, args, summary):
    """Queue a destructive action. It runs only when a person confirms it (execute_pending), never from an LLM turn."""
    pid = "ACT" + uuid.uuid4().hex[:6].upper()
    c = db()
    c.execute("INSERT INTO pending VALUES(?,?,?,?,?,?)", (pid, action, json.dumps(args), summary, time.time(), "pending"))
    c.commit()
    c.close()
    return {"pending_confirmation": pid, "summary": summary,
            "note": "NOT done yet. A person must click Confirm in the dashboard. Tell the user this."}


def list_pending():
    c = db()
    rows = [dict(r) for r in c.execute("SELECT id, action, summary, created FROM pending WHERE status='pending' ORDER BY created")]
    c.close()
    return rows


def execute_pending(pid: str, approve: bool = True) -> dict:
    """Called by the dashboard when a human clicks Confirm / Cancel. Not exposed as an MCP tool."""
    c = db()
    row = c.execute("SELECT * FROM pending WHERE id=? AND status='pending'", (pid,)).fetchone()
    if not row or time.time() - row["created"] > 900:  # proposals expire after 15 minutes
        c.close()
        return {"error": "no such pending action, or it expired"}
    c.execute("UPDATE pending SET status=? WHERE id=?", ("approved" if approve else "cancelled", pid))
    c.commit()
    c.close()
    if not approve:
        return {"cancelled": pid}
    a = json.loads(row["args"])
    if row["action"] == "remove_item":
        return _remove_item(a["sku"])
    if row["action"] == "remove_stock":
        return _remove_stock(a["godown"], a["sku"], a["qty"], a.get("reason", "write-off"))
    return {"error": "unknown action"}


def _catalog():
    c = db()
    rows = {r["sku"]: dict(r) for r in c.execute("SELECT * FROM catalog")}
    c.close()
    return rows


def _hav(a, b):
    r = 6371
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


_COORD = re.compile(r"^\s*\(?\s*(-?\d{1,2}(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])?\s*\)?\s*$", re.I)
_GEO_CACHE = {}
GEO_DIAG = {"last": ""}  # why the last Google call failed (shown in hints)


def _parse_coords(addr: str):
    """'13.08, 80.27' / '13.08N 80.27E' / '(13.08,80.27)' -> (lat, lon), else None."""
    m = _COORD.match(addr or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(3))
    if (m.group(2) or "").upper() == "S":
        lat = -abs(lat)
    if (m.group(4) or "").upper() == "W":
        lon = -abs(lon)
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _geocode(addr: str):
    """Coordinates -> used directly. Full address -> Google Geocoding (cached). Else offline city lookup."""
    addr = (addr or "").strip()
    if addr in _GEO_CACHE:
        return _GEO_CACHE[addr]
    out = _geocode_uncached(addr)
    if out[0] is not None:
        _GEO_CACHE[addr] = out
    return out


def _geocode_uncached(addr: str):
    ll = _parse_coords(addr)
    if ll:
        return ll, "coordinates"
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if os.environ.get("USE_REAL_MAPS") == "1" and key:
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(
                {"address": addr, "key": key, "region": "in"})
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.load(r)
            if data.get("status") == "OK":
                loc = data["results"][0]["geometry"]["location"]
                return (loc["lat"], loc["lng"]), "google-maps"
            GEO_DIAG["last"] = f"google geocoding status {data.get('status')}: {data.get('error_message', '')[:80]}"
        except Exception as e:
            GEO_DIAG["last"] = f"google geocoding request failed: {str(e)[:80]}"  # self-heal: fall through
    low = addr.lower()
    for name, ll in CITY.items():
        if name in low:
            return ll, "offline-geocoder"
    return None, "unresolved"


@mcp.tool()
def check_stock(sku: str, qty: int, destination: str) -> dict:
    """List godowns holding `qty` of `sku`, ranked by distance to the destination address."""
    if _bad_qty(qty):
        return {"error": f"qty must be a whole number between 1 and {MAX_QTY}"}
    destination = _clean(destination, 200)
    dest, src = _geocode(destination)
    if dest is None:
        return {"error": f"could not geocode '{destination}'",
                "hint": "retry with just the city name, e.g. 'Madurai', or latitude,longitude. " + GEO_DIAG["last"]}
    c = db()
    rows = c.execute("SELECT godown, qty FROM stock WHERE sku=?", (sku,)).fetchall()
    c.close()
    if not rows:
        return {"error": f"unknown sku {sku}", "known_skus": list(_catalog())}
    out = []
    for r in rows:
        g = GODOWNS[r["godown"]]
        out.append({"godown": r["godown"], "name": g[0], "available": r["qty"],
                    "enough": r["qty"] >= qty, "distance_km": round(_hav((g[1], g[2]), dest), 1)})
    out.sort(key=lambda x: x["distance_km"])
    ok = [o for o in out if o["enough"]]
    return {"geocode_source": src, "ranked": out,
            "recommended": ok[0]["godown"] if ok else None,
            "note": None if ok else "no single godown has enough; consider splitting or reducing qty"}


@mcp.tool()
def reserve_stock(godown: str, sku: str, qty: int) -> dict:
    """Atomically reserve (decrement) stock at a godown."""
    if _bad_qty(qty):
        return {"error": f"qty must be a whole number between 1 and {MAX_QTY}"}
    c = db()
    cur = c.execute("UPDATE stock SET qty=qty-? WHERE godown=? AND sku=? AND qty>=?", (qty, godown, sku, qty))
    c.commit()
    left = c.execute("SELECT qty FROM stock WHERE godown=? AND sku=?", (godown, sku)).fetchone()
    c.close()
    if cur.rowcount == 0:
        return {"error": f"insufficient stock at {godown} for {sku}", "hint": "call check_stock and pick another godown"}
    return {"reserved": qty, "godown": godown, "remaining": left["qty"]}


@mcp.tool()
def select_box(sku: str, qty: int) -> dict:
    """Choose the cheapest box that fits total weight and volume for the order."""
    if _bad_qty(qty):
        return {"error": f"qty must be a whole number between 1 and {MAX_QTY}"}
    cat = _catalog()
    if sku not in cat:
        return {"error": f"unknown sku {sku}"}
    w, v = cat[sku]["weight"], cat[sku]["volume"]
    tw, tv = round(w * qty * 1.1, 2), round(v * qty * 1.15, 2)  # padding
    for bid, mw, mv, cost in BOXES:
        if tw <= mw and tv <= mv:
            return {"box": bid, "boxes": 1, "total_weight_kg": tw, "total_volume_l": tv, "box_cost": cost}
    big = BOXES[-1]
    n = max(math.ceil(tw / big[1]), math.ceil(tv / big[2]))
    return {"box": big[0], "boxes": n, "total_weight_kg": tw, "total_volume_l": tv,
            "box_cost": big[3] * n, "note": "order split across multiple boxes"}


def _routes_api(origin, dest):
    """Google Routes API: real driving distance, duration and polyline. Returns None on any failure."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if os.environ.get("USE_REAL_MAPS") != "1" or not key:
        return None
    try:
        body = {"origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
                "destination": {"location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}},
                "travelMode": "DRIVE"}
        req = urllib.request.Request(
            "https://routes.googleapis.com/directions/v2:computeRoutes", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"})
        with urllib.request.urlopen(req, timeout=5) as r:
            rt = json.load(r)["routes"][0]
        return {"km": round(rt["distanceMeters"] / 1000, 1), "hours": round(int(rt["duration"].rstrip("s")) / 3600, 1),
                "polyline": rt["polyline"]["encodedPolyline"]}
    except Exception:
        return None  # self-heal: fall back to offline estimate


@mcp.tool()
def get_route(godown: str, destination: str) -> dict:
    """Distance (km), drive ETA and path from a godown to the destination (Google Routes API if enabled, else offline)."""
    dest, src = _geocode(destination)
    if dest is None:
        return {"error": f"could not geocode '{destination}'", "hint": "use the city name only"}
    g = GODOWNS[godown]
    real = _routes_api((g[1], g[2]), dest)
    if real:
        return {"distance_km": real["km"], "drive_hours": real["hours"], "source": src + "+routes-api",
                "from": [g[1], g[2]], "to": list(dest), "polyline": real["polyline"]}
    km = round(_hav((g[1], g[2]), dest) * 1.25, 1)  # road factor
    return {"distance_km": km, "drive_hours": round(km / 55, 1), "source": src + "+offline",
            "from": [g[1], g[2]], "to": list(dest)}


@mcp.tool()
def get_carrier_quote(carrier: str, distance_km: float, weight_kg: float) -> dict:
    """Quote a carrier. Carriers: FastFreight, BlueDart-Mock, EcoMove. FastFreight is flaky on the first call (planted failure)."""
    if carrier not in CARRIERS:
        return {"error": f"unknown carrier {carrier}", "carriers": list(CARRIERS)}
    if carrier == "FastFreight" and os.environ.get("FLAKY_CARRIER", "1") == "1":
        st = json.loads(FAIL_STATE.read_text()) if FAIL_STATE.exists() else {"n": 0}
        st["n"] += 1
        FAIL_STATE.write_text(json.dumps(st))
        if st["n"] % 3 == 1:
            return {"error": "503 Service Unavailable from FastFreight API",
                    "hint": "retry once, or fall back to another carrier"}
    base, pkg, pkm, speed = CARRIERS[carrier]
    return {"carrier": carrier, "price": round(base + pkg * weight_kg + pkm * distance_km, 2),
            "eta_hours": round(distance_km / speed + 6, 1)}


@mcp.tool()
def create_shipment(order_id: str, customer: str, destination: str, godown: str, sku: str, qty: int,
                    box: str, carrier: str, price: float, eta_hours: float, box_cost: float = 0.0) -> dict:
    """Create the shipment record, generate tracking ID and customer notification text."""
    order_id, customer, destination = _clean(order_id, 24), _clean(customer, 60), _clean(destination, 200)
    tid = "TRK" + uuid.uuid4().hex[:8].upper()
    data = dict(tracking_id=tid, order_id=order_id, customer=customer, destination=destination, godown=godown,
                sku=sku, qty=qty, box=box, carrier=carrier, price=price, eta_hours=eta_hours, status="DISPATCHED")
    cat = _catalog().get(sku, {})
    sell, cost = cat.get("price", 0), cat.get("cost", 0)
    data.update(revenue=sell * qty, product_cost=cost * qty, shipping_cost=round(price, 2), packaging_cost=box_cost)
    c = db()
    c.execute("INSERT INTO shipments VALUES(?,?,?)", (tid, json.dumps(data), time.time()))
    c.commit()
    c.close()
    data["notification"] = (f"Hi {customer}, your order {order_id} ({qty} x {sku}) is dispatched via {carrier}. "
                            f"Tracking {tid}, ETA ~{eta_hours}h.")
    return data


@mcp.tool()
def list_catalog() -> dict:
    """All products with price, cost, weight, volume and total stock across godowns."""
    c = db()
    tot = {r["sku"]: r["t"] for r in c.execute("SELECT sku, SUM(qty) t FROM stock GROUP BY sku")}
    c.close()
    return {k: {**v, "stock": tot.get(k, 0)} for k, v in _catalog().items()}


@mcp.tool()
def restock(godown: str, sku: str, qty: int) -> dict:
    """Add `qty` units of an existing product to a godown."""
    if _bad_qty(qty):
        return {"error": f"qty must be a whole number between 1 and {MAX_QTY}"}
    if godown not in GODOWNS:
        return {"error": f"unknown godown {godown}", "godowns": list(GODOWNS)}
    if sku not in _catalog():
        return {"error": f"unknown sku {sku}", "hint": "use add_item to create a new product"}
    c = db()
    c.execute("INSERT INTO stock VALUES(?,?,?) ON CONFLICT(godown, sku) DO UPDATE SET qty = qty + ?", (godown, sku, qty, qty))
    c.commit()
    now = c.execute("SELECT qty FROM stock WHERE godown=? AND sku=?", (godown, sku)).fetchone()["qty"]
    c.close()
    return {"godown": godown, "sku": sku, "added": qty, "now": now}


@mcp.tool()
def add_item(sku: str, name: str, weight_kg: float, volume_l: float, price: float, cost: float,
             godown: str = "GD-BLR", qty: int = 0) -> dict:
    """Create a new product in the catalog and optionally stock it in a godown."""
    sku, name = sku.strip().upper(), _clean(name, 60)
    if not SKU_RE.match(sku):
        return {"error": "sku must be 2-24 chars: letters, digits and dashes, e.g. WEBCAM-HD"}
    if sku in _catalog():
        return {"error": f"sku '{sku}' already exists", "hint": "use restock for existing products"}
    if not name:
        return {"error": "name is required"}
    if not (0 < weight_kg <= 500 and 0 < volume_l <= 2000 and 0 <= cost <= 10_000_000 and 0 <= price <= 10_000_000):
        return {"error": "weight 0-500 kg, volume 0-2000 L, price and cost 0-10,000,000 are required"}
    if qty < 0 or qty > MAX_QTY:
        return {"error": f"qty must be between 0 and {MAX_QTY}"}
    c = db()
    c.execute("INSERT INTO catalog VALUES(?,?,?,?,?,?)", (sku, name, weight_kg, volume_l, price, cost))
    for g in GODOWNS:  # every godown gets a row so it shows up in the stock table
        c.execute("INSERT OR IGNORE INTO stock VALUES(?,?,0)", (g, sku))
    c.commit()
    c.close()
    if qty > 0:
        return {"created": sku, **restock(godown, sku, qty)}
    return {"created": sku}


@mcp.tool()
def remove_item(sku: str) -> dict:
    """Propose deleting a product from the catalog. This only QUEUES the request; a person must confirm it in the dashboard."""
    sku = sku.strip().upper()
    if sku not in _catalog():
        return {"error": f"unknown sku {sku}"}
    c = db()
    left = c.execute("SELECT COALESCE(SUM(qty),0) t FROM stock WHERE sku=?", (sku,)).fetchone()["t"]
    c.close()
    return _propose("remove_item", {"sku": sku}, f"Delete product {sku} and discard {left} units in stock")


def _remove_item(sku):
    c = db()
    left = c.execute("SELECT COALESCE(SUM(qty),0) t FROM stock WHERE sku=?", (sku,)).fetchone()["t"]
    c.execute("DELETE FROM stock WHERE sku=?", (sku,))
    c.execute("DELETE FROM catalog WHERE sku=?", (sku,))
    c.commit()
    c.close()
    return {"removed": sku, "units_discarded": left}


@mcp.tool()
def update_item(sku: str, name: str = "", price: float = -1, cost: float = -1,
                weight_kg: float = -1, volume_l: float = -1) -> dict:
    """Change a product's name, selling price, unit cost, weight or volume. Only fields you pass are changed."""
    sku = sku.strip().upper()
    if sku not in _catalog():
        return {"error": f"unknown sku {sku}"}
    fields = {"name": _clean(name, 60) or None, "price": price if price >= 0 else None, "cost": cost if cost >= 0 else None,
              "weight": weight_kg if weight_kg > 0 else None, "volume": volume_l if volume_l > 0 else None}
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return {"error": "nothing to change", "hint": "pass at least one of name, price, cost, weight_kg, volume_l"}
    if any(k in fields and not (0 <= fields[k] <= 10_000_000) for k in ("price", "cost")):
        return {"error": "price and cost must be between 0 and 10,000,000"}
    c = db()
    c.execute("UPDATE catalog SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE sku=?", (*fields.values(), sku))
    c.commit()
    c.close()
    return {"updated": sku, **fields}


@mcp.tool()
def remove_stock(godown: str, sku: str, qty: int, reason: str = "write-off") -> dict:
    """Remove `qty` units from a godown (damage, loss, transfer out). Cannot go below zero. Large removals need human confirmation."""
    if _bad_qty(qty):
        return {"error": f"qty must be a whole number between 1 and {MAX_QTY}"}
    if qty > BIG_REMOVAL:
        return _propose("remove_stock", {"godown": godown, "sku": sku, "qty": qty, "reason": _clean(reason, 80)},
                        f"Remove {qty} x {sku} from {godown}")
    return _remove_stock(godown, sku, qty, _clean(reason, 80))


def _remove_stock(godown, sku, qty, reason):
    c = db()
    cur = c.execute("UPDATE stock SET qty=qty-? WHERE godown=? AND sku=? AND qty>=? AND ?>0", (qty, godown, sku, qty, qty))
    c.commit()
    row = c.execute("SELECT qty FROM stock WHERE godown=? AND sku=?", (godown, sku)).fetchone()
    c.close()
    if cur.rowcount == 0:
        return {"error": f"cannot remove {qty} x {sku} from {godown}", "available": row["qty"] if row else 0}
    return {"godown": godown, "sku": sku, "removed": qty, "reason": reason, "now": row["qty"]}


@mcp.tool()
def save_lesson(stage: str, problem: str, fix: str) -> dict:
    """Persist a lesson learned from a failure so future runs avoid it (learning loop)."""
    ls = json.loads(LESSONS.read_text()) if LESSONS.exists() else []
    ls.append({"stage": stage, "problem": problem, "fix": fix, "ts": time.strftime("%H:%M:%S")})
    LESSONS.write_text(json.dumps(ls[-30:], indent=1))
    return {"saved": True, "total_lessons": len(ls)}


@mcp.tool()
def get_lessons(stage: str = "") -> list:
    """Fetch past lessons (optionally for one stage) to apply before acting."""
    ls = json.loads(LESSONS.read_text()) if LESSONS.exists() else []
    return [l for l in ls if not stage or l["stage"] == stage][-8:]


if __name__ == "__main__":
    mcp.run("stdio")
