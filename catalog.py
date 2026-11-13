"""Store-catalog scanner: reads designer sale catalogs straight from the stores.

Every price here comes from the store itself (not from a deal site), and every
listed item is checked for: not final sale, your size in stock, and under your
price caps. Runs every ~30 minutes from scanner.py.
"""
import html, json, re, time, urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NOW = datetime.now(timezone.utc)

BIG = re.compile(r"\b(sneakers?|trainers?|shoes?|boots?|loafers?|derbys?|derbies|oxfords?|brogues?|mules?|sandals?|slides?|"
                 r"slip-ons?|espadrilles?|belts?|suits?|blazers?|tuxedos?|sport coats?)\b", re.I)
SHOE = re.compile(r"\b(sneakers?|trainers?|shoes?|boots?|loafers?|derbys?|derbies|oxfords?|brogues?|mules?|sandals?|slides?|slip-ons?|espadrilles?)\b", re.I)
NOT_CLOTHING = re.compile(r"\b(candle|fragrance|perfume|cologne|book|magazine|poster|incense|mug|skateboard|toy|figure|sticker|"
                          r"underwear|boxers?|briefs?|swim|trunks?|socks?|face mask|gift card|tray|cup|mug|bowl|plate|vase|lighter|ashtray|keychain|key ring|"
                          r"blanket|towel|pillow|bottle|tumbler|sticker|pin|patch|lanyard|playing cards|puzzle|lego|art print|zine)\b", re.I)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def kind(title):
    if SHOE.search(title):
        return "shoes"
    return "menswear"


def cap_ok(title, price, cfg):
    cap = cfg["caps"]["shoes_belts_suits"] if BIG.search(title) else cfg["caps"]["other"]
    return price <= cap


def designer_ok(brand, cfg):
    b = (brand or "").lower()
    return not any(x in b for x in cfg["not_designer"])


def size_ok(names, title, cfg):
    """names: list of in-stock size labels. Returns the matching labels."""
    want = cfg["shoe_sizes"] if SHOE.search(title) else cfg["clothing_sizes"]
    hits = []
    for n in names:
        lab = n.strip().upper()
        for w in want:
            w = w.upper()
            if lab == w or lab.startswith(w + " ") or lab.startswith(w + "/") or re.fullmatch(rf"(US|EU|IT|UK|FR|JP)?\s*{re.escape(w)}\s*(M|US)?", lab):
                hits.append(n.strip())
                break
    # one-size items (belts often "OS", bags, hats) count as available
    if not hits and all(re.search(r"one size|^os$|^o/s$|^tu$", n, re.I) for n in names) and names:
        hits = names[:1]
    return hits


# ---------------- SSENSE ----------------
def ssense_list(cfg, log):
    items = []
    for page in range(1, cfg["ssense_pages"] + 1):
        try:
            h = get(f"https://www.ssense.com/en-us/men/sale?sort=price-asc&page={page}")
        except Exception as e:
            log(f"ssense page {page} failed: {e}")
            break
        found = 0
        for m in re.finditer(r'<script type="application/ld\+json">(\{"@context":"https://schema.org","@type":"Product".*?\})</script>(.{0,3000}?)</a>', h, re.S):
            try:
                d = json.loads(m.group(1))
            except Exception:
                continue
            found += 1
            was = re.search(r'line-through">\$(\d[\d,]*)', m.group(2))
            price = float(d["offers"]["price"])
            items.append({"pid": "ss-" + str(d.get("productID")), "brand": d["brand"]["name"], "name": d["name"],
                          "price": price, "was": float(was.group(1).replace(",", "")) if was else None,
                          "url": d["offers"]["url"], "img": d.get("image", "").replace("__IMAGE_PARAMS__", "b_white,c_lpad,g_center,h_480,w_480/c_scale,h_480/f_auto,dpr_1.0"),
                          "store": "SSENSE", "returns": "Free returns, 30 days"})
        if not found or (items and items[-1]["price"] > cfg["caps"]["shoes_belts_suits"]):
            break
        time.sleep(1.5)
    log(f"ssense: {len(items)} sale items read")
    return items


def ssense_check(item):
    """Open the product page: live price, final-sale flag, in-stock sizes."""
    p = get(item["url"]).replace('\\"', '"')
    if re.search(r'"isFinalSale":true|"is_final_sale":true', p):
        return {"final": True}
    low = re.search(r'"lowestPrice":\{"source":"[^"]*","amount":(\d+(?:\.\d+)?)', p)
    reg = re.search(r'"regularPrice":\{"full_price":\{"amount":(\d+(?:\.\d+)?)', p)
    stock = dict(re.findall(r'"(\w+)":\{"inStock":(true|false)\}', p))
    sizes = [n for sku, n in re.findall(r'\{"id":"\d+","sku":"(\w+)","name":"([^"]+)"', p) if stock.get(sku) == "true"]
    return {"final": False, "price": float(low.group(1)) if low else None, "was": float(reg.group(1)) if reg else None, "sizes": sizes}


# ---------------- Shopify stores ----------------
def shopify_list(store, cfg, log):
    items = []
    for page in range(1, 5):
        try:
            d = json.loads(get(f"https://{store['domain']}/collections/{store.get('collection', 'sale')}/products.json?limit=250&page={page}"))
        except Exception as e:
            log(f"{store['domain']} failed: {e}")
            break
        ps = d.get("products", [])
        for p in ps:
            tags = " ".join(p.get("tags", [])).lower()
            body = (p.get("body_html") or "").lower()
            if "final sale" in tags or "final-sale" in tags or "finalsale" in tags or "final sale" in body:
                continue
            if re.search(r"\bwom[ae]n'?s?\b|\bwmns\b", tags + " " + p["title"].lower()) and "unisex" not in tags:
                continue
            vs = [v for v in p["variants"] if v.get("available") and v.get("compare_at_price") and float(v["compare_at_price"]) > float(v["price"])]
            if not vs:
                continue
            price = min(float(v["price"]) for v in vs)
            was = max(float(v["compare_at_price"]) for v in vs)
            sizes = [v.get("option1") or v["title"].split(" / ")[0] for v in vs]
            items.append({"pid": f"sh-{store['domain']}-{p['id']}", "brand": p.get("vendor", ""), "name": p["title"],
                          "price": price, "was": was, "sizes": sizes, "url": f"https://{store['domain']}/products/{p['handle']}",
                          "img": (p["images"][0]["src"] + "&width=480") if p.get("images") else "",
                          "store": store["name"], "returns": store["returns"]})
        if len(ps) < 250:
            break
        time.sleep(1)
    log(f"{store['name']}: {len(items)} returnable sale items read")
    return items


def dell_list(log):
    d = get("https://www.dell.com/en-us/shop/deals").replace("\\u0022", '"').replace("\\u0026", "&").replace("\\/", "/")
    out, seen = [], set()
    for m in re.finditer(r'"title":"([^"]{6,160})"(.{0,3000}?)"marketPrice":(null|"[^"]*").{0,40}"dellPrice":"([^"]*)".{0,200}?"pdUrl":"([^"]+)"', d, re.S):
        title, mid, mp, dp, url = m.groups()
        if mp == "null" or url in seen:
            continue
        seen.add(url)
        imgs = re.findall(r'"image":"(https://i\.dell\.com/[^"]+)"', mid)
        img = re.match(r"x", "") if not imgs else type("M", (), {"group": lambda self, i: imgs[-1]})()
        p, w = float(dp.replace("$", "").replace(",", "")), float(mp.strip('"').replace("$", "").replace(",", ""))
        out.append({"pid": "dell-" + url.rsplit("/", 1)[-1][:40], "brand": "Dell", "name": title.replace("New ", "").strip(),
                    "price": p, "was": w, "url": url, "img": img.group(1) if img else "", "store": "Dell", "returns": "30 days"})
    log(f"dell: {len(out)} discounted computers read")
    return out


def samsung_list(page):
    h = get(page).replace('\\"', '"')
    out = []
    for m in re.finditer(r'"offers":\{"priceCurrency":"USD","@type":"Offer","price":"([\d.]+)","availability":"([^"]+)"\},"image":"([^"]+)","@type":"Product","name":"([^"]+)".{0,400}?"url":"([^"]+)"', h, re.S):
        price, avail, img, name, url = m.groups()
        if "InStock" not in avail:
            continue
        name = re.sub(r"\s*\([^)]*$", "", name).replace("\\u0022", "").strip()
        out.append({"pid": "sam-" + url.rsplit("sku-", 1)[-1][:30], "brand": "Samsung", "name": name, "price": float(price), "was": None,
                    "url": url, "img": img.split("?")[0] + "?$650_519_PNG$", "store": "Samsung", "returns": "15 days"})
    return out


def run(cfg, state, log):
    """Returns the current list of verified catalog deals. state is a dict persisted between runs."""
    cache = state.setdefault("ssense", {})
    out = []

    def keep(it, sizes, cat=None, min_pct=None):
        if not it["was"] or it["was"] <= it["price"]:
            return
        pct = round((1 - it["price"] / it["was"]) * 100)
        if pct < (min_pct if min_pct is not None else cfg["min_pct"]):
            return
        name = it["name"]
        title = name if name.lower().startswith((it["brand"] or "").lower()) else f'{it["brand"]} {name}'
        out.append({"id": it["pid"], "title": title.strip()[:160], "brand": it["brand"],
                    "store": it["store"], "url": it["url"], "post": it["url"], "img": it["img"],
                    "price": it["price"], "was": it["was"], "pct": pct, "tier": "fire" if pct >= 60 else "good",
                    "cat": cat or kind(it["name"]), "priority": True, "source": it["store"],
                    "sizes": sizes[:6], "returns": it["returns"], "verified": NOW.isoformat()})

    def basic(it):
        return (designer_ok(it["brand"], cfg) and not NOT_CLOTHING.search(it["name"])
                and cap_ok(it["name"], it["price"], cfg) and it["was"] and it["was"] > it["price"])

    # SSENSE: list, then open each candidate's product page (cached a few hours)
    cands = [it for it in ssense_list(cfg, log) if basic(it)]
    checks = 0
    for it in cands:
        c = cache.get(it["pid"])
        fresh = c and (NOW - datetime.fromisoformat(c["at"])).total_seconds() < cfg["recheck_hours"] * 3600 and c.get("listed") == it["price"]
        if not fresh:
            if checks >= cfg["product_checks_per_run"]:
                continue
            try:
                r = ssense_check(it)
            except Exception as e:
                log(f"  check failed {it['url']}: {e}")
                continue
            checks += 1
            c = {"at": NOW.isoformat(), "listed": it["price"], **r}
            cache[it["pid"]] = c
            time.sleep(0.8)
        if c.get("final"):
            continue
        if c.get("price") and abs(c["price"] - it["price"]) > 0.01:
            it["price"] = c["price"]  # product page wins
            if not cap_ok(it["name"], it["price"], cfg):
                continue
        if c.get("was"):
            it["was"] = c["was"]
        hits = size_ok(c.get("sizes", []), it["name"], cfg)
        if hits:
            keep(it, hits)
    log(f"ssense: {len(cands)} candidates, {checks} product pages checked this run")

    for store in cfg["shopify_stores"]:
        for it in shopify_list(store, cfg, log):
            if store.get("cat"):  # tech / home / everyday store: no clothing size or price-cap rules
                if it["was"] and it["was"] > it["price"]:
                    keep(it, [], store["cat"])
            elif basic(it):
                hits = size_ok(it["sizes"], it["name"], cfg)
                if hits:
                    keep(it, hits)

    # Dell deals page (laptops and desktops)
    try:
        for it in dell_list(log):
            keep(it, [], "tech", cfg.get("min_pct_tech", 15))
    except Exception as e:
        log(f"dell failed: {e}")

    # Samsung tablets / watches / laptops: track every price, list real drops vs. our own history
    hist = state.setdefault("hist", {})
    for page in cfg.get("samsung_pages", []):
        try:
            for it in samsung_list(page):
                h = hist.setdefault(it["url"], [])
                if not h or (NOW - datetime.fromisoformat(h[-1][0])).total_seconds() > 3 * 3600 or h[-1][1] != it["price"]:
                    h.append([NOW.isoformat(), it["price"]])
                hist[it["url"]] = [x for x in h if (NOW - datetime.fromisoformat(x[0])).days <= 45][-200:]
                peak = max(p for t, p in hist[it["url"]] if (NOW - datetime.fromisoformat(t)).days <= 30)
                if peak > it["price"] * 1.0:
                    it["was"] = peak
                    keep(it, [], "tech", cfg.get("min_pct_tech", 15))
        except Exception as e:
            log(f"samsung {page} failed: {e}")
        time.sleep(1)

    # forget cache entries for items no longer on sale
    live = {d["id"] for d in out} | {it["pid"] for it in cands}
    for k in list(cache):
        if k not in live and (NOW - datetime.fromisoformat(cache[k]["at"])).days >= 2:
            del cache[k]
    return out
