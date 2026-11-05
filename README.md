# Aurva World — Paint-by-Number Warehouse (Railway-ready)

This version of the Aurva PBN Warehouse is designed to be deployed from a private GitHub repository onto Railway, with a shared-password browser login and persistent SQLite database volume.

**Start here:** [RAILWAY_SETUP.md](RAILWAY_SETUP.md) for straightforward, step-by-step instructions.

For local Windows use, the original `start_windows.bat` also works and does not require a password when bound to localhost.

The app deliberately does not connect to Shopify or manage shipping. A packed-kit SKU is flagged for repacking at **10 or fewer** units. Packing a batch consumes its component recipe and increases packed stock; handing over kits decreases packed stock. You can track supplier names, prices and reorder links, and manually maintain price history. It does not yet automatically monitor supplier websites.

## Security and backups

When running on Railway or any non-local address, the server will not start without `AURVA_USER` and a unique `AURVA_PASSWORD` of at least 12 characters. Use the HTTPS Railway domain. Mount a persistent volume at `/data`, set `AURVA_DB_PATH=/data/aurva.db`, and enable backups. Never publish the app's database or credentials to GitHub.

This is a small-team prototype with one shared account, not an enterprise multi-user product. User-specific permissions, audit identity, automated backups outside Railway, and advanced hardening are not included.

## Inventory setup

1. Add actual PBN product SKUs and packed quantities.
2. Review the starter components (initially all zero stock), add artwork-specific canvases, paint colours, packaging stickers and wisdom flyers.
3. Receive or count stock; create and confirm a full per-SKU bill of materials.
4. Pack batches, hand over packed kits and monitor the dashboard alerts.
5. Add supplier product links and reorder thresholds for components.

**Known details included:** 50 cm cord and 2 hooks per kit; 6 hanging instructions per A4 printing sheet; A5/A4 share tube size, A3 separate; 4 packaging stickers per A3 sheet at ₹22; 4 wisdom flyers per A3 sheet at ₹42. Wood consumption and bulk paint usage still require real measurements, and are **not** automatically guessed. All these details should be verified for the specific SKU before its component recipe is confirmed.

**Test:** `python test_app.py` runs a temporary local smoke test.
