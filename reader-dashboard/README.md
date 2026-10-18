# reader-dashboard

The local web dashboard. Single entrypoint: `dashboard.py` (sync → build → serve).
See the [root README](../README.md) for full docs.

```bash
python dashboard.py serve              # SSH sync + build + open browser
python dashboard.py serve --no-sync    # reuse local cache
python dashboard.py serve --local D:/documents   # USB mass-storage instead of SSH
python dashboard.py sync | build       # individual steps
python dashboard.py serve --ccdb D:/backup/cc.db --fmcache D:/backup/fmcache.db
                                       # use copies of the DBs from elsewhere (read only)
python dashboard.py serve --cache D:/kindle-cache   # keep synced files elsewhere
python dashboard.py serve --include-deleted   # also books deleted from the Kindle
```

The Kindle keeps a deleted book's `.sdr` folder (and its reading data). By
default those are skipped: each sync writes `book_files.txt` (the book files on
the device) to the cache, and only `.sdr` folders whose book is still there are
read. With `--include-deleted` they are listed too, marked *(deleted)*.
Cloud-only items, KUAL / KOReader launchers (`.sh`) and My Clippings are never
listed.

Works with whatever is available:

- sidecars only (USB / MTP, no jailbreak): reading time, pace, progress,
  highlights; titles from the `.sdr` folder names
- `+ cc.db`: exact titles, authors, series. Books whose sidecars are not in this
  cc.db (e.g. an older copy) are still listed, from their sidecars
- `+ fmcache.db`: read / unread status

The Calibre plugin ships this dashboard and runs it for you (*Open web
dashboard*), so Calibre users do not need this folder or Python.

- `web/index.html` — sortable/searchable library table
- `web/book.html` — full per-book metadata + raw JSON of every source
- `web/common.js` — shared helpers (date/number formatting, page mapping, read-status)
- Generated `web/library.json` and `cache/` hold your personal data and are git-ignored.

Reads `cc.db` (library), the KRDS sidecars (AZW3 `.azw3f`/`.azw3r`, KFX
`.yjf`/`.yjr`, MOBI `.mbs`/`.mbp1`) for reading data, and
`fmcache.db` (modern read-state + device sessions — see
[../docs/read-state-storage.md](../docs/read-state-storage.md)).
