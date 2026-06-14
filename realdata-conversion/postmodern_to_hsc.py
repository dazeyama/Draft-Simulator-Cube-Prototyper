#!/usr/bin/env python3
"""Convert postmodern draft pick-data CSV -> High School Cube (HSC) CSV format.

Source format (postmodern_pick_data.csv):
    card, elo, mainboard, pickrate, picks, mainboards

Target format (High-School-Cube ...-all.csv), exact 9-column header:
    Name, Color Identity, Times Seen, Times Picked, Pick Rate,
    Avg Pick Position, Wheel Count, P1P1 Count, Elo

Column mapping
--------------
    Name              <- card
    Color Identity    <- Scryfall (alphabetical, e.g. "BU"; colorless = "")
    Times Seen        <- round(picks / pickrate)   [pickrate is an exact fraction]
    Times Picked      <- picks
    Pick Rate         <- pickrate, formatted to 3 decimals
    Avg Pick Position <- "" (not in source, not derivable, not on Scryfall)
    Wheel Count       <- ""
    P1P1 Count        <- ""
    Elo               <- elo
    (source columns `mainboard` / `mainboards` have no HSC equivalent -> dropped)

Usage
-----
CLI:
    python postmodern_to_hsc.py INPUT.csv [-o OUTPUT.csv] [--cache CACHE.json]

As a library (callable from another tool):
    from postmodern_to_hsc import convert
    result = convert("postmodern_pick_data.csv", "out_HSC_format.csv")
    # result -> {"rows": 384, "output": "...", "not_found": [], "unmatched": []}

Pure Python stdlib, no third-party deps. Scryfall color identities are cached to
disk (default: scryfall_ci_cache.json beside this file) so each card name is only
fetched once across runs.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(HERE, "scryfall_ci_cache.json")
SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection"

HEADER = ["Name", "Color Identity", "Times Seen", "Times Picked", "Pick Rate",
          "Avg Pick Position", "Wheel Count", "P1P1 Count", "Elo"]


# --------------------------------------------------------------------- scryfall
def _load_cache(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(path, cache):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
    except OSError:
        pass


def _post_collection(batch):
    payload = json.dumps({"identifiers": [{"name": n} for n in batch]}).encode("utf-8")
    req = urllib.request.Request(
        SCRYFALL_COLLECTION, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "klug-cube-converter/1.0",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_color_identities(names, cache_path=DEFAULT_CACHE):
    """Return {lowercase_name: "BU"-style color identity} for `names`.

    Uses an on-disk cache; only uncached names hit the Scryfall collection
    endpoint (75 identifiers per request). Color identity is returned in
    alphabetical order (B,G,R,U,W) to match HSC's stored convention; colorless
    cards map to "".
    """
    cache = _load_cache(cache_path)
    todo = [n for n in dict.fromkeys(names) if n.lower() not in cache]
    not_found = []

    for i in range(0, len(todo), 75):
        batch = todo[i:i + 75]
        data = _post_collection(batch)
        for card in data.get("data", []):
            ci = "".join(sorted(card.get("color_identity", [])))  # B,G,R,U,W
            cache[card["name"].lower()] = ci
            # also index the front-face name for split / double-faced cards
            cache[card["name"].split(" //")[0].lower()] = ci
        for nf in data.get("not_found", []):
            not_found.append(nf.get("name"))
        time.sleep(0.1)  # be polite to Scryfall

    _save_cache(cache_path, cache)
    return cache, not_found


def _lookup_ci(cache, name):
    key = name.lower()
    if key in cache:
        return cache[key]
    front = key.split(" //")[0]
    return cache.get(front)


# ---------------------------------------------------------------------- convert
def convert(src_path, out_path=None, cache_path=DEFAULT_CACHE):
    """Convert a postmodern pick-data CSV to HSC format.

    Returns a dict: {"rows", "output", "not_found", "unmatched"}.
    `out_path` defaults to "<src>_HSC_format.csv" next to the source.
    """
    if out_path is None:
        base, _ = os.path.splitext(src_path)
        out_path = base + "_HSC_format.csv"

    with open(src_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"No data rows found in {src_path}")

    cache, not_found = fetch_color_identities([r["card"] for r in rows], cache_path)

    unmatched = []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(HEADER)
        for r in rows:
            name = r["card"]
            picks = int(r["picks"])
            pickrate = float(r["pickrate"])
            seen = round(picks / pickrate) if pickrate else ""
            ci = _lookup_ci(cache, name)
            if ci is None:
                unmatched.append(name)
                ci = ""
            w.writerow([name, ci, seen, picks, f"{pickrate:.3f}",
                        "", "", "", r["elo"]])

    return {"rows": len(rows), "output": out_path,
            "not_found": not_found, "unmatched": unmatched}


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert postmodern pick-data CSV to High School Cube format.")
    p.add_argument("input", help="path to postmodern pick-data CSV")
    p.add_argument("-o", "--output", default=None,
                   help="output CSV path (default: <input>_HSC_format.csv)")
    p.add_argument("--cache", default=DEFAULT_CACHE,
                   help="Scryfall color-identity cache file (JSON)")
    args = p.parse_args(argv)

    res = convert(args.input, args.output, args.cache)
    print(f"Rows written : {res['rows']}")
    print(f"Output       : {res['output']}")
    print(f"not_found    : {res['not_found']}")
    print(f"unmatched    : {res['unmatched']}")
    return 0 if not res["unmatched"] else 1


if __name__ == "__main__":
    sys.exit(main())
