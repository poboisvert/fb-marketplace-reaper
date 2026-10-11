"""Persist each search run and compare prices for the same listing slug."""

import json
import re
import unicodedata
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from facebook_marketplace_mcp.parser import MarketplaceListing

PROJECT_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = Path.home() / ".fb-marketplace"
RUNS_DIR = PROJECT_DIR / "runs"
CATALOG_FILE = STORAGE_DIR / "catalog.json"

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "listing"


def listing_slug(title: str, listing_id: str) -> str:
    return f"{slugify(title)}-{listing_id}"


def price_amount(price: str) -> float | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", price or "")
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def query_dir_name(query: str) -> str:
    """Folder name for a search. ``cr-v 2023`` stays ``cr-v 2023``."""
    cleaned = re.sub(r"[\x00-\x1f/\\]", "-", query.strip())
    cleaned = cleaned.strip(" .")
    if cleaned in {"", ".", ".."}:
        return "search"
    return cleaned


def photo_url_valid(url: str, margin_seconds: int = 86400) -> bool:
    """Facebook CDN URLs carry ``oe=<hex unix time>`` and stop loading after it."""
    if not url.startswith("http"):
        return False
    match = re.search(r"[?&]oe=([0-9A-Fa-f]+)", url)
    if not match:
        return True
    return int(match.group(1), 16) > datetime.now(timezone.utc).timestamp() + margin_seconds


def saved_photo_urls(listings: list[MarketplaceListing], query: str = "") -> dict[str, str]:
    """Saved photo URL per listing id, when it has not expired, so the page photo need not be fetched again."""
    catalog = _load_catalog()
    found: dict[str, str] = {}
    for listing in listings:
        entry = catalog.get(listing.id) if isinstance(catalog.get(listing.id), dict) else None
        if entry is None:
            continue
        item_query = query.strip() or entry.get("query") or listing.title or "listing"
        slug = entry.get("slug") or listing_slug(listing.title, listing.id)
        image = str(_load_item(RUNS_DIR / query_dir_name(item_query) / f"{slug}.json").get("image") or "")
        if photo_url_valid(image):
            found[listing.id] = image
    return found


def record_run(listings: list[MarketplaceListing], query: str = "") -> Path:
    """Save listings under ``runs/<query>/<slug>.json``.

    The first time a listing is seen, ``created_at`` and ``original_price`` are
    set and ``updated_at`` matches ``created_at``. A later search that finds the
    listing again sets ``updated_at`` to that time. A lower price keeps the
    original price and stores the lower price. ``image`` is the Facebook photo
    URL; no image file is written.
    """
    migrate_legacy_date_runs()
    seen_at = datetime.now(timezone.utc).isoformat()
    catalog = _load_catalog()
    folders: set[Path] = set()

    for listing in listings:
        entry = catalog.get(listing.id) if isinstance(catalog.get(listing.id), dict) else None
        item_query = query.strip() or (entry or {}).get("query") or listing.title or "listing"
        folder = RUNS_DIR / query_dir_name(item_query)
        folder.mkdir(parents=True, exist_ok=True)
        folders.add(folder)

        slug = (entry or {}).get("slug") or listing_slug(listing.title, listing.id)
        amount = price_amount(listing.price)
        document = _load_item(folder / f"{slug}.json")
        created_at = document.get("created_at") or seen_at
        original_price = document.get("original_price") or listing.price
        original_amount = document.get("original_amount")
        if original_amount is None:
            original_amount = price_amount(str(original_price))
        updated_at = seen_at

        dropped = (
            original_amount is not None
            and amount is not None
            and amount < original_amount
        )
        listing.slug = slug
        listing.query = item_query
        listing.original_price = str(original_price)
        listing.previous_price = str(original_price) if dropped else ""
        listing.price_changed = dropped
        listing.price_dropped = dropped
        listing.created_at = created_at
        listing.updated_at = updated_at

        saved = {
            "slug": slug,
            "id": listing.id,
            "title": listing.title,
            "location": listing.location,
            "seller_name": listing.seller_name,
            "url": listing.url,
            "query": item_query,
            "created_at": created_at,
            "updated_at": updated_at,
            "original_price": original_price,
            "original_amount": original_amount,
            "price": listing.price,
            "amount": amount,
            "price_dropped": dropped,
            "image": listing.image_url or str(document.get("image") or ""),
        }
        if saved["image"].startswith("http"):
            (folder / f"{slug}.png").unlink(missing_ok=True)
        (folder / f"{slug}-page.png").unlink(missing_ok=True)
        _write_item(folder / slug, saved)
        catalog[listing.id] = {
            "slug": slug,
            "query": item_query,
            "price": listing.price,
            "amount": amount,
            "original_price": original_price,
            "original_amount": original_amount,
            "title": listing.title,
            "location": listing.location,
            "seller_name": listing.seller_name,
            "url": listing.url,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    for folder in folders:
        (folder / "listings.csv").unlink(missing_ok=True)
    _save_catalog(catalog)
    write_runs_index()
    if len(folders) == 1:
        return next(iter(folders))
    return RUNS_DIR


def write_runs_index() -> Path:
    """Write ``runs/index.json``, the file list ``runs/index.html`` loads.

    The page reads each item JSON itself, so a later price or ``updated_at``
    shows up without rewriting the HTML.
    """
    migrate_legacy_date_runs()
    runs: dict[str, list[str]] = {}
    if RUNS_DIR.exists():
        for day_dir in sorted(path for path in RUNS_DIR.iterdir() if path.is_dir()):
            files = sorted(path.name for path in day_dir.glob("*.json"))
            if files:
                runs[day_dir.name] = files
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = RUNS_DIR / "index.json"
    manifest.write_text(json.dumps(runs, indent=2) + "\n", encoding="utf-8")
    return manifest


def serve_runs(port: int = 8767) -> None:
    """Serve ``runs/`` so ``index.html`` can read the listing JSON files."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(SimpleHTTPRequestHandler, directory=str(RUNS_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Serving listings at http://127.0.0.1:{port}/index.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


def migrate_legacy_date_runs() -> None:
    """Move ``runs/<YYYY-MM-DD>/`` items into ``runs/<query>/``."""
    if not RUNS_DIR.exists():
        return
    for day_dir in list(RUNS_DIR.iterdir()):
        if not day_dir.is_dir() or not _DATE_DIR.match(day_dir.name):
            continue
        catalog = _load_catalog()
        for path in list(day_dir.glob("*.json")):
            saved = _migrate_dated_item(path)
            if saved and saved.get("id"):
                catalog[str(saved["id"])] = {
                    "slug": saved["slug"],
                    "query": saved["query"],
                    "price": saved["price"],
                    "amount": saved["amount"],
                    "original_price": saved["original_price"],
                    "original_amount": saved["original_amount"],
                    "title": saved["title"],
                    "location": saved["location"],
                    "seller_name": saved["seller_name"],
                    "url": saved["url"],
                    "created_at": saved["created_at"],
                    "updated_at": saved["updated_at"],
                }
        _save_catalog(catalog)
        for leftover in list(day_dir.iterdir()):
            leftover.unlink()
        day_dir.rmdir()


def _migrate_dated_item(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    observations = document.get("observations")
    first = observations[0] if isinstance(observations, list) and observations else document
    if not isinstance(first, dict):
        first = {}
    query = str(first.get("query") or document.get("query") or "search")
    created_at = str(first.get("seen_at") or document.get("created_at") or "")
    price = str(document.get("price") or first.get("price") or "")
    amount = document.get("amount")
    if amount is None:
        amount = first.get("amount")
    if amount is None:
        amount = price_amount(price)
    original_price = str(document.get("original_price") or first.get("price") or price)
    original_amount = document.get("original_amount")
    if original_amount is None:
        original_amount = first.get("amount")
    if original_amount is None:
        original_amount = price_amount(original_price)
    slug = str(document.get("slug") or path.stem)
    saved = {
        "slug": slug,
        "id": document.get("id") or first.get("id") or "",
        "title": document.get("title") or first.get("title") or "",
        "location": document.get("location") or first.get("location") or "",
        "seller_name": document.get("seller_name") or first.get("seller_name") or "",
        "url": document.get("url") or first.get("url") or "",
        "query": query,
        "created_at": created_at,
        "updated_at": str(document.get("updated_at") or created_at),
        "original_price": original_price,
        "original_amount": original_amount,
        "price": price,
        "amount": amount,
        "price_dropped": bool(
            original_amount is not None and amount is not None and amount < original_amount
        ),
    }
    folder = RUNS_DIR / query_dir_name(query)
    folder.mkdir(parents=True, exist_ok=True)
    _write_item(folder / slug, saved)
    (folder / "listings.csv").unlink(missing_ok=True)
    return saved


def _load_item(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _json_safe(value):
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _write_item(stem: Path, row: dict) -> None:
    stem.with_suffix(".json").write_text(json.dumps(_json_safe(row), indent=2) + "\n", encoding="utf-8")
    stem.with_suffix(".csv").unlink(missing_ok=True)


def _load_catalog() -> dict:
    if not CATALOG_FILE.exists():
        return {}
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_catalog(catalog: dict) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(_json_safe(catalog), indent=2), encoding="utf-8")

