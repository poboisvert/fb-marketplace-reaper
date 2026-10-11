import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from facebook_marketplace_mcp.auth import (
    cookies_to_header,
    extract_chrome_cookies,
    get_cookie_value,
)
from facebook_marketplace_mcp.history import record_run, saved_photo_ids
from facebook_marketplace_mcp.provinces import in_province, resolve_province, same_place
from facebook_marketplace_mcp.parser import (
    MarketplaceListing,
    MarketplaceListingDetail,
    listing_page_image_url,
    parse_listing_detail_from_page,
    parse_search_html,
)
from facebook_marketplace_mcp.queries import (
    LOCATION_SEARCH_DOC_ID,
    build_location_search_variables,
)
from facebook_marketplace_mcp.rate_limit import RateLimiter

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
SEARCH_URL = "https://www.facebook.com/marketplace/search/"
MAX_SEARCH_PAGES = 10
SEARCH_FRIENDLY_NAME = "CometMarketplaceSearchContentContainerQuery"
_FEED_CURSOR = re.compile(
    r'"page_info":\{"end_cursor":"((?:\\.|[^"\\])*)","has_next_page":(true|false)'
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class FacebookSession:
    cookie_header: str
    fb_dtsg: str
    lsd: str
    jazoest: str
    client_revision: str
    user_id: str


@dataclass
class SearchParams:
    query: str
    latitude: float
    longitude: float
    radius_km: float
    min_price: float | None = None
    max_price: float | None = None
    category: str | None = None
    limit: int = 20
    province: str | None = None


def parse_search_names(query: str | list[str]) -> list[str]:
    """Split one or more seller wordings into separate Marketplace searches.

    ``crv 2023, cr-v 2023`` and ``["crv 2023", "cr-v 2023"]`` both become two names.
    """
    raw = query if isinstance(query, list) else [query]
    names: list[str] = []
    seen: set[str] = set()
    for part in raw:
        for name in re.split(r"[,;\n]+", part or ""):
            cleaned = name.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                names.append(cleaned)
    return names


@dataclass
class SearchResult:
    listings: list[MarketplaceListing]
    has_next_page: bool


@dataclass
class LocationHit:
    name: str
    latitude: float
    longitude: float
    page_id: str = ""


class FacebookClient:
    def __init__(
        self,
        max_requests_per_minute: int = 3,
        chrome_profile: str | None = None,
    ) -> None:
        self.rate_limiter = RateLimiter(max_requests_per_minute)
        self.chrome_profile = chrome_profile or os.environ.get("CHROME_PROFILE", "Default")
        self.session: FacebookSession | None = None
        self.req_counter = 0

    async def ensure_session(self) -> FacebookSession:
        if self.session is not None:
            return self.session
        return await self.init_session()

    async def init_session(self) -> FacebookSession:
        cookies = extract_chrome_cookies("facebook.com", self.chrome_profile)
        if not cookies:
            raise RuntimeError(
                "No Facebook cookies found in Chrome. "
                "Make sure you're logged into Facebook in Chrome."
            )

        user_id = get_cookie_value(cookies, "c_user")
        if not user_id:
            raise RuntimeError(
                "No c_user cookie found. Make sure you're logged into Facebook in Chrome."
            )

        cookie_header = cookies_to_header(cookies)
        tokens = await self._extract_tokens(cookie_header)
        self.session = FacebookSession(
            cookie_header=cookie_header,
            user_id=user_id,
            **tokens,
        )
        return self.session

    def clear_session(self) -> None:
        self.session = None
        self.req_counter = 0

    async def _extract_tokens(self, cookie_header: str) -> dict[str, str]:
        await self.rate_limiter.wait()
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            response = await http.get(
                MARKETPLACE_URL,
                headers={
                    **BROWSER_HEADERS,
                    "Cookie": cookie_header,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Failed to fetch marketplace page: {response.status_code} {response.reason_phrase}"
            )

        html = response.text
        dtsg_match = (
            _search(r'"DTSGInitData"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*"([^"]+)"', html)
            or _search(r'"DTSGInitialData"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*"([^"]+)"', html)
            or _search(r'"dtsg"\s*:\s*\{"token"\s*:\s*"([^"]+)"', html)
        )
        if not dtsg_match:
            raise RuntimeError(
                "Failed to extract fb_dtsg token. Session may be expired — "
                "try logging into Facebook in Chrome again."
            )

        jazoest = _search(r"jazoest=(\d+)", html) or ""
        lsd = (
            _search(r'"LSD"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*"([^"]+)"', html)
            or _search(r'name="lsd"\s+value="([^"]+)"', html)
            or ""
        )
        client_revision = (
            _search(r'"client_revision"\s*:\s*(\d+)', html)
            or _search(r"__spin_r:\s*(\d+)", html)
            or "1"
        )
        return {
            "fb_dtsg": dtsg_match,
            "lsd": lsd,
            "jazoest": jazoest,
            "client_revision": client_revision,
        }

    async def _graphql_request(
        self,
        doc_id: str,
        variables: dict,
        *,
        friendly_name: str | None = None,
        referer: str | None = None,
    ) -> dict:
        text = await self._graphql_text(
            doc_id,
            variables,
            friendly_name=friendly_name,
            referer=referer,
        )
        json_start = text.find("{")
        if json_start > 0:
            text = text[json_start:]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse GraphQL response: {text[:200]}") from exc

    async def _graphql_text(
        self,
        doc_id: str,
        variables: dict,
        *,
        friendly_name: str | None = None,
        referer: str | None = None,
    ) -> str:
        session = await self.ensure_session()
        await self.rate_limiter.wait()
        self.req_counter += 1

        body = {
            "fb_dtsg": session.fb_dtsg,
            "lsd": session.lsd,
            "jazoest": session.jazoest,
            "doc_id": doc_id,
            "variables": json.dumps(variables),
            "__a": "1",
            "__user": session.user_id,
            "__req": _to_base36(self.req_counter),
            "__rev": session.client_revision,
        }
        headers = {
            **BROWSER_HEADERS,
            "Cookie": session.cookie_header,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Origin": "https://www.facebook.com",
            "Referer": referer or "https://www.facebook.com/marketplace/",
            "X-FB-LSD": session.lsd,
        }
        if friendly_name:
            body["fb_api_caller_class"] = "RelayModern"
            body["fb_api_req_friendly_name"] = friendly_name
            body["server_timestamps"] = "true"
            headers["X-FB-Friendly-Name"] = friendly_name
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            response = await http.post(GRAPHQL_URL, headers=headers, data=body)

        if response.status_code in (401, 403):
            self.session = None
            raise RuntimeError("Session expired. Re-initializing on next request.")
        if response.status_code >= 400:
            raise RuntimeError(
                f"GraphQL request failed: {response.status_code} {response.reason_phrase}"
            )
        return response.text

    async def search_listings(self, params: SearchParams) -> SearchResult:
        """Load the Marketplace search page and read embedded listing cards.

        Mileage is not required. Several names, such as ``crv 2023`` and
        ``cr-v 2023``, are searched separately and combined by listing id.
        A Canadian province searches from that province's Marketplace place
        with a radius that covers the populated province, then keeps listings
        in that province. When the feed reports another page, up to 10 pages
        are loaded. An empty page does not stop the walk.
        """
        names = parse_search_names(params.query)
        if not names:
            raise RuntimeError("Enter at least one search name.")
        session = await self.ensure_session()

        province = None
        search_root = SEARCH_URL
        if params.province:
            province = resolve_province(params.province)
            place = await self._province_place(province.place_query, province.place_name)
            search_root = f"https://www.facebook.com/marketplace/{place.page_id}/search/"

        groups: list[list[MarketplaceListing]] = []
        has_next_page = False
        for name in names:
            await self.rate_limiter.wait()
            listings, more = await self._fetch_search_html(
                session.cookie_header,
                name,
                params,
                search_root,
                province.radius_km if province is not None else None,
            )
            if province is not None:
                listings = [listing for listing in listings if in_province(listing.location, province)]
            groups.append(listings)
            has_next_page = has_next_page or more

        listings = _merge_listings(groups)
        run_query = ", ".join(names)
        keep_photos = saved_photo_ids(listings, query=run_query)
        for listing in listings:
            if listing.id in keep_photos:
                continue
            photo = await self.listing_page_photo(session.cookie_header, listing.id)
            if photo:
                listing.image_url = photo
        record_run(listings, query=run_query, keep_photos=keep_photos)
        return SearchResult(listings=listings, has_next_page=has_next_page)

    async def _fetch_search_html(
        self,
        cookie_header: str,
        name: str,
        params: SearchParams,
        search_root: str,
        radius_km: int | None,
    ) -> tuple[list[MarketplaceListing], bool]:
        query = {"query": name}
        if params.min_price is not None:
            query["minPrice"] = _price_param(params.min_price)
        if params.max_price is not None:
            query["maxPrice"] = _price_param(params.max_price)
        if radius_km is not None:
            query["radius"] = str(radius_km)
        url = f"{search_root}?{urlencode(query)}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            response = await http.get(
                url,
                headers={
                    **BROWSER_HEADERS,
                    "Cookie": cookie_header,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f'Failed to fetch marketplace search for "{name}": '
                f"{response.status_code} {response.reason_phrase}"
            )
        return await self._collect_search_pages(
            response.text,
            params,
            radius_km,
            referer=url,
        )

    async def _collect_search_pages(
        self,
        html: str,
        params: SearchParams,
        radius_km: int | None,
        *,
        referer: str,
    ) -> tuple[list[MarketplaceListing], bool]:
        """Read the first search document, then up to 10 feed pages.

        Facebook often returns an empty second page and more cars on the page
        after that, so an empty page does not end the walk.
        """
        limit = max(params.limit, 0)
        html_listings, _ = parse_search_html(html, limit=limit, category=params.category)
        template = search_pagination_template(html)
        listings: list[MarketplaceListing] = []
        seen: set[str] = set()
        more = False

        def take(items: list[MarketplaceListing]) -> bool:
            nonlocal more
            for item in items:
                if not item.id or item.id in seen:
                    continue
                if len(listings) >= limit:
                    more = True
                    return False
                seen.add(item.id)
                listings.append(item)
            return True

        if template is not None and limit > 0:
            doc_id, base_variables = template
            cursor: str | None = None
            has_next = True
            pages = 0
            while pages < MAX_SEARCH_PAGES and has_next and len(listings) < limit:
                variables = prepare_search_variables(
                    base_variables,
                    cursor=cursor,
                    radius_km=radius_km,
                    params=params,
                )
                try:
                    text = await self._graphql_text(
                        doc_id,
                        variables,
                        friendly_name=SEARCH_FRIENDLY_NAME,
                        referer=referer,
                    )
                except RuntimeError:
                    break
                pages += 1
                page_listings, truncated = parse_search_html(
                    text,
                    limit=200,
                    category=params.category,
                )
                if truncated:
                    more = True
                if not take(page_listings):
                    break
                next_cursor, has_next = feed_cursor(text)
                if not next_cursor or next_cursor == cursor:
                    has_next = False
                    break
                cursor = next_cursor
            else:
                if has_next and len(listings) >= limit:
                    more = True
                elif has_next and pages >= MAX_SEARCH_PAGES:
                    more = True
        elif limit > 0:
            _, has_next = feed_cursor(html)
            more = has_next and len(html_listings) >= limit

        take(html_listings)
        return listings, more

    async def listing_page_photo(self, cookie_header: str, listing_id: str) -> str:
        """Photo shown first when the listing link is opened."""
        await self.rate_limiter.wait()
        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            response = await http.get(
                url,
                headers={
                    **BROWSER_HEADERS,
                    "Cookie": cookie_header,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                },
            )
        if response.status_code >= 400:
            return ""
        return listing_page_image_url(response.text)

    async def get_listing_detail(self, listing_id: str) -> MarketplaceListingDetail:
        session = await self.ensure_session()
        await self.rate_limiter.wait()
        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
            response = await http.get(
                url,
                headers={
                    **BROWSER_HEADERS,
                    "Cookie": session.cookie_header,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Failed to fetch listing {listing_id}: {response.status_code}")
        detail = parse_listing_detail_from_page(response.text, listing_id)
        record_run([detail], query="")
        return detail

    async def search_location(self, query: str) -> list[LocationHit]:
        data = await self._graphql_request(
            LOCATION_SEARCH_DOC_ID,
            build_location_search_variables(query),
        )
        try:
            edges = (
                data.get("data", {})
                .get("city_street_search", {})
                .get("street_results", {})
                .get("edges", [])
            )
        except AttributeError:
            return []

        hits: list[LocationHit] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            location = node.get("location") or {}
            hits.append(
                LocationHit(
                    name=node.get("single_line_address") or node.get("subtitle") or "Unknown",
                    latitude=float(location.get("latitude") or 0),
                    longitude=float(location.get("longitude") or 0),
                    page_id=str((node.get("page") or {}).get("id") or ""),
                )
            )
        return hits

    async def _province_place(self, place_query: str, place_name: str) -> LocationHit:
        hits = await self.search_location(place_query)
        for hit in hits:
            if hit.page_id and same_place(hit.name, place_name):
                return hit
        for hit in hits:
            if hit.page_id:
                return hit
        raise RuntimeError(f'Could not find a Marketplace location for "{place_query}".')


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _to_base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    chars: list[str] = []
    number = value
    while number:
        number, remainder = divmod(number, 36)
        chars.append(digits[remainder])
    return "".join(reversed(chars))


def _merge_listings(groups: list[list[MarketplaceListing]]) -> list[MarketplaceListing]:
    """Keep one record per listing id, in the order the names were searched."""
    merged: list[MarketplaceListing] = []
    by_id: dict[str, MarketplaceListing] = {}
    for group in groups:
        for listing in group:
            current = by_id.get(listing.id)
            if current is None:
                by_id[listing.id] = listing
                merged.append(listing)
                continue
            if not current.image_url and listing.image_url:
                current.image_url = listing.image_url
            if not current.title and listing.title:
                current.title = listing.title
    return merged


def feed_cursor(text: str) -> tuple[str | None, bool]:
    """Return the Marketplace feed cursor and whether another page exists."""
    match = _FEED_CURSOR.search(text)
    if not match:
        return None, False
    try:
        cursor = json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return None, match.group(2) == "true"
    if not isinstance(cursor, str) or not cursor:
        return None, match.group(2) == "true"
    cursor = cursor.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return cursor, match.group(2) == "true"


def search_pagination_template(html: str) -> tuple[str, dict] | None:
    """Read the search query id and variables embedded in the first page."""
    marker = "CometMarketplaceSearchContentContainerQueryRelayPreloader_"
    index = html.find(marker)
    if index < 0:
        return None
    window = html[index : index + 700]
    id_match = re.search(r'"queryID":"(\d+)","variables":', window)
    if not id_match:
        return None
    variables = _json_object_at(html, index + id_match.end())
    if not isinstance(variables, dict):
        return None
    return id_match.group(1), variables


def prepare_search_variables(
    template: dict,
    *,
    cursor: str | None,
    radius_km: int | None,
    params: SearchParams,
) -> dict:
    variables = json.loads(json.dumps(template))
    variables["cursor"] = cursor
    browse = variables.setdefault("params", {}).setdefault("browse_request_params", {})
    if radius_km is not None:
        browse["filter_radius_km"] = int(radius_km)
    if params.min_price is not None:
        browse["filter_price_lower_bound"] = int(params.min_price * 100)
    if params.max_price is not None:
        browse["filter_price_upper_bound"] = int(params.max_price * 100)
    return variables


def _json_object_at(text: str, start: int) -> dict | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _price_param(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)
