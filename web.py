"""The phone app's server: takes photos in, shows what was read, saves corrections, sums spending."""
import hashlib, hmac, json, os, secrets, threading, time
from datetime import date, timedelta
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import bridge

PASSCODE = os.environ.get("APP_PASSCODE", "")
# Authentik username -> person, e.g. "alex=Alex,sam=Sam". Traefik's forward-auth sends the
# signed-in user as X-authentik-username; the app then picks that person by default.
AUTH_PEOPLE = dict(p.split("=", 1) for p in os.environ.get("AUTH_PEOPLE", "").split(",") if "=" in p)


PROXY_IPS = {ip.strip() for ip in os.environ.get("PROXY_IPS", "").split(",") if ip.strip()}


# People who may add, see and change documents for everyone. Names as in PEOPLE.
ADMINS = {a.strip() for a in os.environ.get("ADMINS", "").split(",") if a.strip()}
NAMES = [p["name"] for p in bridge.PEOPLE]


def me(request):
    # trust the header only from the proxy, else anyone on the LAN could claim to be anyone
    peer = request.client.host if request.client else ""
    if PROXY_IPS and peer not in PROXY_IPS:
        if request.headers.get("x-authentik-username"):
            bridge.log(f"sign-in header ignored: request came from {peer}, not from PROXY_IPS")
        return None
    return AUTH_PEOPLE.get(request.headers.get("x-authentik-username", ""))


def who(request):
    """(name, is_admin). With sign-in per person (AUTH_PEOPLE), each person acts as themselves and
    only ADMINS act for others; a request with no known person gets (None, False). Without it (one
    shared passcode, or none) there is no way to tell people apart, so everyone acts for everyone."""
    if not AUTH_PEOPLE:
        return None, True
    name = me(request)
    return name, bool(name and name in ADMINS)


def allowed_people(request):
    name, admin = who(request)
    return NAMES if admin else ([name] if name else [])


def owner_for(request, asked):
    """The person a new entry belongs to: an admin may pick anyone, everyone else only themselves."""
    name, admin = who(request)
    if admin:
        owner = asked or name or ""
        if owner and owner not in NAMES:
            raise HTTPException(400, "Unknown person")
        return owner or None
    if asked and asked != name:
        raise HTTPException(403, "You can only add entries in your own name")
    return name


def check_access(request, doc):
    name, admin = who(request)
    if not admin and doc["owner"] != name:
        raise HTTPException(404, "No such document")   # 404, not 403: don't confirm it exists
COOKIE = "receipts_session"
MAX_BYTES = 30 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
local = threading.local()


def con():
    if not hasattr(local, "con"):
        local.con = bridge.db()
    return local.con


def secret():
    path = os.path.join(os.path.dirname(bridge.DB), "session.key")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return open(path).read().strip()


def token():
    return hmac.new(secret().encode(), ("ok:" + PASSCODE).encode(), hashlib.sha256).hexdigest()


def signed_in(request):
    return not PASSCODE or hmac.compare_digest(request.cookies.get(COOKIE, ""), token())


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/config") and not signed_in(request):
        return JSONResponse({"detail": "Passcode needed"}, status_code=401)
    if path.startswith("/api/") and path != "/api/config" and AUTH_PEOPLE and not me(request):
        return JSONResponse({"detail": "Your sign-in isn't linked to a person in this app"}, status_code=403)
    response = await call_next(request)
    if not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"   # the app shell must pick up new versions
    return response


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    if not hmac.compare_digest(str(body.get("passcode", "")), PASSCODE):
        time.sleep(1.5)
        raise HTTPException(401, "Wrong passcode")
    response.set_cookie(COOKIE, token(), max_age=400 * 86400, httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return {"ok": True}


@app.get("/api/config")
def config(request: Request):
    if not signed_in(request):
        return {"locked": True}
    name, admin = who(request)
    if AUTH_PEOPLE and not name:
        return {"locked": False, "denied": "Your sign-in isn't linked to a person in this app. Ask the admin to add you."}
    return {"locked": False, "me": name, "admin": admin,
            "people": allowed_people(request),
            "kinds": [{"key": k, "label": v["label"], "spending": k in bridge.SPENDING, "fields": v["fields"]} for k, v in bridge.KINDS.items()],
            "categories": bridge.options("Expense category"),
            "methods": bridge.options("Payment method") + ["Unknown"]}


def row(doc, full=False):
    info = json.loads(doc["info"]) if doc["info"] else {}
    out = {"id": doc["id"], "status": doc["status"], "owner": doc["owner"], "error": doc["error"], "created_at": doc["created_at"],
           "kind": info.get("kind"), "title": info.get("title"), "issuer": info.get("issuer"), "date": info.get("date") or doc["taken"],
           "total": info.get("total"), "paperless_id": doc["paperless_id"], "wallos": doc["wallos"], "pdf": bool(doc["original"]),
           "manual": doc["source"] == "manual"}
    if full:
        out["info"] = info
    return out


def get_doc(doc_id):
    doc = con().execute("select * from docs where id=? and status != 'deleted'", (doc_id,)).fetchone()
    if not doc:
        raise HTTPException(404, "No such document")
    return doc


@app.post("/api/docs")
def upload(request: Request, file: UploadFile = File(...), owner: str = Form(""), client_id: str = Form("")):
    owner = owner_for(request, owner)
    raw = file.file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "Photo is larger than 30 MB")
    ref = "app:" + (client_id or secrets.token_hex(8))[:64]
    try:
        doc_id = bridge.add_doc(con(), raw, owner or None, "app", ref)
    except Exception:
        raise HTTPException(400, "That file is not a photo or PDF this app can read")
    return {"id": doc_id}


@app.get("/api/docs")
def docs(request: Request, show: str = "check", limit: int = 60):
    where = {"check": "status in ('queued','uploading','reading','review','failed','duplicate','cancel')",
             "done": "status = 'done'"}.get(show, "status != 'deleted'")
    name, admin = who(request)
    mine, args = ("", ()) if admin else (" and owner = ?", (name,))
    rows = con().execute(f"select * from docs where {where} and status != 'deleted'{mine} order by id desc limit ?", (*args, min(limit, 300))).fetchall()
    waiting = con().execute(f"select count(*) from docs where status in ('review','failed','duplicate'){mine}", args).fetchone()[0]
    return {"docs": [row(d) for d in rows], "waiting": waiting}


@app.get("/api/docs/{doc_id}")
def doc(request: Request, doc_id: int):
    d = get_doc(doc_id); check_access(request, d)
    return row(d, full=True)


@app.post("/api/manual")
async def manual(request: Request):
    """An expense with no photo (petrol with no receipt, cash, ...): ledger only."""
    body = await request.json()
    owner = owner_for(request, body.get("owner"))
    info = bridge.clean(body, time.strftime("%Y-%m-%d"))
    if info["kind"] not in bridge.SPENDING or not info["total"]:
        raise HTTPException(400, "A manual entry needs an amount")
    c = con()
    cur = c.execute("insert into docs (source, source_ref, owner, status, taken, info, created_at, updated_at) values (?,?,?,?,?,?,?,?)",
                    ("manual", "manual:" + secrets.token_hex(8), owner, "done", info["date"], json.dumps(info), bridge.now(), bridge.now()))
    c.commit()
    doc_id = cur.lastrowid
    bridge.save_manual(c, doc_id, info, owner)
    try:
        wallos = bridge.wallos_sync(info, None)
    except Exception as e:
        wallos = "Wallos could not be reached: " + str(e)[:120]
    bridge.set_status(c, doc_id, "done", info=json.dumps(info), wallos=wallos)
    return row(get_doc(doc_id), full=True)


@app.put("/api/docs/{doc_id}")
async def save(doc_id: int, request: Request):
    d = get_doc(doc_id); check_access(request, d)
    if d["source"] == "manual":
        info = bridge.clean(await request.json(), d["taken"])
        bridge.save_manual(con(), doc_id, info, d["owner"])
        try:
            wallos = bridge.wallos_sync(info, None)
        except Exception as e:
            wallos = "Wallos could not be reached: " + str(e)[:120]
        bridge.set_status(con(), doc_id, "done", info=json.dumps(info), wallos=wallos)
        return row(get_doc(doc_id), full=True)
    if not d["paperless_id"]:
        raise HTTPException(409, "This photo has not reached Paperless yet")
    info = bridge.clean(await request.json(), d["taken"])
    try:
        bridge.apply(con(), d["paperless_id"], info, d["owner"], confirmed=True)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])
    try:
        wallos = bridge.wallos_sync(info, d["paperless_id"])
    except Exception as e:
        wallos = "Wallos could not be reached: " + str(e)[:120]
    bridge.set_status(con(), doc_id, "done", info=json.dumps(info), error=bridge.same_as(con(), doc_id, info), wallos=wallos)
    return row(get_doc(doc_id), full=True)


@app.post("/api/docs/{doc_id}/reread")
def reread(request: Request, doc_id: int):
    d = get_doc(doc_id); check_access(request, d)
    if d["status"] in ("uploading", "reading"):
        raise HTTPException(409, "Already being read")
    if d["status"] == "duplicate":
        raise HTTPException(409, "This photo is a duplicate; delete it here instead")
    bridge.set_status(con(), doc_id, "queued", tries=0, error=None)
    bridge.wake.set()
    return {"ok": True}


@app.delete("/api/docs/{doc_id}")
def delete(request: Request, doc_id: int):
    d = get_doc(doc_id); check_access(request, d)
    if d["status"] in ("uploading", "reading"):
        # the worker is on it: it stops at its next step and removes everything
        bridge.set_status(con(), doc_id, "cancel")
        return {"ok": True, "pending": True}
    bridge.forget(con(), d)
    return {"ok": True}


@app.get("/api/docs/{doc_id}/photo")
def photo(request: Request, doc_id: int, size: str = "small"):
    d = get_doc(doc_id); check_access(request, d)
    path = bridge.photo_path(con(), d, small=size != "full")
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No photo")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def period_of(day, kind):
    """Key and (first, last) day of the week (Monday start), month or quarter holding `day`."""
    d = date.fromisoformat(day)
    if kind == "week":
        start = d - timedelta(days=d.weekday()); end = start + timedelta(days=6)
        return start.isoformat(), (start, end)
    if kind == "quarter":
        q = (d.month - 1) // 3; start = date(d.year, q * 3 + 1, 1)
        end = date(d.year + (q == 3), (q * 3 + 3) % 12 + 1, 1) - timedelta(days=1)
        return f"{d.year}-Q{q + 1}", (start, end)
    start = date(d.year, d.month, 1)
    end = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
    return start.strftime("%Y-%m"), (start, end)


def period_label(key, kind, span):
    if kind == "week":
        return f"Week of {span[0].day} {span[0].strftime('%b %Y')}"
    if kind == "quarter":
        return key.replace("-", " ")
    return span[0].strftime("%B %Y")


@app.get("/api/spending")
def spending(period: str = "month", key: str = ""):
    kind = period if period in ("week", "month", "quarter") else "month"
    c = con()
    days = [r[0] for r in c.execute("select date from purchases where date is not null")]
    periods = {}
    for dday in days + [date.today().isoformat()]:
        try:
            k, span = period_of(dday, kind)
        except ValueError:
            continue
        periods[k] = span
    keys = sorted(periods, reverse=True)
    key = key if key in periods else keys[0]
    first, last = (x.isoformat() for x in periods[key])

    def totals(a, b):
        return c.execute("select coalesce(sum(total),0), count(*) from purchases where date between ? and ?", (a, b)).fetchone()

    total = totals(first, last)
    # category totals come from the items where a receipt has them, otherwise from the receipt as a whole
    cats = c.execute("""
        select category, sum(amount) from (
            select coalesce(nullif(i.category,''),'Uncategorised') category, i.price amount
              from items i join purchases p on p.id = i.purchase_id where p.date between ? and ?
            union all
            select coalesce(nullif(p.category,''),'Uncategorised'), p.total from purchases p
             where p.date between ? and ? and not exists (select 1 from items i where i.purchase_id = p.id)
        ) group by category order by 2 desc""", (first, last, first, last)).fetchall()
    shops = c.execute("select coalesce(nullif(merchant,''),'Unknown'), sum(total), count(*) from purchases where date between ? and ? group by 1 order by 2 desc limit 15", (first, last)).fetchall()
    people = c.execute("select coalesce(owner,'Unknown'), sum(total) from purchases where date between ? and ? group by 1 order by 2 desc", (first, last)).fetchall()
    # the eight periods up to the chosen one, oldest first, for the trend chart
    trend, cursor = [], periods[key][0]
    for _ in range(8):
        k, span = period_of(cursor.isoformat(), kind)
        trend.append({"key": k, "label": period_label(k, kind, span), "short": span[0].strftime("%d %b") if kind == "week" else (k[-2:] if kind == "quarter" else span[0].strftime("%b")),
                      "amount": totals(span[0].isoformat(), span[1].isoformat())[0]})
        cursor = span[0] - timedelta(days=1)
    trend.reverse()
    return {"period": kind, "key": key, "label": period_label(key, kind, periods[key]), "from": first, "to": last,
            "periods": [{"key": k, "label": period_label(k, kind, periods[k])} for k in keys],
            "total": total[0], "count": total[1], "trend": trend,
            "categories": [{"name": r[0], "amount": r[1] or 0} for r in cats],
            "shops": [{"name": r[0], "amount": r[1] or 0, "count": r[2]} for r in shops],
            "people": [{"name": r[0], "amount": r[1] or 0} for r in people]}


@app.get("/api/items")
def items(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return {"items": [], "total": 0, "count": 0}
    c = con()
    like = "%" + q.replace("%", "") + "%"
    rows = c.execute("select date, merchant, name, qty, price from item_spend where name like ? order by date desc limit 100", (like,)).fetchall()
    total = c.execute("select coalesce(sum(price),0), count(*) from item_spend where name like ?", (like,)).fetchone()
    return {"items": [dict(r) for r in rows], "total": total[0], "count": total[1]}


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")


@app.on_event("startup")
def start_worker():
    threading.Thread(target=bridge.worker, daemon=True).start()
