# Launch Aurva PBN Warehouse on Railway

This is the hosted, password-protected version of the Aurva PBN Warehouse. It has **no Shopify integration** and alerts when packed stock is **10 or fewer**. This app has a simple shared username/password rather than multi-user accounts.

## 1 — Create a private GitHub repository

1. Sign in to https://github.com/new and create `aurva-pbn-warehouse` as a **Private** repository.
2. Download and unzip `Aurva_PBN_Warehouse_Railway_Ready.zip`.
3. On GitHub, select **uploading an existing file** (or **Add file → Upload files**) and upload the extracted **files and folders** — not the ZIP itself. Put `Dockerfile`, `app.py`, `static/` and the other files at the **repository top level**. In particular, the `Dockerfile` must not be one folder deep.
4. Select **Commit changes**.

## 2 — Connect GitHub to Railway

1. Go to https://railway.com/dashboard and select **New Project**.
2. Choose **Deploy from GitHub repo**, authorize GitHub if prompted, and select your private `aurva-pbn-warehouse` repository.
3. Click **Deploy Now**. The service may initially show an error until the credentials and database volume are configured. This is expected: the app intentionally refuses to expose your warehouse without a password.

## 3 — Set login credentials

On Railway, click the warehouse service tile → **Variables** → add these values:

| Name | Value |
|---|---|
| `AURVA_USER` | Username of your choice, for example `aurva-admin` |
| `AURVA_PASSWORD` | **Your own unique strong password** of at least 12 characters |
| `AURVA_DB_PATH` | `/data/aurva.db` |

Never put your actual password inside the code, GitHub, a chat, or a screenshot.

## 4 — Make stock data persistent

In your Railway project canvas, use the right-click menu (or Command Palette) → **New Volume**. Attach the volume to your warehouse service and set its mount path to **`/data`**. This ensures your packed stock and component inventory remain in the SQLite database after the app is restarted or updated.

Once variables and volume are added, apply/deploy the pending changes (or redeploy the service) and check that it becomes **Active**.

## 5 — Generate the warehouse website link

Railway warehouse service → **Settings → Networking → Public Networking → Generate Domain**.

Open the generated HTTPS address in your browser. A login prompt asks for your `AURVA_USER` and `AURVA_PASSWORD`.

## 6 — Turn on backups

Service → **Backups** tab → enable scheduled volume backups, preferably daily, and verify that backups are being created. Railway storage, hosting and backup resources may be billable.

## If you see an error

- **Deployment failed** / error mentions credentials: check `AURVA_USER` and `AURVA_PASSWORD`, then redeploy.
- **Deployment failed** / database error: confirm the **volume is mounted at `/data`** and the variable `AURVA_DB_PATH` equals `/data/aurva.db`.
- **Application failed to respond**: confirm it shows Active before generating the domain. The app reads Railway's `PORT` automatically and binds to `0.0.0.0`.
- **401 or browser login prompt**: use your credentials from Railway Variables. Do not share them.
- **Can't see inventory after a new deployment**: check that the service is still using the same persistent volume. Do not enter production stock records until the volume is attached.

## Data model & important limits

The app stores data in one SQLite file on the Railway volume, uses a single shared password, and is suitable as a small-team first deployment. No Shopify, shipping, scraping or background supplier price checks. Enter actual product SKUs, starting component quantities and recipes in the UI. Packing a batch deducts the complete recipe and increases packed-kit stock; handing over packed kits decreases packed stock.

Docs: https://docs.railway.com/quick-start · https://docs.railway.com/volumes · https://docs.railway.com/networking/public-networking
