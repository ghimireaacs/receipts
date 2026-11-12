"""Receipts: phone photos -> Paperless + an item-level spending ledger.

Photos arrive two ways: the phone app (web.py, the normal route) or the Immich album "Paperless"
(optional, kept for photos that were taken with the camera app). Each photo is kept in /data/photos,
uploaded to Paperless for OCR, read by the local vision model, filed in Paperless, and written to
the SQLite ledger. It then waits in the app's "To check" list until a person confirms the numbers.
"""
import base64, hashlib, io, json, os, re, sqlite3, threading, time, traceback, uuid
from datetime import datetime
import requests
from PIL import Image, ImageOps
import pillow_heif
import zxingcpp
import pypdfium2

pillow_heif.register_heif_opener()

IMMICH = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH = IMMICH + "/api" if IMMICH else ""
PAPERLESS = os.environ.get("PAPERLESS_URL", "").rstrip("/")
PAPERLESS = PAPERLESS + "/api" if PAPERLESS else ""   # empty: run without Paperless (ledger only)
OLLAMA = os.environ["OLLAMA_URL"].rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
ALBUM = os.environ.get("ALBUM_NAME", "Paperless")
POLL = int(os.environ.get("POLL_SECONDS", "120"))
DB = os.environ.get("DB_PATH", "/db/ledger.db")
PHOTOS = os.environ.get("PHOTO_DIR", "/data/photos")
BACKUP = os.environ.get("BACKUP_DIR", "/data/backup")
WALLOS = os.environ.get("WALLOS_URL", "").rstrip("/")
WALLOS_KEY = os.environ.get("WALLOS_API_KEY", "")
WALLOS_NOTIFY_DAYS = int(os.environ.get("WALLOS_NOTIFY_DAYS", "3"))
WALLOS_CURRENCY = int(os.environ.get("WALLOS_CURRENCY_ID", "1"))   # the currency's id in Wallos
CURRENCY = os.environ.get("CURRENCY", "AUD").upper()   # ISO code, for Paperless amounts
DATE_ORDER = os.environ.get("DATE_ORDER", "DMY").upper()   # how dates are printed locally: DMY, MDY or YMD
TAX_LABEL = os.environ.get("TAX_LABEL", "GST")           # what receipts call the tax (GST, VAT, sales tax)

# Without Paperless, categories and payment methods come from here (with Paperless: its select fields)
DEFAULT_CATEGORIES = ["Groceries", "Household & kitchen", "Dining & takeaway", "Transport & fuel", "Utilities & internet",
                      "Rent & housing", "Health & pharmacy", "Clothing", "Electronics & tech", "Subscriptions", "Education",
                      "Gifts & donations", "Travel", "Entertainment", "Insurance", "Fees, tax & government",
                      "Furniture & appliances", "Personal care", "Other"]
DEFAULT_METHODS = ["Card", "Cash", "Bank transfer", "Direct debit", "Other"]


def _list(name, default):
    v = [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]
    return v or default


CATEGORIES = _list("CATEGORIES", DEFAULT_CATEGORIES)
METHODS = _list("PAYMENT_METHODS", DEFAULT_METHODS)

# PEOPLE=Name:paperless-owner-tag-id:immich-user-id,...  (the last two parts are optional)
PEOPLE = []
for part in os.environ.get("PEOPLE", "").split(","):
    bits = [b.strip() for b in part.split(":")]
    if bits[0]:
        PEOPLE.append({"name": bits[0], "tag": int(bits[1]) if len(bits) > 1 and bits[1] else None,
                       "immich": bits[2] if len(bits) > 2 else None})

im = requests.Session(); im.headers["x-api-key"] = os.environ.get("IMMICH_API_KEY", "")
class PaperlessAuth(requests.auth.AuthBase):
    """PAPERLESS_TOKEN, or a token fetched once with PAPERLESS_USER / PAPERLESS_PASSWORD."""
    def __init__(self):
        self.token = os.environ.get("PAPERLESS_TOKEN", "")

    def __call__(self, r):
        if not self.token and os.environ.get("PAPERLESS_USER") and PAPERLESS:
            t = requests.post(f"{PAPERLESS}/token/", timeout=30,
                              data={"username": os.environ["PAPERLESS_USER"], "password": os.environ.get("PAPERLESS_PASSWORD", "")})
            t.raise_for_status()
            self.token = t.json()["token"]
        if self.token:
            r.headers["Authorization"] = "Token " + self.token
        return r


pl = requests.Session(); pl.auth = PaperlessAuth(); pl.headers["Accept"] = "application/json; version=9"

VITAL = ["Expiry date", "Issue date", "Document number"]
# what the model may answer -> how Paperless files it (names are looked up, so ids never go stale)
KINDS = {
    "receipt": {"label": "Receipt", "type": "Receipt", "path": "Receipts", "tags": [], "fields": ["Amount", "GST", "Expense category", "Payment method", "Store location", "Line items", "Document number"]},
    "bill": {"label": "Bill / invoice", "type": "Bill / Invoice", "path": "Bills", "tags": ["to-pay"], "fields": ["Amount", "GST", "Due date", "Expense category", "Document number"]},
    "identity_document": {"label": "Identity document", "type": "Identity document", "path": "Vital", "tags": ["vital"], "fields": VITAL},
    "visa_or_immigration": {"label": "Visa / immigration", "type": "Visa / Immigration", "path": "Vital", "tags": ["vital"], "fields": VITAL},
    "certificate": {"label": "Certificate", "type": "Certificate / Qualification", "path": "Vital", "tags": ["vital"], "fields": ["Issue date", "Document number"]},
    "bank_statement": {"label": "Bank statement", "type": "Bank statement", "path": "Financial", "tags": [], "fields": []},
    "payslip": {"label": "Payslip", "type": "Payslip", "path": "Financial", "tags": [], "fields": ["Amount"]},
    "tax_document": {"label": "Tax document", "type": "Tax document", "path": "Financial", "tags": [], "fields": []},
    "insurance": {"label": "Insurance", "type": "Insurance", "path": "Financial", "tags": [], "fields": ["Expiry date", "Document number"]},
    "contract": {"label": "Contract", "type": "Contract / Agreement", "path": None, "tags": [], "fields": []},
    "academic_record": {"label": "Academic record", "type": "Academic record", "path": None, "tags": [], "fields": []},
    "medical": {"label": "Medical", "type": "Medical", "path": None, "tags": [], "fields": []},
    "vehicle": {"label": "Vehicle", "type": "Vehicle", "path": None, "tags": [], "fields": ["Expiry date"]},
    "warranty_or_manual": {"label": "Warranty / manual", "type": "Warranty / Manual", "path": None, "tags": ["warranty"], "fields": []},
    "letter": {"label": "Letter / notice", "type": "Letter / Notice", "path": None, "tags": [], "fields": []},
    "other": {"label": "Other", "type": "Other", "path": None, "tags": [], "fields": []},
}
SPENDING = ("receipt", "bill")
MANAGED_FIELDS = {n for k in KINDS.values() for n in k["fields"]}
RULE_TAGS = {t for k in KINDS.values() for t in k["tags"]}

lock = threading.RLock()      # one Paperless filing at a time (worker thread and web requests)
wake = threading.Event()      # set by the web app when a photo arrives


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.executescript("""
    create table if not exists docs (id integer primary key, source text, source_ref text unique, owner text, photo text,
        paperless_id integer, status text, error text, tries integer default 0, taken text, info text, created_at text, updated_at text);
    create index if not exists docs_status on docs(status);
    create table if not exists purchases (id integer primary key, paperless_id integer unique, kind text, date text, merchant text,
        location text, total real, gst real, payment_method text, category text, owner text, added_at text);
    create table if not exists items (id integer primary key, purchase_id integer references purchases(id) on delete cascade,
        name text, qty real, price real, category text);
    create index if not exists items_name on items(name);
    create view if not exists item_spend as select p.date, p.merchant, p.location, i.name, i.qty, i.price, i.category, p.owner, p.paperless_id
        from items i join purchases p on p.id = i.purchase_id;
    """)
    cols = [r[1] for r in c.execute("pragma table_info(docs)")]
    for col in ("barcode", "original", "wallos", "sha256"):
        if col not in cols:
            c.execute(f"alter table docs add column {col} text")
    if "doc_id" not in [r[1] for r in c.execute("pragma table_info(purchases)")]:
        c.execute("alter table purchases add column doc_id integer")   # set for entries typed in without a photo
    return c


def migrate(con):
    """One-off: carry the first version's Immich bookkeeping into the docs table."""
    if not con.execute("select 1 from sqlite_master where name='processed'").fetchone():
        return
    by_immich = {p["immich"]: p["name"] for p in PEOPLE if p["immich"]}
    for r in con.execute("select * from processed").fetchall():
        p = con.execute("select * from purchases where paperless_id=?", (r["paperless_id"],)).fetchone() if r["paperless_id"] else None
        info = None
        if p:
            items = [dict(x) for x in con.execute("select name, qty, price, category from items where purchase_id=?", (p["id"],))]
            info = {"kind": p["kind"], "title": "", "issuer": p["merchant"], "location": p["location"], "date": p["date"], "total": p["total"],
                    "gst": p["gst"], "payment_method": p["payment_method"], "category": p["category"], "items": items}
        con.execute("insert or ignore into docs (source, source_ref, owner, paperless_id, status, taken, info, created_at, updated_at) values (?,?,?,?,?,?,?,?,?)",
                    ("immich", r["asset_id"], by_immich.get(p["owner"]) if p else None, r["paperless_id"],
                     "review" if r["paperless_id"] else "deleted", p["date"] if p else None, json.dumps(info) if info else None, r["at"], r["at"]))
    for imm, name in by_immich.items():
        con.execute("update purchases set owner=? where owner=?", (name, imm))
    con.execute("drop table processed")
    con.commit()
    log("moved the old Immich records into the new table")


# ---------- Paperless helpers ----------

def pl_all(kind):
    out, url = {}, f"{PAPERLESS}/{kind}/?page_size=1000"
    while url:
        r = pl.get(url, timeout=60); r.raise_for_status(); j = r.json()
        for x in j["results"]:
            out[x["name"]] = x
        url = j.get("next")
        if url:  # Paperless builds "next" from its public URL; keep using the internal one
            url = PAPERLESS + url.split("/api", 1)[1]
    return out


_ref = {"at": 0, "v": None}


def ref():
    """Paperless types, tags, paths, fields and correspondents by name, refreshed every five minutes."""
    with lock:
        if not _ref["v"] or time.time() - _ref["at"] > 300:
            _ref["v"] = {k: pl_all(k2) for k, k2 in [("types", "document_types"), ("tags", "tags"), ("paths", "storage_paths"),
                                                     ("fields", "custom_fields"), ("correspondents", "correspondents")]}
            _ref["at"] = time.time()
        return _ref["v"]


def options(field_name):
    if not PAPERLESS:
        return CATEGORIES if field_name == "Expense category" else METHODS
    return [o["label"] for o in ref()["fields"][field_name]["extra_data"]["select_options"]]


def option_id(field, label):
    for o in (field.get("extra_data") or {}).get("select_options", []):
        if o["label"].lower() == str(label).lower():
            return o["id"]
    return None


def correspondent_id(name):
    cache = ref()["correspondents"]
    name = re.sub(r"\s+", " ", name or "").strip()[:120]
    if len(name) < 2:
        return None
    for k, v in cache.items():
        if k.lower() == name.lower():
            return v["id"]
    r = pl.post(f"{PAPERLESS}/correspondents/", json={"name": name, "matching_algorithm": 0}, timeout=60)
    if r.ok:
        cache[name] = r.json(); return cache[name]["id"]
    return None


def wait_task(task_id, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        r = pl.get(f"{PAPERLESS}/tasks/?task_id={task_id}", timeout=60); r.raise_for_status()
        j = r.json(); j = j.get("results", j) if isinstance(j, dict) else j
        if j and j[0]["status"] == "SUCCESS":
            return int(j[0]["related_document"]), None
        if j and j[0]["status"] == "FAILURE":
            return None, str(j[0].get("result"))
        time.sleep(5)
    return None, "timed out waiting for Paperless"


# ---------- photos ----------

def to_jpeg(raw):
    """Return (full-size jpeg, smaller jpeg for the model and the app)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    full = io.BytesIO(); img.save(full, "JPEG", quality=92)
    small = img.copy(); small.thumbnail((1600, 1600)); sm = io.BytesIO(); small.save(sm, "JPEG", quality=85)
    return full.getvalue(), sm.getvalue()


def is_pdf(raw):
    return raw[:5] == b"%PDF-"


def pdf_page_one(raw):
    """First page of a PDF as a JPEG (about 200 dpi), for the app's preview and the model."""
    pdf = pypdfium2.PdfDocument(raw)
    try:
        img = pdf[0].render(scale=200 / 72).to_pil().convert("RGB")
    finally:
        pdf.close()
    out = io.BytesIO(); img.save(out, "JPEG", quality=92)
    return out.getvalue()


def save_photo(raw, with_original=False):
    """Store the photo (or a PDF's first page) and its smaller copy; return the path relative to PHOTOS.
    With with_original, a PDF is kept beside it as <name>.pdf and (photo, original) is returned."""
    pdf = is_pdf(raw)
    full, small = to_jpeg(pdf_page_one(raw) if pdf else raw)
    rel = datetime.now().strftime("%Y/%m/") + uuid.uuid4().hex + ".jpg"
    path = os.path.join(PHOTOS, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(full)
    with open(path[:-4] + ".small.jpg", "wb") as fh:
        fh.write(small)
    original = None
    if pdf and with_original:
        original = rel[:-4] + ".pdf"
        with open(os.path.join(PHOTOS, original), "wb") as fh:
            fh.write(raw)
    return (rel, original) if with_original else rel


def photo_path(con, doc, small=True):
    """Path of a document's photo. Documents from before the app are fetched from Paperless once."""
    rel = doc["photo"]
    if not rel and doc["paperless_id"] and PAPERLESS:
        r = pl.get(f"{PAPERLESS}/documents/{doc['paperless_id']}/download/?original=true", timeout=120)
        if not r.ok:
            return None
        rel = save_photo(r.content)
        con.execute("update docs set photo=? where id=?", (rel, doc["id"])); con.commit()
    if not rel:
        return None
    path = os.path.join(PHOTOS, rel)
    return path[:-4] + ".small.jpg" if small else path


def pdf_text(path):
    """The text layer of every page of a PDF (empty for a scanned PDF)."""
    try:
        pdf = pypdfium2.PdfDocument(path)
        try:
            return "\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
        finally:
            pdf.close()
    except Exception:
        return ""


def add_doc(con, raw, owner, source, source_ref, taken=None):
    """Queue a new photo. Returns the row id (the existing one if this photo was already sent)."""
    row = con.execute("select id from docs where source_ref=?", (source_ref,)).fetchone()
    if row:
        return row["id"]
    digest = hashlib.sha256(raw).hexdigest()
    same = con.execute("select id from docs where sha256=? and status not in ('deleted','duplicate')", (digest,)).fetchone()
    rel, original = save_photo(raw, with_original=True)
    status, error = ("duplicate", f"The same file as document {same['id']}. Delete this one.") if same else ("queued", None)
    cur = con.execute("insert into docs (source, source_ref, owner, photo, original, status, error, sha256, taken, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                      (source, source_ref, owner, rel, original, status, error, digest, taken or datetime.now().strftime("%Y-%m-%d"), now(), now()))
    con.commit()
    wake.set()
    return cur.lastrowid


# ---------- the model ----------

def ask_model(jpeg, ocr_text):
    """One call: what is this, and (for spending) who sold what, where, for how much."""
    categories, methods = options("Expense category"), options("Payment method")
    item = {"type": "object", "properties": {"name": {"type": "string"}, "qty": {"type": "number"}, "price": {"type": "number"},
            "category": {"type": "string", "enum": categories}}, "required": ["name", "price", "category"]}
    schema = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string", "description": "short plain title, max 8 words"},
        "issuer": {"type": "string", "description": "shop, company or authority that issued it"},
        "location": {"type": "string", "description": "shop branch, suburb or address as printed"},
        "date": {"type": "string"}, "due_date": {"type": "string"}, "expiry_date": {"type": "string"}, "issue_date": {"type": "string"},
        "document_number": {"type": "string", "description": "receipt: register and transaction number as printed, e.g. 'POS 066 TRANS 7272'; bill: invoice number; ID papers: document number"},
        "barcode_number": {"type": "string", "description": "the digits printed under a barcode on the document, if there is one"},
        "total": {"type": "number"}, "gst": {"type": "number"},
        "payment_method": {"type": "string", "enum": methods + ["Unknown"]},
        "category": {"type": "string", "enum": categories},
        "items": {"type": "array", "items": item}},
        "required": ["kind", "title", "issuer", "location", "date", "total", "gst", "payment_method", "category", "items"]}
    prompt = (
        "You file household paperwork. Look at the photo; the text below (if any) is the same document and is more reliable for numbers.\n"
        "Copy what is printed. Never invent a value: use an empty string or 0 when something is not printed.\n"
        "kind: receipt = proof of a purchase already paid; bill = an amount still to be paid; otherwise the closest document kind.\n"
        "issuer: the shop or company name. location: the shop's branch, suburb or street address.\n"
        + {"DMY": "Printed dates are day first (DD/MM/YYYY).", "MDY": "Printed dates are month first (MM/DD/YYYY).",
           "YMD": "Printed dates are year first (YYYY/MM/DD)."}.get(DATE_ORDER, "") + " Answer all dates as YYYY-MM-DD.\n"
        "date: for a receipt the purchase date; for a bill the issue date, or if none is printed the LAST day of the billing period (never its first day). due_date: the pay-by date if printed.\n"
        f"total = final amount. gst = the {TAX_LABEL} or other tax amount if printed.\n"
        "payment_method: Card if it mentions card, EFTPOS, Visa, Mastercard, or tap; Cash if cash; else Unknown.\n"
        "items: one entry per purchased product, empty list if not a receipt or bill. price = the amount in the right-hand price column for that product,\n"
        "  which is what was paid for the whole line. A line like '0.462 kg NET @ $7.90/kg' or 'Qty 2 @ $2.75 each' is not the price: it belongs to the product above it,\n"
        "  and that product's price is the right-hand amount (0.462 x 7.90 = 3.65; 2 x 2.75 = 5.50). qty = N from a 'Qty N @' line, the kg from a per-kg line, otherwise 1 (pack sizes like 150g or 2Kg are part of the name, not qty).\n"
        "  For a bill, the items are its charge lines (supply charge, usage, plan fee and so on), each with its amount; qty = the units billed (days, kWh, GB) if printed.\n"
        "  The item prices must add up to the subtotal (before any discount lines). Food and drink bought at a supermarket is Groceries, not Dining & takeaway.\n"
        "category: the best single category for the whole document.\n\nOCR text:\n" + (ocr_text or "(none)")[:6000])
    body = {"model": MODEL, "stream": False, "format": schema, "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(jpeg).decode()]}]}
    # The graphics card is shared (Immich's photo analysis, other models); wait for it to be free.
    for attempt in range(4):
        try:
            r = requests.post(f"{OLLAMA}/api/chat", timeout=900, json=body)
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])
        except Exception as e:
            if attempt == 3:
                raise
            log(f"  model busy ({str(e)[:80]}), retrying in 60 s")
            time.sleep(60)


def day(v):
    return v if re.fullmatch(r"(19|20)\d\d-[01]\d-[0-3]\d", v or "") else None


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def clean(info, taken=None):
    """Bring model output or an edit from the app into one shape."""
    info = info or {}
    kind = info.get("kind") if info.get("kind") in KINDS else "other"
    text = lambda k, n=120: re.sub(r"\s+", " ", str(info.get(k) or "")).strip()[:n]
    items = []
    for i in (info.get("items") or []) if kind in SPENDING else []:
        name = re.sub(r"\s+", " ", str(i.get("name") or "")).strip()[:120]
        if name:
            items.append({"name": name, "qty": num(i.get("qty")) or 1, "price": num(i.get("price")) or 0, "category": i.get("category") or ""})
    # small models often copy the rate ($/kg, $/day, $/kWh) instead of the line amount; when
    # qty x rate adds up to the total and the prices as given do not, use qty x rate
    total = num(info.get("total"))
    if total and items:
        as_given = sum(i["price"] for i in items)
        as_rate = sum(round(i["qty"] * i["price"], 2) for i in items)
        if abs(as_given - total) > 0.05 and abs(as_rate - total) <= max(0.05, total * 0.005):
            for i in items:
                i["price"] = round(i["qty"] * i["price"], 2)
    return {"kind": kind, "title": text("title"), "issuer": text("issuer"), "location": text("location"),
            "date": day(info.get("date")) or taken, "due_date": day(info.get("due_date")), "expiry_date": day(info.get("expiry_date")),
            "issue_date": day(info.get("issue_date")), "document_number": text("document_number", 60),
            "barcode_number": re.sub(r"\D", "", str(info.get("barcode_number") or ""))[:40],
            "total": num(info.get("total")), "gst": num(info.get("gst")),
            "payment_method": info.get("payment_method") or "Unknown", "category": info.get("category") or "", "items": items}


def apply(con, paperless_id, info, owner, confirmed=False):
    """Write one document's details to Paperless and to the ledger. `info` must come from clean()."""
    with lock:
        r, f = ref(), ref()["fields"]
        rule = KINDS[info["kind"]]
        doc = pl.get(f"{PAPERLESS}/documents/{paperless_id}/", timeout=60); doc.raise_for_status(); doc = doc.json()
        tags = set(doc["tags"]) - {r["tags"][t]["id"] for t in RULE_TAGS if t in r["tags"]}
        tags |= {r["tags"][t]["id"] for t in rule["tags"]}
        if confirmed and "inbox" in r["tags"]:
            tags.discard(r["tags"]["inbox"]["id"])
        patch = {"document_type": r["types"][rule["type"]]["id"], "tags": sorted(tags),
                 "storage_path": r["paths"][rule["path"]]["id"] if rule["path"] else None}
        if info["date"]:
            patch["created"] = info["date"]
        if info["title"]:
            patch["title"] = info["title"]
        cid = correspondent_id(info["issuer"])
        if cid:
            patch["correspondent"] = cid
        values = {
            "Amount": f"{CURRENCY}{info['total']:.2f}" if info["total"] else None,
            "GST": f"{CURRENCY}{info['gst']:.2f}" if info["gst"] else None,
            "Expense category": option_id(f["Expense category"], info["category"]),
            "Payment method": option_id(f["Payment method"], info["payment_method"]),
            "Store location": info["location"] or None,
            "Line items": "\n".join(f"{i['qty']:g} x {i['name']} = {i['price']:.2f}" for i in info["items"]) or None,
            "Due date": info["due_date"], "Expiry date": info["expiry_date"], "Issue date": info["issue_date"],
            "Document number": info["document_number"] or None,
        }
        managed = {f[n]["id"] for n in MANAGED_FIELDS}
        keep = [c for c in doc.get("custom_fields", []) if c["field"] not in managed]   # fields a person added in Paperless
        patch["custom_fields"] = keep + [{"field": f[n]["id"], "value": values.get(n)} for n in rule["fields"]]
        p = pl.patch(f"{PAPERLESS}/documents/{paperless_id}/", json=patch, timeout=60)
        if not p.ok:
            raise RuntimeError(f"Paperless refused the update: {p.status_code} {p.text[:200]}")

        con.execute("delete from items where purchase_id in (select id from purchases where paperless_id=?)", (paperless_id,))
        con.execute("delete from purchases where paperless_id=?", (paperless_id,))
        if info["kind"] in SPENDING:
            cur = con.execute("insert into purchases (paperless_id, kind, date, merchant, location, total, gst, payment_method, category, owner, added_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                              (paperless_id, info["kind"], info["date"], info["issuer"], info["location"] or None, info["total"], info["gst"],
                               info["payment_method"], info["category"], owner, now()))
            con.executemany("insert into items (purchase_id, name, qty, price, category) values (?,?,?,?,?)",
                            [(cur.lastrowid, i["name"], i["qty"], i["price"], i["category"]) for i in info["items"]])
        con.commit()


def save_manual(con, doc_id, info, owner):
    """Ledger rows keyed by the app's own document id: entries typed in without a photo, and every
    document when the app runs without Paperless."""
    with lock:
        con.execute("delete from items where purchase_id in (select id from purchases where doc_id=?)", (doc_id,))
        con.execute("delete from purchases where doc_id=?", (doc_id,))
        if info["kind"] in SPENDING:
            cur = con.execute("insert into purchases (doc_id, kind, date, merchant, location, total, gst, payment_method, category, owner, added_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                              (doc_id, info["kind"], info["date"], info["issuer"], info["location"] or None, info["total"], info["gst"],
                               info["payment_method"], info["category"], owner, now()))
            con.executemany("insert into items (purchase_id, name, qty, price, category) values (?,?,?,?,?)",
                            [(cur.lastrowid, i["name"], i["qty"], i["price"], i["category"]) for i in info["items"]])
        con.commit()


def forget(con, doc):
    """Remove a document everywhere: ledger, photo files, and (to its trash) Paperless."""
    with lock:
        con.execute("delete from items where purchase_id in (select id from purchases where doc_id=?)", (doc["id"],))
        con.execute("delete from purchases where doc_id=?", (doc["id"],))
        if doc["paperless_id"]:
            con.execute("delete from items where purchase_id in (select id from purchases where paperless_id=?)", (doc["paperless_id"],))
            con.execute("delete from purchases where paperless_id=?", (doc["paperless_id"],))
            if PAPERLESS:
                pl.delete(f"{PAPERLESS}/documents/{doc['paperless_id']}/", timeout=60)
        if doc["photo"]:
            base = os.path.join(PHOTOS, doc["photo"])[:-4]
            for p in (base + ".jpg", base + ".small.jpg", base + ".pdf"):
                if os.path.exists(p):
                    os.remove(p)
        # keep the row so the same Immich photo is never taken in twice
        con.execute("update docs set status='deleted', photo=null, original=null, info=null, updated_at=? where id=?", (now(), doc["id"]))
        con.commit()
        # warnings that pointed at this document are checked again (most simply disappear)
        for other in con.execute("select id, info from docs where error like ? and info is not null", (f"%document {doc['id']}%",)).fetchall():
            con.execute("update docs set error=? where id=?", (same_as(con, other["id"], clean(json.loads(other["info"]))), other["id"]))
        con.commit()


def _key(v):
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


NUMBER = re.compile(r"\b(?:(?:POS|REG|TERM(?:INAL)?|LANE)\s*#?\s*(\d{1,4})\W{0,3})?(TRANS(?:ACTION)?|TXN|RECEIPT|INVOICE|INV|DOCKET)\s*(?:NO\.?|NUM(?:BER)?|#)?\s*:?\s*([A-Z]{0,3}-?\d[\dA-Z-]{2,})", re.I)


DATE = re.compile(r"(?<!\d)([0-3]?\d)[/.-]([01]?\d)[/.-]((?:19|20)\d\d|\d\d)(?!\d)")


def printed_date(text):
    """First date printed on the document, read in DATE_ORDER, as YYYY-MM-DD."""
    if DATE_ORDER == "YMD":
        return None   # year-first dates are already unambiguous; the model's answer stands
    for d, m, y in DATE.findall(text or ""):
        if DATE_ORDER == "MDY":
            d, m = m, d
        y = int(y) + (2000 if len(y) == 2 else 0)
        try:
            return datetime(y, int(m), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


MONTH = r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
PERIOD = re.compile(r"period[^\n\d]{0,20}(" + MONTH + r")\s*(?:-|–|to)\s*(" + MONTH + r")", re.I)


def any_date(v):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return printed_date(v)


def billing_period(text):
    """(start, end) of a printed billing period, as YYYY-MM-DD, or (None, None)."""
    m = PERIOD.search(text or "")
    return (any_date(m.group(1)), any_date(m.group(2))) if m else (None, None)


AMOUNT = re.compile(r"\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})(?!\d)")


def amounts_from_text(info, text):
    """When the items don't add up to the total, take each item's amount from its own line in the
    document text (the last money figure on the line that starts with the item's name). Used only
    if the result adds up to the total."""
    if not info["items"] or not info["total"] or not text:
        return
    if abs(sum(i["price"] for i in info["items"]) - info["total"]) <= 0.05:
        return
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    found = []
    for item in info["items"]:
        name = _key(item["name"])[:14]
        line = next((l for l in lines if name and _key(l).startswith(name)), None)
        nums = AMOUNT.findall(line or "")
        if not nums:
            return
        found.append(float(nums[-1].replace(",", "")))
    if abs(sum(found) - info["total"]) <= 0.05:
        for item, amount in zip(info["items"], found):
            item["price"] = amount
            item["qty"] = 1


def likely_date(value, taken):
    """With no text to read the date from, the model may swap day and month (10/11 read as 11 Oct).
    When the swapped reading is also a real date, keep whichever is not in the future and nearer the
    day the photo was taken: receipts are photographed soon after the purchase."""
    try:
        d = datetime.fromisoformat(value)
        ref = datetime.fromisoformat(taken) if taken else datetime.now()
    except (TypeError, ValueError):
        return value
    if d.day > 12 or d.day == d.month:
        return value
    try:
        swapped = d.replace(month=d.day, day=d.month)
    except ValueError:
        return value
    ok = [x for x in (d, swapped) if x <= ref]
    return min(ok, key=lambda x: ref - x).strftime("%Y-%m-%d") if ok else value


def printed_number(text):
    """Register + transaction (or receipt/invoice) number from the OCR text, when the model left it out."""
    m = NUMBER.search(text or "")
    if not m:
        return ""
    return (f"POS {m.group(1)} " if m.group(1) else "") + f"{m.group(2).upper()} {m.group(3)}"


def read_barcodes(path):
    """Text of the barcodes and QR codes on the photo (receipt and transaction barcodes), joined, or None."""
    try:
        img = ImageOps.grayscale(Image.open(path))
        found = sorted({b.text.strip() for b in zxingcpp.read_barcodes(img) if b.text.strip()})
        return " | ".join(found)[:500] or None
    except Exception as e:
        log("  barcode read failed:", e)
        return None


def same_as(con, doc_id, info):
    """A warning when another document looks like the same receipt or bill, else None.
    Two different photos of one receipt are different files, so Paperless can't tell; the printed
    transaction number can, and shop + date + total is the fallback."""
    if info["kind"] not in SPENDING:
        return None
    number, shop = _key(info["document_number"]), _key(info["issuer"])

    def same_shop(other):
        o = _key(other)
        if not shop or not o:
            return True
        return shop[:6] == o[:6] or (len(shop) >= 5 and shop in o) or (len(o) >= 5 and o in shop)

    me = con.execute("select barcode from docs where id=?", (doc_id,)).fetchone()
    if me and me["barcode"]:
        # a shop can print the same promo QR on every receipt: only trust a barcode never seen on another day
        same = [r for r in con.execute("select id, info from docs where id != ? and barcode = ? and info is not null and status not in ('deleted','duplicate')", (doc_id, me["barcode"]))]
        if same and all(json.loads(r["info"]).get("date") == info["date"] for r in same):
            return f"Same barcode as document {same[0]['id']}: the same receipt. Delete one of them."
    for other in con.execute("select id, info from docs where id != ? and info is not null and status not in ('deleted','duplicate')", (doc_id,)):
        o = json.loads(other["info"])
        if o.get("kind") not in SPENDING or not same_shop(o.get("issuer")):
            continue
        if number and len(number) >= 4 and _key(o.get("document_number")) == number:
            return f"Same transaction number as document {other['id']}: probably the same receipt. Delete one of them."
        if info["total"] and o.get("date") == info["date"] and o.get("total") == info["total"]:
            return f"Same shop, date and total as document {other['id']}: possibly the same receipt. Delete one if so."
        if info["kind"] == "bill" and o.get("kind") == "bill" and info["total"] and o.get("total") == info["total"]:
            try:
                days = abs((datetime.fromisoformat(info["date"]) - datetime.fromisoformat(o.get("date"))).days)
            except (TypeError, ValueError):
                days = 99
            if days <= 31 or (info["due_date"] and info["due_date"] == o.get("due_date")):
                return f"Same biller and amount as document {other['id']} within a month: probably the same bill (one may be a screenshot of it). Delete one if so."
    return None


# ---------- Wallos (bill reminders) ----------

WALLOS_CATEGORY = {   # Paperless expense category -> Wallos category name
    "Utilities & internet": "Utilities", "Rent & housing": "Utilities", "Insurance": "Insurance",
    "Transport & fuel": "Transport", "Health & pharmacy": "Health & Wellbeing", "Education": "Education",
    "Subscriptions": "Software", "Entertainment": "Entertainment", "Gifts & donations": "Charity & Donations",
    "Groceries": "Food & Beverages", "Dining & takeaway": "Food & Beverages", "Electronics & tech": "Technology",
}


def wallos_sync(info, paperless_id):
    """Create or update the Wallos entry for a confirmed bill, so Wallos reminds before it is due.
    Matched by name (the biller); returns a line for the app, or None when nothing was sent."""
    if not (WALLOS and WALLOS_KEY) or info["kind"] != "bill" or not info["issuer"] or not info["total"]:
        return None
    if not info["due_date"]:
        return "Not sent to Wallos: add the due date to get a reminder."
    api = f"{WALLOS}/api/subscriptions"
    subs = requests.get(f"{api}/get_subscriptions.php", params={"api_key": WALLOS_KEY}, timeout=30).json().get("subscriptions", [])
    existing = next((s for s in subs if _key(s["name"]) == _key(info["issuer"])), None)
    cats = requests.get(f"{WALLOS}/api/categories/get_categories.php", params={"api_key": WALLOS_KEY}, timeout=30).json().get("categories", [])
    want = WALLOS_CATEGORY.get(info["category"], "No category")
    cat_id = next((c["id"] for c in cats if c["name"] == want), None)
    form = {"api_key": WALLOS_KEY, "price": f"{info['total']:.2f}", "next_payment": info["due_date"],
            "notify": 1, "notes": (f"Last bill: Paperless document {paperless_id} ({info['date']}), from the Receipts app." if paperless_id
                                   else f"Last bill: {info['date']}, entered by hand in the Receipts app.")}
    if existing:
        form.update(action="edit", id=existing["id"])
    else:
        form.update(action="add", name=info["issuer"], currency_id=WALLOS_CURRENCY, cycle=3, frequency=1, auto_renew=1,
                    start_date=info["date"] or info["due_date"], payer_user_id=1, notify_days_before=WALLOS_NOTIFY_DAYS)
        if cat_id:
            form["category_id"] = cat_id
    r = requests.post(f"{api}/set_subscriptions.php", data=form, timeout=30).json()
    if not r.get("success"):
        return "Wallos refused it: " + str(r.get("message"))[:150]
    verb = "updated" if existing else "added (monthly)"
    return f"Wallos {verb}: {info['issuer']} {CURRENCY} {info['total']:.2f} due {info['due_date']}"


# ---------- the worker ----------

def set_status(con, doc_id, status, **more):
    cols = "".join(f", {k}=?" for k in more)
    con.execute(f"update docs set status=?, updated_at=?{cols} where id=?", (status, now(), *more.values(), doc_id))
    con.commit()


class Cancelled(Exception):
    pass


def check_cancel(con, doc_id):
    """The app asked to delete this document while it was being read: stop and remove it."""
    row = con.execute("select * from docs where id=?", (doc_id,)).fetchone()
    if row and row["status"] == "cancel":
        forget(con, row)
        log(f"  document {doc_id} deleted while being read")
        raise Cancelled()


def handle(con, doc):
    """Take one queued photo through Paperless and the model."""
    try:
        _handle(con, doc)
    except Cancelled:
        pass


def _handle(con, doc):
    log(f"reading document {doc['id']} ({doc['source']}, {doc['owner'] or 'no owner'})")
    paperless_id = doc["paperless_id"]
    if not paperless_id and PAPERLESS:
        check_cancel(con, doc["id"])
        set_status(con, doc["id"], "uploading")
        original = doc["original"]   # a PDF goes to Paperless as the PDF, so its text layer is used
        with open(os.path.join(PHOTOS, original or doc["photo"]), "rb") as fh:
            full = fh.read()
        name, mime = (f"phone-{doc['id']}.pdf", "application/pdf") if original else (f"phone-{doc['id']}.jpg", "image/jpeg")
        form = [("title", f"Phone photo {doc['taken']}"), ("created", doc["taken"]), ("tags", str(ref()["tags"]["from-phone"]["id"]))]
        tag = next((p["tag"] for p in PEOPLE if p["name"] == doc["owner"] and p["tag"]), None)
        if tag:
            form.append(("tags", str(tag)))
        up = pl.post(f"{PAPERLESS}/documents/post_document/", data=form, timeout=300, files={"document": (name, full, mime)})
        up.raise_for_status()
        paperless_id, err = wait_task(up.text.strip().strip('"'))
        if err and "duplicate" in err.lower():
            log("  already in Paperless")
            return set_status(con, doc["id"], "duplicate", error="This photo is already in Paperless.")
        if err:
            raise RuntimeError(err)
        con.execute("update docs set paperless_id=? where id=?", (paperless_id, doc["id"])); con.commit()
        check_cancel(con, doc["id"])

    barcode = read_barcodes(photo_path(con, {**dict(doc), "paperless_id": paperless_id}, small=False))
    check_cancel(con, doc["id"])
    set_status(con, doc["id"], "reading", barcode=barcode)
    if PAPERLESS:
        content = pl.get(f"{PAPERLESS}/documents/{paperless_id}/", timeout=60).json().get("content", "")
    else:   # no OCR without Paperless: a PDF's own text, or the photo alone
        content = pdf_text(os.path.join(PHOTOS, doc["original"])) if doc["original"] else ""
    with open(photo_path(con, {**dict(doc), "paperless_id": paperless_id}), "rb") as fh:
        small = fh.read()
    try:
        info = clean(ask_model(small, content), doc["taken"])
        if info["kind"] in SPENDING:
            info["document_number"] = info["document_number"] or printed_number(content)
            if info["kind"] == "receipt":   # a receipt prints one date; a bill has several (period, issue, due)
                info["date"] = printed_date(content) or likely_date(info["date"], doc["taken"])   # the model sometimes reads 08/11 as 11 Aug
            else:
                amounts_from_text(info, content)
                start, end = billing_period(content)
                if end and (not info["date"] or info["date"] == start):   # the model tends to take the period's first day
                    info["date"] = end
    except Exception as e:
        log("  model could not read it:", e)
        info = clean({}, doc["taken"])
        return set_status(con, doc["id"], "review", info=json.dumps(info), error="The model could not read this one. Fill it in by hand, or tap Read again.")
    if not barcode and len(info["barcode_number"]) >= 8:
        con.execute("update docs set barcode=? where id=?", (info["barcode_number"], doc["id"])); con.commit()
    check_cancel(con, doc["id"])
    if PAPERLESS:
        apply(con, paperless_id, info, doc["owner"])
    else:
        save_manual(con, doc["id"], info, doc["owner"])
    set_status(con, doc["id"], "review", info=json.dumps(info), error=same_as(con, doc["id"], info))
    log(f"  read as {KINDS[info['kind']]['label']}" + (f" -> Paperless document {paperless_id}" if PAPERLESS else "") + (f", total {info['total']}, {len(info['items'])} items" if info["kind"] in SPENDING else ""))


def album_asset_ids(album_id):
    """Every asset id in the album, including ones the other person added."""
    r = im.get(f"{IMMICH}/timeline/buckets", params={"albumId": album_id}, timeout=60); r.raise_for_status()
    ids = []
    for b in r.json():
        t = im.get(f"{IMMICH}/timeline/bucket", params={"albumId": album_id, "timeBucket": b["timeBucket"]}, timeout=60); t.raise_for_status()
        j = t.json()
        ids += j["id"] if isinstance(j, dict) else [a["id"] for a in j]
    return ids


def pull_immich(con):
    """Take photos out of the Immich album into the queue."""
    me = im.get(f"{IMMICH}/users/me", timeout=60).json()["id"]
    albums = im.get(f"{IMMICH}/albums", timeout=60).json() + im.get(f"{IMMICH}/albums?shared=true", timeout=60).json()
    by_immich = {p["immich"]: p["name"] for p in PEOPLE if p["immich"]}
    for album_id in {a["id"] for a in albums if a["albumName"].strip().lower() == ALBUM.lower()}:
        for asset_id in album_asset_ids(album_id):
            if not con.execute("select 1 from docs where source_ref=?", (asset_id,)).fetchone():
                asset = im.get(f"{IMMICH}/assets/{asset_id}", timeout=60).json()
                if asset.get("type") != "IMAGE":
                    continue
                r = im.get(f"{IMMICH}/assets/{asset_id}/original", timeout=300); r.raise_for_status()
                taken = (asset.get("localDateTime") or asset.get("fileCreatedAt") or "")[:10] or None
                add_doc(con, r.content, by_immich.get(asset.get("ownerId")), "immich", asset_id, taken)
                log("took one photo from the Immich album")
                if asset.get("ownerId") == me:   # keep paperwork out of the photo timeline (archived, not deleted)
                    im.put(f"{IMMICH}/assets", json={"ids": [asset_id], "visibility": "archive"}, timeout=60)
            im.delete(f"{IMMICH}/albums/{album_id}/assets", json={"ids": [asset_id]}, timeout=60)


def backup(con):
    """Daily copy of the ledger next to the photos (the live file is on local disk)."""
    os.makedirs(BACKUP, exist_ok=True)
    target = os.path.join(BACKUP, "ledger.db")
    if os.path.exists(target) and time.time() - os.path.getmtime(target) < 86400:
        return
    tmp = target + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = sqlite3.connect(tmp)
    con.backup(out); out.close()
    os.replace(tmp, target)
    log("ledger copied to", target)


def worker():
    con = db()
    migrate(con)
    # anything interrupted by a restart goes back in the queue
    con.execute("update docs set status='queued' where status in ('uploading','reading')"); con.commit()
    log(f'worker started; model {MODEL}' + (f', also watching Immich album "{ALBUM}"' if IMMICH else ""))
    last_pull = 0
    while True:
        try:
            if IMMICH and time.time() - last_pull > POLL:
                last_pull = time.time()
                pull_immich(con)
            backup(con)
        except Exception as e:
            log("background step failed:", e)
        wake.clear()
        for doc in con.execute("select * from docs where status='queued' order by id").fetchall():
            try:
                handle(con, doc)
            except Exception as e:
                if con.execute("select status from docs where id=?", (doc["id"],)).fetchone()["status"] == "cancel":
                    forget(con, con.execute("select * from docs where id=?", (doc["id"],)).fetchone())
                    continue
                tries = doc["tries"] + 1
                log(f"  FAILED (try {tries}/3): {e}"); traceback.print_exc()
                set_status(con, doc["id"], "queued" if tries < 3 else "failed", tries=tries, error=str(e)[:300])
                time.sleep(20)
        wake.wait(15)


if __name__ == "__main__":
    worker()
