# Draft Simulator Cube Prototyper

A lightweight, single‑file web tool for **prototyping Magic: The Gathering cubes** from draft pick‑data. Load a CSV of cards, and the app sorts them into colored buckets, pulls type lines and card images from [Scryfall](https://scryfall.com), and lets you build a balanced "prototype cube" by picking the top *N* cards per color/category — with fine‑grained manual overrides, live previews, and one‑click export.

> Built for cube designers who want to turn raw draft‑log statistics into a tuned, balanced card list.

---

## Table of contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Loading data](#loading-data)
- [The buckets](#the-buckets)
- [Building a prototype cube](#building-a-prototype-cube)
- [Per‑card overrides](#per-card-overrides)
- [Editing & adding cards](#editing--adding-cards)
- [Exporting](#exporting)
- [Mouse & keyboard reference](#mouse--keyboard-reference)
- [CSV format](#csv-format)
- [How it works](#how-it-works)
- [Files & caches](#files--caches)
- [Acknowledgements & license](#acknowledgements--license)

---

## What it does

- **Sorts your cards into 8 color buckets** (White, Blue, Black, Red, Green, Colorless/Artifact, Gold/Multicolor, Lands) using each card's color identity and type line.
- **Enriches cards from Scryfall** — adds a Type Line column and shows the card image when you hover a name. Everything is cached so each card is only fetched once.
- **Builds a "prototype cube"** by taking the top *N* cards of each category (e.g. *top 50 of each color*, *top 5 of each guild pair*), ranked by any column you choose (Pick Rate, Elo, etc.).
- **Live, color‑coded preview** of exactly which cards make the cut, with manual overrides that re‑balance automatically.
- **Exports** the finished cube as CSV, copies the card list as plaintext, or saves a full working copy.

No build step, no database, no third‑party Python packages — just a Python script and your browser.

---

## Requirements

- **Python 3.8+** (uses only the standard library)
- A web browser
- An internet connection on first use (to fetch card data from Scryfall; cached afterward)

---

## Quick start

From the project folder:

```bash
python app.py
```

Then open **http://localhost:8003** in your browser.

**Windows convenience scripts** are included:

- `start.bat` — launches the server and opens your browser. While it's running, press **SPACE** in the console to quit, or **BACKSPACE** to restart the server (handy after editing the code).
- `stop.bat` — stops the server / frees port 8003.

Nothing loads automatically — import a CSV to begin (see below).

---

## Loading data

You can load a card CSV three ways:

| Method | How |
| --- | --- |
| **Import CSV…** | Header button → pick a file from anywhere on disk |
| **Load last CSV** | Reopens the most recently loaded file |
| **Drag & drop** | Drop a `.csv` anywhere on the window |

On load, the app automatically fetches each card's **Type Line** and **color identity image** from Scryfall (a brief loading overlay appears the first time; subsequent loads are instant from cache).

### Real pick‑data auto‑conversion

If your CSV uses the raw draft‑log "postmodern" header —

```
card, elo, mainboard, pickrate, picks, mainboards
```

— it's **automatically converted** to the app's format on import (color identities pulled from Scryfall, "Times Seen" computed from picks ÷ pickrate). A converted copy is saved to `realdata-conversion/converted/` so you can reuse it.

---

## The buckets

Cards are partitioned into these sections, in order:

1. **White / Blue / Black / Red / Green** — single‑color cards *and single‑color lands of that color*
2. **Colorless / Artifact** — colorless non‑land cards
3. **Gold (multicolor)** — non‑land cards with 2+ colors
4. **Lands** — non‑mono lands (0 or 2+ colors)

Within **Gold** and **Lands**, cards are sub‑grouped by guild color pair (Azorius, Dimir, … Simic), then 3+‑color, then colorless, separated by divider lines. Each section header shows `selected / total` and has its own controls.

---

## Building a prototype cube

The **Build by category** panel (left sidebar) defines how many cards to keep from each category:

| Category | Meaning |
| --- | --- |
| **Mono‑Color** | top *X* of **each** color (W/U/B/R/G) |
| **Color‑Pair** | top *X* of **each** of the 10 guild pairs (non‑land) |
| **Many‑Color** | top *X* of 3+‑color non‑lands |
| **Artifacts‑Colorless** | top *X* of colorless non‑lands |
| **Dual‑Lands** | top *X* of **each** 2‑color land pair |
| **Gold‑Lands** | top *X* of 3+‑color lands |
| **Colorless‑Lands** | top *X* of colorless lands |

- **Rank by** any numeric column (default **Pick Rate**, high→low). This mirrors the table's sort — clicking a column header re‑ranks everything.
- **MAX** sets a field to the largest value that every sub‑bucket can fully supply (no shortfall).
- **ALL** includes every card; **Reset all to defaults** restores the starting values.
- A **live total** updates as you type. A field turns **red** when you've asked for more cards than are available.

The selection is previewed instantly in the table:

| Highlight | Meaning |
| --- | --- |
| Normal (lit) | included in the cube |
| **Greyed out** | excluded (didn't make the top‑*N*) |
| **Green** | a substitute pulled in only because you banned something |
| **Red** | bumped out because a lower card was manually included |
| **Purple** | color identity was edited, or it's a manually added card |

---

## Per‑card overrides

The first column is a tri‑state **include/ban** checkbox:

- On an **included** card → click toggles **✕ (ban)**.
- On an **excluded** card → click toggles **✓ (include)**.

Manual includes/bans are **compensated automatically** to keep the total steady — banning a card pulls in the next one down; force‑including a card drops the weakest one. The swap cascades *same category → same display bucket → whole list*, and warns only when nothing can be swapped.

**Per‑color size:** mono‑color headers have **− / +** buttons to make individual colors larger or smaller than the others (a `*` marks any changed/overridden section).

**Stealth mode** (right‑click a checkbox): **Stealth Include** / **Stealth Ban** force a card in/out **without** any compensating swap (the total simply changes by one). Stealth marks show a `*` next to the checkbox. **Reset** clears the cell.

Each section header also has a **Reset** that clears that section's marks and size override.

---

## Editing & adding cards

- **Edit color identity:** right‑click a Color Identity cell → choose a color, guild, colorless, or *Reset to default*. The card instantly re‑buckets. Edited cells show in purple.
- **Add cards:** the **+** button (bottom‑right) opens a search box with Scryfall **autocomplete**. Pick a card and it's added to the cube with its real color identity and type line. Added cards get a Pick Rate shown as **∞** (so they rank at the top) and a purple row. Right‑click the **+** to **Update Cache** (refresh the card‑name list).
- **Remove an added card:** right‑click its **∞** Pick Rate cell → **Remove Card**.

---

## Exporting

The **Create prototype cube** button (top‑right) downloads the **selected** cards as a CSV. Right‑click it for more options:

- **Save CSV** — saves the **entire** dataset (all rows + columns, including conversions and added cards) as `<name>_full.csv`, ready to re‑import.
- **Copy Plaintext** — copies all included card names to the clipboard, one per line.

All downloads go to your browser's Downloads folder.

---

## Mouse & keyboard reference

| Action | Result |
| --- | --- |
| Click a column header | Sort all buckets by that column |
| Hover a card name | Show the card image |
| Click checkbox (col 1) | Toggle include / ban |
| Right‑click checkbox | Stealth Include / Stealth Ban / Reset |
| Right‑click Color Identity cell | Edit color identity |
| Right‑click ∞ cell | Remove (added) card |
| Right‑click **+** button | Update card‑name cache |
| Right‑click **Create prototype cube** | Save CSV / Copy Plaintext |
| Floating ⬆ button | Click: scroll to top · Right‑click: jump to a section |
| `Esc` | Close any open menu / the add‑card box |

---

## CSV format

The app's native format has these columns (only **Name** is strictly required; the rest power ranking and stats):

```
Name, Color Identity, Times Seen, Times Picked, Pick Rate, Avg Pick Position, Wheel Count, P1P1 Count, Elo
```

- **Color Identity** is a string of `W U B R G` letters (any order; empty = colorless). If omitted/blank, cards fall into Colorless/Lands until you edit them.
- **Type Line** is added automatically from Scryfall.
- The raw **postmodern** header (`card, elo, mainboard, pickrate, picks, mainboards`) is auto‑converted on import.

---

## How it works

- A **single Python file** (`app.py`) runs a stdlib `http.server` on port 8003 and serves a self‑contained HTML/CSS/JS page — there is no front‑end framework or build tooling.
- The **selection logic runs in the browser**, so the live preview and the export always match exactly. The server only loads/serves data, talks to Scryfall, and writes files.
- **Scryfall** provides type lines, color identities, card images, and the autocomplete catalog. Results are cached to disk so the tool works offline after the first fetch and stays polite to the API.

---

## Files & caches

Generated alongside the app:

| File / folder | Purpose |
| --- | --- |
| `scryfall_cache.json` | Type lines + image URLs per card |
| `card_names_cache.json` | Scryfall card‑name catalog (for + autocomplete) |
| `last_csv.csv`, `last_meta.json` | The most recently loaded dataset (powers "Load last") |
| `realdata-conversion/` | The postmodern→native converter and its `converted/` output + cache |

These are safe to delete; they'll be regenerated on demand.

---

## Acknowledgements & license

- Card data, color identities, type lines, and images courtesy of **[Scryfall](https://scryfall.com)**. This project is unofficial and not affiliated with or endorsed by Scryfall or Wizards of the Coast. *Magic: The Gathering* is © Wizards of the Coast.
- Please respect [Scryfall's API guidelines](https://scryfall.com/docs/api) — the tool caches aggressively and throttles requests to that end.

**License:** released for public use — add the license of your choice (MIT recommended) as a `LICENSE` file.

Built by Dazey