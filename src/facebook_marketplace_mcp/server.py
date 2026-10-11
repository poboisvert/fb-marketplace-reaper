import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from facebook_marketplace_mcp.client import FacebookClient, SearchParams
from facebook_marketplace_mcp.notify import notify_new_listings
from facebook_marketplace_mcp.monitors import (
    add_monitor,
    delete_monitor,
    get_monitor,
    load_monitors,
    params_from_dict,
    update_monitor_seen_ids,
)

mcp = MCPServer("facebook-marketplace", version="1.0.0")
client = FacebookClient(
    max_requests_per_minute=3,
    chrome_profile=os.environ.get("CHROME_PROFILE", "Default"),
)


def main() -> None:
    mcp.run(transport="stdio")


@mcp.tool(
    description="Search Facebook Marketplace listings by query, location, and filters",
    structured_output=False,
)
async def search_listings(
    query: Annotated[
        str,
        Field(
            description=(
                "Search names. Separate seller wordings with commas, "
                "for example 'crv 2023, cr-v 2023'. Results are combined."
            )
        ),
    ],
    latitude: Annotated[float, Field(description="Latitude of search center")],
    longitude: Annotated[float, Field(description="Longitude of search center")],
    radius_km: Annotated[float, Field(description="Search radius in kilometers (default: 50)")] = 50,
    min_price: Annotated[float | None, Field(description="Minimum price filter in dollars")] = None,
    max_price: Annotated[float | None, Field(description="Maximum price filter in dollars")] = None,
    category: Annotated[str | None, Field(description="Category ID to filter by")] = None,
    limit: Annotated[int, Field(description="Max results per name (default: 20), across at most 10 feed pages")] = 20,
    province: Annotated[
        str | None,
        Field(description="Canadian province or territory, for example quebec or QC"),
    ] = None,
) -> str:
    try:
        result = await client.search_listings(
            SearchParams(
                query=query,
                latitude=latitude,
                longitude=longitude,
                radius_km=radius_km,
                min_price=min_price,
                max_price=max_price,
                category=category,
                limit=limit,
                province=province,
            )
        )
    except Exception as exc:
        return f"Error searching listings: {exc}"

    if not result.listings:
        return f'No listings found for "{query}" within {radius_km}km.'

    summary = "\n\n".join(
        (
            f"{index}. **{listing.title}** — {listing.price}"
            f"{f' (was {listing.previous_price})' if listing.price_changed and listing.previous_price else ''}\n"
            f"   slug: {listing.slug}\n"
            f"   📍 {listing.location} | 👤 {listing.seller_name}"
            f"{' ⏳ PENDING' if listing.is_pending else ''}\n"
            f"   🔗 {listing.url}"
        )
        for index, listing in enumerate(result.listings, start=1)
    )
    more = "\n\n_More results available._" if result.has_next_page else ""
    return f'Found {len(result.listings)} listings for "{query}":\n\n{summary}{more}'


@mcp.tool(
    description="Get full details for a specific Facebook Marketplace listing",
    structured_output=False,
)
async def get_listing(
    listing_id: Annotated[str, Field(description="Facebook Marketplace listing ID")],
) -> str:
    try:
        listing = await client.get_listing_detail(listing_id)
    except Exception as exc:
        return f"Error fetching listing: {exc}"

    parts = [
        f"# {listing.title}",
        "",
        f"**Price:** {listing.price}",
        f"**Condition:** {listing.condition}" if listing.condition else None,
        f"**Location:** {listing.location}",
        "**Status:** ⏳ Pending" if listing.is_pending else None,
        "",
        f"## Description\n{listing.description}" if listing.description else None,
        "",
        f"**Seller:** {listing.seller_name}",
        f"**Profile:** {listing.seller_profile_url}" if listing.seller_profile_url else None,
        "",
        (
            f"**Images:** {len(listing.images)} photo(s)\n"
            + "\n".join(f"  {index}. {url}" for index, url in enumerate(listing.images, start=1))
        )
        if listing.images
        else None,
        "",
        f"🔗 {listing.url}",
    ]
    return "\n".join(part for part in parts if part is not None)


@mcp.tool(
    description="Look up a city/town name to get coordinates for use with search_listings",
    structured_output=False,
)
async def search_location(
    query: Annotated[
        str,
        Field(description="Location search query (e.g. 'Dedham MA', 'Boston', 'Brooklyn NY')"),
    ],
) -> str:
    try:
        results = await client.search_location(query)
    except Exception as exc:
        return f"Error searching locations: {exc}"

    if not results:
        return (
            f'No locations found for "{query}". '
            "Try a city or town name with state abbreviation."
        )

    lines = [
        f"{index}. **{hit.name}** — lat: {hit.latitude}, lng: {hit.longitude}"
        for index, hit in enumerate(results, start=1)
    ]
    return (
        f'Found {len(results)} location(s) for "{query}":\n\n'
        + "\n".join(lines)
        + "\n\nUse these coordinates with search_listings."
    )


@mcp.tool(
    description="Save a search query as a monitor to track new listings over time",
    structured_output=False,
)
async def monitor_search(
    name: Annotated[str, Field(description="Name for this saved search monitor")],
    query: Annotated[str, Field(description="Search query")],
    latitude: Annotated[float, Field(description="Latitude of search center")],
    longitude: Annotated[float, Field(description="Longitude of search center")],
    radius_km: Annotated[float, Field(description="Search radius in km")] = 50,
    min_price: Annotated[float | None, Field(description="Min price filter in dollars")] = None,
    max_price: Annotated[float | None, Field(description="Max price filter in dollars")] = None,
    category: Annotated[str | None, Field(description="Category ID")] = None,
    province: Annotated[
        str | None,
        Field(description="Canadian province or territory, for example quebec or QC"),
    ] = None,
) -> str:
    try:
        monitor = add_monitor(
            name,
            SearchParams(
                query=query,
                latitude=latitude,
                longitude=longitude,
                radius_km=radius_km,
                min_price=min_price,
                max_price=max_price,
                category=category,
                limit=250,
                province=province,
            ),
        )
    except Exception as exc:
        return f"Error: {exc}"

    return (
        f'Monitor "{monitor.name}" saved.\n'
        f"ID: {monitor.id}\n"
        f'Keywords: {", ".join(monitor.params.get("queries") or [query])}\n'
        f"Within {radius_km}km\n"
        "Use check_monitors to check for new listings."
    )


@mcp.tool(
    description="Check saved monitors for new listings since last check",
    structured_output=False,
)
async def check_monitors(
    monitor_name: Annotated[
        str | None,
        Field(description="Check a specific monitor by name, or omit to check all"),
    ] = None,
) -> str:
    try:
        if monitor_name:
            found = get_monitor(monitor_name)
            monitors = [found] if found else []
        else:
            monitors = load_monitors()

        if not monitors:
            if monitor_name:
                return f'Monitor "{monitor_name}" not found.'
            return "No monitors saved. Use monitor_search to create one."

        results: list[str] = []
        for monitor in monitors:
            search_result = await client.search_listings(params_from_dict(monitor.params))
            new_listings = [
                listing for listing in search_result.listings if listing.id not in monitor.seenIds
            ]
            if new_listings:
                update_monitor_seen_ids(monitor.name, [listing.id for listing in new_listings])
                listing_summary = "\n\n".join(
                    (
                        f"  {index}. **{listing.title}** — {listing.price}\n"
                        f"     📍 {listing.location}\n"
                        f"     🔗 {listing.url}"
                    )
                    for index, listing in enumerate(new_listings, start=1)
                )
                results.append(
                    f"### 🔔 {monitor.name} — {len(new_listings)} new listing(s)\n\n{listing_summary}"
                )
            else:
                update_monitor_seen_ids(monitor.name, [])
                results.append(f"### {monitor.name} — no new listings")
            notify_new_listings(monitor.name, new_listings)
        return "\n\n---\n\n".join(results)
    except Exception as exc:
        return f"Error checking monitors: {exc}"


@mcp.tool(
    name="delete_monitor",
    description="Delete a saved search monitor",
    structured_output=False,
)
async def delete_monitor_tool(
    name: Annotated[str, Field(description="Name of the monitor to delete")],
) -> str:
    deleted = delete_monitor(name)
    if deleted:
        return f'Monitor "{name}" deleted.'
    return f'Monitor "{name}" not found.'


@mcp.tool(description="List all saved search monitors", structured_output=False)
async def list_monitors() -> str:
    monitors = load_monitors()
    if not monitors:
        return "No monitors saved. Use monitor_search to create one."

    lines = []
    for monitor in monitors:
        params = monitor.params
        checked = f" | Last checked: {monitor.lastChecked}" if monitor.lastChecked else ""
        lines.append(
            f"- **{monitor.name}** — \"{params.get('query', '')}\" ({params.get('radiusKm', 50)}km)\n"
            f"  Created: {monitor.createdAt}{checked}\n"
            f"  Seen: {len(monitor.seenIds)} listings"
        )
    return "## Saved Monitors\n\n" + "\n\n".join(lines)
