#!/usr/bin/env python
"""The toolbar action: Import reading stats / Open dashboard / Configure."""
import os
import shutil
import subprocess
import sys
import tempfile

from calibre.gui2 import Dispatcher, error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.threaded_jobs import ThreadedJob

from calibre_plugins.kindle_reading.config import prefs

DBG_LOG = os.path.join(tempfile.gettempdir(), "kindle_reading_debug.log")
# Step tracing is off unless KINDLE_READING_DEBUG is set in the environment.
# Useful for diagnosing a freeze: each GUI-thread step is timestamped to DBG_LOG.
_DEBUG = bool(os.environ.get("KINDLE_READING_DEBUG"))


def _dbg(msg):
    if not _DEBUG:
        return
    try:
        import time
        with open(DBG_LOG, "a", encoding="utf-8") as f:
            f.write("%.3f  %s\n" % (time.time(), msg))
    except Exception:  # noqa
        pass


class KindleReadingAction(InterfaceAction):

    name = "Kindle Reading Dashboard"
    action_spec = ("Kindle Reading", "reader.png",
                   "Import Kindle reading metadata into custom columns", None)
    action_type = "current"

    def genesis(self):
        from qt.core import QMenu
        icon = get_icons("images/reader.png", "Kindle Reading")
        if icon is not None and not icon.isNull():
            self.qaction.setIcon(icon)
        m = self.qaction.menu()
        if m is None:
            m = QMenu(self.gui)
            self.qaction.setMenu(m)
        self.menu = m
        m.clear()
        m.addAction("Import reading stats (all books)",
                    lambda: self.import_stats())
        m.addAction("Refresh selected book(s)", self.refresh_selected)
        m.addAction("Show stats in Book Details (device + library)",
                    self.enable_book_details)
        m.addAction("Open web dashboard", self.open_dashboard)
        m.addSeparator()
        m.addAction("Configure…", self.show_config)
        # NB: triggered emits a `checked` bool — swallow it so it is not passed
        # as restrict_ids to import_stats.
        self.qaction.triggered.connect(lambda checked=False: self.import_stats())
        self._install_device_hook()
        self._install_ssh_watch()
        _dbg("genesis done")

    # -------------------------------------- auto-import when SSH comes up
    def _install_ssh_watch(self):
        """A Kindle on USB networking (Source = ssh / auto) is not a calibre device,
        so device_detected never fires for it. Probe its SSH port every few
        seconds instead (in a thread: the probe can take seconds) and import
        when it goes from unreachable to reachable."""
        from qt.core import QTimer
        self._ssh_up = False
        self._ssh_probing = False
        # moves the probe result from the thread to the GUI thread
        self._ssh_done = Dispatcher(self._ssh_probed)
        self._ssh_timer = QTimer(self.gui)
        self._ssh_timer.timeout.connect(self._ssh_probe)
        self._ssh_timer.start(10000)

    def _ssh_probe(self):
        if self._ssh_probing or prefs["source"] not in ("ssh", "auto") \
                or not prefs["auto_on_connect"]:
            return
        if prefs["source"] == "auto" and self._mtp_or_usb_connected():
            # auto reads a connected USB / MTP Kindle; device_detected
            # already imports for it
            return
        import threading
        from calibre_plugins.kindle_reading import engine
        host = prefs["ssh_host"]
        done = self._ssh_done
        self._ssh_probing = True

        def probe():
            up = False
            try:
                up = engine.ssh_reachable(host, timeout=2)
            finally:
                _dbg("ssh watch: %s reachable=%s" % (host, up))
                done(up)

        threading.Thread(target=probe, daemon=True).start()

    def _mtp_or_usb_connected(self):
        try:
            return bool(self.gui.device_manager.is_device_connected)
        except Exception:  # noqa
            return False

    def _ssh_probed(self, up):
        self._ssh_probing = False
        if up and not self._ssh_up:
            _dbg("ssh watch: %s came up, auto-import" % prefs["ssh_host"])
            self.import_stats(silent=True)
        self._ssh_up = up

    # ---------------------------------------------- auto-import on connect
    def _install_device_hook(self):
        """Wrap the GUI's device_detected method so we can auto-import when a
        device connects (calibre exposes no signal for it)."""
        try:
            orig = self.gui.device_detected

            def wrapped(connected, device_kind, _orig=orig, _self=self):
                _orig(connected, device_kind)
                try:
                    if connected and prefs["auto_on_connect"]:
                        _dbg("device_detected: connected, auto-import scheduled")
                        from qt.core import QTimer
                        # delay so calibre finishes mounting the device
                        QTimer.singleShot(
                            4000, lambda: _self.import_stats(silent=True))
                except Exception as e:  # noqa
                    _dbg("device hook error %r" % e)

            self.gui.device_detected = wrapped
        except Exception as e:  # noqa
            _dbg("install_device_hook failed %r" % e)

    # ---------------------------------------------------------------- config
    def show_config(self):
        _dbg("show_config: enter")
        self.interface_action_base_plugin.do_user_config(self.gui)
        _dbg("show_config: dialog closed")

    # ------------------------------------------------- show in Book Details
    def enable_book_details(self):
        _dbg("enable_book_details: enter")
        """The device-book *grid* can't host custom columns (Calibre limitation),
        but the Book Details panel can — and it shows the matched library record
        for books that are on the device too. Add our columns to that panel so
        the reading stats are visible on the device page."""
        from calibre.gui2 import gprefs
        from calibre_plugins.kindle_reading.config import enabled_columns
        db = self.gui.current_db.new_api
        custom = set(db.field_metadata.custom_field_keys())
        wanted = [c for c in enabled_columns() if c in custom]
        if not wanted:
            return error_dialog(
                self.gui, "No columns",
                "Create and import the columns first (Configure → Create "
                "missing columns, then Import reading stats).", show=True)
        # Book Details reads gprefs['book_display_fields'] as a list of
        # (field, show?) tuples. When it is UNSET, calibre shows every field —
        # including custom columns — by default, so there is nothing to do.
        # Only when the user has customised the list do we ensure our columns
        # are present and turned on (and never drop the existing fields).
        fields = gprefs.get("book_display_fields", None)
        if not fields:
            return info_dialog(
                self.gui, "Already visible",
                "Book Details shows all custom columns by default, so the "
                "reading stats already appear there — for the selected book on "
                "both the library and the device page (device books that are "
                "matched to your library).\n\n"
                "Calibre does not allow custom columns as grid columns in the "
                "device list itself; the details panel is the supported place.",
                show=True)
        fields = [(f, bool(v)) for (f, v) in fields]
        have = {f for f, _ in fields}
        fields = [(f, True if f in wanted else v) for (f, v) in fields]
        for w in wanted:
            if w not in have:
                fields.append((w, True))
        gprefs["book_display_fields"] = fields
        try:
            self.gui.book_details.refresh()
        except Exception:  # noqa
            pass
        info_dialog(
            self.gui, "Book Details updated",
            "Reading columns enabled in the Book Details panel. They show for "
            "the selected book on both the library and the device page (for "
            "device books matched to your library).\n\n"
            "Note: Calibre does not allow custom columns as grid columns in the "
            "device list itself — the details panel is the supported place.",
            show=True)

    # --------------------------------------------------------- detect device
    def _device_prefixes(self):
        """Cheap attribute reads only (no filesystem IO) — the actual isdir
        probing happens in the worker thread so the UI can't freeze."""
        out = []
        try:
            dev = self.gui.device_manager.connected_device
            for attr in ("_main_prefix", "_card_a_prefix", "_card_b_prefix"):
                p = getattr(dev, attr, None) if dev else None
                if p:
                    out.append(p)
        except Exception:  # noqa
            pass
        return out

    # --------------------------------------------------------- refresh subset
    def refresh_selected(self):
        ids = []
        try:
            ids = list(self.gui.library_view.get_selected_ids())
        except Exception:  # noqa
            pass
        if not ids:
            return error_dialog(self.gui, "No selection",
                                "Select one or more books in the library first.",
                                show=True)
        self.import_stats(restrict_ids=set(ids))

    def _cfg(self):
        """Settings snapshot handed to the worker threads."""
        cfg = {k: prefs[k] for k in (
            "source", "ssh_host", "ssh_user", "ssh_password", "ssh_key",
            "ccdb_path", "fmcache_path")}
        cfg["usb_pref"] = prefs["usb_documents"]
        cfg["dev_prefixes"] = self._device_prefixes()
        return cfg

    def _mtp_device(self, cfg, source):
        """The connected MTP Kindle to read from, or None (USB/SSH path)."""
        if source == "ssh" or cfg["usb_pref"]:
            return None
        from calibre_plugins.kindle_reading import engine
        dev = getattr(self.gui.device_manager, "connected_device", None)
        return dev if engine.is_mtp_kindle(dev) else None

    # --------------------------------------------------------- import stats
    def import_stats(self, restrict_ids=None, silent=False):
        _dbg("import_stats: enter (restrict=%s silent=%s)"
             % (None if restrict_ids is None else len(restrict_ids), silent))
        cfg = self._cfg()
        source = prefs["source"]
        _dbg("import_stats: source=%s dev_prefixes=%r" % (source, cfg["dev_prefixes"]))
        dev = self._mtp_device(cfg, source)
        if dev is not None:
            return self._import_via_mtp(dev, cfg, source, restrict_ids, silent)
        self._start_import_job(cfg, source, restrict_ids, silent)

    def _import_via_mtp(self, dev, cfg, source, restrict_ids, silent):
        """MTP Kindle: fetch the sidecars as a Calibre device job (the MTP
        driver only accepts calls on the device thread), then decode them in
        the usual worker job."""
        _dbg("_import_via_mtp: queue device job")
        from calibre_plugins.kindle_reading import engine
        cache = tempfile.mkdtemp(prefix="kindle_reading_")

        def fetched(job):
            if job.failed:
                shutil.rmtree(cache, ignore_errors=True)
                self._report_failure(
                    str(getattr(job, "exception", "") or "MTP fetch failed."),
                    getattr(job, "_details", "") or "", silent)
                return
            _dbg("_import_via_mtp: fetched %r sidecars" % (job.result,))
            cfg["mtp_synced"] = True
            self._start_import_job(cfg, source, restrict_ids, silent, cache=cache)

        self.gui.device_manager.create_job(
            engine.sync_mtp, fetched,
            "Kindle reading: fetch sidecars over MTP", args=[dev, cache])
        self.gui.status_bar.show_message(
            "Reading Kindle sidecars over MTP…", 3000)

    def _start_import_job(self, cfg, source, restrict_ids, silent, cache=None):
        job = ThreadedJob(
            "kindle_reading_import",
            "Importing Kindle reading metadata",
            self._do_import, (cfg, source, cache), {},
            # ThreadedJob calls its callback in the worker thread; Dispatcher
            # moves it to the GUI thread (it writes the library and the UI)
            Dispatcher(self._import_done))
        # stash per-run options for the callback (callback only gets the job)
        job.kr_restrict = restrict_ids
        job.kr_silent = bool(silent)
        _dbg("import_stats: submitting job")
        self.gui.job_manager.run_threaded_job(job)
        _dbg("import_stats: job submitted (returning to event loop)")
        self.gui.status_bar.show_message("Importing Kindle reading metadata…", 3000)

    def _do_import(self, cfg, source, cache=None, notifications=None, abort=None,
                   log=None):
        """Runs in a worker thread: sync + decode. Returns the book list.
        `cache` is pre-filled when the sidecars came over MTP."""
        _dbg("_do_import: worker start (source=%s)" % source)
        from calibre_plugins.kindle_reading import engine
        cache = cache or tempfile.mkdtemp(prefix="kindle_reading_")

        def progress(i, n):
            if notifications is not None and n:
                notifications.put((i / float(n), "synced %d/%d" % (i, n)))

        try:
            books, used = engine.collect(cache, cfg, source, progress)
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        _dbg("_do_import: worker done (%d books, via %s)" % (len(books), used))
        return {"books": books, "source": used}

    def _import_done(self, job):
        _dbg("_import_done: callback start (failed=%s)" % job.failed)
        silent = bool(getattr(job, "kr_silent", False))
        restrict = getattr(job, "kr_restrict", None)
        try:
            if job.failed:
                self._report_failure(
                    str(getattr(job, "exception", "") or "Import failed."),
                    getattr(job, "traceback", "") or "", silent)
                return
            result = job.result or {}
            books = [b for b in result.get("books", []) if b.get("has_data")]
            used = result.get("source", "?")
            matched, updated = self._write_columns(books, restrict_ids=restrict)
            _dbg("_import_done: wrote columns matched=%d updated=%d"
                 % (matched, updated))
            # Non-blocking: a modal dialog from the job callback can wedge the
            # event loop. Report via the status bar instead.
            m = getattr(self, "_last_match", {})
            skipped = []
            if m.get("unmatched"):
                skipped.append("%d not in the library" % m["unmatched"])
            if m.get("ambiguous"):
                skipped.append("%d skipped: same title on several books" % m["ambiguous"])
            self.gui.status_bar.show_message(
                "Kindle import: %d books with reading data on the Kindle (opened at least "
                "once); matched %d (device %d, ASIN %d, title %d)%s; %d books updated "
                "(source: %s)" % (len(books), matched, m.get("device", 0), m.get("asin", 0),
                                  m.get("title", 0),
                                  ("; " + ", ".join(skipped)) if skipped else "",
                                  updated, used), 15000)
            _dbg("_import_done: status message shown (done)")
        except Exception as e:  # noqa
            import traceback
            _dbg("_import_done: EXCEPTION %r\n%s" % (e, traceback.format_exc()))

    def _report_failure(self, msg, det, silent):
        _dbg("import failed: %s" % msg[:80])
        # auto-on-connect for a non-Kindle device shouldn't nag
        if not silent:
            error_dialog(self.gui, "Kindle reading import",
                         msg, det_msg=det, show=True)
        else:
            self.gui.status_bar.show_message("Kindle import: " + msg, 6000)

    # ------------------------------------------------------- write to library
    def _device_matches(self, all_ids):
        """{device_key: library book id} from calibre's matching of the books
        on the connected device (book.application_id, set by calibre when it
        matched the device book by UUID / APP_ID / title+author)."""
        from calibre_plugins.kindle_reading.engine import device_key
        out = {}
        try:
            booklists = self.gui.booklists() if self.gui.device_manager.is_device_connected else ()
        except Exception as e:  # noqa
            _dbg("_device_matches: %r" % e)
            return out
        for bl in booklists:
            for book in (bl or ()):
                bid = getattr(book, "application_id", None)
                lpath = getattr(book, "lpath", None)
                if lpath and getattr(book, "in_library", None) and bid in all_ids:
                    out[device_key(lpath)] = bid
        _dbg("_device_matches: %d device books matched by calibre" % len(out))
        return out

    def _write_columns(self, books, restrict_ids=None):
        from calibre_plugins.kindle_reading.config import enabled_columns
        db = self.gui.current_db.new_api
        custom = set(db.field_metadata.custom_field_keys())
        # {lookup: field_key} for enabled fields whose column actually exists
        colmap = {col: key for col, key in enabled_columns().items()
                  if col in custom}

        if not colmap:
            if not getattr(self, "_silent_write", False):
                error_dialog(self.gui, "No columns",
                             "No enabled custom columns exist yet. Use "
                             "Configure → tick fields → Create missing columns.",
                             show=True)
            return 0, 0

        # --- match indexes over the Calibre library ---
        all_ids = set(db.all_book_ids())
        # 1) calibre's own device matching (UUID / APP_ID / author, the same
        #    as the Device tab), keyed by the book file's path on the device
        by_device = self._device_matches(all_ids)
        # 2) ASIN identifiers; 3) title, used only when it is unambiguous
        by_asin, by_title, authors_of = {}, {}, {}
        for bid in all_ids:
            ids = db.field_for("identifiers", bid) or {}
            for k, v in ids.items():
                if k in ("amazon", "mobi-asin") or k.startswith("amazon_"):
                    by_asin[str(v).upper()] = bid
            by_title.setdefault(_norm(db.field_for("title", bid) or ""), []).append(bid)
            names = set(db.field_for("authors", bid) or ())
            names.add(db.field_for("author_sort", bid) or "")
            authors_of[bid] = {_norm(a) for a in names if a}

        match_title = bool(prefs["match_by_title"])
        changes = {lookup: {} for lookup in colmap}
        how = {"device": 0, "asin": 0, "title": 0}
        unmatched = ambiguous = 0
        for b in books:
            bid, method = None, None
            key = b.get("device_key")
            if key and key in by_device:
                bid, method = by_device[key], "device"
            elif b.get("asin") and str(b["asin"]).upper() in by_asin:
                bid, method = by_asin[str(b["asin"]).upper()], "asin"
            elif match_title and b.get("norm_title") in by_title:
                cands = by_title[b["norm_title"]]
                if len(cands) > 1 and b.get("author"):
                    # same title more than once: the author has to decide
                    cands = [c for c in cands if _norm(b["author"]) in authors_of[c]]
                if len(cands) == 1:
                    bid, method = cands[0], "title"
                else:
                    ambiguous += 1
                    continue
            if bid is None:
                unmatched += 1
                continue
            if restrict_ids is not None and bid not in restrict_ids:
                continue
            how[method] += 1
            for lookup, field in colmap.items():
                val = b.get(field)
                if val is not None and db.field_for(lookup, bid) != val:
                    changes[lookup][bid] = val

        changed_ids = set()
        for lookup, mp in changes.items():
            if not mp:
                continue
            try:
                db.set_field(lookup, mp)
                changed_ids.update(mp)
            except Exception as e:  # noqa  (e.g. a datetime value calibre rejects)
                _dbg("_write_columns: set_field %s failed %r" % (lookup, e))
        self._last_match = dict(how, unmatched=unmatched, ambiguous=ambiguous)
        matched, updated = sum(how.values()), len(changed_ids)
        # Targeted refresh of only the changed rows. A full model().refresh()
        # here rebuilds the entire library view and, with a device connected,
        # re-runs on-device matching — slow enough to look like a freeze.
        if changed_ids:
            _dbg("_write_columns: before refresh_ids (%d)" % len(changed_ids))
            try:
                self.gui.library_view.model().refresh_ids(list(changed_ids))
            except Exception as e:  # noqa
                _dbg("_write_columns: refresh_ids EXC %r" % e)
            _dbg("_write_columns: after refresh_ids")
        return matched, updated

    # --------------------------------------------------------- web dashboard
    def open_dashboard(self):
        """Built-in dashboard: read the Kindle (USB / MTP / SSH, like the
        import), then run the dashboard bundled in this plugin on calibre's
        own Python. A custom dashboard.py set in Configure runs as before."""
        _dbg("open_dashboard: enter")
        custom = prefs["dashboard_py"]
        if custom:
            if not os.path.isfile(custom):
                return error_dialog(
                    self.gui, "dashboard.py not found",
                    "The custom dashboard.py set in Configure does not exist:\n%s\n\n"
                    "Clear the field to use the built-in dashboard." % custom, show=True)
            return self._launch(_dashboard_command(custom, ["serve"]),
                                os.path.dirname(os.path.abspath(custom)))
        try:
            dash_dir = self._extract_dashboard()
        except Exception as e:  # noqa
            return error_dialog(self.gui, "Dashboard", "Could not unpack the built-in "
                                "dashboard: %s" % e, show=True)
        cfg, source = self._cfg(), prefs["source"]
        staging = tempfile.mkdtemp(prefix="kindle_reading_dash_")
        dev = self._mtp_device(cfg, source)
        if dev is not None:
            from calibre_plugins.kindle_reading import engine
            self.gui.device_manager.create_job(
                engine.sync_mtp,
                lambda job: self._dashboard_synced(job, dash_dir, staging, cfg),
                "Kindle reading: read the Kindle for the dashboard", args=[dev, staging])
        else:
            job = ThreadedJob(
                "kindle_reading_dashboard", "Reading the Kindle for the dashboard",
                self._dashboard_sync, (cfg, source, staging), {},
                Dispatcher(lambda job: self._dashboard_synced(job, dash_dir, staging, cfg)))
            self.gui.job_manager.run_threaded_job(job)
        self.gui.status_bar.show_message("Reading the Kindle for the dashboard…", 4000)

    def _dashboard_sync(self, cfg, source, staging, notifications=None, abort=None,
                        log=None):
        from calibre_plugins.kindle_reading import engine
        return engine.sync(staging, cfg, source)

    def _dashboard_synced(self, job, dash_dir, staging, cfg):
        """Swap the fresh data in (only if the read worked: a disconnected
        Kindle keeps showing the last data), then start the dashboard."""
        cache = os.path.join(dash_dir, "cache")
        note = None
        if job.failed:
            shutil.rmtree(staging, ignore_errors=True)
            note = str(getattr(job, "exception", "") or "could not read the Kindle")
        else:
            shutil.rmtree(cache, ignore_errors=True)
            shutil.move(staging, cache)
        # an empty documents/ (a sync that read nothing) is no data: the
        # dashboard would exit with "nothing to build" in a hidden window
        has_sidecars = any(files for _d, _s, files in
                           os.walk(os.path.join(cache, "documents")))
        has_ccdb = bool(cfg.get("ccdb_path")) or os.path.isfile(os.path.join(cache, "cc.db"))
        if not (has_sidecars or has_ccdb):
            return error_dialog(
                self.gui, "No reading data yet",
                "Connect the Kindle (USB, or MTP with Calibre connected to it), or "
                "pick a copy of cc.db in Configure, then open the dashboard again."
                "\n\n" + (note or ""), show=True)
        args = ["serve", "--no-sync", "--cache", cache]
        for key, opt in (("ccdb_path", "--ccdb"), ("fmcache_path", "--fmcache")):
            if cfg.get(key) and os.path.isfile(cfg[key]):
                args += [opt, cfg[key]]
        self._launch(_dashboard_command(os.path.join(dash_dir, "dashboard.py"), args,
                                        prefer_calibre=True), dash_dir)
        if note:
            self.gui.status_bar.show_message(
                "Kindle not read (%s) - showing the last data" % note[:80], 10000)

    def _extract_dashboard(self):
        """Unpack dashboard/ from this plugin's zip into calibre's config folder
        (again after each plugin update). The data cache next to it is kept."""
        from calibre.utils.config import config_dir
        import zipfile
        base = self.interface_action_base_plugin
        dest = os.path.join(config_dir, "plugins", "kindle_reading_dashboard")
        marker = os.path.join(dest, ".version")
        with zipfile.ZipFile(base.plugin_path) as zf:
            # version + the files' CRCs, so a rebuilt zip with the same
            # version number is still extracted again
            crc = sum(i.CRC for i in zf.infolist() if i.filename.startswith("dashboard/"))
            ver = "%s:%08x" % (".".join(map(str, base.version)), crc & 0xffffffff)
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == ver:
                    return dest
        except OSError:
            pass
        with zipfile.ZipFile(base.plugin_path) as zf:
            for name in zf.namelist():
                if not name.startswith("dashboard/") or name.endswith("/"):
                    continue
                target = os.path.join(dest, *name[len("dashboard/"):].split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(zf.read(name))
        with open(marker, "w", encoding="utf-8") as f:
            f.write(ver)
        return dest

    def _launch(self, cmd, cwd):
        if not cmd:
            return error_dialog(
                self.gui, "Python not found",
                "Could not find calibre-debug or Python to run the dashboard. "
                "Install Python 3 from https://www.python.org/downloads/", show=True)
        try:
            from calibre_plugins.kindle_reading.engine import _no_window_kwargs
            subprocess.Popen(cmd, cwd=cwd, **_no_window_kwargs())
            self.gui.status_bar.show_message(
                "Launching dashboard… a browser tab will open.", 5000)
        except Exception as e:  # noqa
            error_dialog(self.gui, "Launch failed", str(e), show=True)


def _norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _dashboard_command(path, args, prefer_calibre=False):
    """How to run dashboard.py. calibre's own interpreter (calibre-debug -e)
    needs no install (the dashboard is stdlib-only); a system Python is only
    needed by a custom dashboard.py that syncs over SSH itself (paramiko)."""
    calibre = None
    exe_dir = os.path.dirname(sys.executable)
    for name in ("calibre-debug.exe", "calibre-debug"):
        cd = os.path.join(exe_dir, name)
        if os.path.isfile(cd):
            calibre = [cd, "-e", path, "--"] + args
            break
    python = None
    for c in ("python3", "python"):
        py = shutil.which(c)
        # skip the Windows Store "python" stub, which only opens the Store
        if py and "WindowsApps" not in py:
            python = [py, path] + args
            break
    return (calibre or python) if prefer_calibre else (python or calibre)
