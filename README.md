# GooglePhotos EXIF Mover
Tool to organize Google Photos, export via TakeOut, based on EXIF image data/info 

Rebuild a clean, chronologically-ordered photo library out of a Google Takeout
export.

Google Photos lets you download your albums via [Google Takeout](https://takeout.google.com/),
but the download is messy:

- Every photo is accompanied by a JSON sidecar file holding the real EXIF-style
  metadata (`photoTakenTime`, GPS, description, people, "favorite" flag, …).
- The metadata is **not** inside the media file itself, so devices that play a
  slideshow based on EXIF date get the order wrong.
- RAW files (CR2/CR3) and HEIC images are often silently converted to JPEG but
  keep their original extension — a trap for anything that relies on file type.
- Sidecar filenames are truncated to fit the 50-character zip-entry limit,
  producing names like `IMG_0112.HEIC.supp.json` or even
  `IMG_0112.HEIC.supplement(1).json` for duplicates.

This script handles all of that:

1. Walks the Takeout folder **recursively**.
2. Pairs every media file with its JSON sidecar, even when the sidecar name is
   mangled, duplicated, or belongs to the edited variant (`-editada`, `-edited`).
3. Injects `DateTimeOriginal`, GPS, description, rating, and people
   tags into the media file using **ExifTool**.
4. Resets the filesystem mtime to the real capture date, as a belt-and-braces
   fallback for slideshow apps that ignore EXIF.
5. Copies the result into a tidy `Organized/YYYY/MM/DD/` tree (Europe/Lisbon
   local time), leaving the originals untouched.

The intended use-case is a photo frame / iPad wall display that polls a shared
folder on a NAS and slideshows from oldest to newest.

## Prerequisites

- **Python 3.9+** (`zoneinfo` is required).
- **ExifTool** — the heavy lifter. Get it any of these ways:
  - macOS: `brew install exiftool`
  - Debian / Ubuntu: `sudo apt install libimage-exiftool-perl`
  - Windows: download the portable `.zip` from
    [exiftool.org](https://exiftool.org/) and make sure `exiftool.exe` is on
    your `PATH`.
  - **Zero-install fallback**: drop the `Image-ExifTool-X.Y.tar.gz` from
    [exiftool.org](https://exiftool.org/) next to `organizar_fotos.py` and
    the script will extract and use it on first run (Perl 5.8+ required).

## Installation

Copy `organize_photos.py` into the folder that contains your Takeout dump
(the folder with all the `.jpg` / `.heic` / `.json` files, possibly inside
subfolders produced by Takeout), or run it from anywhere pointing at that
folder with `--source`.

## Usage

```bash
# Always start with a dry-run to see what would happen:
python3 organize_photos.py --dry-run

# Process for real:
python3 organize_photos.py --verbose
```

Running the command again on the same folder only processes **new** files.
The script is safe to run on a recurring basis — just drop the next Takeout
export into the same place and re-run.

### Options

| Flag | Default | What it does |
|---|---|---|
| `--source PATH` | `.` | Folder with the Takeout dump (scanned recursively). |
| `--dest PATH` | `<source>/Organized` | Where the cleaned-up photos land. |
| `--dry-run` | off | Write `DRY_RUN_REPORT.md`, touch nothing. |
| `--verbose` | off | Print progress every 100 files. |

### Environment variables

| Variable | Purpose |
|---|---|
| `EXIFTOOL` | Absolute path to a specific `exiftool` binary, overriding auto-detection. |

## What goes where

```
<source>/
|-- IMG_0001.JPG
|-- IMG_0001.JPG.supplemental-metadata.json
|-- ...
|-- Organized/
    |-- 2019/
    |   |-- 07/
    |   |   |-- 22/
    |   |       |-- IMG_5605.HEIC
    |   |       +-- ...
    |-- 2024/
    |   +-- ...
    |-- _no_date/       # orphans + suspicious timestamps (< 1995 or future)
    +-- _log.csv        # append-only audit log across runs
```

Originals are copied, not moved — the Takeout dump is never altered. Safe to
delete it afterwards once you have verified the output.

## Fields injected

| JSON field | EXIF / XMP / QuickTime target |
|---|---|
| `photoTakenTime.timestamp` | `AllDates`, `EXIF:DateTimeOriginal`, `EXIF:CreateDate`, `EXIF:ModifyDate`, `XMP:DateTimeOriginal`, `QuickTime:CreateDate`, `QuickTime:MediaCreateDate`, … |
| `geoData.latitude/longitude/altitude` | `GPSLatitude(Ref)`, `GPSLongitude(Ref)`, `GPSAltitude(Ref)` — skipped if all zero |
| `description` | `EXIF:ImageDescription`, `XMP:Description` |
| `favorited: true` | `XMP:Rating = 5` |
| `people[].name` | `XMP:Subject` keywords |

The Unix timestamp from Takeout is UTC; we write EXIF in UTC with an explicit
`+00:00` offset and use **Europe/Lisbon** (with DST handled automatically) only
for the `YYYY/MM/DD/` folder names.

## Edge cases handled

- Duplicate media: `IMG_0158(1).HEIC` paired with `IMG_0158.HEIC.supplemental-metadata(1).json`.
- Edited copies: `IMG_5120-editada.HEIC` falls back to the original's JSON.
- Heavily truncated JSON names: `COLLAGE.jpg` vs `COLLAGE.j.json`, or just
  `NAME.json` without extension.
- Live Photo movies: `IMG_XXXX.MP4` inherits metadata from its sibling
  `IMG_XXXX.HEIC`/`.JPG`.
- Format masquerade: `.CR3` / `.HEIC` files that are actually JPEG get the full
  EXIF treatment via ExifTool; magic-byte detection in the report tells you
  what the file really is.
- Suspicious timestamps (pre-1995 or future-dated) go to `_no_date/` instead
  of polluting the real date tree.

## Troubleshooting

- **"exiftool not found"** — install it or drop the `Image-ExifTool-*.tar.gz`
  next to the script.
- **Report says "orphans" but the JSONs are right there** — open a GitHub
  issue with a directory listing (`ls` / `dir`) of one failing pair so the
  pairing regex can be extended.
- **Dates look shifted by an hour** — the folder name is Lisbon local time.
  Summer/winter transitions are handled correctly by `zoneinfo`, but if you
  are in a different timezone change the `LISBON = ZoneInfo("Europe/Lisbon")`
  line near the top of the script.

## License

MIT — see `LICENSE`.
