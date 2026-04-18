#!/usr/bin/env python3
"""
GooglePhotos EXIF Mover
-----------------------
Read a Google Takeout export, inject the metadata from each photo's JSON
sidecar into the photo itself (EXIF / XMP / QuickTime), then organise the
result into a YYYY/MM/DD/ folder tree, so a slideshow that relies on file
order or EXIF date can play the photos chronologically.

Usage:
    python3 organize_photos.py                 # scans current folder, processes
    python3 organize_photos.py --dry-run       # analyse only, write report
    python3 organize_photos.py --source PATH --dest PATH
    python3 organize_photos.py --verbose

The script is idempotent: running it again on the same folder only processes
new files. Safe to run on a recurring basis as you add more Takeouts.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

LISBON = ZoneInfo("Europe/Lisbon")
UTC = timezone.utc

# Dates before this year or in the future are considered suspicious
# and routed to the _no_date folder for manual review.
SENTINEL_YEAR_MIN = 1995

# Magic numbers to detect the real file format (Google Photos often
# converts HEIC/RAW to JPEG while keeping the original extension).
MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
MAGIC_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"heif", b"mif1", b"msf1"}
MAGIC_MP4_BRANDS = {b"isom", b"mp42", b"mp41", b"avc1", b"iso2", b"iso4", b"iso5", b"M4V ", b"dash"}
MAGIC_MOV_BRAND = b"qt  "
MAGIC_GIF_87A = b"GIF87a"
MAGIC_GIF_89A = b"GIF89a"
MAGIC_BMP = b"BM"

# Suffixes Google Takeout appends between the media filename and ".json".
# Takeout truncates "supplemental-metadata" to fit the 50-char zip-entry limit,
# so we match anything starting with "supp" (plus the empty case).

# The "edited" marker Google appends (localised). If present, the edited
# version shares the JSON sidecar of the original.
EDITED_MARKERS = ("-editada", "-editado", "-edited", "-EDIT")

# Project-owned files we never want to treat as media.
IGNORE_NAMES = {
    "DRY_RUN_REPORT.md",
    "REPORT.md",
    "_log.csv",
    "README.md",
    "LICENSE",
}
IGNORE_EXTS = {".tar", ".gz", ".tgz", ".md", ".py", ".csv", ".txt", ".gitignore"}
# Folders we never descend into (both destination output and dev stuff).
IGNORE_DIRS = {"Organized", ".git", "__pycache__"}


def locate_exiftool() -> str:
    """Find the exiftool executable. Search order:
    1. EXIFTOOL environment variable
    2. 'exiftool' on PATH
    3. ./Image-ExifTool-*/exiftool next to the script (or cwd)
    4. Auto-extract ./Image-ExifTool-*.tar.gz if only the tarball is present
    """
    env = os.environ.get("EXIFTOOL")
    if env and Path(env).is_file():
        return env

    found = shutil.which("exiftool")
    if found:
        return found

    search_dirs = [Path(__file__).parent, Path.cwd()]
    for d in search_dirs:
        for cand in glob.glob(str(d / "Image-ExifTool-*" / "exiftool")):
            if Path(cand).is_file():
                return cand

    for d in search_dirs:
        for tar in glob.glob(str(d / "Image-ExifTool-*.tar.gz")):
            print(f"-> Extracting {tar} ...")
            with tarfile.open(tar) as t:
                t.extractall(d)
            for cand in glob.glob(str(d / "Image-ExifTool-*" / "exiftool")):
                if Path(cand).is_file():
                    return cand

    raise RuntimeError(
        "exiftool not found. Install it (apt install libimage-exiftool-perl, "
        "brew install exiftool, or download from exiftool.org) or place the "
        "Image-ExifTool-X.Y.tar.gz next to this script."
    )


EXIFTOOL_PATH: Optional[str] = None  # set in main()


@dataclass
class MediaFile:
    path: Path
    json_path: Optional[Path] = None
    photo_taken_ts: Optional[int] = None  # epoch seconds UTC
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None
    description: str = ""
    favorited: bool = False
    people: list[str] = field(default_factory=list)
    real_format: str = ""  # jpeg / png / heic / mp4 / mov / gif / bmp / unknown
    error: str = ""


# ---------------------------------------------------------------------------
# Pairing: media file -> JSON sidecar
# ---------------------------------------------------------------------------

def _base_for_edited(stem: str) -> Optional[str]:
    """If stem ends with an 'edited' marker, return the base stem."""
    for mk in EDITED_MARKERS:
        if stem.endswith(mk):
            return stem[: -len(mk)]
    return None


def find_json_for(media: Path) -> Optional[Path]:
    """Locate the Takeout JSON sidecar for a media file.

    Handles every pattern I have seen in the wild:
      - NAME.EXT.supplemental-metadata.json
      - NAME.EXT.<anything starting with 'supp'>.json  (Takeout truncation)
      - NAME(n).EXT  <-  NAME.EXT.supplemental-metadata(n).json
      - NAME-edited.EXT  <-  shares NAME.EXT's JSON
      - NAME.EXT  <-  very aggressive truncation yielding just NAME.json
      - Live Photo MP4/MOV  <-  sibling HEIC/JPG's JSON
    """
    parent = media.parent
    name = media.name
    stem = media.stem
    ext = media.suffix

    # 1. Generic match: NAME.EXT<.supp...>.json
    prefix = name + "."
    for f in parent.iterdir():
        if not f.name.startswith(prefix) or f.suffix.lower() != ".json":
            continue
        middle = f.name[len(prefix):-5]
        if middle == "" or middle.startswith("supp"):
            return f

    # 2. Takeout duplicates: FOO(n).EXT  <->  FOO.EXT.<supp*>(n).json
    m = re.match(r"^(?P<base>.+?)\((?P<n>\d+)\)$", stem)
    if m:
        base, n = m.group("base"), m.group("n")
        prefix2 = f"{base}{ext}."
        suffix2 = f"({n}).json"
        for f in parent.iterdir():
            if not f.name.startswith(prefix2) or not f.name.endswith(suffix2):
                continue
            middle = f.name[len(prefix2):-len(suffix2)]
            if middle.startswith("supp"):
                return f

    # 3. Edited variants share the original's JSON
    base_stem = _base_for_edited(stem)
    if base_stem is not None:
        original_name = base_stem + ext
        prefix3 = original_name + "."
        for f in parent.iterdir():
            if not f.name.startswith(prefix3) or f.suffix.lower() != ".json":
                continue
            middle = f.name[len(prefix3):-5]
            if middle == "" or middle.startswith("supp"):
                return f

    # 4. Very aggressive truncation: JSON basename is a prefix of the media name
    best = None
    for f in parent.iterdir():
        if f.suffix.lower() != ".json":
            continue
        basename = f.name[:-5]
        if len(basename) < 8:
            continue
        if name.startswith(basename):
            if best is None or len(f.name) > len(best.name):
                best = f
    if best is not None:
        return best

    # 5. Live Photo: MP4/MOV inherits metadata from sibling HEIC/JPG
    if ext.lower() in (".mp4", ".mov"):
        for sibling_ext in (".HEIC", ".heic", ".JPG", ".jpg", ".JPEG", ".jpeg"):
            sibling = parent / (stem + sibling_ext)
            if sibling.exists():
                for cand in parent.iterdir():
                    if not cand.name.startswith(sibling.name + ".") or cand.suffix.lower() != ".json":
                        continue
                    middle = cand.name[len(sibling.name) + 1:-5]
                    if middle == "" or middle.startswith("supp"):
                        return cand

    return None


def sniff_format(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except Exception:
        return "unknown"

    if head.startswith(MAGIC_JPEG):
        return "jpeg"
    if head.startswith(MAGIC_PNG):
        return "png"
    if head.startswith(MAGIC_GIF_87A) or head.startswith(MAGIC_GIF_89A):
        return "gif"
    if head.startswith(MAGIC_BMP):
        return "bmp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in MAGIC_HEIC_BRANDS:
            return "heic"
        if brand == MAGIC_MOV_BRAND:
            return "mov"
        if brand in MAGIC_MP4_BRANDS:
            return "mp4"
        return "mp4"
    return "unknown"


def parse_json(json_path: Path) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_media(source: Path, dest: Path) -> list[Path]:
    """Walk source recursively, yielding media files only.

    Skips JSON sidecars, the destination tree, the script/readme files and
    any ExifTool payload that might be colocated.
    """
    media: list[Path] = []
    dest_resolved = dest.resolve()
    for root, dirs, files in os.walk(source):
        # Prune ignored folders + the destination folder
        root_p = Path(root).resolve()
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRS
            and not d.startswith("Image-ExifTool-")
            and (root_p / d).resolve() != dest_resolved
        ]
        for fn in files:
            if fn in IGNORE_NAMES:
                continue
            p = Path(root) / fn
            suf = p.suffix.lower()
            if suf == ".json":
                continue
            if suf in IGNORE_EXTS:
                continue
            if fn.startswith("Image-ExifTool"):
                continue
            media.append(p)
    return media


def gather(source: Path, dest: Path) -> list[MediaFile]:
    result: list[MediaFile] = []
    for m in iter_media(source, dest):
        mf = MediaFile(path=m)
        mf.json_path = find_json_for(m)
        mf.real_format = sniff_format(m)

        if mf.json_path:
            try:
                data = parse_json(mf.json_path)
                ts_str = (data.get("photoTakenTime") or {}).get("timestamp")
                if ts_str:
                    ts = int(ts_str)
                    if ts > 0:
                        mf.photo_taken_ts = ts
                geo = data.get("geoData") or {}
                lat = geo.get("latitude")
                lon = geo.get("longitude")
                alt = geo.get("altitude")
                if lat and lon and not (lat == 0.0 and lon == 0.0):
                    mf.lat, mf.lon, mf.alt = lat, lon, alt
                mf.description = (data.get("description") or "").strip()
                mf.favorited = bool(data.get("favorited"))
                mf.people = [p.get("name", "") for p in (data.get("people") or []) if p.get("name")]
            except Exception as e:
                mf.error = f"json-parse: {e}"

        result.append(mf)
    return result


# ---------------------------------------------------------------------------
# Date logic + target-path computation
# ---------------------------------------------------------------------------

def is_sentinel(ts: int) -> bool:
    dt = datetime.fromtimestamp(ts, UTC)
    return dt.year < SENTINEL_YEAR_MIN or dt > datetime.now(UTC)


def compute_target(mf: MediaFile, dest: Path) -> Path:
    if mf.photo_taken_ts is None or is_sentinel(mf.photo_taken_ts):
        return dest / "_no_date" / mf.path.name
    dt_local = datetime.fromtimestamp(mf.photo_taken_ts, UTC).astimezone(LISBON)
    folder = dest / f"{dt_local.year:04d}" / f"{dt_local.month:02d}" / f"{dt_local.day:02d}"
    return folder / mf.path.name


# ---------------------------------------------------------------------------
# Reporting (dry-run)
# ---------------------------------------------------------------------------

def summarize(items: list[MediaFile]) -> dict:
    paired = sum(1 for m in items if m.json_path and m.photo_taken_ts)
    orphans = [m for m in items if not (m.json_path and m.photo_taken_ts)]
    by_format = Counter(m.real_format for m in items)
    by_ext = Counter(m.path.suffix.lower().lstrip(".") for m in items)
    by_year = Counter()
    for m in items:
        if m.photo_taken_ts:
            y = datetime.fromtimestamp(m.photo_taken_ts, UTC).astimezone(LISBON).year
            by_year[y] += 1
    sentinels = [
        m for m in items
        if m.photo_taken_ts and is_sentinel(m.photo_taken_ts)
    ]
    return {
        "total": len(items),
        "paired": paired,
        "orphans": orphans,
        "by_format": by_format,
        "by_ext": by_ext,
        "by_year": by_year,
        "sentinels": sentinels,
    }


def write_dry_run_report(s: dict, source: Path, dest: Path, report_path: Path,
                         items: list[MediaFile]):
    import random
    lines = []
    lines.append("# Dry-run report — Google Photos EXIF Mover")
    lines.append("")
    lines.append(f"- Generated: {datetime.now(LISBON).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- Source: `{source}`")
    lines.append(f"- Destination: `{dest}`")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Media files found: **{s['total']}**")
    lines.append(f"- Paired with valid `photoTakenTime`: **{s['paired']}**")
    lines.append(f"- Orphans (no JSON / no valid timestamp): **{len(s['orphans'])}**")
    lines.append(f"- Sentinel dates (< {SENTINEL_YEAR_MIN} or in the future): **{len(s['sentinels'])}**")
    lines.append("")
    lines.append("## Real format distribution (magic-byte sniffed)")
    lines.append("")
    lines.append("| Real format | Count |")
    lines.append("|---|---:|")
    for fmt, n in sorted(s["by_format"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {fmt or '(empty)'} | {n} |")
    lines.append("")
    lines.append("## Extension distribution")
    lines.append("")
    lines.append("| Extension | Count |")
    lines.append("|---|---:|")
    for ext, n in sorted(s["by_ext"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| .{ext} | {n} |")
    lines.append("")
    lines.append("## Photos per year (Lisbon time)")
    lines.append("")
    lines.append("| Year | Count |")
    lines.append("|---|---:|")
    for year in sorted(s["by_year"].keys()):
        lines.append(f"| {year} | {s['by_year'][year]} |")
    lines.append("")

    if s["sentinels"]:
        lines.append("## Suspicious dates (sent to `_no_date/`)")
        lines.append("")
        for m in s["sentinels"][:30]:
            dt = datetime.fromtimestamp(m.photo_taken_ts, UTC).astimezone(LISBON)
            lines.append(f"- `{m.path.name}` -> {dt.isoformat()}")
        if len(s["sentinels"]) > 30:
            lines.append(f"- ... (+{len(s['sentinels']) - 30} more)")
        lines.append("")

    lines.append("## Orphans (will go to `_no_date/`)")
    lines.append("")
    if not s["orphans"]:
        lines.append("_None_")
    else:
        for m in s["orphans"][:50]:
            reason = "no JSON" if not m.json_path else "no valid timestamp"
            lines.append(f"- `{m.path.name}` ({reason})")
        if len(s["orphans"]) > 50:
            lines.append(f"- ... (+{len(s['orphans']) - 50} more)")
    lines.append("")

    lines.append("## Sample of 20 predicted target paths")
    lines.append("")
    paired_items = [m for m in items if m.photo_taken_ts and not is_sentinel(m.photo_taken_ts)]
    sample = random.Random(42).sample(paired_items, k=min(20, len(paired_items)))
    for m in sample:
        tgt = compute_target(m, dest)
        try:
            rel = tgt.relative_to(dest)
            lines.append(f"- `{m.path.name}` -> `{dest.name}/{rel}`")
        except ValueError:
            lines.append(f"- `{m.path.name}` -> `{tgt}`")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# EXIF injection
# ---------------------------------------------------------------------------

def exiftool_args_for(mf: MediaFile) -> list[str]:
    if mf.photo_taken_ts is None:
        return []
    dt_utc = datetime.fromtimestamp(mf.photo_taken_ts, UTC)
    dt_str = dt_utc.strftime("%Y:%m:%d %H:%M:%S")
    args = [
        f"-AllDates={dt_str}",
        f"-EXIF:DateTimeOriginal={dt_str}",
        f"-EXIF:CreateDate={dt_str}",
        f"-EXIF:ModifyDate={dt_str}",
        "-EXIF:OffsetTimeOriginal=+00:00",
        "-EXIF:OffsetTimeDigitized=+00:00",
        "-EXIF:OffsetTime=+00:00",
        f"-XMP:DateTimeOriginal={dt_str}",
        f"-QuickTime:CreateDate={dt_str}",
        f"-QuickTime:ModifyDate={dt_str}",
        f"-QuickTime:TrackCreateDate={dt_str}",
        f"-QuickTime:TrackModifyDate={dt_str}",
        f"-QuickTime:MediaCreateDate={dt_str}",
        f"-QuickTime:MediaModifyDate={dt_str}",
    ]
    if mf.lat is not None and mf.lon is not None:
        args += [
            f"-GPSLatitude={abs(mf.lat)}",
            f"-GPSLatitudeRef={'N' if mf.lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(mf.lon)}",
            f"-GPSLongitudeRef={'E' if mf.lon >= 0 else 'W'}",
        ]
        if mf.alt is not None:
            args += [
                f"-GPSAltitude={abs(mf.alt)}",
                f"-GPSAltitudeRef={'0' if mf.alt >= 0 else '1'}",
            ]
    if mf.description:
        args += [
            f"-EXIF:ImageDescription={mf.description}",
            f"-XMP:Description={mf.description}",
        ]
    if mf.favorited:
        args += ["-XMP:Rating=5"]
    if mf.people:
        for person in mf.people:
            args += [f"-XMP:Subject+={person}"]
    return args


def run_exiftool(target: Path, args: list[str]) -> Optional[str]:
    cmd = [EXIFTOOL_PATH, "-overwrite_original", "-P"] + args + [str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return proc.stderr.strip() or proc.stdout.strip() or "exiftool nonzero"
    except Exception as e:
        return f"exiftool exception: {e}"
    return None


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    stem, suf = p.stem, p.suffix
    while True:
        cand = p.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def already_processed(mf: MediaFile, dest: Path) -> Optional[Path]:
    """True if this source file has already been copied into dest."""
    tgt = compute_target(mf, dest)
    src_size = mf.path.stat().st_size
    if tgt.exists() and tgt.stat().st_size == src_size:
        return tgt
    for i in range(1, 10):
        cand = tgt.with_name(f"{tgt.stem}_{i}{tgt.suffix}")
        if cand.exists() and cand.stat().st_size == src_size:
            return cand
    return None


def execute(items: list[MediaFile], dest: Path, log_path: Path,
            verbose: bool = False) -> Counter:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "_tmp"
    tmp.mkdir(exist_ok=True)
    no_date = dest / "_no_date"
    no_date.mkdir(exist_ok=True)

    total = len(items)
    counters: Counter = Counter()

    first_time = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        log = csv.writer(f)
        if first_time:
            log.writerow(["timestamp", "source", "target", "status", "details"])

        for i, mf in enumerate(items, 1):
            if verbose and i % 100 == 0:
                print(f"  {i}/{total} ...")

            now_iso = datetime.now(LISBON).strftime("%Y-%m-%d %H:%M:%S")

            if already_processed(mf, dest):
                counters["skip"] += 1
                continue

            # Orphan / sentinel branch
            if mf.photo_taken_ts is None or is_sentinel(mf.photo_taken_ts):
                tgt = unique_path(no_date / mf.path.name)
                try:
                    shutil.copy2(mf.path, tgt)
                    reason = "no timestamp" if mf.photo_taken_ts is None else "suspicious-timestamp"
                    log.writerow([now_iso, str(mf.path), str(tgt.relative_to(dest)),
                                  "orphan", mf.error or reason])
                    counters["orphan"] += 1
                except Exception as e:
                    log.writerow([now_iso, str(mf.path), "", "error", f"copy: {e}"])
                    counters["error"] += 1
                continue

            staging = unique_path(tmp / mf.path.name)
            try:
                shutil.copy2(mf.path, staging)
            except Exception as e:
                log.writerow([now_iso, str(mf.path), "", "error", f"copy: {e}"])
                counters["error"] += 1
                continue

            err = run_exiftool(staging, exiftool_args_for(mf))
            status = "ok" if not err else "exif-warn"

            try:
                os.utime(staging, (mf.photo_taken_ts, mf.photo_taken_ts))
            except Exception as e:
                if not err:
                    err = f"utime: {e}"

            final = unique_path(compute_target(mf, dest))
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(staging), str(final))
            except Exception as e:
                log.writerow([now_iso, str(mf.path), "", "error", f"move: {e}"])
                counters["error"] += 1
                continue

            log.writerow([now_iso, str(mf.path), str(final.relative_to(dest)),
                          status, err or ""])
            counters[status] += 1

    try:
        tmp.rmdir()
    except OSError:
        pass

    return counters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global EXIFTOOL_PATH

    ap = argparse.ArgumentParser(
        description="Organise a Google Takeout photo dump into YYYY/MM/DD/ "
                    "with EXIF data injected from the JSON sidecars."
    )
    ap.add_argument("--source", default=".",
                    help="Folder containing the Takeout export (scanned recursively). "
                         "Default: current directory.")
    ap.add_argument("--dest", default=None,
                    help="Destination folder. Default: <source>/Organizadas")
    ap.add_argument("--dry-run", action="store_true",
                    help="Analyse only; write DRY_RUN_REPORT.md without touching files.")
    ap.add_argument("--verbose", action="store_true",
                    help="Print progress every 100 files.")
    args = ap.parse_args()

    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve() if args.dest else source / "Organized"

    if not args.dry_run:
        EXIFTOOL_PATH = locate_exiftool()
        print(f"-> ExifTool: {EXIFTOOL_PATH}")

    print(f"-> Scanning {source} (recursive) ...")
    items = gather(source, dest)
    print(f"  {len(items)} media files found.")

    s = summarize(items)
    print(f"  With timestamp: {s['paired']} | Orphans: {len(s['orphans'])} | "
          f"Sentinels: {len(s['sentinels'])}")

    if args.dry_run:
        report = source / "DRY_RUN_REPORT.md"
        write_dry_run_report(s, source, dest, report, items)
        print(f"OK  Report: {report}")
        return

    log_path = dest / "_log.csv"
    dest.mkdir(parents=True, exist_ok=True)
    counters = execute(items, dest, log_path, verbose=args.verbose)
    print("Done.")
    print(f"  New OK:              {counters.get('ok', 0)}")
    print(f"  New with EXIF warn:  {counters.get('exif-warn', 0)}")
    print(f"  Orphans/sentinels:   {counters.get('orphan', 0)}")
    print(f"  Skipped (already):   {counters.get('skip', 0)}")
    print(f"  Errors:              {counters.get('error', 0)}")
    print(f"  Log: {log_path}")


if __name__ == "__main__":
    main()
