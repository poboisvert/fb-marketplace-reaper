# Marketplace runs

`index.html` is the browser for saved searches. It reads each `<slug>.json` through `index.json` and checks again every 15 seconds. From the server project:

```bash
facebook-marketplace serve
```

Then open `http://127.0.0.1:8767/index.html`. `--port` changes the port.

## Preview

The left side lists each search folder and how many listings it holds. Listings are cheapest first. A row shows the photo, price, title, place, seller, and the time it was last updated.

![Search list with listing rows](preview-list.png)

Click a row and the saved Marketplace page opens underneath that row. Click the same row again to close it. Only one row stays open.

![Open listing with the Marketplace page snapshot](preview-open.png)

The open row shows the same listing photo as the thumbnail, the listing URL, the current price, the original price, when the listing was first saved, when it was last found, and an **Open listing** link. A later lower price keeps the original price struck through and shows the new price in orange.

## What a search folder contains

Each search name is its own folder. Several names in one search share one folder, such as `crv 2023, cr-v 2023/`.

| File | Contents |
|------|----------|
| `<slug>.json` | Title, price, original price, place, seller, URL, `created_at`, `updated_at` |
| `<slug>.png` | Older items only. New items store the Facebook photo URL in `image` instead |
