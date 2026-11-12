"""Create the Paperless document types, tags, storage paths and custom fields this app files into.

Only adds what is missing; never changes or deletes anything that already exists. Safe to run again.

    docker compose run --rm receipt-bridge python setup_paperless.py
"""
import os
import requests
import bridge

PAPERLESS = bridge.PAPERLESS
pl = bridge.pl

TAGS = ["from-phone", "inbox", "to-pay", "vital", "warranty"]
STORAGE_PATHS = {   # name -> path template (Paperless placeholders)
    "Receipts": "Receipts/{{ created_year }}/{{ created_month }}/{{ title }}",
    "Bills": "Bills/{{ created_year }}/{{ correspondent }}/{{ title }}",
    "Vital": "Vital/{{ document_type }}/{{ title }}",
    "Financial": "Financial/{{ created_year }}/{{ document_type }}/{{ title }}",
}
CATEGORIES = bridge.CATEGORIES   # set CATEGORIES / PAYMENT_METHODS in .env before running this to start with your own
METHODS = bridge.METHODS
FIELDS = {   # name -> (Paperless data type, extra data)
    "Amount": ("monetary", {"default_currency": bridge.CURRENCY}),
    "GST": ("monetary", {"default_currency": bridge.CURRENCY}),
    "Expense category": ("select", {"select_options": [{"label": c} for c in CATEGORIES]}),
    "Payment method": ("select", {"select_options": [{"label": m} for m in METHODS]}),
    "Store location": ("string", {}),
    "Line items": ("longtext", {}),
    "Due date": ("date", {}),
    "Expiry date": ("date", {}),
    "Issue date": ("date", {}),
    "Document number": ("string", {}),
}


def existing(kind):
    return set(bridge.pl_all(kind))


def create(kind, body):
    r = pl.post(f"{PAPERLESS}/{kind}/", json=body, timeout=60)
    if not r.ok:
        raise SystemExit(f"Paperless refused {kind} {body.get('name')!r}: {r.status_code} {r.text[:200]}")
    print(f"  added {kind[:-1].replace('_', ' ')}: {body['name']}")


def main():
    if not PAPERLESS:
        raise SystemExit("PAPERLESS_URL is empty: the app runs without Paperless, nothing to set up.")
    pl.get(f"{PAPERLESS}/ui_settings/", timeout=30).raise_for_status()   # fails early on a bad URL or token
    added = 0
    have = existing("document_types")
    for name in sorted({k["type"] for k in bridge.KINDS.values()} - have):
        create("document_types", {"name": name, "matching_algorithm": 0}); added += 1
    have = existing("tags")
    for name in [t for t in TAGS if t not in have]:
        create("tags", {"name": name, "matching_algorithm": 0, "is_inbox_tag": name == "inbox"}); added += 1
    have = existing("storage_paths")
    for name, path in STORAGE_PATHS.items():
        if name not in have:
            create("storage_paths", {"name": name, "path": path, "matching_algorithm": 0}); added += 1
    have = existing("custom_fields")
    for name, (dtype, extra) in FIELDS.items():
        if name not in have:
            create("custom_fields", {"name": name, "data_type": dtype, "extra_data": extra}); added += 1
    print(f"Done: {added} added, everything else was already there." if added else "Nothing to add: Paperless is ready.")


if __name__ == "__main__":
    main()
