"""Command-line access to Marketplace search without an MCP client."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

from facebook_marketplace_mcp.client import FacebookClient, SearchParams, parse_search_names
from facebook_marketplace_mcp.history import RUNS_DIR, query_dir_name, serve_runs
from facebook_marketplace_mcp.notify import notify_new_listings
from facebook_marketplace_mcp.monitors import (
    add_monitor,
    delete_monitor,
    get_monitor,
    load_monitors,
    params_from_dict,
    update_monitor_seen_ids,
)


class CommandError(Exception):
    pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="facebook-marketplace",
        description="Search Facebook Marketplace from the terminal using the Chrome session.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", parents=[_common()], help="Search listings")
    search.add_argument(
        "query",
        nargs="+",
        help='One or more names, for example "crv 2023" "cr-v 2023"',
    )
    search.add_argument("--min-price", type=float)
    search.add_argument("--max-price", type=float)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--category")
    search.add_argument("--latitude", type=float, default=0)
    search.add_argument("--longitude", type=float, default=0)
    search.add_argument("--radius-km", type=float, default=50)
    search.add_argument(
        "--province",
        help="Canadian province or territory, for example quebec or QC",
    )
    search.set_defaults(func=_search)

    location = sub.add_parser("location", parents=[_common()], help="Look up a city")
    location.add_argument("query")
    location.set_defaults(func=_location)

    listing = sub.add_parser("listing", parents=[_common()], help="Show one listing")
    listing.add_argument("listing_id")
    listing.set_defaults(func=_listing)

    monitor = sub.add_parser("monitor", help="Saved searches")
    monitor_sub = monitor.add_subparsers(dest="monitor_command", required=True)

    add = monitor_sub.add_parser("add", parents=[_common()], help="Save a search")
    add.add_argument("name")
    add.add_argument(
        "--query",
        action="append",
        required=True,
        help='Search name. Repeat the flag, or separate names with commas: "crv 2023, cr-v 2023"',
    )
    add.add_argument("--min-price", type=float)
    add.add_argument("--max-price", type=float)
    add.add_argument("--limit", type=int, default=250)
    add.add_argument("--category")
    add.add_argument("--latitude", type=float, default=0)
    add.add_argument("--longitude", type=float, default=0)
    add.add_argument("--radius-km", type=float, default=50)
    add.add_argument(
        "--province",
        help="Canadian province or territory, for example quebec or QC",
    )
    add.set_defaults(func=_monitor_add)

    check = monitor_sub.add_parser("check", parents=[_common()], help="Show new listings")
    check.add_argument("name", nargs="?")
    check.set_defaults(func=_monitor_check)

    monitor_sub.add_parser("list", parents=[_common()], help="List saved searches").set_defaults(
        func=_monitor_list
    )

    delete = monitor_sub.add_parser("delete", parents=[_common()], help="Delete a saved search")
    delete.add_argument("name")
    delete.set_defaults(func=_monitor_delete)

    serve = sub.add_parser("serve", help="Serve saved listings in a browser")
    serve.add_argument("--port", type=int, default=8767)

    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            serve_runs(args.port)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1) from exc
        return
    try:
        asyncio.run(args.func(args))
    except CommandError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


def _common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--chrome-profile",
        default=os.environ.get("CHROME_PROFILE", "Default"),
        help="Chrome profile directory (default: CHROME_PROFILE or Default)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON records")
    return parser


def _client(args: argparse.Namespace) -> FacebookClient:
    return FacebookClient(chrome_profile=args.chrome_profile)


def _params(args: argparse.Namespace, limit: int | None = None) -> SearchParams:
    names = parse_search_names(args.query)
    if not names:
        raise CommandError("Enter at least one search name.")
    return SearchParams(
        query=", ".join(names),
        latitude=args.latitude,
        longitude=args.longitude,
        radius_km=args.radius_km,
        min_price=args.min_price,
        max_price=args.max_price,
        category=args.category,
        limit=args.limit if limit is None else limit,
        province=getattr(args, "province", None),
    )


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if isinstance(payload, str):
        print(payload)


def _price_cell(listing) -> str:
    if listing.price_changed and listing.previous_price:
        return f"{listing.price} (was {listing.previous_price})"
    return listing.price


def _listing_row(listing) -> str:
    pending = " pending" if listing.is_pending else ""
    return (
        f"{listing.slug}\t{_price_cell(listing)}\t{listing.title}{pending}\t"
        f"{listing.location}\t{listing.seller_name}\t{listing.url}"
    )


async def _search(args: argparse.Namespace) -> None:
    params = _params(args)
    result = await _client(args).search_listings(params)
    _print_saved(result.listings, params.query)
    if args.json:
        _emit(
            {
                "count": len(result.listings),
                "has_next_page": result.has_next_page,
                "listings": [asdict(listing) for listing in result.listings],
            },
            True,
        )
        return
    if not result.listings:
        print(f'No listings found for "{params.query}".')
        return
    print("slug\tprice\ttitle\tlocation\tseller\turl")
    for listing in result.listings:
        print(_listing_row(listing))
    if result.has_next_page:
        print("More results available.", file=sys.stderr)


async def _location(args: argparse.Namespace) -> None:
    hits = await _client(args).search_location(args.query)
    if args.json:
        _emit([asdict(hit) for hit in hits], True)
        return
    if not hits:
        print(f'No locations found for "{args.query}".')
        return
    print("name\tlatitude\tlongitude")
    for hit in hits:
        print(f"{hit.name}\t{hit.latitude}\t{hit.longitude}")


async def _listing(args: argparse.Namespace) -> None:
    listing = await _client(args).get_listing_detail(args.listing_id)
    _print_saved([listing], listing.query)
    if args.json:
        _emit(asdict(listing), True)
        return
    print(listing.title)
    print(f"Price: {listing.price}")
    if listing.condition:
        print(f"Condition: {listing.condition}")
    if listing.location:
        print(f"Location: {listing.location}")
    if listing.seller_name:
        print(f"Seller: {listing.seller_name}")
    if listing.description:
        print()
        print(listing.description)
    print()
    print(listing.url)
    if listing.slug:
        print(f"Slug: {listing.slug}")
    if listing.price_changed and listing.previous_price:
        print(f"Price changed: {listing.previous_price} -> {listing.price}")


async def _monitor_add(args: argparse.Namespace) -> None:
    existed = get_monitor(args.name) is not None
    monitor = add_monitor(args.name, _params(args))
    if args.json:
        _emit(asdict(monitor), True)
        return
    verb = "updated" if existed else "saved"
    print(f'Monitor "{monitor.name}" {verb} ({monitor.id}).')
    for keyword in monitor.params.get("queries") or []:
        print(f"  {keyword}")


async def _monitor_check(args: argparse.Namespace) -> None:
    if args.name:
        found = get_monitor(args.name)
        if found is None:
            raise CommandError(f'Monitor "{args.name}" not found.')
        monitors = [found]
    else:
        monitors = load_monitors()
        if not monitors:
            raise CommandError("No monitors saved.")

    client = _client(args)
    reports = []
    for monitor in monitors:
        result = await client.search_listings(params_from_dict(monitor.params))
        new_listings = [listing for listing in result.listings if listing.id not in monitor.seenIds]
        update_monitor_seen_ids(monitor.name, [listing.id for listing in new_listings])
        notify_new_listings(monitor.name, new_listings)
        reports.append(
            {
                "name": monitor.name,
                "listings": [asdict(listing) for listing in new_listings],
            }
        )

    for monitor in monitors:
        _print_saved([], monitor.params.get("query", ""))
    if args.json:
        _emit(reports, True)
        return
    for report in reports:
        listings = report["listings"]
        print(f'{report["name"]}: {len(listings)} new')
        for listing in listings:
            pending = " pending" if listing["is_pending"] else ""
            price = listing["price"]
            if listing.get("price_changed") and listing.get("previous_price"):
                price = f'{price} (was {listing["previous_price"]})'
            print(
                f'{listing.get("slug", "")}\t{price}\t{listing["title"]}{pending}\t'
                f'{listing["location"]}\t{listing["seller_name"]}\t{listing["url"]}'
            )


async def _monitor_list(args: argparse.Namespace) -> None:
    monitors = load_monitors()
    if args.json:
        _emit([asdict(monitor) for monitor in monitors], True)
        return
    if not monitors:
        print("No monitors saved.")
        return
    for monitor in monitors:
        params = monitor.params
        prices = ""
        if params.get("minPrice") is not None or params.get("maxPrice") is not None:
            prices = f'{params.get("minPrice", "")}-{params.get("maxPrice", "")} '
        checked = f" checked {monitor.lastChecked}" if monitor.lastChecked else ""
        keywords = params.get("queries") or parse_search_names(params.get("query", ""))
        print(f"{monitor.name}\t{prices}seen {len(monitor.seenIds)}{checked}")
        for keyword in keywords:
            print(f"  {keyword}")


async def _monitor_delete(args: argparse.Namespace) -> None:
    if not delete_monitor(args.name):
        raise CommandError(f'Monitor "{args.name}" not found.')
    if args.json:
        _emit({"deleted": args.name}, True)
        return
    print(f'Monitor "{args.name}" deleted.')


def _print_saved(listings, query: str) -> None:
    drops = [listing for listing in listings if getattr(listing, "price_dropped", False)]
    folder = RUNS_DIR / query_dir_name(query)
    if drops:
        print(f"{len(drops)} lower price(s).", file=sys.stderr)
    if listings:
        print(f"Saved {len(listings)} item(s) to {folder}", file=sys.stderr)
    elif query:
        print(f"Saved this check to {folder}", file=sys.stderr)
