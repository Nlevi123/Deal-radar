# Deal Radar (standalone)

Runs free on GitHub, with no Claude and no AI credits.

- **Scanner** (`scanner.py`): every 5 minutes it reads Slickdeals (frontpage, popular, price-mistake searches, your hunts), DealNews and Reddit deal subs. It keeps price mistakes, deals of 40% off or more, and your priority hunts (tablets, Galaxy Watch, laptops, desktops, On shoes, designer menswear). It skips final-sale, refurbished, open-box, eBay and Adidas items, and checks your tracked products' prices once an hour.
- **App** (`index.html`): a shopping-style page with the newest deals first. Tap ✕ to remove a deal (Undo is in the toast, and the **Removed** tab restores it). Removals are saved on your phone.
- **Alerts**: phone push notifications through the free ntfy app.

## Setup (about 10 minutes, all free)
1. Make a GitHub account at github.com.
2. Create a new **public** repository named `deal-radar`.
3. Upload every file in this folder, including the `.github/workflows/scan.yml` file.
4. Go to repo **Settings → Pages → Source: Deploy from a branch → main / (root)**. Your app will be at `https://<your-username>.github.io/deal-radar/`. Open it in Chrome and choose **⋮ → Add to Home screen**.
5. Go to repo **Actions** and enable workflows. To scan right away, open **Deal Radar scan** and tap **Run workflow**.
6. For alerts, install **ntfy** from the Play Store, tap **+**, and subscribe to the topic named in `config.json` (`ntfy_topic`).

## Changing what it hunts
Edit `config.json` on GitHub:
- `priority`: words to hunt for
- `blocked_stores`: stores to skip
- `min_pct` / `alert_pct`: discount thresholds
- `tracked`: product pages to price-watch (add a `target` price to get an alert when it drops to that)
