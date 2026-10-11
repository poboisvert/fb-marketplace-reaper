# Facebook Marketplace MCP Server — Context

## Architecture

Python stdio MCP server, plus a `facebook-marketplace` CLI that calls the same client without a model turn. It uses the logged-in Chrome Facebook session. A headless Chrome window is opened only to snapshot a listing page after a search.

- Location lookup posts to Facebook `/api/graphql/` with `LOCATION_SEARCH_DOC_ID`.
- Listing search loads `https://www.facebook.com/marketplace/search/?query=...` and reads embedded listing cards, then follows the feed cursor for at most 10 pages. Mileage is optional. Price bounds go on the URL as `minPrice` and `maxPrice`.
- Several names in one search, such as `crv 2023` and `cr-v 2023`, are requested separately and combined by listing id. Commas or repeated names both work.
- Listing detail loads `/marketplace/item/{id}/`.
- `latitude`, `longitude`, and `radius_km` remain on `search_listings`. The search page uses the Chrome account's Marketplace city, unless `--province` selects a Canadian province.

## Key Files

- `src/facebook_marketplace_mcp/cli.py` — `facebook-marketplace` terminal commands (no MCP import)
- `src/facebook_marketplace_mcp/__main__.py` — MCP stdio entry point (`python -m facebook_marketplace_mcp`)
- `src/facebook_marketplace_mcp/server.py` — `MCPServer` tool registration and stdio run
- `src/facebook_marketplace_mcp/client.py` — session tokens, location GraphQL, search and listing HTTP. Splits a search into several names and merges the rows.
- `src/facebook_marketplace_mcp/auth.py` — Chrome cookie DB copy, Keychain password, AES decrypt
- `src/facebook_marketplace_mcp/queries.py` — location `doc_id` and GraphQL variables
- `src/facebook_marketplace_mcp/parser.py` — search HTML and listing page parsing
- `src/facebook_marketplace_mcp/monitors.py` — `~/.fb-marketplace/monitors.json`. Each monitor stores a `queries` list, such as `crv 2023` and `cr-v 2023`, and the last 500 seen ids. Adding the same name again replaces that list.
- `src/facebook_marketplace_mcp/history.py` — slug, price catalog, and `runs/<query>/<slug>.json`. A combined search is one folder, such as `runs/crv 2023, cr-v 2023/`. Each item keeps `created_at`, `updated_at` from the latest search that found it, the original price, and a later lower price. `image` is the Facebook photo URL, not a saved file; an expired URL (`oe=`) is fetched again. `facebook-marketplace serve` hosts `runs/` so `index.html` can browse those folders.
- `src/facebook_marketplace_mcp/notify.py` — one Slack message when a monitor check finds new listings. Reads `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` from the shell or `server/.env`. Empty checks stay quiet.
- `src/facebook_marketplace_mcp/rate_limit.py` — 3 requests/minute with jitter

## Fragility Points

- `LOCATION_SEARCH_DOC_ID` changes when Facebook deploys. Update `queries.py`.
- Further search pages use the `CometMarketplaceSearchContentContainerQuery` id embedded in the first HTML page. That id changes when Facebook deploys.
- `fb_dtsg` rotates per session. A 401 or 403 clears the cache; the next call reads tokens again.
- Marketplace HTML changes break search cards and listing detail parsing.
- Rate limit default is 3 requests/minute.

## Dependencies

- `mcp` — MCP server (`MCPServer`, stdio)
- `httpx` — Marketplace HTTP
- `cryptography` — AES-128-CBC cookie decrypt
- stdlib `sqlite3` — Chrome cookie database

## Cookie Encryption (macOS Chrome)

- AES-128-CBC, PBKDF2 with SHA-1, salt `saltysalt`, 1003 iterations, 16-byte key
- Key from Keychain: `security find-generic-password -w -s "Chrome Safe Storage" -a "Chrome"`
- IV: 16 space characters. Encrypted values are prefixed with `v10`.
- After decrypting, skip Chrome's 32-byte header.
