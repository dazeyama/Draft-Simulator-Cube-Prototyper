"""
CSV trait editor / exporter for Magic card draft data.

Run:  python app.py   then open http://localhost:8003

Reads a cards CSV (auto-detected in the parent ClaudeProjects folder), lets you:
  - Add a "Type Line" column pulled from Scryfall (batched, cached to disk).
  - Filter rows with simple per-column conditions (numeric min/max, text contains).
  - Export the resulting CSV (downloads in browser and writes a copy to disk).

Pure Python stdlib, no third-party deps. Scryfall lookups are cached in
scryfall_cache.json so each card name is only fetched once.
"""
import os, sys, csv, json, io, time, glob, threading, tempfile, importlib.util
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)                      # C:\ClaudeProjects
CACHE = os.path.join(HERE, "scryfall_cache.json")
CARD_NAMES_CACHE = os.path.join(HERE, "card_names_cache.json")
PORT = 8003
SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection"
SCRYFALL_NAMED = "https://api.scryfall.com/cards/named"
SCRYFALL_CARD_NAMES = "https://api.scryfall.com/catalog/card-names"
UA = "klug-csvtool/1.0 (Magic draft CSV editor)"

# Scryfall fields available to pull. Only Type Line is exposed in the UI for now,
# but adding another is a one-liner: map a label -> the Scryfall card key.
SCRYFALL_FIELDS = {
    "Type Line": "type_line",
}

_lock = threading.Lock()


# ----------------------------------------------------------------------------- data
def _rows_to_data(rows):
    if not rows:
        return [], []
    header, body = rows[0], rows[1:]
    return header, [dict(zip(header, row)) for row in body]


def parse_csv_text(text):
    text = (text or "").lstrip("﻿")     # strip a leading BOM
    return _rows_to_data(list(csv.reader(io.StringIO(text))))


# Real draft pick-data ("postmodern") format -> auto-convert to our HSC format on import.
POSTMODERN_HEADER = ["card", "elo", "mainboard", "pickrate", "picks", "mainboards"]
CONVERT_SCRIPT = os.path.join(HERE, "realdata-conversion", "postmodern_to_hsc.py")
CONVERTED_DIR = os.path.join(HERE, "realdata-conversion", "converted")   # saved converted CSVs


def maybe_convert_postmodern(name, text):
    """If the CSV header is exactly the postmodern pick-data format, run
    realdata-conversion/postmodern_to_hsc.py to convert it to HSC format before
    loading, saving the converted CSV into the 'converted' folder. Otherwise
    return the text unchanged."""
    try:
        first = next(csv.reader(io.StringIO((text or "").lstrip("﻿"))))
    except StopIteration:
        return text
    if [c.strip().lower() for c in first] != POSTMODERN_HEADER:
        return text
    try:
        spec = importlib.util.spec_from_file_location("postmodern_to_hsc", CONVERT_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # normalize cell whitespace so the converter's exact column keys match
        rows = [[c.strip() for c in row]
                for row in csv.reader(io.StringIO(text.lstrip("﻿")))]
        os.makedirs(CONVERTED_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(name or "converted"))[0]
        out = os.path.join(CONVERTED_DIR, base + "_HSC_format.csv")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.csv")
            with open(src, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            res = mod.convert(src, out)              # writes the converted CSV into CONVERTED_DIR
        with open(out, encoding="utf-8-sig") as f:
            converted = f.read()
        print(f"postmodern->HSC: {res.get('rows')} rows -> {out}; unmatched={res.get('unmatched')}")
        return converted
    except Exception as e:
        print("postmodern conversion failed:", e)
        return text


def load_cache():
    if os.path.exists(CACHE):
        try:
            return json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(c):
    tmp = CACHE + ".tmp"
    json.dump(c, open(tmp, "w", encoding="utf-8"), indent=0, ensure_ascii=False)
    os.replace(tmp, CACHE)


# ------------------------------------------------------------------------- scryfall
def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": UA}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _entry(card):
    """Build a cache entry: the configured CSV fields plus a hover image URL."""
    e = {label: _field(card, key) for label, key in SCRYFALL_FIELDS.items()}
    e["_image"] = _image(card)
    return e


def fetch_scryfall(names):
    """Return {name: {scryfall card fields...}} for the given names, using and
    updating the on-disk cache. Misses fall back to a fuzzy single lookup.
    Entries missing the image URL (e.g. from an older cache) are refetched."""
    cache = load_cache()
    todo = [n for n in dict.fromkeys(names)
            if n and (n not in cache or "_image" not in cache.get(n, {}))]

    # Batch via the collection endpoint, 75 identifiers per request.
    for i in range(0, len(todo), 75):
        chunk = todo[i:i + 75]
        try:
            res = _post_json(SCRYFALL_COLLECTION,
                             {"identifiers": [{"name": n} for n in chunk]})
        except Exception as e:
            print("collection request failed:", e)
            res = {"data": [], "not_found": [{"name": n} for n in chunk]}
        # Map returned cards back to the requested names (case-insensitive).
        found = {}
        for card in res.get("data", []):
            for key in (card.get("name", ""),) + tuple(
                    fn.get("name", "") for fn in card.get("card_faces", []) or []):
                found[key.lower()] = card
        for n in chunk:
            card = found.get(n.lower())
            if card:
                cache[n] = _entry(card)
        # Fuzzy fallback for anything still missing in this chunk.
        for nf in res.get("not_found", []):
            n = nf.get("name", "")
            if not n or "_image" in cache.get(n, {}):
                continue
            try:
                time.sleep(0.1)
                card = _get_json(SCRYFALL_NAMED + "?fuzzy=" + urllib.parse.quote(n))
                cache[n] = _entry(card)
            except Exception:
                cache[n] = {**{label: "" for label in SCRYFALL_FIELDS}, "_image": ""}  # tried
        save_cache(cache)
        time.sleep(0.1)
    return cache


def _field(card, key):
    """Read a Scryfall field, falling back to joined card faces for DFCs."""
    if card.get(key) not in (None, ""):
        return card[key]
    faces = card.get("card_faces") or []
    vals = [f.get(key, "") for f in faces if f.get(key)]
    return " // ".join(vals)


def _image(card):
    """A 'normal' card image URL for hover previews (front face for DFCs)."""
    uris = card.get("image_uris")
    if uris and uris.get("normal"):
        return uris["normal"]
    for f in card.get("card_faces") or []:
        u = f.get("image_uris")
        if u and u.get("normal"):
            return u["normal"]
    return ""


# No CSV is loaded at startup. The active dataset is set via /api/load_csv (file
# browser / drag-drop) or /api/load_last. The most recent CSV is persisted so it
# can be reloaded across restarts.
LAST_CSV = os.path.join(HERE, "last_csv.csv")
LAST_META = os.path.join(HERE, "last_meta.json")
CSV_NAME = ""
HEADER, DATA = [], []


def last_name():
    try:
        return json.load(open(LAST_META, encoding="utf-8")).get("name", "")
    except Exception:
        return ""


def set_csv(name, text):
    """Make the given CSV text the active dataset, persist it as 'last', and
    automatically add Type Line (+ hover images) from Scryfall."""
    global CSV_NAME, HEADER, DATA
    text = maybe_convert_postmodern(name, text)   # auto-convert real pick-data CSVs to HSC format
    HEADER, DATA = parse_csv_text(text)
    CSV_NAME = name
    try:
        with open(LAST_CSV, "w", encoding="utf-8") as f:
            f.write(text)
        json.dump({"name": name}, open(LAST_META, "w", encoding="utf-8"))
    except Exception as e:
        print("last-save failed:", e)
    if DATA:
        ensure_typeline()


# ------------------------------------------------------------------- categories
# Mutually exclusive buckets keyed off color-identity count + land-vs-nonland.
CATEGORY_ORDER = ["Mono-Color", "Color-Pair", "Many-Color", "Artifacts-Colorless",
                  "Dual-Lands", "Gold-Lands", "Colorless-Lands"]
CATEGORY_DEFAULTS = {"Mono-Color": 50, "Color-Pair": 5, "Many-Color": 2,
                     "Artifacts-Colorless": 22, "Dual-Lands": 2, "Gold-Lands": 5,
                     "Colorless-Lands": 11}
# These take "top X per color / per pair"; the rest take top X overall.
SUBDIVIDED = {"Mono-Color", "Color-Pair", "Dual-Lands"}
WUBRG = "WUBRG"


def canon_ci(s):
    """Canonical color identity in WUBRG order, e.g. 'RW' -> 'WR'."""
    return "".join(c for c in WUBRG if c in (s or ""))


def ci_count(s):
    return sum(1 for c in (s or "") if c in "WUBRG")


def categorize(row):
    is_land = "land" in (row.get("Type Line", "") or "").lower()
    c = ci_count(row.get("Color Identity", ""))
    if is_land:
        if c == 0:
            return "Colorless-Lands"
        if c == 2:
            return "Dual-Lands"                 # exactly 2 colors -> per-pair
        if c >= 3:
            return "Gold-Lands"                 # 3+ colors
        return "Mono-Color"                     # single-color land
    if c == 0:
        return "Artifacts-Colorless"
    if c == 1:
        return "Mono-Color"
    if c == 2:
        return "Color-Pair"
    return "Many-Color"


def ensure_typeline():
    """Fetch Type Line (and hover images) for every row and add the column at
    position 2 if absent. Returns the number of rows with no Type Line."""
    global HEADER, DATA
    cache = fetch_scryfall([r.get("Name", "") for r in DATA])
    if "Type Line" not in HEADER:
        HEADER.insert(1, "Type Line")           # column 2, right after Name
    missing = 0
    for r in DATA:
        info = cache.get(r.get("Name", ""), {})
        r["Type Line"] = info.get("Type Line", "")
        if not r["Type Line"]:
            missing += 1
    return missing


def image_map():
    cache = load_cache()
    return {r["Name"]: cache[r["Name"]]["_image"] for r in DATA
            if cache.get(r.get("Name", ""), {}).get("_image")}


# ---------------------------------------------------------- add cards / name cache
def fetch_card_names():
    """Fetch the full Scryfall card-name catalog (~35k names) and cache it."""
    data = _get_json(SCRYFALL_CARD_NAMES)
    names = data.get("data", [])
    try:
        json.dump(names, open(CARD_NAMES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError as e:
        print("card-names cache save failed:", e)
    return names


def load_card_names(refresh=False):
    if not refresh and os.path.exists(CARD_NAMES_CACHE):
        try:
            return json.load(open(CARD_NAMES_CACHE, encoding="utf-8"))
        except Exception:
            pass
    try:
        return fetch_card_names()
    except Exception as e:
        print("card-names fetch failed:", e)
        return []


def _resave_last():
    """Re-serialize the in-memory dataset to last_csv.csv (so added cards persist)."""
    try:
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(HEADER)
        for r in DATA:
            w.writerow([r.get(c, "") for c in HEADER])
        with open(LAST_CSV, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
    except Exception as e:
        print("last-resave failed:", e)


def add_card(name):
    """Look the card up on Scryfall and append it to the dataset (blank stats,
    Color Identity + Type Line filled). Returns the card's name, or None."""
    global DATA
    card = None
    for q in ("exact", "fuzzy"):
        try:
            card = _get_json(SCRYFALL_NAMED + f"?{q}=" + urllib.parse.quote(name))
            break
        except Exception:
            card = None
    if not card:
        return None
    cname = card.get("name", name)
    row = {c: "" for c in HEADER}
    row["Name"] = cname
    if "Color Identity" in row:
        row["Color Identity"] = "".join(sorted(card.get("color_identity", [])))
    if "Type Line" in row:
        row["Type Line"] = _field(card, "type_line")
    if "Pick Rate" in row:
        row["Pick Rate"] = "1.0"           # added cards rank top; shown as "∞" in the UI
    DATA.append(row)
    cache = load_cache()
    cache[cname] = {"Type Line": _field(card, "type_line"), "_image": _image(card)}
    save_cache(cache)
    _resave_last()
    return cname


def remove_card(name):
    """Drop the row with this Name from the dataset. Returns True if removed."""
    global DATA
    before = len(DATA)
    DATA = [r for r in DATA if r.get("Name") != name]
    if len(DATA) != before:
        _resave_last()
        return True
    return False


def current_payload():
    return {"csv_name": CSV_NAME, "has_csv": bool(DATA),
            "columns": HEADER, "rows": DATA, "images": image_map(),
            "has_last": os.path.exists(LAST_CSV), "last_name": last_name(),
            "category_defaults": CATEGORY_DEFAULTS, "category_order": CATEGORY_ORDER}


# ----------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/data":
            self._json(current_payload())
        elif path == "/api/card_names":
            self._json({"names": load_card_names()})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}

        if path == "/api/load_csv":
            self._load_csv(body)
        elif path == "/api/load_last":
            self._load_last(body)
        elif path == "/api/write":
            self._write(body)
        elif path == "/api/add_card":
            self._add_card(body)
        elif path == "/api/remove_card":
            with _lock:
                ok = remove_card((body.get("name") or "").strip())
                payload = current_payload() if ok else None
            self._json(payload) if payload else self._send(404, "not found", "text/plain")
        elif path == "/api/update_card_names":
            self._json({"count": len(load_card_names(refresh=True))})
        else:
            self._send(404, "not found", "text/plain")

    def _add_card(self, body):
        with _lock:
            nm = add_card((body.get("name") or "").strip())
            payload = {**current_payload(), "added_card": nm} if nm else None
        if payload:
            self._json(payload)
        else:
            self._send(404, "card not found", "text/plain")

    def _load_csv(self, body):
        with _lock:
            set_csv(body.get("name", "uploaded.csv"), body.get("text", ""))
            self._json(current_payload())

    def _load_last(self, body):
        with _lock:
            if os.path.exists(LAST_CSV):
                with open(LAST_CSV, encoding="utf-8") as f:
                    set_csv(last_name() or "last.csv", f.read())
            self._json(current_payload())

    def _write(self, body):
        """Write the exact rows the client selected (build categories + the
        per-card included/banned overrides are all resolved client-side)."""
        with _lock:
            rows = body.get("rows", [])
            columns = body.get("columns") or HEADER
            filename = (body.get("filename") or "category-build.csv").strip()
            if not filename.lower().endswith(".csv"):
                filename += ".csv"
            self._write_csv(rows, columns, filename)

    def _write_csv(self, rows, columns, filename, extra_headers=None):
        """Serialize rows->CSV and return it as a browser download (Excel-friendly
        BOM). The browser saves it to the user's Downloads folder; nothing is
        written to the program directory."""
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])
        text = buf.getvalue()
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        headers.update(extra_headers or {})
        self._send(200, ("﻿" + text).encode("utf-8"), "text/csv", headers)


# ----------------------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Draft Simulator Cube Prototyper</title>
<style>
  :root{--bg:#0f1115;--panel:#1a1d24;--line:#2a2f3a;--fg:#e6e9ef;--mut:#9aa3b2;--acc:#5b9dff;--ok:#46c98a;}
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);height:100vh;overflow:hidden;display:flex;flex-direction:column}
  header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  h1{font-size:16px;margin:0}
  .path{color:var(--mut);font-size:12px}
  button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer}
  button.sec{background:var(--panel);border:1px solid var(--line);color:var(--fg)}
  button:disabled{opacity:.5;cursor:default}
  .wrap{display:flex;gap:0;flex:1;min-height:0}
  .side{width:320px;min-width:320px;border-right:1px solid var(--line);overflow:auto;padding:14px}
  .main{flex:1;overflow-y:auto;overflow-x:hidden;padding:0}
  .grp{margin-bottom:14px}
  .grp h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 8px}
  .filter{border:1px solid var(--line);border-radius:6px;padding:8px;margin-bottom:8px;background:var(--panel)}
  .filter .lbl{font-weight:600;margin-bottom:6px;font-size:13px}
  .filter .row{display:flex;gap:6px;align-items:center}
  input[type=text],input[type=number]{background:#11141a;border:1px solid var(--line);color:var(--fg);border-radius:5px;padding:6px 8px;width:100%;font-size:13px}
  .num input{width:90px}
  .count{color:var(--mut);font-size:12px;margin:0;padding:10px 14px}
  table{border-collapse:collapse;width:100%;font-size:12.5px;table-layout:fixed}
  th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  th{position:sticky;top:-1px;background:var(--panel);cursor:pointer;z-index:3;box-shadow:0 1px 0 var(--line);white-space:normal;vertical-align:bottom;line-height:1.18;font-size:11px;overflow-wrap:break-word}
  th.active{color:var(--acc)}
  tr:nth-child(even) td{background:rgba(255,255,255,.02)}
  td[data-name]{color:var(--acc);cursor:help}
  /* bucketed view: pastel rows with dark text, no zebra */
  #tbl.buckets tr td{background:transparent;color:#15171c;border-color:#0002}
  #tbl.buckets td[data-name]{color:#14306e;font-weight:600;cursor:help}
  #tbl.buckets tr.bhead td{font-weight:700;text-transform:uppercase;letter-spacing:.04em;font-size:12px;color:#1b1d22;padding:7px 9px;border-top:2px solid #0006}
  /* cards outside their category's top-X (won't be exported) */
  #tbl.buckets tr.excluded{opacity:.45}
  #tbl.buckets tr.excluded td{filter:grayscale(1)}
  #tbl.buckets tr.excluded td.statecell{filter:none}
  /* substitutes pulled in only by manual bans/edits (green) */
  #tbl.buckets tr.substitute td.statecell{box-shadow:inset 4px 0 0 #1f8a4c}
  /* excluded because a lower card was manually included (red) */
  #tbl.buckets tr.bumped{opacity:.85}
  #tbl.buckets tr.bumped td.statecell{box-shadow:inset 4px 0 0 #c0334a}
  /* color-identity edited (purple) */
  #tbl.buckets tr.ciedit td.statecell{box-shadow:inset 4px 0 0 #7b1fa2}
  #tbl.buckets tr.dim{opacity:.55}
  /* 3px divider between sub-buckets (guilds, 3+, colorless) inside Gold/Lands */
  #tbl.buckets tr.subdiv td{border-top:3px solid #000}
  /* blank spacer rows at the bottom for extra scroll room */
  #tbl.buckets tr.blankrow td{border:0;background:transparent;height:36px}
  /* editable Color Identity cell + right-click menu */
  #tbl.buckets td.cieditable{cursor:context-menu}
  #tbl.buckets td.ci-edited{color:#7b1fa2;font-weight:800;font-style:italic}
  #ciMenu,#createMenu,#cbMenu,#navMenu,#addMenu,#rmMenu{position:fixed;display:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;box-shadow:0 8px 30px #000b;z-index:300;font-size:12.5px;padding:4px;min-width:140px;max-height:62vh;overflow:auto}
  #ciMenu .ci-item,#createMenu .ci-item,#cbMenu .ci-item,#navMenu .ci-item,#addMenu .ci-item,#rmMenu .ci-item{padding:5px 10px;border-radius:4px;cursor:pointer;white-space:nowrap;color:var(--fg)}
  #ciMenu .ci-item:hover,#createMenu .ci-item:hover,#cbMenu .ci-item:hover,#navMenu .ci-item:hover,#addMenu .ci-item:hover,#rmMenu .ci-item:hover{background:var(--acc);color:#fff}
  #topBtn,#addCardBtn{position:fixed;bottom:20px;right:20px;width:46px;height:46px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px #0008;z-index:150}
  #topBtn:hover,#addCardBtn:hover{filter:brightness(1.12)}
  #addPanel{position:fixed;bottom:76px;right:20px;display:none;width:280px;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 30px #000b;z-index:300;padding:8px}
  #addPanel.on{display:block}
  #addInput{width:100%;background:#11141a;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:8px 10px;font-size:13px}
  #addSugg{max-height:240px;overflow:auto;margin-top:6px}
  #addSugg .sugg{padding:6px 9px;border-radius:5px;cursor:pointer;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--fg)}
  #addSugg .sugg:hover{background:var(--acc);color:#fff}
  .stealthmark{color:var(--acc);font-weight:800;margin-left:2px;vertical-align:middle}
  #ciMenu .ci-sep{height:1px;background:var(--line);margin:4px 0}
  #ciMenu .ci-reset{color:var(--mut)}
  /* included/banned tri-state checkbox */
  th.stateh{width:66px;white-space:nowrap;font-size:9px;line-height:1.2;text-align:center;cursor:default;text-transform:uppercase;letter-spacing:0;padding:5px 3px}
  td.statecell{width:1%;text-align:center;padding:3px 6px}
  .tri{display:inline-block;width:16px;height:16px;border:1.5px solid #8a8f99;border-radius:3px;background:#fbfbfb;cursor:pointer;line-height:13px;font-size:12px;font-weight:800;color:#fff;vertical-align:middle}
  .tri.s1{background:#2faa6a;border-color:#238954}
  .tri.s1::after{content:'\2713'}
  .tri.s2{background:#d6455c;border-color:#b23147}
  .tri.s2::after{content:'\2717'}
  .toast{position:fixed;bottom:18px;right:18px;background:var(--ok);color:#04150d;padding:10px 16px;border-radius:8px;font-weight:600;opacity:0;transition:.25s;pointer-events:none;max-width:480px}
  .toast.show{opacity:1}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:6px}
  @keyframes s{to{transform:rotate(360deg)}}
  #cardpreview{position:fixed;display:none;width:235px;border-radius:11px;box-shadow:0 8px 30px #000b;pointer-events:none;z-index:100}
  .cat{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .cat .meta{flex:1;min-width:0}
  .cat .cn{font-weight:600;font-size:13px}
  .cat .cd{color:var(--mut);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cat input{width:52px;text-align:right}
  .cat .maxbtn{flex:none;padding:5px 9px;font-size:11px;font-weight:600}
  .rankrow{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:12px;color:var(--mut);flex-wrap:wrap}
  select{background:#11141a;border:1px solid var(--line);color:var(--fg);border-radius:5px;padding:6px 8px;font-size:13px}
  .chk{display:flex;align-items:center;gap:4px;color:var(--fg)}
  .chk input{width:auto}
  .empty{color:var(--mut);text-align:center;margin-top:14vh;font-size:15px;line-height:2}
  .empty b{color:var(--fg)}
  #overlay{position:fixed;inset:0;background:#0b0d12cc;display:none;align-items:center;justify-content:center;flex-direction:column;gap:14px;z-index:200;font-size:15px}
  #overlay.on{display:flex}
  #overlay .big{width:34px;height:34px;border:3px solid #fff3;border-top-color:var(--acc);border-radius:50%;animation:s .7s linear infinite}
  #drop{position:fixed;inset:0;background:#5b9dff22;border:3px dashed var(--acc);display:none;align-items:center;justify-content:center;z-index:150;font-size:22px;font-weight:700;color:var(--acc)}
  #drop.on{display:flex}
  button.big{font-size:14px;font-weight:600;padding:10px 18px}
  .hdrbtn{margin-left:10px;font-size:10px;font-weight:700;padding:2px 8px;border:1px solid #0000005e;border-radius:4px;background:#ffffffb3;color:#1b1d22;cursor:pointer;text-transform:none;letter-spacing:0;vertical-align:middle}
  .hdrbtn:hover{background:#fff}
  .hdrbtn.szbtn{padding:0 7px;font-size:14px;font-weight:800;line-height:1.3;margin:0 3px}
  .chip{background:var(--acc);border:0;border-radius:20px;padding:8px 16px;font-size:15px;font-weight:700;color:#fff}
  .chip b{color:#fff;font-size:18px}
  .totalrow{margin:10px 0 6px;font-size:13px;color:var(--mut)}
  .totalrow b{color:var(--acc);font-size:16px}
  .flbl{display:block;font-size:11px;color:var(--mut);margin-top:8px}
  .typetable{width:100%;table-layout:auto;border-collapse:separate;border-spacing:0;margin-top:12px;font-size:12.5px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .typetable th{position:static;background:var(--panel);text-align:left;padding:7px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:700;white-space:normal;box-shadow:none;cursor:default;border:0}
  .typetable td{padding:5px 10px;border:0;border-top:1px solid var(--line);max-width:none}
  .typetable td.tt-num{text-align:right;font-weight:700;color:var(--fg)}
  .selwarn{display:none;background:#3a2a12;border:1px solid #7a5a1e;color:#ffcf8a;border-radius:6px;padding:7px 9px;font-size:12px;line-height:1.35;margin-bottom:6px}
  .cat input.warn{border-color:#e0556b;background:#3a1820;color:#ffb3bf}
</style></head>
<body>
<header>
  <h1>Draft Simulator Cube Prototyper</h1>
  <input type="file" id="fileInput" accept=".csv,text/csv" style="display:none">
  <button id="importBtn" class="sec">Import CSV…</button>
  <button id="lastBtn" class="sec">Load last CSV</button>
  <span class="path" id="path"></span>
  <span style="flex:1"></span>
  <button id="resetIncBtn" class="sec" style="font-size:12px;padding:6px 10px" title="Clear all ✓/✕ marks">Reset include list</button>
  <span class="chip"><b id="totalHdr">0</b>&nbsp;cards</span>
  <button id="createBtn" class="big" title="Right click for plaintext">Create prototype cube</button>
</header>
<div class="wrap">
  <div class="side">
    <div class="grp">
      <h3>Build by category</h3>
      <div class="rankrow"><span>Top X ranked by</span>
        <select id="rankBy"></select>
        <label class="chk"><input type="checkbox" id="rankDesc" checked>high→low</label>
      </div>
      <div id="cats"></div>
      <button id="allBtn" class="sec" style="width:100%;margin-top:4px">ALL — include every card</button>
      <button id="defaultsBtn" class="sec" style="width:100%;margin-top:4px">Reset all to defaults</button>
      <div id="selWarn" class="selwarn"></div>
      <label class="flbl">Output filename</label>
      <input id="cfname" type="text" value="category-build.csv" style="margin-top:4px">
      <table class="typetable">
        <thead><tr><th colspan="2">Total cards of these types</th></tr></thead>
        <tbody id="typeBody"></tbody>
      </table>
    </div>
  </div>
  <div class="main">
    <div class="count" id="count"></div>
    <table id="tbl" class="buckets"><thead></thead><tbody></tbody></table>
  </div>
</div>
<img id="cardpreview" alt="">
<div id="ciMenu"></div>
<div id="createMenu"><div class="ci-item" data-savecsv="1">Save CSV</div><div class="ci-item" data-copy="1">Copy Plaintext</div></div>
<div id="cbMenu">
  <div class="ci-item" data-cb="3">Stealth Include</div>
  <div class="ci-item" data-cb="4">Stealth Ban</div>
  <div class="ci-sep"></div>
  <div class="ci-item ci-reset" data-cb="0">Reset</div>
</div>
<button id="topBtn" title="Right click for navigation" aria-label="Scroll to top"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="4" x2="19" y2="4"/><line x1="12" y1="9" x2="12" y2="20"/><polyline points="7 14 12 9 17 14"/></svg></button>
<div id="navMenu"></div>
<button id="addCardBtn" title="Add cards — right-click to update cache" aria-label="Add cards"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
<div id="addPanel"><input id="addInput" type="text" placeholder="Add a card…" autocomplete="off" spellcheck="false"><div id="addSugg"></div></div>
<div id="addMenu"><div class="ci-item" data-updatecache="1">Update Cache</div></div>
<div id="rmMenu"><div class="ci-item ci-reset" data-removecard="1">Remove Card</div></div>
<div id="drop">Drop a .csv to load</div>
<div id="overlay"><div class="big"></div><div id="overlayMsg">Loading…</div></div>
<div class="toast" id="toast"></div>
<script>
let COLS=[], ROWS=[], IMAGES={}, NUMERIC={}, sortCol=null, sortDir=1;
let cardState={};   // card Name -> 0 unchecked, 1 included (✓), 2 banned (✕)
let ciOriginal={};  // card Name -> original Color Identity (kept for "reset to default")
let sizeOffset={};  // mono display bucket (White/Blue/...) -> +/- offset to its Mono-Color quota
let ciMenuTarget=null;
let cbMenuTarget=null;
let rmMenuTarget=null;
let cardNames=null;   // cached Scryfall card-name catalog for the + add-card autocomplete
let loadedName='';    // name of the currently-loaded CSV (for "Save CSV")
const CI_OPTIONS=[
  ['W','W'],['U','U'],['B','B'],['R','R'],['G','G'],
  ['Azorius (WU)','WU'],['Orzhov (WB)','WB'],['Dimir (UB)','UB'],['Izzet (UR)','UR'],['Rakdos (BR)','BR'],
  ['Golgari (BG)','BG'],['Gruul (RG)','RG'],['Boros (WR)','WR'],['Selesnya (WG)','WG'],['Simic (UG)','UG'],
  ['Colorless (blank)',''],
];
const CATS=[
  ['Mono-Color',50,'top X of EACH color (W U B R G)'],
  ['Color-Pair',5,'top X of EACH of 10 color pairs'],
  ['Many-Color',2,'>2-color non-land (total)'],
  ['Artifacts-Colorless',22,'colorless non-land (total)'],
  ['Dual-Lands',2,'top X of EACH land color pair'],
  ['Gold-Lands',5,'lands, >2 colors (total)'],
  ['Colorless-Lands',11,'lands, no color (total)'],
];

function isNum(v){ if(v===""||v==null)return false; return !isNaN(parseFloat(v))&&isFinite(v); }
function detectNumeric(){
  NUMERIC={};
  for(const c of COLS){
    let n=0,tot=0;
    for(const r of ROWS){ const v=r[c]; if(v!==""&&v!=null){tot++; if(isNum(v))n++;} }
    NUMERIC[c]= tot>0 && n/tot>0.8;   // mostly-numeric column
  }
}

function deriveExportName(name){
  let base=String(name||'').replace(/\.csv$/i,'');
  // "...-500drafts-all" -> "...-prototype-cube-all"
  if(/\d+drafts/i.test(base)) base=base.replace(/\d+drafts/i,'prototype-cube');
  else base=base+'-prototype-cube';
  return base+'.csv';
}

function applyData(d){
  COLS=d.columns||[]; ROWS=d.rows||[]; IMAGES=d.images||{}; cardState={}; ciOriginal={}; sizeOffset={};
  loadedName=d.csv_name||'';
  document.getElementById('path').textContent = d.has_csv ? (d.csv_name+'  ('+ROWS.length+' rows)') : 'no CSV loaded';
  const lb=document.getElementById('lastBtn');
  lb.disabled = !d.has_last;
  lb.textContent = d.has_last ? ('Load last ('+d.last_name+')') : 'Load last CSV';
  if(d.has_csv) document.getElementById('cfname').value=deriveExportName(d.csv_name);
  sortCol = COLS.includes('Pick Rate') ? 'Pick Rate' : null;   // default: Pick Rate high->low
  sortDir = -1;
  detectNumeric(); buildRankBy(); render(); updateTotals();
}

function showLoading(on,msg){
  document.getElementById('overlayMsg').textContent = msg||'Loading…';
  document.getElementById('overlay').classList.toggle('on', !!on);
}

async function sendLoad(url, payload){
  showLoading(true, 'Loading CSV & fetching Type Lines from Scryfall…');
  try{
    const d=await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload||{})})).json();
    applyData(d);
    if(d.has_csv) toast('Loaded '+d.csv_name+' ('+ROWS.length+' rows)');
    else toast('Nothing to load');
  }catch(e){ toast('Load failed'); }
  showLoading(false);
}

function importFile(file){
  if(!file) return;
  const reader=new FileReader();
  reader.onload=()=>sendLoad('/api/load_csv',{name:file.name,text:String(reader.result)});
  reader.readAsText(file);
}

async function load(){            // initial: no CSV auto-loaded, just read state
  buildCats(); buildTypeTable(); buildCiMenu(); buildNavMenu();
  applyData(await (await fetch('/api/data')).json());
}

document.getElementById('importBtn').onclick=()=>document.getElementById('fileInput').click();
document.getElementById('fileInput').onchange=e=>{ importFile(e.target.files[0]); e.target.value=''; };
document.getElementById('lastBtn').onclick=()=>sendLoad('/api/load_last',{});

// Drag & drop a CSV anywhere on the window.
let dragDepth=0;
const dropEl=document.getElementById('drop');
window.addEventListener('dragenter',e=>{ e.preventDefault(); dragDepth++; dropEl.classList.add('on'); });
window.addEventListener('dragover',e=>e.preventDefault());
window.addEventListener('dragleave',e=>{ if(--dragDepth<=0){dragDepth=0; dropEl.classList.remove('on');} });
window.addEventListener('drop',e=>{
  e.preventDefault(); dragDepth=0; dropEl.classList.remove('on');
  const f=[...(e.dataTransfer.files||[])].find(f=>f.name.toLowerCase().endsWith('.csv'))||e.dataTransfer.files[0];
  importFile(f);
});

// --- category logic (mirrors the server's categorize/canon_ci) ---
const SUBDIV=new Set(['Mono-Color','Color-Pair','Dual-Lands']);
const EXPECTED={'Mono-Color':5,'Color-Pair':10,'Dual-Lands':10};   // # of sub-buckets when full
function cic(s){ let n=0; for(const c of (s||'')) if('WUBRG'.includes(c))n++; return n; }
function canon(s){ return [...'WUBRG'].filter(c=>(s||'').includes(c)).join(''); }
function categorize(r){
  const land=(r['Type Line']||'').toLowerCase().includes('land');
  const c=cic(r['Color Identity']);
  if(land){
    if(c===0) return 'Colorless-Lands';
    if(c===2) return 'Dual-Lands';
    if(c>=3)  return 'Gold-Lands';
    return 'Mono-Color';
  }
  if(c===0) return 'Artifacts-Colorless';
  if(c===1) return 'Mono-Color';
  if(c===2) return 'Color-Pair';
  return 'Many-Color';
}
function computeBuckets(){
  const b={}; for(const [name] of CATS) b[name]={};
  for(const r of ROWS){
    const cat=categorize(r);
    const sub=SUBDIV.has(cat)?canon(r['Color Identity']):'';
    b[cat][sub]=(b[cat][sub]||0)+1;
  }
  return b;
}

// --- display buckets (one per colored section), in order; colors match legend ---
const BUCKETS=[
  ['White','White cards','#fbfbda'],
  ['Blue','Blue cards','#d2e6f5'],
  ['Black','Black cards','#d3d0d8'],
  ['Red','Red cards','#f8d2d2'],
  ['Green','Green cards','#d9f0cd'],
  ['Colorless','Colorless / Artifact','#e4e4e4'],
  ['Gold','Gold (multicolor)','#f2eea5'],
  ['Lands','Lands','#f6ddb6'],
];
const MONO2BUCKET={W:'White',U:'Blue',B:'Black',R:'Red',G:'Green'};
const MONO_BUCKETS=new Set(['White','Blue','Black','Red','Green']);   // sections with +/- size buttons
const LANDS_COLOR=(BUCKETS.find(b=>b[0]==='Lands')||[])[2]||'#f6ddb6';  // lands in mono sections use this
const COLORLESS_COLOR=(BUCKETS.find(b=>b[0]==='Colorless')||[])[2]||'#e4e4e4';  // Gold Talismans/Signets use this
const SUB_COLOR='#bce6c6';   // substitute cards pulled in only by manual bans/edits (green)
const BUMP_COLOR='#f3bdbf';  // cards bumped OUT because a lower card was manually included (red)
const EDIT_COLOR='#d6c2ee';  // cards whose Color Identity was manually edited (purple)
function bucketOf(r){
  const ci=r['Color Identity']||'';
  const c=cic(ci);
  if(c===1) return MONO2BUCKET[canon(ci)];          // mono (land or not) -> color bucket
  const land=(r['Type Line']||'').toLowerCase().includes('land');
  if(land) return 'Lands';                          // non-mono lands (0 or 2+ colors)
  return c===0 ? 'Colorless' : 'Gold';              // non-land: 0 -> colorless, 2+ -> gold
}

// Within the multi-color display buckets (Gold, Lands), order cards by guild pair,
// then 3+ colors, then colorless — no sub-headers, just ordering. Identities are in
// WUBRG order, so Boros=WR, Selesnya=WG, Simic=UG.
const GUILD_ORDER=['WU','WB','UB','UR','BR','BG','RG','WR','WG','UG'];  // Azorius..Simic
const SUBORDERED=new Set(['Gold','Lands']);
function subIndex(r){
  const ci=canon(r['Color Identity']||''); const c=cic(ci);
  if(c===2){ const i=GUILD_ORDER.indexOf(ci); return i>=0?i:10; }
  if(c>=3) return 10;            // 3+ colors
  return 11;                     // colorless
}

// Final selection, honoring the per-card included(✓)/banned(✕) overrides on top
// of the build categories. Forced changes are compensated to keep the total
// steady, cascading: same category -> same display bucket -> whole list.
// Returns {set, warning, total, target}.
function computeSelected(stFn){
  if(!ROWS.length) return {set:new Set(), warning:null, total:0, target:0};
  const col=sortCol, desc=(sortDir<0);
  const rk=r=>{ const v=parseFloat(r[col]); return isFinite(v)?v:0; };
  const best=(a,b)=> desc ? rk(b)-rk(a) : rk(a)-rk(b);   // best-ranked first
  const worst=(a,b)=> -best(a,b);
  const st=stFn||(r=>cardState[r['Name']]||0);           // 0 neutral / 1 ✓ / 2 ✕ / 3 stealth✓ / 4 stealth✕
  // base-pass state: stealth-include (3) is NOT a fill candidate (it's a pure extra, added
  // after), so a ban's substitute skips it; stealth-ban (4) stays neutral then is removed after.
  const bst=r=>{ const s=st(r); return s===3 ? -1 : s===4 ? 0 : s; };
  const X={}; for(const [name] of CATS){ const inp=document.getElementById('cat-'+name); X[name]=inp?(parseInt(inp.value)||0):0; }

  // sub-buckets (build category + per-color/pair sub) and display buckets
  const subB={};
  for(const r of ROWS){
    const cat=categorize(r);
    const sub=SUBDIV.has(cat)?canon(r['Color Identity']):'';
    const k=cat+'|'+sub; (subB[k]=subB[k]||{cat,sub,rows:[]}).rows.push(r);
  }
  const sel=new Set(); let N=0; const dbQuota={};
  // Pass 1 — same category: force in ✓, fill the rest with top neutral, drop ✕
  for(const k in subB){
    const {cat,sub,rows}=subB[k];
    let xv=X[cat];
    if(cat==='Mono-Color') xv += (sizeOffset[MONO2BUCKET[sub]]||0);   // per-color size override
    xv=Math.max(0,xv);
    const q=Math.min(xv, rows.length); N+=q;
    const db=bucketOf(rows[0]); dbQuota[db]=(dbQuota[db]||0)+q;
    const fin=rows.filter(r=>bst(r)===1);
    const neutral=rows.filter(r=>bst(r)===0).sort(best);
    fin.forEach(r=>sel.add(r));
    const fill=Math.max(0, xv-fin.length);
    for(let i=0;i<fill && i<neutral.length;i++) sel.add(neutral[i]);
  }
  // Pass 2 — same display bucket: fix each bucket back to its quota
  const DB={}; for(const r of ROWS){ const d=bucketOf(r); (DB[d]=DB[d]||[]).push(r); }
  for(const d in DB){
    const rows=DB[d], quota=dbQuota[d]||0;
    let cnt=rows.reduce((n,r)=>n+(sel.has(r)?1:0),0);
    if(cnt<quota){
      const cand=rows.filter(r=>!sel.has(r) && bst(r)===0).sort(best);
      for(let i=0;i<cand.length && cnt<quota;i++){ sel.add(cand[i]); cnt++; }
    }else if(cnt>quota){
      const rem=rows.filter(r=>sel.has(r) && bst(r)!==1).sort(worst);
      for(let i=0;i<rem.length && cnt>quota;i++){ sel.delete(rem[i]); cnt--; }
    }
  }
  // Pass 3 — whole list: restore the grand total N if it still deviates
  let total=sel.size, warning=null;
  if(total<N){
    const cand=ROWS.filter(r=>!sel.has(r) && bst(r)===0).sort(best);
    for(let i=0;i<cand.length && total<N;i++){ sel.add(cand[i]); total++; }
    if(total<N) warning=`${N-total} card${N-total===1?'':'s'} short of the ${N}-card target — not enough un-banned cards left to backfill.`;
  }else if(total>N){
    const rem=ROWS.filter(r=>sel.has(r) && bst(r)!==1).sort(worst);
    for(let i=0;i<rem.length && total>N;i++){ sel.delete(rem[i]); total--; }
    if(total>N) warning=`${total-N} card${total-N===1?'':'s'} over the ${N}-card target — too many checked (✓) cards to balance.`;
  }
  // Stealth marks: force in/out with NO compensation (applied after the balanced base)
  for(const r of ROWS){ const s=st(r); if(s===3) sel.add(r); else if(s===4) sel.delete(r); }
  return {set:sel, warning, total:sel.size, target:N};
}

// Rows in display order (bucket order; Gold/Lands sub-ordered by guild) — used
// for the export so the CSV matches what's shown.
function displayOrderedRows(){
  const groups={}; for(const [k] of BUCKETS) groups[k]=[];
  for(const r of displayRows()){ const g=groups[bucketOf(r)]; if(g) g.push(r); }
  let out=[];
  for(const [key] of BUCKETS){
    let gr=groups[key];
    if(SUBORDERED.has(key)) gr=gr.slice().sort((a,b)=>subIndex(a)-subIndex(b));
    out=out.concat(gr);
  }
  return out;
}

function maxFor(name){
  if(!ROWS.length) return 0;
  const counts=Object.values(computeBuckets()[name]);
  if(!counts.length) return 0;
  // subdivided fields are "per color/pair": cap at the smallest sub-bucket so every
  // color/pair can fully supply it (no shortfall). Others: total available.
  return SUBDIV.has(name) ? Math.min(...counts) : counts.reduce((a,c)=>a+c,0);
}

function allFor(name){
  if(!ROWS.length) return 0;
  const counts=Object.values(computeBuckets()[name]);
  if(!counts.length) return 0;
  // include EVERY card: subdivided -> largest sub-bucket (covers the biggest color/
  // pair, so all are taken; smaller ones go red). Others: total available.
  return SUBDIV.has(name) ? Math.max(...counts) : counts.reduce((a,c)=>a+c,0);
}

function buildCats(){
  const host=document.getElementById('cats'); host.innerHTML='';
  for(const [name,def,desc] of CATS){
    const row=document.createElement('div'); row.className='cat';
    row.innerHTML=`<div class="meta"><div class="cn">${name}</div><div class="cd">${desc}</div></div>
      <input type="number" min="0" id="cat-${name}" value="${def}">
      <button class="maxbtn sec" data-maxfor="${name}" title="Set to the maximum available">MAX</button>`;
    host.appendChild(row);
  }
  host.querySelectorAll('input').forEach(i=>i.addEventListener('input',()=>{ updateTotals(); render(); }));
  host.querySelectorAll('.maxbtn').forEach(btn=>btn.onclick=()=>{
    if(!ROWS.length){ toast('Load a CSV first'); return; }
    const inp=document.getElementById('cat-'+btn.dataset.maxfor);
    inp.value=maxFor(btn.dataset.maxfor); updateTotals(); render();
  });
}

document.getElementById('resetIncBtn').onclick=()=>{
  if(!Object.keys(cardState).length && !Object.keys(sizeOffset).length){ toast('Nothing to reset'); return; }
  cardState={}; sizeOffset={}; render();
  toast('Include list & color sizes reset');
};

document.getElementById('allBtn').onclick=()=>{
  if(!ROWS.length){ toast('Load a CSV first'); return; }
  for(const [name] of CATS) document.getElementById('cat-'+name).value=allFor(name);
  updateTotals(); render();
};

document.getElementById('defaultsBtn').onclick=()=>{
  for(const [name,def] of CATS) document.getElementById('cat-'+name).value=def;
  updateTotals(); render();
};

// "Top X ranked by" drives the table sort (and vice-versa, via syncRankControls)
document.getElementById('rankBy').onchange=e=>{ sortCol=e.target.value; render(); };
document.getElementById('rankDesc').onchange=e=>{ sortDir=e.target.checked?-1:1; render(); };

function updateTotals(){   // per-field red warnings only (total is set in render)
  const b=ROWS.length?computeBuckets():null;
  for(const [name] of CATS){
    const inp=document.getElementById('cat-'+name);
    if(!inp) continue;
    const x=parseInt(inp.value)||0;
    let taken=0, capped=false;
    if(b){
      const counts=Object.values(b[name]);          // cards in each present sub-bucket
      if(SUBDIV.has(name)){
        for(const cnt of counts){ taken+=Math.min(x,cnt); if(cnt<x) capped=true; }
        if(counts.length<EXPECTED[name]) capped=true;   // a color/pair is absent entirely
      }else{
        const avail=counts.reduce((a,c)=>a+c,0);
        taken=Math.min(x,avail); if(avail<x) capped=true;
      }
    }
    const isWarn = !!b && x>0 && capped;
    inp.classList.toggle('warn', isWarn);
    inp.title = isWarn
      ? `Only ${taken} card${taken===1?'':'s'} available — would include all ${taken} (you asked for ${SUBDIV.has(name)?x+' per color/pair':x})`
      : '';
  }
}

function buildRankBy(){
  // mirrors the table sort, so it offers every column the viewer can sort by
  const sel=document.getElementById('rankBy'); sel.innerHTML='';
  for(const c of COLS){
    const o=document.createElement('option'); o.value=c; o.textContent=c;
    sel.appendChild(o);
  }
  sel.value=sortCol||'';
}

// keep the "Top X ranked by" select + high→low box in sync with the table sort
function syncRankControls(){
  const rb=document.getElementById('rankBy');
  if(rb && sortCol!=null) rb.value=sortCol;
  document.getElementById('rankDesc').checked=(sortDir<0);
}

function displayRows(){
  let out=ROWS;
  if(sortCol!=null){
    const num=NUMERIC[sortCol];
    out=out.slice().sort((a,b)=>{
      let x=a[sortCol]??'', y=b[sortCol]??'';
      if(num){x=parseFloat(x)||0;y=parseFloat(y)||0; return (x-y)*sortDir;}
      return String(x).localeCompare(String(y))*sortDir;
    });
  }
  return out;
}

function colWidth(c){   // fixed-layout widths so the table fits with no horizontal scroll
  if(c==='Color Identity') return '44px';   // thin
  if(c==='Name') return '26%';              // flexible, kept wider (clips last)
  if(c==='Type Line') return '16%';         // flexible, clips first
  return '62px';                            // numeric/other columns
}
// header display: 2-word labels stack one word per line; overrides force a 2-line split
const HEADER_BREAKS={'Avg Pick Position':'Avg Pick<br>Position'};
function headerHtml(c){ return HEADER_BREAKS[c] || esc(c).replace(/ /g,'<br>'); }

// sidebar "Total cards of these types" table (one row per display bucket; Gold -> Multicolor)
const TYPE_LABELS={Gold:'Multicolor'};
function buildTypeTable(){
  const body=document.getElementById('typeBody'); body.innerHTML='';
  for(const [key] of BUCKETS){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${TYPE_LABELS[key]||key}</td><td class="tt-num" id="tt-${key}">0</td>`;
    body.appendChild(tr);
  }
}

function buildNavMenu(){
  document.getElementById('navMenu').innerHTML =
    BUCKETS.map(([key,label])=>`<div class="ci-item" data-jump="${key}">Jump to ${label}</div>`).join('');
}
function jumpToSection(key){
  const main=document.querySelector('.main');
  const target=document.getElementById('bhead-'+key); if(!target) return;
  const headH=(document.querySelector('#tbl thead')||{}).offsetHeight||0;
  const top = target.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - headH - 4;
  main.scrollTo({top:Math.max(0,top), behavior:'auto'});
}
// keep the float button the same visible gap from the right as from the bottom (clear the .main scrollbar)
function placeTopBtn(){   // HUD order from the right edge: [+ add] [nav] ... [toast]
  const main=document.querySelector('.main');
  const sb=main?Math.max(0, main.offsetWidth-main.clientWidth):0;
  document.getElementById('addCardBtn').style.right=(20+sb)+'px';     // rightmost
  document.getElementById('addPanel').style.right=(20+sb)+'px';       // above the + button
  document.getElementById('topBtn').style.right=(20+sb+58)+'px';      // left of +
  document.getElementById('toast').style.right=(20+sb+116)+'px';      // left of nav
}
window.addEventListener('resize',placeTopBtn);

// --- editable Color Identity (right-click menu) ---
function buildCiMenu(){
  document.getElementById('ciMenu').innerHTML =
    CI_OPTIONS.map(([label,val])=>`<div class="ci-item" data-val="${val}">${label}</div>`).join('')
    + '<div class="ci-sep"></div><div class="ci-item ci-reset" data-reset="1">Reset to default</div>';
}
function openCiMenu(name,x,y){
  closeMenus();
  ciMenuTarget=name;
  const m=document.getElementById('ciMenu'); m.style.display='block';
  const w=m.offsetWidth||150, h=m.offsetHeight||320;
  m.style.left=Math.max(6,Math.min(x,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(y,innerHeight-h-6))+'px';
}
function closeCiMenu(){ document.getElementById('ciMenu').style.display='none'; ciMenuTarget=null; }
function openCbMenu(name,x,y){
  closeMenus();
  cbMenuTarget=name;
  const m=document.getElementById('cbMenu'); m.style.display='block';
  const w=m.offsetWidth||150, h=m.offsetHeight||130;
  m.style.left=Math.max(6,Math.min(x,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(y,innerHeight-h-6))+'px';
}
function setCi(name,val){
  const r=ROWS.find(x=>x['Name']===name); if(!r) return;
  if(!(name in ciOriginal)) ciOriginal[name]=r['Color Identity']||'';
  r['Color Identity']=val;
  if(ciOriginal[name]===val) delete ciOriginal[name];   // matches original -> no longer "edited"
  render();
}
function resetCi(name){
  const r=ROWS.find(x=>x['Name']===name);
  if(r && (name in ciOriginal)){ r['Color Identity']=ciOriginal[name]; delete ciOriginal[name]; }
  render();
}

// copy the include-list card names (one per line, in display order) to the clipboard
async function copyPlaintext(){
  if(!ROWS.length){ toast('Load a CSV first'); return; }
  const sel=computeSelected().set;
  const names=displayOrderedRows().filter(r=>sel.has(r)).map(r=>r['Name']);
  const text=names.join('\n');
  try{
    await navigator.clipboard.writeText(text);
    toast('Copied '+names.length+' card names');
  }catch(e){
    const ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); toast('Copied '+names.length+' card names'); }
    catch(_){ toast('Copy failed'); }
    ta.remove();
  }
}

function render(){
  if(!COLS.length){
    document.querySelector('#tbl thead').innerHTML='';
    document.querySelector('#tbl tbody').innerHTML='';
    document.getElementById('count').innerHTML=
      '<div class="empty">No CSV loaded.<br><b>Import CSV…</b>, <b>Load last CSV</b>, or <b>drag &amp; drop</b> a .csv onto the window.<br>Type Line is fetched automatically on load.</div>';
    return;
  }
  const rows=displayRows();      // globally sorted; one sort drives every bucket
  const thead=document.querySelector('#tbl thead');
  thead.innerHTML='<tr><th class="stateh" title="included / banned">included/<br>banned</th>'+COLS.map(c=>`<th data-c="${c}" title="${esc(c)}" style="width:${colWidth(c)}" class="${c===sortCol?'active':''}">${headerHtml(c)}${c===sortCol?(sortDir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr>';
  thead.querySelectorAll('th[data-c]').forEach(th=>th.onclick=()=>{
    const c=th.dataset.c; if(sortCol===c)sortDir*=-1; else{sortCol=c;sortDir=-1;} render();
  });
  syncRankControls();
  // group the sorted rows into buckets (sort order preserved within each)
  const groups={}; for(const [k] of BUCKETS) groups[k]=[];
  for(const r of rows){ const g=groups[bucketOf(r)]; if(g) g.push(r); }
  const result=computeSelected();     // final selection incl. ✓/✕ overrides
  const sel=result.set;
  const baseSet=computeSelected(()=>0).set;   // natural selection, ignoring overrides
  document.getElementById('totalHdr').textContent=result.total;
  const warnEl=document.getElementById('selWarn');
  if(result.warning){ warnEl.textContent='⚠ '+result.warning; warnEl.style.display='block'; }
  else warnEl.style.display='none';
  const ncol=COLS.length;
  let html='';
  for(const [key,label,color] of BUCKETS){
    let gr=groups[key];
    // multi-color buckets: order by guild pair -> 3+ -> colorless (stable, keeps sort)
    if(SUBORDERED.has(key)) gr=gr.slice().sort((a,b)=>subIndex(a)-subIndex(b));
    const inThis=gr.reduce((n,r)=>n+(sel.has(r)?1:0),0);
    const sizeChanged = MONO_BUCKETS.has(key) && (sizeOffset[key]||0)!==0;   // size adjusted via +/-
    const hasStealth = gr.some(r=>{ const s=cardState[r['Name']]; return s===3||s===4; });  // stealth ✓/✕ in section
    const star = (sizeChanged || hasStealth) ? '*' : '';
    const tc=document.getElementById('tt-'+key); if(tc) tc.textContent=inThis+(star?' *':'');
    let hdrBtn='';
    if(key==='Gold'){
      hdrBtn += ' <button type="button" class="hdrbtn rockbtn" data-pat="signet" data-act="ban">Ban Signets</button>';
      hdrBtn += '<button type="button" class="hdrbtn rockbtn" data-pat="talisman" data-act="ban">Ban Talismans</button>';
    }
    hdrBtn += ` <button type="button" class="hdrbtn sectreset" data-bucket="${key}">Reset</button>`;  // clears this section's ✓/✕ + size
    const cnt = MONO_BUCKETS.has(key)
      ? `<button type="button" class="hdrbtn szbtn" data-bucket="${key}" data-d="-1">-</button> ${inThis} / ${gr.length}${star} <button type="button" class="hdrbtn szbtn" data-bucket="${key}" data-d="1">+</button>`
      : `${inThis} / ${gr.length}${star}`;
    html+=`<tr class="bhead" id="bhead-${key}" style="background:${color}"><td colspan="${ncol+1}">${label} — ${cnt}${hdrBtn}</td></tr>`;
    const subOrdered=SUBORDERED.has(key); let prevSub=null;
    for(const r of gr){
      const inSel=sel.has(r);
      const stt=cardState[r['Name']]||0;
      const isSub  = inSel && stt!==1 && stt!==3 && !baseSet.has(r);   // pulled in only via bans/edits -> green
      const isBump = !inSel && stt!==2 && stt!==4 && baseSet.has(r);   // bumped out by a manual inclusion -> red
      const edited = (r['Name'] in ciOriginal) || r['Pick Rate']==='1.0';   // CI-edited or added(∞) -> purple
      const land=(r['Type Line']||'').toLowerCase().includes('land');
      let bg, cls;
      if(edited){ bg=EDIT_COLOR; cls='ciedit'+(inSel?'':' dim'); }
      else if(isSub){ bg=SUB_COLOR; cls='substitute'; }
      else if(isBump){ bg=BUMP_COLOR; cls='bumped'; }
      else {
        bg = land ? LANDS_COLOR                                                       // lands in mono sections -> lands color
           : (key==='Gold' && /talisman|signet/i.test(r['Name']||'')) ? COLORLESS_COLOR  // Gold Talismans/Signets -> colorless color
           : color;
        cls = inSel?'':'excluded';
      }
      if(subOrdered){ const si=subIndex(r); if(prevSub!==null && si!==prevSub) cls=(cls?cls+' ':'')+'subdiv'; prevSub=si; }
      const nm=esc(r['Name']??'');
      // currently included -> only "ban" (✕) is meaningful; currently excluded -> only "include" (✓)
      const tog = inSel ? 2 : 1;
      const tip = (stt!==0) ? 'Click to clear · right-click for stealth' : (inSel ? 'Included — click to ban (✕)' : 'Excluded — click to include (✓)');
      const triState = stt===3?1 : stt===4?2 : stt;   // stealth shows the same glyph
      const star = (stt===3||stt===4) ? '<span class="stealthmark">*</span>' : '';
      const stateCell=`<td class="statecell"><span class="tri s${triState}" data-card="${nm}" data-toggle="${tog}" title="${tip}"></span>${star}</td>`;
      html+='<tr style="background:'+bg+'"'+(cls?` class="${cls}"`:'')+'>'+stateCell+COLS.map(c=>{
        if(c==='Pick Rate' && r['Pick Rate']==='1.0') return `<td class="cieditable" data-rmcard="${esc(r['Name']??'')}" title="Right click to remove card">∞</td>`;
        const v=esc(fmtCell(c, r[c]??''));
        if(c==='Name') return `<td data-name="${v}" title="${v}">${v}</td>`;
        if(c==='Color Identity'){
          const ed=(r['Name'] in ciOriginal)?' ci-edited':'';
          return `<td class="cieditable${ed}" data-ci-card="${esc(r['Name']??'')}" title="Right click to edit">${v}</td>`;
        }
        return `<td title="${v}">${v}</td>`;
      }).join('')+'</tr>';
    }
  }
  for(let i=0;i<3;i++) html+=`<tr class="blankrow"><td colspan="${ncol+1}"></td></tr>`;  // scroll padding
  document.querySelector('#tbl tbody').innerHTML=html;
  document.getElementById('count').textContent=
    `${ROWS.length} cards · ${result.total} selected (greyed = excluded) · click a header to sort · hover a name for the image`;
  placeTopBtn();
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmtCell(c,v){   // display-only formatting (raw value/sort unaffected)
  if(c==='Elo' && v!==''){ const n=parseFloat(v); if(isFinite(n)) return String(Math.round(n)); }
  return v;
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}

function downloadBlob(blob,fname){
  const url=URL.createObjectURL(blob); const a=document.createElement('a');
  a.href=url; a.download=fname.endsWith('.csv')?fname:fname+'.csv'; a.click(); URL.revokeObjectURL(url);
}


document.getElementById('createBtn').onclick=async()=>{
  if(!ROWS.length){ toast('Load a CSV first'); return; }
  const b=document.getElementById('createBtn'); b.disabled=true;
  const orig=b.textContent; b.innerHTML='<span class="spin"></span>Building…';
  try{
    const result=computeSelected();
    const ordered=displayOrderedRows().filter(r=>result.set.has(r));   // selected, in display order
    const fname=document.getElementById('cfname').value||'category-build.csv';
    const res=await fetch('/api/write',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({columns:COLS, rows:ordered, filename:fname})});
    if(!res.ok){ toast('Export failed'); }
    else{ downloadBlob(await res.blob(), fname); toast('Exported '+ordered.length+' cards'+(result.warning?' — ⚠ '+result.warning:'')); }
  }catch(e){ toast('Export failed'); }
  b.disabled=false; b.textContent=orig;
};

// Hover card-image preview over Name cells.
const pv=document.getElementById('cardpreview');
function imgUrl(name){
  return IMAGES[name] || ('https://api.scryfall.com/cards/named?fuzzy='+encodeURIComponent(name)+'&format=image&version=normal');
}
const tbl=document.getElementById('tbl');
// toggle the included/banned state. Each card allows only its meaningful override:
// in-build cards toggle neutral<->ban(✕), out-of-build cards toggle neutral<->include(✓).
tbl.addEventListener('click',e=>{
  const t=e.target.closest('.tri'); if(!t) return;
  const name=t.dataset.card;
  // an existing override clears; a neutral card flips its current status (incl->ban, excl->include)
  cardState[name] = (cardState[name]||0)!==0 ? 0 : (parseInt(t.dataset.toggle)||1);
  render();
});

// Color Identity: right-click a cell to open the edit menu
tbl.addEventListener('contextmenu',e=>{
  const td=e.target.closest('[data-ci-card]'); if(!td) return;
  e.preventDefault();
  openCiMenu(td.dataset.ciCard, e.clientX, e.clientY);
});
document.getElementById('ciMenu').addEventListener('click',e=>{
  const it=e.target.closest('.ci-item'); if(!it||!ciMenuTarget) return;
  if(it.dataset.reset) resetCi(ciMenuTarget); else setCi(ciMenuTarget, it.dataset.val||'');
  closeCiMenu();
});
function closeMenus(){ closeCiMenu(); ['createMenu','cbMenu','navMenu','addMenu','rmMenu'].forEach(id=>document.getElementById(id).style.display='none'); cbMenuTarget=null; rmMenuTarget=null; }
function closeAddPanel(){ document.getElementById('addPanel').classList.remove('on'); }
document.addEventListener('click',e=>{
  if(!e.target.closest('#ciMenu,#createMenu,#cbMenu,#navMenu,#addMenu,#rmMenu,#topBtn,#addCardBtn')) closeMenus();
  if(!e.target.closest('#addPanel,#addCardBtn')) closeAddPanel();
});

// Right-click an ∞ (added card, Pick Rate 1.0) Pick Rate cell -> Remove Card
tbl.addEventListener('contextmenu',e=>{
  const td=e.target.closest('[data-rmcard]'); if(!td) return;
  e.preventDefault();
  closeMenus();
  rmMenuTarget=td.dataset.rmcard;
  const m=document.getElementById('rmMenu'); m.style.display='block';
  const w=m.offsetWidth||150,h=m.offsetHeight||40;
  m.style.left=Math.max(6,Math.min(e.clientX,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(e.clientY,innerHeight-h-6))+'px';
});
document.getElementById('rmMenu').addEventListener('click',e=>{
  if(!e.target.closest('[data-removecard]')||rmMenuTarget==null) return;
  removeCard(rmMenuTarget); closeMenus();
});
async function removeCard(name){
  showLoading(true,'Removing '+name+'…');
  try{
    const res=await fetch('/api/remove_card',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    if(res.ok){ const d=await res.json(); ROWS=d.rows||ROWS; IMAGES=d.images||IMAGES; render(); updateTotals(); toast('Removed '+name); }
    else toast('Remove failed');
  }catch(e){ toast('Remove failed'); }
  showLoading(false);
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ closeMenus(); closeAddPanel(); } });
document.querySelector('.main').addEventListener('scroll',closeMenus);

// Checkbox right-click menu: Stealth Include / Stealth Ban / Reset
tbl.addEventListener('contextmenu',e=>{
  const cell=e.target.closest('.statecell'); if(!cell) return;
  const tri=cell.querySelector('.tri'); if(!tri) return;
  e.preventDefault();
  openCbMenu(tri.dataset.card, e.clientX, e.clientY);
});
document.getElementById('cbMenu').addEventListener('click',e=>{
  const it=e.target.closest('.ci-item'); if(!it||cbMenuTarget==null) return;
  cardState[cbMenuTarget]=parseInt(it.dataset.cb)||0;   // 3 stealth✓ / 4 stealth✕ / 0 reset
  closeMenus(); render();
});

// Floating button: click -> top; right-click -> jump-to-section menu
document.getElementById('topBtn').addEventListener('click',()=>{
  document.querySelector('.main').scrollTo({top:0, behavior:'auto'});
});
document.getElementById('topBtn').addEventListener('contextmenu',e=>{
  e.preventDefault();
  closeMenus();
  const m=document.getElementById('navMenu'); m.style.display='block';
  const w=m.offsetWidth||190, h=m.offsetHeight||320;
  m.style.left=Math.max(6,Math.min(e.clientX,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(e.clientY,innerHeight-h-6))+'px';
});
document.getElementById('navMenu').addEventListener('click',e=>{
  const it=e.target.closest('[data-jump]'); if(!it) return;
  jumpToSection(it.dataset.jump); closeMenus();
});

// --- Add cards (+) button: autocomplete from the cached Scryfall card-name list ---
function renderSugg(q){
  const box=document.getElementById('addSugg');
  q=(q||'').trim().toLowerCase();
  if(!q || !cardNames){ box.innerHTML=''; return; }
  const starts=[], contains=[];
  for(const n of cardNames){ const l=n.toLowerCase();
    if(l.startsWith(q)) starts.push(n); else if(l.includes(q)) contains.push(n); }
  const list=starts.slice(0,12); if(list.length<12) list.push(...contains.slice(0,12-list.length));
  box.innerHTML=list.map(n=>`<div class="sugg" data-name="${esc(n)}">${esc(n)}</div>`).join('');
}
async function addCard(name){
  if(!name) return;
  showLoading(true,'Adding '+name+'…');
  try{
    const res=await fetch('/api/add_card',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    if(res.ok){ const d=await res.json(); ROWS=d.rows||ROWS; IMAGES=d.images||IMAGES; render(); updateTotals();
      toast('Added '+(d.added_card||name));
      const inp=document.getElementById('addInput'); inp.value=''; document.getElementById('addSugg').innerHTML=''; inp.focus(); }
    else toast('Card not found: '+name);
  }catch(e){ toast('Add failed'); }
  showLoading(false);
}
const addCardBtn=document.getElementById('addCardBtn');
addCardBtn.addEventListener('click',async()=>{
  const panel=document.getElementById('addPanel');
  if(panel.classList.contains('on')){ closeAddPanel(); return; }
  if(!ROWS.length){ toast('Load a CSV first'); return; }
  if(!cardNames){ showLoading(true,'Loading card names…'); try{ cardNames=(await (await fetch('/api/card_names')).json()).names||[]; }catch(e){ cardNames=[]; } showLoading(false); }
  placeTopBtn(); panel.classList.add('on');
  const inp=document.getElementById('addInput'); inp.value=''; document.getElementById('addSugg').innerHTML=''; inp.focus();
});
addCardBtn.addEventListener('contextmenu',e=>{
  e.preventDefault();
  closeMenus();
  const m=document.getElementById('addMenu'); m.style.display='block';
  const w=m.offsetWidth||150,h=m.offsetHeight||40;
  m.style.left=Math.max(6,Math.min(e.clientX,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(e.clientY,innerHeight-h-6))+'px';
});
document.getElementById('addInput').addEventListener('input',e=>renderSugg(e.target.value));
document.getElementById('addInput').addEventListener('keydown',e=>{
  if(e.key==='Enter'){ const f=document.querySelector('#addSugg .sugg'); if(f) addCard(f.dataset.name); }
  else if(e.key==='Escape') closeAddPanel();
});
document.getElementById('addSugg').addEventListener('click',e=>{ const s=e.target.closest('.sugg'); if(s) addCard(s.dataset.name); });
document.getElementById('addMenu').addEventListener('click',async e=>{
  if(!e.target.closest('[data-updatecache]')) return;
  closeMenus();
  showLoading(true,'Updating card-name cache from Scryfall…');
  try{ const d=await (await fetch('/api/update_card_names',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json(); cardNames=null; toast('Card cache updated ('+d.count+' names)'); }
  catch(e){ toast('Cache update failed'); }
  showLoading(false);
});

// Create button: right-click -> "Copy Plaintext" menu
document.getElementById('createBtn').addEventListener('contextmenu',e=>{
  e.preventDefault();
  closeMenus();
  const m=document.getElementById('createMenu'); m.style.display='block';
  const w=m.offsetWidth||140, h=m.offsetHeight||40;
  m.style.left=Math.max(6,Math.min(e.clientX,innerWidth-w-6))+'px';
  m.style.top=Math.max(6,Math.min(e.clientY,innerHeight-h-6))+'px';
});
document.getElementById('createMenu').addEventListener('click',e=>{
  if(e.target.closest('[data-savecsv]')){ saveCsv(); closeMenus(); }
  else if(e.target.closest('[data-copy]')){ copyPlaintext(); closeMenus(); }
});
// Save a full copy of the entire dataset (all rows + columns, incl. converted/added cards)
async function saveCsv(){
  if(!ROWS.length){ toast('Load a CSV first'); return; }
  const fname=(loadedName||'cube').replace(/\.csv$/i,'')+'_full.csv';
  try{
    const res=await fetch('/api/write',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({columns:COLS, rows:ROWS, filename:fname})});
    if(res.ok){ downloadBlob(await res.blob(), fname); toast('Saved '+ROWS.length+' cards — '+fname); }
    else toast('Save failed');
  }catch(e){ toast('Save failed'); }
}

// Gold header buttons: ban/reset Signets and Talismans (scoped to Gold cards)
tbl.addEventListener('click',e=>{
  const b=e.target.closest('.rockbtn'); if(!b) return;
  const re=new RegExp(b.dataset.pat,'i'), val=b.dataset.act==='reset'?0:2;
  let n=0;
  for(const r of ROWS){
    if(bucketOf(r)==='Gold' && re.test(r['Name']||'') && (cardState[r['Name']]||0)!==val){ cardState[r['Name']]=val; n++; }
  }
  render();
  toast((val?'Banned ':'Reset ')+n+' card'+(n===1?'':'s'));
});

// Section header +/- : adjust this color's size (offset to the Mono-Color quota)
tbl.addEventListener('click',e=>{
  const b=e.target.closest('.szbtn'); if(!b) return;
  const bk=b.dataset.bucket, d=parseInt(b.dataset.d)||0;
  const field=parseInt(document.getElementById('cat-Mono-Color').value)||0;
  const avail=ROWS.filter(r=>bucketOf(r)===bk).length;
  let off=(sizeOffset[bk]||0)+d;
  off=Math.max(-field, Math.min(off, avail-field));   // keep effective size in [0, available]
  sizeOffset[bk]=off;
  render();
});

// Section header "Reset": clear all ✓/✕ marks AND the size offset for that bucket
tbl.addEventListener('click',e=>{
  const b=e.target.closest('.sectreset'); if(!b) return;
  const bk=b.dataset.bucket; let n=0;
  for(const r of ROWS){ if(bucketOf(r)===bk && (cardState[r['Name']]||0)!==0){ cardState[r['Name']]=0; n++; } }
  delete sizeOffset[bk];
  render();
  toast('Reset '+n+' mark'+(n===1?'':'s')+' in '+(TYPE_LABELS[bk]||bk));
});
tbl.addEventListener('mouseover',e=>{
  const td=e.target.closest('td[data-name]'); if(!td)return;
  pv.src=imgUrl(td.dataset.name); pv.style.display='block';
});
tbl.addEventListener('mousemove',e=>{
  if(pv.style.display!=='block')return;
  let x=e.clientX+18, y=e.clientY+18;
  if(x+245>innerWidth) x=e.clientX-245-18;
  if(y+330>innerHeight) y=Math.max(8,innerHeight-338);
  pv.style.left=x+'px'; pv.style.top=y+'px';
});
tbl.addEventListener('mouseout',e=>{
  if(e.target.closest('td[data-name]')) pv.style.display='none';
});

load();
</script>
</body></html>"""


if __name__ == "__main__":
    print(f"Serving on http://localhost:{PORT}  (import a CSV in the browser)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
