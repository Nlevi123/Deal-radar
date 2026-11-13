#!/usr/bin/env python3
"""Deal Radar scanner: runs on GitHub Actions, no AI, no Claude credits.

Reads deal feeds (Slickdeals, DealNews, Reddit), keeps the ones that match what
you hunt for, saves them to deals.json (the app reads that file), checks
your tracked products for price drops, and sends phone alerts through ntfy.
"""
import hashlib, html, json, os, re, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
DEALS_PATH = os.path.join(ROOT, "deals.json")
STATE_PATH = os.path.join(ROOT, "tracked.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NOW = datetime.now(timezone.utc)
NS = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/",
      "content": "http://purl.org/rss/1.0/modules/content/", "dn": "https://www.dealnews.com/ns/rss/1.0.htm"}


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def money(s):
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def prices(text):
    """Pull price, was-price and percent off out of a deal title/description."""
    t = text.replace(",", "")
    t = re.sub(r"\$\s?\d+(?:\.\d{1,2})?\s*(?:\w+\s)?(?:credit|gift card|off|cash ?back|reward|promo|bonus)", " ", t, flags=re.I)
    t = re.sub(r"(?:on|over|orders?|w/|with|spend)\s*\$\s?\d+(?:\.\d\d)?\s*\+", " ", t, flags=re.I)
    t = re.sub(r"(?:free shipping|ship)\s*(?:w/|on)?\s*\$\s?\d+", " ", t, flags=re.I)
    nums = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d{1,2})?)", t)]
    pct = None
    m = re.search(r"(\d{2})\s?%\s?off", t, re.I)
    if m:
        pct = int(m.group(1))
    price = nums[0] if nums else None
    was = None
    m = re.search(r"(?:reg(?:ularly)?\.?|was|list(?: price)?(?: of)?|msrp|retail)\s*:?\s*\$\s?(\d+(?:\.\d{1,2})?)", t, re.I)
    if m:
        was = float(m.group(1))
    elif len(nums) >= 2 and nums[1] > nums[0] * 1.15:
        was = nums[1]
    if price and was and was > price and not pct:
        pct = round((1 - price / was) * 100)
    return price, was, pct


def store_of(title, text):
    m = re.search(r"\s@\s*([A-Z][\w&'.\- ]{1,30})", title) or re.search(r"\bat\s+([A-Z][\w&'.\-]+(?:\s[A-Z][\w&'.\-]+)?)", title)
    return m.group(1).strip() if m else ""


def first_img(htmltext):
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", htmltext or "")
    return html.unescape(m.group(1)) if m else ""


def parse_feed(url, xml):
    out = []
    root = ET.fromstring(xml)
    src = "Reddit" if "reddit.com" in url else "DealNews" if "dealnews" in url else "Slickdeals"
    if root.tag.endswith("feed"):  # Atom (Reddit)
        sub = re.search(r"/r/([^/]+)/", url)
        for e in root.findall("a:entry", NS):
            title = clean(e.findtext("a:title", "", NS))
            content = e.findtext("a:content", "", NS) or ""
            link = (e.find("a:link", NS).attrib.get("href") if e.find("a:link", NS) is not None else "")
            ext = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
            thumb = e.find("media:thumbnail", NS)
            out.append({"title": title, "url": html.unescape(ext.group(1)) if ext and "reddit.com" not in ext.group(1) else link,
                        "post": link, "text": clean(content), "img": thumb.attrib.get("url") if thumb is not None else first_img(content),
                        "date": e.findtext("a:published", "", NS) or e.findtext("a:updated", "", NS),
                        "source": f"r/{sub.group(1)}" if sub else src, "store": ""})
    else:  # RSS (Slickdeals, DealNews)
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            desc = it.findtext("description") or ""
            enc = it.findtext("content:encoded", "", NS) or ""
            link = (it.findtext("link") or "").split("?utm_")[0].replace("?iref=rss", "")
            mc = it.find("media:content", NS)
            img = first_img(enc) or first_img(desc) or (mc.attrib.get("url") if mc is not None else "")
            d = it.findtext("pubDate") or ""
            try:
                d = parsedate_to_datetime(d).astimezone(timezone.utc).isoformat()
            except Exception:
                d = NOW.isoformat()
            dn_price = it.findtext("dn:price", "", NS)
            out.append({"title": title, "url": link, "post": link, "text": clean(desc + " " + enc), "img": img,
                        "date": d, "source": src, "store": it.findtext("dn:retailer", "", NS) or "",
                        "dn_price": money(dn_price) if dn_price else None})
    return out


def has(text, words):
    t = " " + text.lower() + " "
    return next((w for w in words if w in t), None)


def classify(item):
    title, body = item["title"], item["text"]
    both = f"{title} {body}"
    if has(both, CFG["blocked_words"]):
        return None
    blob = f'{title} {item.get("store", "")} {item.get("url", "")}'.lower()
    if any(b in blob for b in CFG.get("blocked_stores", [])):
        return None
    price, was, pct = prices(title)
    if not price:
        price, was2, pct2 = prices(body)
        was, pct = was or was2, pct or pct2
    if item.get("dn_price") and not price:
        price = item["dn_price"]
    if price and was and was > price:
        pct = pct or round((1 - price / was) * 100)
    glitch = has(title, CFG["glitch_words"])
    prio = None
    for p in CFG["priority"]:
        if has(both, p["words"]):
            prio = p
            break
    pct = pct or 0
    if prio and re.search(r"\b(backpack|bag|sleeve|case|stand|charger|cable|screen protector|band|strap)s?\b", title.lower()):
        prio = None  # accessory for a laptop/tablet/watch, not the device itself
    curated = item.get("curated", False)  # Slickdeals frontpage/popular: already vetted as hot by the community
    keep = glitch or (prio and (pct >= 20 or curated)) or pct >= CFG["min_pct"]
    if not keep:
        return None
    if prio and prio["cat"] == "menswear" and re.search(r"\b(xs|xxs|l|xl|xxl)\b only", both.lower()):
        return None
    tier = "glitch" if glitch else "fire" if pct >= 60 else "good"
    cat = prio["cat"] if prio else guess_cat(both)
    store = item["store"] or store_of(title, body)
    return {"title": title[:160], "store": store, "url": item["url"], "post": item["post"], "img": item["img"],
            "price": price, "was": was, "pct": pct or None, "tier": tier, "cat": cat,
            "priority": bool(prio), "source": item["source"], "postedAt": item["date"]}


def guess_cat(t):
    t = t.lower()
    for cat, words in [("shoes", ["shoe", "sneaker", "boot", "nike", "adidas", "new balance", "hoka", "asics"]),
                       ("menswear", ["jacket", "shirt", "pants", "jeans", "hoodie", "sweater", "coat", "men's"]),
                       ("tech", ["tv", "monitor", "ssd", "gpu", "rtx", "headphone", "earbuds", "phone", "camera", "speaker", "router", "watch", "tablet", "laptop"]),
                       ("household", ["vacuum", "kitchen", "blender", "air fryer", "mattress", "towel", "cookware", "home"]),
                       ("accessories", ["wallet", "bag", "backpack", "sunglasses", "belt", "case", "charger"])]:
        if any(w in t for w in words):
            return cat
    return "everyday"


def key_of(d):
    base = re.sub(r"[^a-z0-9]+", " ", d["title"].lower())[:70]
    return hashlib.sha1(base.encode()).hexdigest()[:12]


def page_price(url):
    """Best-effort current price from a product page (JSON-LD or meta tags)."""
    h = get(url)
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                offers = x.get("offers")
                if offers:
                    for o in (offers if isinstance(offers, list) else [offers]):
                        p = o.get("price") or o.get("lowPrice") if isinstance(o, dict) else None
                        if p and money(str(p)):
                            return money(str(p))
                stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    for pat in [r'property="product:price:amount"\s+content="([\d.,]+)"', r'itemprop="price"\s+content="([\d.,]+)"',
                r'"currentPrice"\s*:\s*\{?[^}]*?"price"\s*:\s*([\d.]+)', r'"price"\s*:\s*"?([\d]+\.\d{2})']:
        m = re.search(pat, h)
        if m and money(m.group(1)):
            return money(m.group(1))
    return None


def push(msg, url=None, tags="moneybag"):
    topic = os.environ.get("NTFY_TOPIC") or CFG.get("ntfy_topic", "")
    if not topic or "CHANGE-ME" in topic:
        print("  (no ntfy topic set, skipping push)")
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
                                 headers={"Title": "Deal Radar", "Tags": tags, **({"Click": url} if url else {})})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("  push failed:", e)


def main():
    db = load(DEALS_PATH, {"deals": []})
    deals = {d["id"]: d for d in db.get("deals", [])}
    alerts = []
    found = 0

    for url in CFG["feeds"]:
        try:
            items = parse_feed(url, get(url))
        except Exception as e:
            print("feed failed:", url, e)
            continue
        for it in items:
            it["curated"] = "mode=frontpage" in url or "mode=popdeals" in url
            d = classify(it)
            if not d:
                continue
            d["id"] = key_of(d)
            if d["id"] in deals:
                continue
            d["foundAt"] = NOW.isoformat()
            deals[d["id"]] = d
            found += 1
            if d["tier"] == "glitch" or (d["pct"] or 0) >= CFG["alert_pct"] or (d["priority"] and d["tier"] == "fire"):
                alerts.append(d)
        time.sleep(3 if "reddit" in url else 1)

    # Price tracker
    state = load(STATE_PATH, {})
    last_track = state.get("_lastRun")
    due = not last_track or NOW - datetime.fromisoformat(last_track) > timedelta(minutes=55)
    if due:
        state["_lastRun"] = NOW.isoformat()
    for t in (CFG.get("tracked", []) if due else []):
        s = state.setdefault(t["url"], {"name": t["name"], "history": []})
        s["name"] = t["name"]
        try:
            p = page_price(t["url"])
        except Exception as e:
            p = None
            s["error"] = str(e)[:80]
        s["checkedAt"] = NOW.isoformat()
        if not p:
            s.setdefault("error", "price not readable")
            continue
        s.pop("error", None)
        prev = s.get("last")
        s["history"] = (s["history"] + [{"t": NOW.isoformat(), "p": p}])[-60:]
        s["last"], s["low"] = p, min(p, s.get("low", p))
        hit = (t.get("target") and p <= t["target"]) or (prev and p <= prev * 0.75)
        if hit and prev != p:
            d = {"id": key_of({"title": t["name"] + str(p)}), "title": f'{t["name"]} price drop', "store": "",
                 "url": t["url"], "post": t["url"], "img": "", "price": p, "was": prev,
                 "pct": round((1 - p / prev) * 100) if prev else None, "tier": "glitch" if prev and p <= prev * 0.4 else "fire",
                 "cat": "tech", "priority": True, "source": "Price tracker", "postedAt": NOW.isoformat(), "foundAt": NOW.isoformat()}
            deals[d["id"]] = d
            alerts.insert(0, d)
        time.sleep(1)
    json.dump(state, open(STATE_PATH, "w"), indent=1)

    # Drop deals older than keep_days
    cutoff = NOW - timedelta(days=CFG["keep_days"])
    keep = [d for d in deals.values() if datetime.fromisoformat(d["foundAt"]) >= cutoff]
    keep.sort(key=lambda d: d.get("postedAt") or d["foundAt"], reverse=True)
    json.dump({"updatedAt": NOW.isoformat(), "lastRunFound": found, "deals": keep}, open(DEALS_PATH, "w"), indent=1)

    for d in alerts[:CFG["max_alerts_per_run"]]:
        lead = "PRICE MISTAKE" if d["tier"] == "glitch" else f'-{d["pct"]}%' if d["pct"] else "DEAL"
        price = f' ${d["price"]:g}' if d["price"] else ""
        push(f'{lead}:{price} {d["title"][:120]}', d["url"], "rotating_light" if d["tier"] == "glitch" else "moneybag")
    print(f"saved {found} new deals, {len(keep)} total, {min(len(alerts), CFG['max_alerts_per_run'])} alerts")


if __name__ == "__main__":
    main()
