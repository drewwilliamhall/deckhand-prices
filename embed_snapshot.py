#!/usr/bin/env python3
"""Wire an app HTML file to live prices and refresh its built-in fallback snapshot.

    python embed_snapshot.py prices.json ../app/tcg-deckhand.html ../site/app.html \\
        --prices-url https://raw.githubusercontent.com/OWNER/REPO/main/prices.json

Safe to run again and again. Each run:
  * replaces the price tables baked into the page with the ones in prices.json
    (these are only used when the live file can't be loaded);
  * the first time, changes the page so it fetches the live file on every load.
"""
import argparse, json, re, sys

MARK = "window.LIVE_PRICES"
TABLES = ["SHOP_PRICES", "DATA", "UPCOMING", "CT_PRICES"]

BOOT = r"""
<script id="live-boot">
// Load current prices, then start the app. If the live file can't be reached within
// a few seconds the app starts anyway with the prices saved in this page.
(function(){
  var URL = %(url)s;
  var started = false;
  function start(){
    if (started) return; started = true;
    var s = document.createElement('script');
    s.textContent = document.getElementById('app-src').textContent;
    document.body.appendChild(s);
  }
  if (!URL || !window.fetch){ start(); return; }
  var ctl = window.AbortController ? new AbortController() : null;
  var timer = setTimeout(function(){ if (ctl) ctl.abort(); start(); }, 6000);
  fetch(URL, {cache:'no-cache', signal: ctl ? ctl.signal : undefined})
    .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(function(j){
      if (!started && j && j.DATA && j.DATA.cards && j.DATA.cards.length > 500 && j.CT_PRICES && j.SHOP_PRICES && j.UPCOMING)
        window.LIVE_PRICES = j;
    })
    .catch(function(){})
    .then(function(){ clearTimeout(timer); start(); });
})();
</script>
"""

STAMP_JS = r"""
{ // when the prices on screen were last refreshed
  const L = window.LIVE_PRICES, el = document.getElementById('priceStamp');
  if (el){
    if (L && L.generated){
      const mins = Math.max(0, Math.round((Date.now() - new Date(L.generated).getTime()) / 60000));
      const ago = mins < 2 ? 'just now' : mins < 90 ? mins + ' min ago' : mins < 2880 ? Math.round(mins / 60) + ' hours ago' : Math.round(mins / 1440) + ' days ago';
      el.textContent = 'Prices updated ' + ago + '.';
      el.title = new Date(L.generated).toLocaleString();
    } else el.textContent = 'Showing prices saved on ' + AS_OF + '.';
  }
  const fc = document.getElementById('feedCount');
  if (fc) fc.textContent = STORES.filter(s => SHOP_PRICES[s.id] && Object.keys(SHOP_PRICES[s.id]).length).length;
}
"""


def swap_table(html, name, value):
    start = html.index("const %s = " % name) + len("const %s = " % name)
    pos = start
    guard = "(%s && %s.%s) || " % (MARK, MARK, name)
    if html.startswith(guard, pos):
        pos += len(guard)
    _obj, end = json.JSONDecoder().raw_decode(html[pos:])
    return html[:start] + guard + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + html[pos + end:]


def once(html, old, new, what):
    if html.count(old) != 1:
        sys.exit("Could not find the %s in the page (found %d places) - nothing was changed." % (what, html.count(old)))
    return html.replace(old, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prices")
    ap.add_argument("html", nargs="+")
    ap.add_argument("--prices-url", default=None, help="where the page fetches live prices from")
    a = ap.parse_args()
    P = json.load(open(a.prices, encoding="utf-8"))
    for path in a.html:
        html = open(path, encoding="utf-8").read()
        for t in TABLES:
            html = swap_table(html, t, P[t])
        if 'id="app-src"' not in html:
            # 1. the app script waits for the loader
            i = html.rindex("<script>")
            html = html[:i] + '<script type="text/plain" id="app-src">' + html[i + len("<script>"):]
            j = html.rindex("</script>") + len("</script>")
            html = html[:j] + BOOT % {"url": json.dumps(a.prices_url or "")} + html[j:]
            # 2. CardTrader prices for alternate printings too
            html = once(html,
                "    if (card.variants && card.pick !== 0) return null; // snapshot covers the standard printing only\n    const p = CT_PRICES[card.id];",
                "    const p = (card.variants && card.pick !== 0) ? CT_PRICES['t' + card.variants[card.pick][4]] : CT_PRICES[card.id];",
                "CardTrader price lookup")
            # 3. banner: when prices were refreshed, and how many store feeds are live
            html = once(html,
                "<b>TCGplayer, CardTrader and 37 store feeds are real prices.</b> All prices in US dollars.</span>",
                '<b>TCGplayer, CardTrader and <span id="feedCount">37</span> store feeds are real prices.</b> All prices in US dollars. <span id="priceStamp"></span></span>',
                "banner text")
            html = once(html,
                "{ const el = document.getElementById('asOf'); if (el) el.textContent = AS_OF; }\n",
                "{ const el = document.getElementById('asOf'); if (el) el.textContent = AS_OF; }\n" + STAMP_JS,
                "date line")
        elif a.prices_url is not None:
            html = re.sub(r"(<script id=\"live-boot\">.*?var URL = )[^;]*;", lambda m: m.group(1) + json.dumps(a.prices_url) + ";", html, count=1, flags=re.S)
        open(path, "w", encoding="utf-8").write(html)
        print("updated", path)


if __name__ == "__main__":
    main()
