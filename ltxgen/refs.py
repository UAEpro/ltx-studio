"""Reference images from Wikimedia Commons with license filtering + credits (for cast/location/prop panels).

  search QUERY [--n 8]                         list license-OK images (JSON)
  get "File:Title.jpg" --out DIR [--name N]    download (<=1600 px) and append a credit line to DIR/CREDITS.md
  best QUERY --out DIR [--name N] [--landscape]  search + download the first acceptable photo

Only Public domain, CC0, CC BY and CC BY-SA images are accepted. Prints one JSON object.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
UA = {"User-Agent": "LTXStudioRefs/1.0 (personal film project)"}
API = "https://commons.wikimedia.org/w/api.php?"
OK_LICENSE = re.compile(r"^(public domain|pd|cc0|cc by(-sa)? [0-9.]+)", re.I)


def _get(url: str) -> bytes:
    for i in range(6):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 5 * (i + 1)))
    raise RuntimeError("rate limited by Wikimedia")


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", html or "")).strip()


def search(query: str, n: int = 12) -> list[dict]:
    p = {"action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
         "gsrlimit": n, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1600, "format": "json"}
    r = json.loads(_get(API + urllib.parse.urlencode(p)))
    out = []
    for pg in sorted((r.get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0)):
        ii = pg["imageinfo"][0]
        m = ii.get("extmetadata", {})
        lic = m.get("LicenseShortName", {}).get("value", "")
        if not OK_LICENSE.match(lic) or ii.get("mime") not in ("image/jpeg", "image/png"):
            continue
        out.append({"title": pg["title"], "width": ii["width"], "height": ii["height"], "license": lic,
                    "author": _clean(m.get("Artist", {}).get("value", ""))[:80],
                    "desc": _clean(m.get("ImageDescription", {}).get("value", ""))[:160],
                    "thumb": ii.get("thumburl") or ii["url"], "source": ii["descriptionurl"]})
    return out


def download(item: dict, out_dir: Path, name: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = ".png" if item["thumb"].lower().endswith(".png") else ".jpg"
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name or item["title"].removeprefix("File:").rsplit(".", 1)[0])[:60]
    dest = out_dir / f"{base}{ext}"
    dest.write_bytes(_get(item["thumb"]))
    with open(out_dir / "CREDITS.md", "a", encoding="utf-8") as f:
        f.write(f"- {dest.name}: \"{item['title'].removeprefix('File:')}\" by {item['author'] or 'unknown'}, "
                f"{item['license']}, {item['source']}\n")
    return {**item, "path": str(dest)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--n", type=int, default=12)
    g = sub.add_parser("get"); g.add_argument("title"); g.add_argument("--out", required=True); g.add_argument("--name")
    b = sub.add_parser("best"); b.add_argument("query"); b.add_argument("--out", required=True)
    b.add_argument("--name"); b.add_argument("--landscape", action="store_true")
    a = p.parse_args()
    if a.cmd == "search":
        print(json.dumps({"ok": True, "results": search(a.query, a.n)}, ensure_ascii=False, indent=2))
    elif a.cmd == "get":
        pr = {"action": "query", "titles": a.title, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
              "iiurlwidth": 1600, "format": "json"}
        r = json.loads(_get(API + urllib.parse.urlencode(pr)))
        pg = next(iter(r["query"]["pages"].values()))
        ii = pg["imageinfo"][0]; m = ii.get("extmetadata", {})
        lic = m.get("LicenseShortName", {}).get("value", "")
        if not OK_LICENSE.match(lic):
            print(json.dumps({"ok": False, "error": f"license not allowed: {lic}"})); sys.exit(1)
        item = {"title": pg["title"], "license": lic, "author": _clean(m.get("Artist", {}).get("value", ""))[:80],
                "thumb": ii.get("thumburl") or ii["url"], "source": ii["descriptionurl"]}
        print(json.dumps({"ok": True, **download(item, Path(a.out), a.name)}, ensure_ascii=False, indent=2))
    else:
        res = [x for x in search(a.query, 20) if not a.landscape or x["width"] >= x["height"] * 1.2]
        if not res:
            print(json.dumps({"ok": False, "error": f"no license-OK image for {a.query!r}"})); sys.exit(1)
        print(json.dumps({"ok": True, **download(res[0], Path(a.out), a.name)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
