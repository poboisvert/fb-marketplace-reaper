import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from facebook_marketplace_mcp.client import SearchParams, parse_search_names

STORAGE_DIR = Path.home() / ".fb-marketplace"
MONITORS_FILE = STORAGE_DIR / "monitors.json"


@dataclass
class SavedMonitor:
    id: str
    name: str
    params: dict
    seenIds: list[str]
    createdAt: str
    lastChecked: str | None


def _ensure_storage_dir() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def load_monitors() -> list[SavedMonitor]:
    _ensure_storage_dir()
    if not MONITORS_FILE.exists():
        return []
    try:
        data = json.loads(MONITORS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [SavedMonitor(**item) for item in data]


def save_monitors(monitors: list[SavedMonitor]) -> None:
    _ensure_storage_dir()
    payload = [asdict(monitor) for monitor in monitors]
    MONITORS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def params_to_dict(params: SearchParams) -> dict:
    """Persist the TypeScript field names so existing monitor files still load."""
    names = parse_search_names(params.query)
    stored = {
        "query": ", ".join(names) if names else params.query,
        "queries": names,
        "latitude": params.latitude,
        "longitude": params.longitude,
        "radiusKm": params.radius_km,
        "limit": params.limit,
    }
    if params.min_price is not None:
        stored["minPrice"] = params.min_price
    if params.max_price is not None:
        stored["maxPrice"] = params.max_price
    if params.category is not None:
        stored["category"] = params.category
    if params.province is not None:
        stored["province"] = params.province
    return stored


def params_from_dict(data: dict) -> SearchParams:
    names = data.get("queries") or parse_search_names(data.get("query", ""))
    query = ", ".join(names) if names else data.get("query", "")
    return SearchParams(
        query=query,
        latitude=data["latitude"],
        longitude=data["longitude"],
        radius_km=data.get("radiusKm", data.get("radius_km", 50)),
        min_price=data.get("minPrice", data.get("min_price")),
        max_price=data.get("maxPrice", data.get("max_price")),
        category=data.get("category"),
        limit=data.get("limit", 250),
        province=data.get("province"),
    )


def add_monitor(name: str, params: SearchParams) -> SavedMonitor:
    monitors = load_monitors()
    stored = params_to_dict(params)
    existing = next((monitor for monitor in monitors if monitor.name == name), None)
    if existing is not None:
        existing.params = stored
        save_monitors(monitors)
        return existing

    monitor = SavedMonitor(
        id=str(uuid.uuid4()),
        name=name,
        params=stored,
        seenIds=[],
        createdAt=datetime.now(timezone.utc).isoformat(),
        lastChecked=None,
    )
    monitors.append(monitor)
    save_monitors(monitors)
    return monitor


def get_monitor(name: str) -> SavedMonitor | None:
    for monitor in load_monitors():
        if monitor.name == name:
            return monitor
    return None


def update_monitor_seen_ids(name: str, new_ids: list[str]) -> None:
    monitors = load_monitors()
    monitor = next((item for item in monitors if item.name == name), None)
    if monitor is None:
        return

    combined = list(dict.fromkeys([*monitor.seenIds, *new_ids]))
    monitor.seenIds = combined[-500:]
    monitor.lastChecked = datetime.now(timezone.utc).isoformat()
    save_monitors(monitors)


def delete_monitor(name: str) -> bool:
    monitors = load_monitors()
    remaining = [monitor for monitor in monitors if monitor.name != name]
    if len(remaining) == len(monitors):
        return False
    save_monitors(remaining)
    return True
