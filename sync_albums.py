#!/usr/bin/env python3
"""
sync_albums.py (repo copy — used by the scheduled GitHub Action)

Syncs Cloudinary photo folders -> photography.html album pages on
omarkutbi.com. Cloudinary is the source of truth: every folder there
should have a matching album page and listing; anything removed from
Cloudinary gets removed from the site too.

Credentials come from environment variables (set as GitHub Actions
secrets by .github/workflows/sync-albums.yml) rather than being hardcoded
here, since this file lives in a public repo. For manual local runs,
Omar has a separate copy of this script with the credentials filled in
directly.

What it does, each run:
  1. Lists every folder in the Cloudinary account (each folder = one album).
  2. Compares that against local state (albums.json, sitting next to this
     script) to find folders that are new, and albums whose folder no
     longer exists in Cloudinary.
  3. For each NEW folder: samples a few images for EXIF capture date / GPS
     to auto-derive a date (and, if GPS is present, a place name via
     OpenStreetMap reverse geocoding). Falls back to a year found in the
     folder name, then "date unknown" if neither is available. Builds an
     album page listing every photo, optimized via Cloudinary's
     f_auto,q_auto,w_800 delivery transformation.
  4. For each REMOVED folder: deletes its album .html file and drops it
     from the listing.
  5. Rebuilds photography.html from the current album list, newest-first.
  6. Writes everything to disk next to this script (the repo checkout).
     Commit/push is handled by the workflow's git steps.

Run: python3 sync_albums.py
"""

import json
import os
import re
import sys
import base64
import urllib.request
import urllib.parse
import urllib.error

# ---------------------------------------------------------------------------
# Config — read from environment (GitHub Actions secrets). No credentials
# are hardcoded in this file since it lives in a public repo.
# ---------------------------------------------------------------------------

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
    sys.exit(
        "Missing Cloudinary credentials. Set CLOUDINARY_CLOUD_NAME, "
        "CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET as environment variables "
        "(GitHub Actions secrets in CI)."
    )

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}"


# ---------------------------------------------------------------------------
# Cloudinary
# ---------------------------------------------------------------------------

def cloudinary_get(path, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    auth = base64.b64encode(
        f"{CLOUDINARY_API_KEY}:{CLOUDINARY_API_SECRET}".encode()
    ).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_folders():
    data = cloudinary_get("/folders")
    return [f["path"] for f in data.get("folders", [])]


def list_folder_resources(folder):
    resources = []
    params = {"asset_folder": folder, "max_results": 500}
    while True:
        data = cloudinary_get("/resources/by_asset_folder", params)
        resources.extend(data.get("resources", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
        params["next_cursor"] = cursor
    return resources


def get_resource_detail(public_id):
    """Fetch a single resource with EXIF/metadata for date/GPS detection."""
    try:
        return cloudinary_get(
            f"/resources/image/upload/{urllib.parse.quote(public_id, safe='')}",
            {"exif": "true", "image_metadata": "true"},
        )
    except urllib.error.HTTPError:
        return {}


# ---------------------------------------------------------------------------
# Date / location detection
# ---------------------------------------------------------------------------

EXIF_DATE_KEYS = ["DateTimeOriginal", "DateTime", "CreateDate"]
YEAR_RE = re.compile(r"(19|20)\d{2}")
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def parse_exif_date(meta):
    for key in EXIF_DATE_KEYS:
        val = meta.get(key)
        if val:
            m = re.match(r"(\d{4}):(\d{2}):(\d{2})", val)
            if m:
                year, month, _ = m.groups()
                return int(year), int(month)
    return None


def parse_gps(meta):
    lat = meta.get("GPSLatitude")
    lon = meta.get("GPSLongitude")
    if lat and lon:
        return lat, lon
    return None


def reverse_geocode(lat, lon):
    """Best-effort place name via OSM Nominatim. Returns None on failure."""
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?format=json&lat={lat}&lon={lon}&zoom=10"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "omarkutbi.com album sync (personal site)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("state")
        country = addr.get("country")
        parts = [p for p in [city, country] if p]
        return ", ".join(parts) if parts else None
    except Exception:
        return None


def derive_date_and_location(folder, resources, sample_size=6):
    """
    Try, in order: EXIF capture date from a sample of photos in the folder,
    then a year found in the folder name. Returns (date_label, date_sort,
    location_or_None, source_str) — source_str is for the run summary.
    """
    sample = resources[:sample_size]
    for r in sample:
        detail = get_resource_detail(r["public_id"])
        meta = detail.get("image_metadata", {}) or {}
        exif = detail.get("exif", {}) or {}
        combined = {**exif, **meta}

        date = parse_exif_date(combined)
        gps = parse_gps(combined)
        location = reverse_geocode(*gps) if gps else None

        if date:
            year, month = date
            label = f"{MONTHS[month - 1]} {year}"
            sort_key = f"{year:04d}-{month:02d}-01"
            return label, sort_key, location, "exif"

    # Fallback: year from folder name, month unknown.
    m = YEAR_RE.search(folder)
    if m:
        year = int(m.group(0))
        return str(year), f"{year:04d}-01-01", None, "folder name"

    return "date unknown", "0000-00-00", None, "none"


# ---------------------------------------------------------------------------
# Local state (albums.json) + bootstrap
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(OUTPUT_DIR, "albums.json")
PHOTOGRAPHY_PATH = os.path.join(OUTPUT_DIR, "photography.html")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)

    albums = []
    if os.path.exists(PHOTOGRAPHY_PATH):
        with open(PHOTOGRAPHY_PATH) as f:
            html = f.read()
        for href, title, date_label in re.findall(
            r'<a href="([\w\-\.]+\.html)">([^<]+)</a><span class="date">([^<]*)</span>',
            html,
        ):
            if href == "album-template.html":
                continue
            slug = href[:-5]
            albums.append({
                "slug": slug,
                "title": title,
                "date_label": date_label,
                "date_sort": "0000-00-00",
                "folder": None,
            })
    return {"albums": albums}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def slugify(name):
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "album"


# ---------------------------------------------------------------------------
# Page generation
# ---------------------------------------------------------------------------

def album_image_tags(resources):
    def sort_key(r):
        m = re.search(r"-(\d+)_", r["public_id"])
        return int(m.group(1)) if m else 0

    ordered = sorted(resources, key=sort_key)
    tags = []
    for i, r in enumerate(ordered, 1):
        url = (
            f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/upload/"
            f"f_auto,q_auto,w_800/{r['public_id']}.{r['format']}"
        )
        tags.append(
            f'      <div class="photo-box"><img src="{url}" alt="photo {i}"></div>'
        )
    return "\n".join(tags)


ALBUM_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title} — omar kutbi</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div class="wrapper">
    <header>
      <nav>
        <a href="index.html">about</a>
        <a href="writing.html">writing</a>
        <a href="photography.html" class="current">photography</a>
      </nav>
    </header>

    <p><a href="photography.html">&larr; all albums</a></p>

    <h2>{title}</h2>
    <p class="note">{date_label}{location_suffix}</p>

    <div class="photo-grid">
{image_tags}
    </div>

  </div>
</body>
</html>
"""

PHOTOGRAPHY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>photography — omar kutbi</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div class="wrapper">
    <header>
      <nav>
        <a href="index.html">about</a>
        <a href="writing.html">writing</a>
        <a href="photography.html" class="current">photography</a>
      </nav>
    </header>

    <p class="note">albums, hosted here on the site.</p>

    <ul class="writing-list">
{album_rows}
    </ul>

  </div>
</body>
</html>
"""


def build_album_page(title, date_label, location, resources):
    location_suffix = f" — {location}" if location else ""
    return ALBUM_TEMPLATE.format(
        title=title,
        date_label=date_label,
        location_suffix=location_suffix,
        image_tags=album_image_tags(resources),
    )


def build_photography_page(albums_sorted):
    rows = []
    for a in albums_sorted:
        rows.append(
            f'      <li><a href="{a["slug"]}.html">{a["title"]}</a>'
            f'<span class="date">{a["date_label"]}</span></li>'
        )
    rows.append(
        '      <li><a href="album-template.html">[album name]</a>'
        '<span class="date">month year</span></li>'
    )
    return PHOTOGRAPHY_TEMPLATE.format(album_rows="\n".join(rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching Cloudinary folders...")
    folders = list_folders()
    print(f"  found {len(folders)}: {folders}")

    current_slugs = {slugify(f): f for f in folders}

    state = load_state()
    state_by_slug = {a["slug"]: a for a in state["albums"]}

    summary = []
    changed = False

    # --- removals: albums in state whose Cloudinary folder is gone ---
    for slug in list(state_by_slug.keys()):
        if slug not in current_slugs:
            album_path = os.path.join(OUTPUT_DIR, f"{slug}.html")
            if os.path.exists(album_path):
                os.remove(album_path)
                summary.append((state_by_slug[slug].get("folder", slug), slug,
                                 "folder removed from Cloudinary — page deleted"))
            else:
                summary.append((state_by_slug[slug].get("folder", slug), slug,
                                 "folder removed from Cloudinary — listing dropped"))
            del state_by_slug[slug]
            changed = True

    # --- additions: Cloudinary folders not yet on the site ---
    for slug, folder in current_slugs.items():
        if slug in state_by_slug:
            summary.append((folder, slug, "already listed — skipped"))
            continue

        print(f"New folder detected: '{folder}' -> generating '{slug}.html'")
        resources = list_folder_resources(folder)
        if not resources:
            summary.append((folder, slug, "no images found — skipped"))
            continue

        date_label, date_sort, location, source = derive_date_and_location(
            folder, resources
        )
        title = re.sub(r"\s*-?\s*(19|20)\d{2}\s*$", "", folder).strip().lower() or slug

        page = build_album_page(title, date_label, location, resources)
        with open(os.path.join(OUTPUT_DIR, f"{slug}.html"), "w") as f:
            f.write(page)

        state_by_slug[slug] = {
            "slug": slug,
            "title": title,
            "date_label": date_label,
            "date_sort": date_sort,
            "folder": folder,
        }
        summary.append((
            folder, slug,
            f"created — {len(resources)} photos, date via {source}"
            + (f", location: {location}" if location else "")
        ))
        changed = True

    # --- rebuild photography.html + state from what's left ---
    albums_sorted = sorted(
        state_by_slug.values(), key=lambda a: a["date_sort"], reverse=True
    )
    new_photography_html = build_photography_page(albums_sorted)

    if os.path.exists(PHOTOGRAPHY_PATH):
        with open(PHOTOGRAPHY_PATH) as f:
            old_photography_html = f.read()
    else:
        old_photography_html = None

    if new_photography_html != old_photography_html:
        with open(PHOTOGRAPHY_PATH, "w") as f:
            f.write(new_photography_html)
        changed = True

    state["albums"] = list(albums_sorted)
    save_state(state)

    print("\n--- Run summary ---")
    for folder, slug, note in summary:
        print(f"  {folder!r:30s} -> {slug:20s} {note}")

    print(f"\nphotography.html {'updated' if changed else 'unchanged'}.")
    print("(the workflow's git step checks for actual file changes before committing)")


if __name__ == "__main__":
    main()
