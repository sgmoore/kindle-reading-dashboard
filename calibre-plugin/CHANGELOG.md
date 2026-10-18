# Calibre plugin changelog

One `## X.Y.Z` section per release. The release workflow uses the matching
section as the GitHub release notes, and `tools/mobileread_update.py` builds
the MobileRead "Version History" from the first line of each section.

## 1.4.0
- SSH with a password now works without installing Python: Configure > "Install SSH support…" downloads paramiko and its libraries from PyPI into Calibre's config folder (one time, about 15 MB).
- Clear message when Source = ssh with a password but SSH support is not installed.
- On Windows, when the Kindle's USB network adapter got an automatic 169.254.x address, the "not reachable" message shows the exact command that fixes it.
- Source = auto now also finds a Kindle on USB networking (SSH) when no USB / MTP Kindle is connected and the SSH host answers.
- "Import automatically when the Kindle connects" now also works over SSH (Source = auto or ssh): the plugin checks every 10 seconds whether the Kindle answers and imports when it appears.
- Open web dashboard no longer starts a second server each time on Windows (the old one kept running).
- Books deleted from the Kindle no longer show up: the Kindle keeps a deleted book's .sdr folder, so reading files are now only read for books that are still on the device (USB, MTP and SSH).
- The dashboard no longer lists cloud-only items (not downloaded), KUAL / KOReader launchers (.sh) or My Clippings.
- Open web dashboard no longer fails silently when the Kindle was read but nothing came back (e.g. password login without SSH support): it shows an error instead.

## 1.3.3
- Books are matched to the library with Calibre's own device matching first (the same as the Device tab: UUID, application id, title + author), then ASIN, then title.
- Title matching never guesses: if several library books share the title, the author must match too, otherwise the book is skipped.
- Clearer status message: books with reading data (opened at least once), matches per method, skipped books, and books actually updated.

## 1.3.2
- Progress % no longer goes past 100: it is now how far into the book you are (the Kindle's own value from cc.db, else furthest page / total pages), not the cumulative share read.
- Fixed "QObject: Cannot create children for a parent that is in a different thread" on import / refresh: results are now applied in Calibre's GUI thread.

## 1.3.1
- Web dashboard opened from Calibre: the page's button is now "Rebuild" (Calibre reads the Kindle; reopen the dashboard from Calibre to read it again).
- No more 404 for the page icon.

## 1.3.0
- Open web dashboard works out of the box: the dashboard ships inside the plugin and reads the Kindle like the import does (USB, MTP or SSH); no dashboard.py path, no Python install.
- Works without cc.db: the dashboard is built from the reading-data files alone when cc.db is not available.
- Optional cc.db / fmcache.db copies from anywhere (Configure, with Browse) for exact titles, series and read status, in the import and the dashboard; books missing from an older cc.db copy are still included.
- Help in Configure explaining what each source gives, with links.

## 1.2.0
- Browse buttons for the documents folder, SSH key and dashboard.py in Configure.
- Open web dashboard works without installing Python (uses Calibre's own when no system Python is found).

## 1.1.0
- Support for MTP Kindles (Scribe 3, Colorsoft, 2024 Paperwhite); requires Calibre 7.25+.
- Clean title/ASIN for books in documents/Downloads/Items01.

## 1.0.0
- Initial release.
