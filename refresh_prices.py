#!/usr/bin/env python3
"""TCG Deckhand - live price refresh.

Pulls current prices from three places and writes one file, prices.json, that the
app loads every time the page opens:

  tcg    TCGplayer market prices, through TCG API (tcgapi.dev).  Needs TCGAPI_KEY.
  ct     CardTrader lowest English near-mint listings.           Needs CARDTRADER_TOKEN.
  shops  Public Shopify product feeds of the stores in stores.json. No key.

    python refresh_prices.py                    # everything
    python refresh_prices.py --sources ct,shops # just the fast-moving sources

Standard library only. Keys come from the environment, or from a .env file next
to this script (never commit that file). Each source is independent: if one fails,
the previous numbers for that source are kept and the others still update.
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.error, urllib.request
from collections import Counter, defaultdict
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "TCGDeckhandBot/1.0 (+https://tcgdeckhand.net; hello@tcgdeckhand.net)"
TCG_BASE = "https://api.tcgapi.dev/v1"
TCG_GAME = "one-piece-card-game"
CT_BASE = "https://api.cardtrader.com/api/v2"
CT_GAME_ID = 15  # One Piece

# Used only when no exchange-rate service answers and no earlier rates are saved.
FX_FALLBACK = {"USD": 1.0, "CAD": 1.41, "AUD": 1.42, "GBP": 0.755, "EUR": 0.86,
               "NZD": 1.72, "SGD": 1.30, "JPY": 153.0}


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(d=None):
    return (d or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a):
    print(*a, flush=True)


def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class HttpError(Exception):
    def __init__(self, code, url):
        super().__init__(f"HTTP {code} for {url}")
        self.code = code


def http_json(url, headers=None, timeout=45, retries=2):
    """GET a URL and parse JSON. Returns (data, response_headers)."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace")), resp.headers
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries:
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            raise HttpError(e.code, url.split("?")[0])
        except Exception as e:  # timeouts, DNS, bad JSON
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:120]}")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# TCGplayer market prices (TCG API)
# --------------------------------------------------------------------------
SKIP_SET = re.compile(r"Release Event|Pre-Release|Tournament Cards|Promotion Cards|Demo Deck|"
                      r"Revision Pack|Collection Sets", re.I)
CARD_NO = re.compile(r"^[A-Z]{1,4}\d{0,2}-\d{3}$")
SEALED_SKIP = re.compile(r"Sleeved|Double Pack|Special DON|Promotion Pack|Revision Pack|Display|"
                         r"Bonus Pack|Set of|Bundle|Deck Set", re.I)


def set_rank(name):
    """0 main boosters, 1 extra boosters, 2 premium boosters, 3 decks, None = not pulled."""
    if SKIP_SET.search(name):
        return None
    if name.startswith("ST-") or "Deck Set" in name:
        return 3
    if "Premium Booster" in name:
        return 2
    if "Extra Booster" in name:
        return 1
    return 0


def slim(r):
    return {"n": r.get("name") or "", "num": (r.get("number") or "").strip().upper(),
            "r": r.get("rarity") or "", "id": r.get("tcgplayer_id"),
            "s": 0 if r.get("product_type") == "Cards" else 1,
            "f": 1 if (r.get("printing") == "Foil" or r.get("foil_only")) else 0,
            "m": r.get("market_price"), "md": r.get("median_price"), "lo": r.get("low_price"),
            "l": r.get("total_listings") or 0}


def tcg_fetch(state, key, reserve=3, cache_dir=None):
    """Refresh as many sets as today's request allowance covers, stalest first."""
    sets, page = [], 1
    while True:
        if cache_dir:
            sets = read_json(os.path.join(cache_dir, "sets.json"), [])
            break
        d, _ = http_json(f"{TCG_BASE}/games/{TCG_GAME}/sets?per_page=100&page={page}")
        sets += d["data"]
        if not d.get("meta", {}).get("has_more"):
            break
        page += 1
    state["sets"] = {str(s["id"]): {"name": s["name"], "release": s.get("release_date")}
                     for s in sets if set_rank(s["name"]) is not None}
    rows = state.setdefault("rows", {})
    for sid in list(rows):
        if sid not in state["sets"]:
            del rows[sid]
    todo = sorted(state["sets"], key=lambda sid: ((rows.get(sid) or {}).get("fetched", "")[:10],
                                                  set_rank(state["sets"][sid]["name"]),
                                                  state["sets"][sid]["release"] or ""))
    remaining, done, stopped = None, 0, ""
    for sid in todo:
        got, page, complete = [], 1, False
        while True:
            if remaining is not None and remaining <= reserve:
                stopped = "daily request allowance used up"
                break
            try:
                if cache_dir:
                    d = read_json(os.path.join(cache_dir, f"{sid}_{page}.json"), None)
                    if d is None:
                        raise RuntimeError("not in cache")
                else:
                    d, h = http_json(f"{TCG_BASE}/sets/{sid}/cards?per_page=100&page={page}",
                                     {"X-API-Key": key})
                    try:
                        remaining = int(h.get("X-RateLimit-Remaining"))
                    except (TypeError, ValueError):
                        pass
                    time.sleep(0.25)
            except HttpError as e:
                stopped = ("daily request allowance used up" if e.code == 429
                           else "key rejected" if e.code in (401, 403) else str(e))
                break
            except Exception as e:
                stopped = str(e)
                break
            got += [slim(r) for r in d.get("data", [])]
            if not d.get("meta", {}).get("has_more"):
                complete = True
                break
            page += 1
        if complete:
            rows[sid] = {"fetched": iso(), "rows": got}
            done += 1
        if stopped:
            break
    return done, len(todo), stopped


def name_parts(name):
    """'Nami - OP01-016 (Luffy Deck)' -> ('Nami', ['Luffy Deck'])."""
    tags = [t.strip() for t in re.findall(r"\(([^()]*)\)", name)]
    tags = [t for t in tags if t and not re.fullmatch(r"\d{3}|[A-Z]{1,4}\d{0,2}-\d{3}", t)]
    base = re.sub(r"\([^()]*\)", "", name)
    base = re.sub(r"\s+-\s+[A-Z]{1,4}\d{0,2}-\d{3}\s*$", "", base.strip())
    return re.sub(r"\s+", " ", base).strip(), tags


VARIANT_TAG = re.compile(r"alternate art|parallel|manga|^sp$|^tr$|reprint|full art|wanted poster|"
                         r"box topper|gold|signature|pre-?release|winner|judge|finalist|pack|deck|"
                         r"promo|anniversary|event|textured|serial|jolly roger|foil|version|vol\.|"
                         r"stamped|treasure|tournament|championship|collection|binder|set", re.I)


def price_of(r):
    for k in ("m", "md", "lo"):
        if isinstance(r.get(k), (int, float)) and r[k] > 0:
            return round(float(r[k]), 2)
    return None


def set_code(name, rows):
    m = re.match(r"ST-(\d+)", name)
    if m:
        return f"ST{int(m.group(1)):02d}"
    rank = set_rank(name)
    if rank == 3:
        return None
    if rank == 2:
        v = re.search(r"Vol\.?\s*(\d+)", name)
        return f"PRB{int(v.group(1)) if v else 1:02d}"
    want = "EB" if rank == 1 else "OP"
    c = Counter(r["num"].split("-")[0] for r in rows
                if not r["s"] and CARD_NO.match(r["num"]) and r["num"].startswith(want))
    return c.most_common(1)[0][0] if c else None


def sealed_kind(name):
    if SEALED_SKIP.search(name):
        return None
    n = re.sub(r"\([^()]*\)", "", name).strip()
    if re.search(r"\bCase$", n):
        return "CASE"
    if n.startswith(("Starter Deck", "Ultra Deck")):
        return "DECK"
    if re.search(r"\bBox$", n):
        return "BOX"
    if re.search(r"\bPack$", n):
        return "PACK"
    return None


def tcg_build(state, today):
    """Turn the saved per-set rows into the shapes the app expects."""
    sets, rows = state.get("sets", {}), state.get("rows", {})
    codes, by_no, sealed_best, sealed_all = {}, defaultdict(list), {}, defaultdict(list)
    for sid, s in sets.items():
        rs = (rows.get(sid) or {}).get("rows") or []
        codes[sid] = set_code(s["name"], rs)
    # the first (oldest) set to claim a code keeps it
    seen = {}
    for sid in sorted(sets, key=lambda x: sets[x]["release"] or "9"):
        c = codes[sid]
        if c and c in seen:
            codes[sid] = None
        elif c:
            seen[c] = sid
    for sid, s in sets.items():
        short = re.sub(r"^ST-\d+:\s*", "", s["name"])
        for r in (rows.get(sid) or {}).get("rows") or []:
            p = price_of(r)
            if r["s"]:
                kind, code = sealed_kind(r["n"]), codes[sid]
                if not kind or not code or p is None:
                    continue
                label = r["n"] if code.startswith("ST") else s["name"]
                cand = [code, label, kind, r["n"], p, r["l"], r["id"], s["release"] or "",
                        r.get("m") is not None]
                sealed_all[(code, kind)].append(cand)
            elif CARD_NO.match(r["num"]) and p is not None:
                by_no[r["num"]].append((sid, short, r, p))

    # several versions of one product (print waves): prefer one with a market price,
    # then the cheapest; a case has to cost at least six boxes to be believable
    for kind in ("PACK", "DECK", "BOX", "CASE"):
        for (code, k), cands in sealed_all.items():
            if k != kind:
                continue
            if kind == "CASE" and (code, "BOX") in sealed_best:
                cands = [c for c in cands if c[4] >= 6 * sealed_best[(code, "BOX")][4]]
            if cands:
                sealed_best[(code, k)] = min(cands, key=lambda c: (not c[8], c[4]))

    release = {sid: (s["release"] or "9") for sid, s in sets.items()}
    cards = []
    for no in sorted(by_no):
        home_code = no.split("-")[0]
        base, other, names = [], [], Counter()
        parsed = [(sid, short, r, p) + name_parts(r["n"]) for sid, short, r, p in by_no[no]]
        # a bracketed word on every printing is part of the name, e.g. "Mr.2.Bon.Kurei (Bentham)"
        common = set(parsed[0][5])
        for x in parsed:
            common &= set(x[5])
        common = {t for t in common if not VARIANT_TAG.search(t)}
        has_home = any(codes[x[0]] == home_code and not (set(x[5]) - common) for x in parsed)
        first = None
        if not has_home:   # card has no set of its own in the list: earliest plain printing is the base
            plain = [x for x in parsed if not (set(x[5]) - common)]
            if plain:
                first = min(plain, key=lambda x: (release[x[0]], x[3]))[2]["id"]
        for sid, short, r, p, name, tags in parsed:
            if common:
                name = name + " (" + ") (".join(t for t in tags if t in common) + ")"
                tags = [t for t in tags if t not in common]
            names[name] += 1
            home = codes[sid] == home_code or r["id"] == first
            if home and not tags:
                base.append([("Foil" if r["f"] else "Standard"), r["r"], p, r["l"], r["id"], name])
            else:
                bits = list(tags)
                if not home:
                    if not bits and r["f"]:
                        bits.append("Foil")
                    bits.append(short)
                other.append([" · ".join(bits), r["r"], p, r["l"], r["id"], name])
        base.sort(key=lambda v: (v[0] != "Standard", v[2]))
        other.sort(key=lambda v: v[2])
        variants, used = [], set()
        for v in base + other:
            if v[0] in used:       # same label twice: keep the first (cheaper) one
                continue
            used.add(v[0])
            variants.append(v[:5])
        name = (base[0][5] if base else names.most_common(1)[0][0])
        cards.append([no, name, variants])

    released, upcoming = [], []
    for (code, kind), c in sealed_best.items():
        (upcoming if c[7] > today else released).append(c)

    def set_list(items):
        out = {}
        for c in items:
            out[c[0]] = [c[0], c[1], c[7]]
        return sorted(out.values(), key=lambda x: x[2], reverse=True)

    strip = lambda items: [c[:7] for c in sorted(items, key=lambda c: (c[7], c[0], c[2]), reverse=True)]
    fetched = [v.get("fetched", "") for v in rows.values()]
    data = {"asOf": (max(fetched)[:10] if fetched else today), "cards": cards,
            "sealed": strip(released), "sets": set_list(released)}
    up = {"sets": sorted(set_list(upcoming), key=lambda x: x[2]), "sealed": strip(upcoming)}
    return data, up


# --------------------------------------------------------------------------
# CardTrader lowest English near-mint listings
# --------------------------------------------------------------------------
def plausible(price, ref, sealed=False):
    """Guard against a listing matched to the wrong printing or product."""
    if ref is None or ref <= 0:
        return True
    if sealed:
        return 0.4 * ref <= price <= 3.0 * ref
    return 0.1 * ref <= price <= max(6.0 * ref, ref + 3.0)


CT_SEALED_KIND = {193: "BOX", 194: "PACK", 195: "DECK", 321: "CASE"}
CT_SEALED_SKIP = re.compile(r"asian|japanese|chinese|korean|sleeved|promotion|double pack|sticker|"
                            r"sleeves|storage|special set|precious|pre-errata|display|bundle", re.I)


def ct_fetch(token, data, upcoming, usd, only=None):
    hdr = {"Authorization": "Bearer " + token}
    exps, _ = http_json(CT_BASE + "/expansions", hdr)
    wanted = [e for e in exps if e.get("game_id") == CT_GAME_ID
              and re.fullmatch(r"(op|st|eb|prb)-?\d\d", e.get("code") or "", re.I)
              and (not only or re.search(only, e.get("code") or "", re.I))]
    base_by_tcg, var_by_tcg, ref, ids = {}, {}, {}, set()
    for no, _name, variants in data["cards"]:
        ids.add(no)
        ref[no] = variants[0][2]
        for i, v in enumerate(variants):
            if len(v) > 4 and v[4]:
                if i == 0:
                    base_by_tcg[v[4]] = no
                else:
                    var_by_tcg[v[4]] = f"t{v[4]}"
                    ref[f"t{v[4]}"] = v[2]
    sealed_by_tcg = {}
    for c in data["sealed"] + upcoming["sealed"]:
        sealed_by_tcg[c[6]] = f"{c[0]}-{c[2]}"
        ref[f"{c[0]}-{c[2]}"] = c[4]

    out, rejected, last_mp = {}, 0, 0.0
    for e in sorted(wanted, key=lambda x: x["code"]):
        home = re.sub(r"[^a-z0-9]", "", e["code"].lower()).upper()
        bps, _ = http_json(f"{CT_BASE}/blueprints/export?expansion_id={e['id']}", hdr, timeout=90)
        want = {}
        for b in bps:
            fp = b.get("fixed_properties") or {}
            cn = (fp.get("collector_number") or "").strip().upper()
            tid, ver = b.get("tcg_player_id"), (b.get("version") or "").strip()
            if cn:
                if tid in base_by_tcg:
                    key = base_by_tcg[tid]
                elif tid in var_by_tcg:
                    key = var_by_tcg[tid]
                elif cn in ids and not ver and cn.split("-")[0] == home:
                    key = cn
                else:
                    continue
            else:
                # sealed product: English only, and only the plain box / pack / case / deck
                label = (b.get("name") or "") + " " + ver
                kind = CT_SEALED_KIND.get(b.get("category_id"))
                if CT_SEALED_SKIP.search(label) or not kind:
                    continue
                if tid in sealed_by_tcg:
                    key = sealed_by_tcg[tid]
                elif f"{home}-{kind}" in ref:
                    key = f"{home}-{kind}"
                else:
                    continue
            want[str(b["id"])] = key
        if not want:
            continue
        wait = 1.15 - (time.time() - last_mp)     # this endpoint allows one call a second
        if wait > 0:
            time.sleep(wait)
        mp, _ = http_json(f"{CT_BASE}/marketplace/products?expansion_id={e['id']}", hdr, timeout=180)
        last_mp = time.time()
        for bid, prods in mp.items():
            key = want.get(str(bid))
            if not key:
                continue
            sealed = not (key in ids or key.startswith("t"))
            best = None
            for p in prods:
                ph = p.get("properties_hash") or {}
                if ph.get("onepiece_language") not in (None, "en"):
                    continue
                if not sealed and ph.get("condition") != "Near Mint":
                    continue
                if p.get("graded") or p.get("on_vacation") or ph.get("signed") or ph.get("altered"):
                    continue
                if (p.get("quantity") or 0) < 1 or (p.get("bundle_size") or 1) != 1:
                    continue
                price = usd((p.get("price_cents") or 0) / 100.0, p.get("price_currency") or "USD")
                if price is None or price <= 0:
                    continue
                if not plausible(price, ref.get(key), sealed):
                    rejected += 1
                    if os.environ.get("DECKHAND_DEBUG"):
                        log("   set aside", key, p.get("name_en"), round(price, 2), "vs TCGplayer", ref.get(key))
                    continue
                if best is None or price < best:
                    best = price
            if best is not None and (key not in out or best < out[key]):
                out[key] = round(best, 2)
    return out, len(wanted), rejected


# --------------------------------------------------------------------------
# Store feeds (Shopify)
# --------------------------------------------------------------------------
OTHER_GAME = re.compile(r"gundam|union arena|pok[eé]mon|magic|\bmtg\b|yu-?gi-?oh|digimon|lorcana|"
                        r"dragon ?ball|weiss|flesh and blood|star wars|riftbound|vanguard|"
                        r"battle spirits|grand archive|sorcery|altered|hololive|naruto", re.I)
ONE_PIECE = re.compile(r"one\s*piece|optcg", re.I)
CARD_ID = re.compile(r"(?<![A-Za-z0-9])((?:OP|EB|ST|PRB)\d{2}-\d{3})(?!\d)", re.I)
SET_TOKEN = re.compile(r"(?<![A-Za-z0-9])(OP|EB|ST|PRB)-?(\d{2})(?![-\d])", re.I)
# anything that marks a printing other than the standard one from the card's own set
NOT_STANDARD = re.compile(
    r"alternate art|alt\.? art|\baa\b|parallel|manga|\bsp\b|special card|\btr\b|treasure rare|"
    r"full art|serial|wanted poster|textured|\(gold\)|gold[- ]stamp|signature|signed|promo|"
    r"\bop-pr\b|judge|winner|finalist|top player|pre-?release|release event|tournament|"
    r"championship|treasure cup|regional|box topper|gift collection|premium card collection|"
    r"best selection|binder|anniversary|pack vol|pack 20\d\d|welcome pack|dash pack|deck party|"
    r"event pack|sealed battle|reprint|premium booster|\bprb|jolly roger|stamped|sample|errata|"
    r"japanese|\bjpn?\b|chinese|korean|french|german|graded|\bpsa\b|\bbgs\b|\bcgc\b|damaged|"
    r"played|learn together|set sail|demo deck|magazine|v-?jump|saikyo|playmat|sleeve|uncut|"
    r"oversized|don!!|misprint|error|\bart\b|pandaman|celebration|\bpack\b|\bfest\b|\bslab\b|"
    r"\(v\.?\s*\d\)", re.I)
BAD_VARIANT = re.compile(r"played|damaged|poor|heavily|moderately|\b(lp|mp|hp|dmg)\b|japanese|"
                         r"french|german|chinese|korean|spanish|italian|graded", re.I)
SEALED_BAD = re.compile(
    r"sleeved|double pack|bundle|set of|japanese|japon|\bjpn?\b|\bcn\b|\bkr\b|asia|chinese|korean|break|"
    r"opened|empty|playmat|sleeve|binder|deposit|bonus|promotion|tournament|ticket|entry|"
    r"wave 1|loose|single pack from|art set|\bdon!!|storage|deck box|card case|top ?loader", re.I)


def norm(s):
    s = (s or "").lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def vol_of(s):
    m = re.search(r"\bvol\w*\s*(\d+)", s)
    return m.group(1) if m else None


def shop_catalog(data, upcoming):
    """What we try to recognise in a store feed."""
    base = {no: (variants[0][0], variants[0][2]) for no, _n, variants in data["cards"]
            if variants[0][0] in ("Standard", "Foil")}
    sealed, names = [], {}
    for c in data["sealed"] + upcoming["sealed"]:
        code, label, kind, product, price = c[0], c[1], c[2], c[3], c[4]
        nm = norm(re.sub(r"^(Starter Deck|Ultra Deck)[^:]*:\s*", "", label))
        nm = re.sub(r"^extra booster (one piece )?", "", nm)
        sealed.append({"key": f"{code}-{kind}", "code": code, "kind": kind, "name": nm, "ref": price})
        if not code.startswith("ST") and len(nm) > 5:
            names[nm] = code
    return base, sealed, names


def shop_match(products, cat, usd_rate, op_only=False):
    """products: Shopify /products.json items. Returns {card or sealed id: USD price}."""
    base, sealed, set_names = cat
    singles, boxes = defaultdict(list), defaultdict(list)
    for p in products:
        title = p.get("title") or ""
        ptype = p.get("product_type") or ""
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        meta = " ".join([ptype, " ".join(tags)])
        if OTHER_GAME.search(ptype) or re.search(r"gundam|union arena", title, re.I):
            continue
        if not op_only and not ONE_PIECE.search(title + " " + meta + " " + (p.get("vendor") or "")):
            continue
        offers = []
        for v in p.get("variants") or []:
            vt = v.get("title") or ""
            if v.get("available") is not True or BAD_VARIANT.search(vt):
                continue
            try:
                price = float(v.get("price"))
            except (TypeError, ValueError):
                continue
            if price > 0:
                offers.append((price / usd_rate, bool(re.search(r"foil", vt, re.I))))
        if not offers:
            continue
        ids = {m.upper() for m in CARD_ID.findall(title)}
        if not ids:
            ids = {m.upper() for m in CARD_ID.findall(" ".join(str(v.get("sku") or "")
                                                               for v in p.get("variants") or []))}
        nt = norm(title)
        if len(ids) == 1:
            no = next(iter(ids))
            if no not in base or NOT_STANDARD.search(title):
                continue
            home = no.split("-")[0]
            toks = {(a + b).upper() for a, b in SET_TOKEN.findall(title)}
            if toks and home not in toks:
                continue
            if any(nm in nt and code != home for nm, code in set_names.items()):
                continue
            if not home.startswith("ST") and re.search(r"starter deck|ultra deck", nt):
                continue    # a deck reprint of a booster card
            label, ref = base[no]
            if label == "Standard" and any(not f for _p, f in offers):
                offers = [o for o in offers if not o[1]]
            price = min(o[0] for o in offers)
            if plausible(price, ref):
                singles[no].append(price)
        elif not ids:
            if SEALED_BAD.search(title) or re.search(r"single", ptype, re.I) or re.search(r"\(\d{3}\)", title):
                continue
            toks = {(a + b).upper() for a, b in SET_TOKEN.findall(title)}
            if len(toks) > 1:
                continue
            is_case = bool(re.search(r"\bcase\b|\bcarton\b", nt))
            is_deck = bool(re.search(r"starter deck|ultra deck", nt))
            is_display = bool(re.search(r"\bdisplay\b", nt))     # a full box of packs, or of decks
            if is_deck and (is_display or is_case):
                continue
            is_box = (bool(re.search(r"\bbox\b", nt)) or is_display) and not is_case and not is_deck
            rest = re.sub(r"(extra|premium) booster", " ", nt)   # "booster" inside a set's own name doesn't count
            is_pack = bool(re.search(r"\bpack\b|\bbooster\b", rest)) and not (is_box or is_case or is_deck)
            kind = "CASE" if is_case else "BOX" if is_box else "DECK" if is_deck else "PACK" if is_pack else ""
            hits = [s for s in sealed if s["kind"] == kind and s["code"] in toks]
            if not hits and not toks:
                named = [s for s in sealed if s["kind"] == kind and len(s["name"]) > 5
                         and s["name"] in nt and vol_of(s["name"]) == vol_of(nt)]
                if named:   # the most specific name wins ("red monkey d luffy" over "monkey d luffy")
                    longest = max(len(s["name"]) for s in named)
                    hits = [s for s in named if len(s["name"]) == longest]
            if len(hits) != 1:
                continue
            price = min(o[0] for o in offers)
            if plausible(price, hits[0]["ref"], sealed=True):
                boxes[hits[0]["key"]].append(price)
    out = {k: round(min(v), 2) for k, v in singles.items()}
    out.update({k: round(min(v), 2) for k, v in boxes.items()})
    return out


def guess_currency(url, regions=""):
    host = urlparse(url).netloc.lower()
    for tld, cur in ((".com.au", "AUD"), (".co.uk", "GBP"), (".ca", "CAD"), (".de", "EUR"),
                     (".fr", "EUR"), (".it", "EUR"), (".nl", "EUR"), (".es", "EUR"),
                     (".co.nz", "NZD"), (".sg", "SGD"), (".jp", "JPY")):
        if host.endswith(tld):
            return cur
    first = (regions or "us").split(",")[0].strip().lower()
    return {"us": "USD", "ca": "CAD", "uk": "GBP", "au": "AUD", "eu": "EUR"}.get(first, "USD")


def shops_fetch(stores, cat, rates, prev, max_pages=20, delay=1.0):
    """One polite pass over each store. Keeps a store's earlier prices if it can't be reached."""
    out, info = {}, {}
    old_info = (prev.get("sources", {}).get("shops", {}) or {}).get("stores", {})
    for i, st in enumerate(stores, 1):
        sid, base = st["id"], st["url"].rstrip("/")
        cur = st.get("currency") or "USD"
        products, err = [], ""
        try:
            try:   # Shopify stores publish their currency here
                meta, _ = http_json(base + "/meta.json", timeout=15, retries=0)
                cur = (meta.get("currency") or cur).upper()
            except Exception:
                pass
            for page in range(1, max_pages + 1):
                d, _ = http_json(f"{base}/products.json?limit=250&page={page}", timeout=30, retries=1)
                items = d.get("products") if isinstance(d, dict) else None
                if not items:
                    break
                products += items
                time.sleep(delay)
        except Exception as e:
            err = str(e)[:100]
        rate = rates.get(cur)
        if products and rate:
            out[sid] = shop_match(products, cat, rate, st.get("one_piece_only", False))
            info[sid] = {"updated": iso(), "n": len(out[sid]), "currency": cur}
            log(f"  [{i}/{len(stores)}] {sid}: {len(products)} products, {len(out[sid])} matched ({cur})")
        else:
            keep = prev.get("SHOP_PRICES", {}).get(sid)
            stamp = (old_info.get(sid) or {}).get("updated", "")
            fresh = stamp and (now() - dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                               .replace(tzinfo=dt.timezone.utc)).days < 14
            if keep and fresh:
                out[sid], info[sid] = keep, dict(old_info[sid], note="kept: " + (err or "no data"))
            log(f"  [{i}/{len(stores)}] {sid}: no update ({err or 'no products'})")
        time.sleep(delay)
    return out, info


def fx_rates(prev):
    """Units of each currency per 1 US dollar."""
    for url in ("https://api.frankfurter.dev/v1/latest?base=USD",
                "https://open.er-api.com/v6/latest/USD"):
        try:
            d, _ = http_json(url, timeout=20, retries=1)
            r = d.get("rates") or {}
            if r.get("CAD") and r.get("EUR"):
                r = {k: float(v) for k, v in r.items()}
                r["USD"] = 1.0
                return r, iso()
        except Exception:
            continue
    old = prev.get("sources", {}).get("fx", {})
    if old.get("rates"):
        return old["rates"], old.get("updated", "")
    return dict(FX_FALLBACK), "built-in estimate"


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Refresh TCG Deckhand prices.json")
    ap.add_argument("--sources", default="tcg,ct,shops")
    ap.add_argument("--out", default=os.path.join(HERE, "prices.json"))
    ap.add_argument("--state", default=os.path.join(HERE, "state", "tcg.json"))
    ap.add_argument("--stores", default=os.path.join(HERE, "stores.json"))
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--tcg-cache", help="developer option: read saved TCG API pages from a folder")
    ap.add_argument("--ct-codes", help="developer option: only CardTrader expansions matching this pattern (merged into the existing prices)")
    ap.add_argument("--shop-raw", help="developer option: match a saved feed file instead of fetching")
    a = ap.parse_args()
    load_env(os.path.join(HERE, ".env"))
    want = {s.strip() for s in a.sources.split(",") if s.strip()}
    prev = read_json(a.out, {})
    src = dict(prev.get("sources") or {})
    today = now().strftime("%Y-%m-%d")
    problems = []

    # --- TCGplayer ---
    state = read_json(a.state, {})
    if "tcg" in want:
        key = os.environ.get("TCGAPI_KEY", "")
        if not key and not a.tcg_cache:
            problems.append("tcg: TCGAPI_KEY is not set")
        else:
            try:
                done, total, stopped = tcg_fetch(state, key, cache_dir=a.tcg_cache)
                log(f"tcg: refreshed {done} of {total} sets" + (f" - stopped: {stopped}" if stopped else ""))
                if done:
                    write_json(a.state, state)
                    src["tcg"] = {"updated": iso(), "sets_refreshed": done, "sets": total}
                if stopped and not done:
                    problems.append("tcg: " + stopped)
            except Exception as e:
                problems.append(f"tcg: {e}")
    data, upcoming = prev.get("DATA"), prev.get("UPCOMING")
    if state.get("rows"):
        new_data, new_up = tcg_build(state, today)
        if not data or len(new_data["cards"]) >= 0.8 * len(data["cards"]):
            data, upcoming = new_data, new_up
        else:
            problems.append("tcg: new card list looked incomplete, kept the previous one")
    if not data:
        log("No card list yet - run with the tcg source first.")
        log("\n".join(problems))
        return 1
    src.setdefault("tcg", {})["cards"] = len(data["cards"])

    rates, fx_when = fx_rates(prev)
    src["fx"] = {"updated": fx_when, "rates": {k: rates[k] for k in FX_FALLBACK if k in rates}}
    usd = lambda amount, cur: (amount / rates[cur]) if rates.get(cur) else None

    # --- CardTrader ---
    ct = prev.get("CT_PRICES") or {}
    if "ct" in want:
        token = os.environ.get("CARDTRADER_TOKEN", "")
        if not token:
            problems.append("ct: CARDTRADER_TOKEN is not set")
        else:
            try:
                new_ct, n_exp, rej = ct_fetch(token, data, upcoming, usd, a.ct_codes)
                if a.ct_codes:
                    new_ct = dict(ct, **new_ct)
                log(f"ct: {len(new_ct)} prices from {n_exp} expansions ({rej} listings set aside as implausible)")
                if len(new_ct) >= 0.5 * len(ct):
                    ct = new_ct
                    src["ct"] = {"updated": iso(), "prices": len(ct)}
                else:
                    problems.append("ct: result looked incomplete, kept the previous prices")
            except Exception as e:
                problems.append(f"ct: {e}")

    # --- Stores ---
    shops = prev.get("SHOP_PRICES") or {}
    if "shops" in want:
        cat = shop_catalog(data, upcoming)
        stores = read_json(a.stores, [])
        try:
            if a.shop_raw:
                raw, by = read_json(a.shop_raw, []), defaultdict(dict)
                for r in raw:   # saved rows are one per variant: regroup into products
                    p = by[r["vendor"]].setdefault(r["handle"], {"title": r["title"],
                            "product_type": r["type"], "tags": [], "variants": []})
                    p["variants"].append({"title": r["variant"], "sku": r["sku"], "price": r["price"],
                                          "available": r["available"]})
                shops, info = {}, {}
                for st in stores:
                    if st["id"] in by and rates.get(st.get("currency") or "USD"):
                        shops[st["id"]] = shop_match(list(by[st["id"]].values()), cat,
                                                     rates[st["currency"]], st.get("one_piece_only", False))
                        info[st["id"]] = {"updated": iso(dt.datetime.fromtimestamp(os.path.getmtime(a.shop_raw), dt.timezone.utc)),
                                          "n": len(shops[st["id"]]), "note": "saved feed"}
            else:
                log(f"shops: {len(stores)} stores")
                shops, info = shops_fetch(stores, cat, rates, prev, a.max_pages)
            shops = {k: v for k, v in shops.items() if v}
            stamps = [v["updated"] for v in info.values() if v.get("updated")]
            src["shops"] = {"updated": (min(stamps) if a.shop_raw and stamps else iso()), "stores": info,
                            "prices": sum(len(v) for v in shops.values())}
            log(f"shops: {src['shops']['prices']} prices across {len(shops)} stores")
        except Exception as e:
            problems.append(f"shops: {e}")

    out = {"v": 1, "generated": iso(), "sources": src, "DATA": data, "UPCOMING": upcoming,
           "CT_PRICES": ct, "SHOP_PRICES": shops}
    write_json(a.out, out)
    log(f"wrote {a.out}: {len(data['cards'])} cards, {len(data['sealed'])} sealed, "
        f"{len(ct)} CardTrader prices, {sum(len(v) for v in shops.values())} store prices")
    for p in problems:
        log("PROBLEM " + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
