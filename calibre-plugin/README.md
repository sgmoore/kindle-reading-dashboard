# Kindle Reading Dashboard — Calibre plugin

Imports the reading metadata your Kindle stores **on-device** into Calibre
custom columns, and launches the standalone web dashboard.

Per book it can fill: **reading hours, pace (WPM), progress %, furthest page,
words read, read/reading/unread status.** All decoded from the device's own
KRDS sidecars (`.azw3f`/`.azw3r`, `.yjf`/`.yjr`, `.mbs`/`.mbp1`)
(+ `cc.db` / `fmcache.db` over SSH) — no cloud, no
Amazon API.

## Build & install

```bash
cd calibre-plugin
python build.py                                   # -> kindle_reading_dashboard.zip
calibre-customize -a kindle_reading_dashboard.zip # or: Calibre → Preferences → Plugins → Load from file
```

Restart Calibre. A **Kindle Reading** button appears on the toolbar.

## Fields you can import

Tick any of these in **Configure** (each maps to a custom column you name):
reading hours, reading minutes, pace (WPM, net), WPM incl. breaks, progress %,
furthest page, words read, words in book, highlights/notes count, read status,
last-read date. Six are on by default.

## First run

1. **Configure…** (button menu):
   - **Fields to import** — tick the fields you want; each row has the column lookup name. Click **Create missing columns…** to create the ticked ones, then restart Calibre so they show.
   - **Data source**: `auto` (USB, with cc.db when present), `usb`, or `ssh`. Auto never silently SSHs — a disconnected device gives a clear message.
   - **USB documents folder**: leave blank to auto-detect the connected Kindle; or set e.g. `E:/documents`.
   - **SSH**: host / user / password / optional key. Used only when Source = ssh.
   - **Import automatically when the Kindle connects** — runs the import a few seconds after the device mounts.
   - **cc.db file / fmcache.db file** (optional): copies of the Kindle's
     `/var/local/cc.db` and `/mnt/us/system/fmcache/fmcache.db` from anywhere on
     your computer (pulled once over SSH, from a backup, …). They are only read,
     never changed, and add exact titles / series (cc.db) and read status
     (fmcache.db) to the import and the dashboard. Books on the device that an
     older cc.db copy does not know are still included, from their sidecars.
   - **Help: what you need** explains what each source gives and links to the
     jailbreak / USBNetwork guides.
   - **Custom dashboard.py**: leave blank to use the dashboard built into the
     plugin (recommended).
   - The folder / file fields have a **Browse…** button.
2. **Open web dashboard** — reads the Kindle the same way as the import (USB,
   MTP or SSH), then opens the dashboard that ships inside the plugin in your
   browser. Nothing to install: it runs on Calibre's own Python. If the Kindle
   is not connected, the last data read is shown.
3. **Import reading stats (all books)** — syncs, decodes, matches to your library, writes the enabled columns.
4. **Refresh selected book(s)** — same, but only updates the rows you have selected in the library.

## How matching works

Each book's reading data is matched to your Calibre library in this order:

1. **Calibre's own device matching** — the same matching as the Device tab
   (UUID, application id, title + author). Used when Calibre is connected to
   the Kindle; it covers books sent from Calibre.
2. **Amazon ASIN** identifier (`amazon` / `mobi-asin` / `amazon_*`).
3. **Title** (optional): only when it is unambiguous — if several library books
   share the title, the author has to match too, otherwise the book is skipped.

Only books that were opened on the Kindle have reading data, so the count is
usually much smaller than the library on the device. "Books updated" counts
books whose values actually changed. Without `cc.db` (USB / MTP), titles come
from the `.sdr` folder name; read status needs `fmcache.db` (SSH, or a copy
set in Configure).

## Device page

The reading columns also appear on the **device page** (Kindle connected) in the
**Book Details panel** for any device book that is matched to your library —
use menu → *Show stats in Book Details*. Calibre does **not** allow custom
columns as *grid* columns in the device book list itself; the details panel is
the supported place. (Custom columns show in Book Details by default, so this is
usually a no-op unless you previously hid fields.)

## Troubleshooting — black screen / popping command windows

Fixed in 1.0. Cause: when the Kindle is plugged in as a **USB drive**, usbnet
SSH (`192.168.15.244`) is unreachable, and the SSH fallback over the system
`ssh` binary opened one console window per file and blocked the UI. Now:

- USB is auto-detected and preferred, so SSH is not used at all over USB.
- A 2.5 s TCP probe (`ssh_reachable`) aborts SSH fast instead of flooding.
- All subprocesses run with `CREATE_NO_WINDOW` on Windows — no popups.

## Source / SSH notes

- **USB mass-storage** exposes the sidecars (reading time, pace, progress,
  furthest page, annotations) but **not** `cc.db`/`fmcache.db`.
- **MTP** (Scribe 3, Colorsoft, 2024+ Paperwhite — no drive letter): when
  Calibre itself is connected to the Kindle, the sidecars are read through
  Calibre's device connection. Same data as USB mass-storage. Needs
  **Calibre 7.25+**. Used automatically for `auto`/`usb` unless a USB
  documents folder is set.
- **SSH (usbnet)** pulls everything, including modern "Mark as Read" state.
- SSH uses **paramiko** if Calibre has it, otherwise the system **`ssh`**
  binary (key-based / agent auth recommended — non-interactive password auth
  via plain `ssh` is unreliable; prefer a key, or use paramiko).

Decoding reuses the bundled `krds.py` parser, identical to the toolkit's.

## Releasing

1. Bump `version` in `__init__.py` and add a `## X.Y.Z` section to `CHANGELOG.md`.
2. Commit, then tag and push: `git tag plugin-vX.Y.Z && git push origin plugin-vX.Y.Z`.
   The **Calibre plugin release** workflow checks the tag matches `version`,
   builds the zip and publishes the GitHub release (notes = the CHANGELOG section).
3. The same workflow then updates the MobileRead plugin thread with
   `tools/mobileread_update.py`: it uploads the zip as
   `kindle_reading_dashboard-X.Y.Z.zip`, saves, removes the old zip, saves, and
   verifies exactly the new zip is on the post (it never leaves the post without
   one); it also refreshes the Version History. Needs the repository secrets
   `MOBILEREAD_USER` / `MOBILEREAD_PASSWORD` and the repository variable
   `MOBILEREAD_AUTO=true` (set it to anything else to skip the forum step).
   To redo or test only this step: Actions -> Calibre plugin release -> Run
   workflow (dry run by default; post 4616176, the thread's second post, is the
   test post: use `attachments_only`, and `test_variant` to force a replace).
