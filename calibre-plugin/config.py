#!/usr/bin/env python
"""Persistent settings + the configuration widget."""
from calibre.utils.config import JSONConfig

try:
    from qt.core import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
                         QComboBox, QLabel, QPushButton, QGroupBox, QGridLayout,
                         QCheckBox, QScrollArea, QProgressDialog, QTimer, Qt,
                         QFrame, QSize)
except ImportError:  # very old calibre
    from PyQt5.Qt import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
                          QComboBox, QLabel, QPushButton, QGroupBox, QGridLayout,
                          QCheckBox, QScrollArea, QProgressDialog, QTimer, Qt,
                          QFrame, QSize)

from calibre_plugins.kindle_reading.fields import (
    FIELDS, FIELD_LABEL, FIELD_DT, DEFAULT_COL, DEFAULT_ON)

# stored under the calibre config dir as plugins/kindle_reading.json
prefs = JSONConfig("plugins/kindle_reading")

prefs.defaults["source"] = "auto"          # auto | usb | ssh
prefs.defaults["usb_documents"] = ""        # blank => auto from connected device
prefs.defaults["ssh_host"] = "192.168.15.244"
prefs.defaults["ssh_user"] = "root"
prefs.defaults["ssh_password"] = "kindle"
prefs.defaults["ssh_key"] = ""              # private-key path (optional)
prefs.defaults["dashboard_py"] = ""         # custom dashboard.py (blank = the bundled one)
prefs.defaults["ccdb_path"] = ""            # a copy of the Kindle's cc.db (optional)
prefs.defaults["fmcache_path"] = ""         # a copy of the Kindle's fmcache.db (optional)

HELP_HTML = """
<b>What works with what</b><br>
&bull; <b>USB or MTP only</b> (any Kindle, no jailbreak): reading time, pace,
progress, furthest page, words read, highlights. Titles come from the file names.<br>
&bull; <b>+ cc.db</b> (the Kindle's library database, <code>/var/local/cc.db</code>):
exact titles, authors, series and ASINs.<br>
&bull; <b>+ fmcache.db</b> (<code>/mnt/us/system/fmcache/fmcache.db</code>):
read / unread status as marked on the Kindle.<br><br>
cc.db is not visible over USB on most Kindles. To get it you need a
<a href="https://kindlemodding.org/jailbreaking/">jailbroken Kindle</a> with
<a href="https://www.mobileread.com/forums/showthread.php?t=225030">USBNetwork (SSH)</a>:
then either set Source = ssh, or copy both files to your computer once and
pick them below. Copies are only read, never changed.<br><br>
Nothing else needs to be installed: the web dashboard runs on Calibre's own
Python. SSH with a password needs SSH support: click
<i>Install SSH support…</i> in the SSH section (a one-time download from
PyPI into Calibre's config folder, no Python install).
Only a <i>custom</i> dashboard.py that syncs over SSH by itself needs
<a href="https://www.python.org/downloads/">Python 3</a> with
<code>pip install paramiko</code>.
"""
prefs.defaults["match_by_title"] = True
prefs.defaults["auto_on_connect"] = False   # import automatically when device connects
# per-field enable + column lookup name, keyed by field key
prefs.defaults["field_on"] = dict(DEFAULT_ON)
prefs.defaults["field_col"] = dict(DEFAULT_COL)


def field_on(key):
    return bool(prefs["field_on"].get(key, DEFAULT_ON[key]))


def field_col(key):
    return prefs["field_col"].get(key, DEFAULT_COL[key])


def enabled_columns():
    """{lookup_name: field_key} for every enabled field with a '#' lookup."""
    out = {}
    for key, *_ in FIELDS:
        col = field_col(key)
        if field_on(key) and col.startswith("#"):
            out[col] = key
    return out


def _form(box):
    """QFormLayout whose fields take the full width (the Windows style
    otherwise keeps them at their small size hint)."""
    f = QFormLayout(box)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return f


class ConfigWidget(QWidget):

    def __init__(self):
        QWidget.__init__(self)
        # one scroll for the whole page: calibre's dialog takes its size from
        # sizeHint(), and on a short screen the sections would be squashed
        outer_l = QVBoxLayout(self)
        outer_l.setContentsMargins(0, 0, 0, 0)
        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._page = QWidget()
        page_scroll.setWidget(self._page)
        outer_l.addWidget(page_scroll)
        v = QVBoxLayout(self._page)

        # ---- source ----
        src_box = QGroupBox("Data source")
        sf = _form(src_box)
        self.source = QComboBox()
        self.source.addItems(["auto", "usb", "ssh"])
        self.source.setCurrentText(prefs["source"])
        sf.addRow("Source", self.source)
        self.usb_documents = QLineEdit(prefs["usb_documents"])
        self.usb_documents.setPlaceholderText("blank = auto-detect connected Kindle")
        sf.addRow("USB documents folder", self._with_browse(
            self.usb_documents, "Choose the Kindle's documents folder"))
        db_filter = [("SQLite database", ["db"])]
        self.ccdb_path = QLineEdit(prefs["ccdb_path"])
        self.ccdb_path.setPlaceholderText("optional: a copy of cc.db")
        sf.addRow("cc.db file", self._with_browse(
            self.ccdb_path, "Choose a copy of the Kindle's cc.db", filters=db_filter))
        self.fmcache_path = QLineEdit(prefs["fmcache_path"])
        self.fmcache_path.setPlaceholderText("optional: a copy of fmcache.db")
        sf.addRow("fmcache.db file", self._with_browse(
            self.fmcache_path, "Choose a copy of the Kindle's fmcache.db", filters=db_filter))
        v.addWidget(src_box)

        help_box = QGroupBox("Help: what you need")
        hl = QVBoxLayout(help_box)
        help_label = QLabel(HELP_HTML)
        help_label.setWordWrap(True)
        help_label.setOpenExternalLinks(True)
        hl.addWidget(help_label)
        v.addWidget(help_box)

        # ---- ssh ----
        ssh_box = QGroupBox("SSH (usbnet) — only used when Source = ssh")
        ef = _form(ssh_box)
        self.ssh_host = QLineEdit(prefs["ssh_host"])
        self.ssh_user = QLineEdit(prefs["ssh_user"])
        self.ssh_password = QLineEdit(prefs["ssh_password"])
        self.ssh_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_key = QLineEdit(prefs["ssh_key"])
        self.ssh_key.setPlaceholderText("optional private-key path")
        ef.addRow("Host", self.ssh_host)
        ef.addRow("User", self.ssh_user)
        ef.addRow("Password", self.ssh_password)
        ef.addRow("Key file", self._with_browse(
            self.ssh_key, "Choose the SSH private key", filters=[]))
        support = QWidget()
        sh = QHBoxLayout(support)
        sh.setContentsMargins(0, 0, 0, 0)
        self.ssh_support = QLabel()
        sh.addWidget(self.ssh_support, 1)
        self.ssh_install = QPushButton("Install SSH support…")
        self.ssh_install.clicked.connect(self._install_ssh)
        sh.addWidget(self.ssh_install)
        ef.addRow("SSH support", support)
        self._show_ssh_support()
        v.addWidget(ssh_box)

        # ---- fields (checklist) ----
        col_box = QGroupBox("Fields to import (tick = add as a custom column)")
        outer = QVBoxLayout(col_box)
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)
        grid.addWidget(QLabel("<b>Field</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Type</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Column lookup name</b>"), 0, 2)
        self.field_checks = {}
        self.field_edits = {}
        for i, (key, label, dt, _dc, _on) in enumerate(FIELDS, start=1):
            cb = QCheckBox(label)
            cb.setChecked(field_on(key))
            e = QLineEdit(field_col(key))
            self.field_checks[key] = cb
            self.field_edits[key] = e
            grid.addWidget(cb, i, 0)
            grid.addWidget(QLabel(dt), i, 1)
            grid.addWidget(e, i, 2)
        outer.addLayout(grid)
        self.match_by_title = QCheckBox("Also match by title when Calibre's device matching and ASIN find nothing "
                                        "(only when the title is unambiguous)")
        self.match_by_title.setChecked(bool(prefs["match_by_title"]))
        outer.addWidget(self.match_by_title)
        self.auto_on_connect = QCheckBox("Import automatically when the Kindle connects")
        self.auto_on_connect.setChecked(bool(prefs["auto_on_connect"]))
        outer.addWidget(self.auto_on_connect)
        create = QPushButton("Create missing columns…")
        create.clicked.connect(self._create_columns)
        outer.addWidget(create)
        v.addWidget(col_box)

        # ---- dashboard ----
        d_box = QGroupBox("Web dashboard")
        df = _form(d_box)
        self.dashboard_py = QLineEdit(prefs["dashboard_py"])
        self.dashboard_py.setPlaceholderText("blank = built-in dashboard (recommended)")
        df.addRow("Custom dashboard.py", self._with_browse(
            self.dashboard_py, "Choose reader-dashboard/dashboard.py",
            filters=[("Python script", ["py"])]))
        v.addWidget(d_box)

        v.addWidget(QLabel("Changes to columns require a Calibre restart."))

    def sizeHint(self):
        want = self._page.sizeHint()
        h = want.height() + 20
        try:
            from qt.core import QApplication
            h = min(h, QApplication.primaryScreen().availableGeometry().height() - 160)
        except Exception:  # noqa
            pass
        return QSize(max(860, want.width() + 30), max(500, h))

    def _with_browse(self, edit, title, filters=None):
        """`edit` plus a Browse… button. filters=None picks a folder;
        otherwise a single file (filters as calibre's choose_files takes them)."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        btn = QPushButton("Browse…")
        h.addWidget(btn)

        def browse():
            from calibre.gui2 import choose_dir, choose_files
            if filters is None:
                path = choose_dir(self, "kindle-reading-dir", title)
            else:
                picked = choose_files(self, "kindle-reading-file", title, filters=filters,
                                      all_files=True, select_only_single_file=True)
                path = picked[0] if picked else None
            if path:
                edit.setText(path)

        btn.clicked.connect(browse)
        return row

    def _show_ssh_support(self):
        from calibre_plugins.kindle_reading import sshdeps
        ver = sshdeps.paramiko_version()
        if ver:
            self.ssh_support.setText("Installed (paramiko %s)" % ver)
            self.ssh_install.setText("Reinstall…")
        else:
            self.ssh_support.setText("Not installed: password login will not work")
            self.ssh_install.setText("Install SSH support…")

    def _install_ssh(self):
        import threading
        import traceback
        from calibre.gui2 import question_dialog, error_dialog, info_dialog
        from calibre_plugins.kindle_reading import sshdeps
        if not question_dialog(
                self, "Install SSH support",
                "Download paramiko and the libraries it needs (about 15 MB) "
                "from PyPI (pypi.org) into Calibre's config folder?\n\n"
                "Nothing is installed system-wide; delete the folder\n%s\n"
                "to remove it." % sshdeps.lib_dir()):
            return
        dlg = QProgressDialog("Starting…", None, 0, 100, self)
        dlg.setWindowTitle("Install SSH support")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.show()
        state = {"msg": "Starting…", "frac": 0.0, "done": False, "err": None}

        def work():
            try:
                sshdeps.install(lambda m, f: state.update(msg=m, frac=f))
            except Exception as e:  # noqa
                state["err"] = (str(e), traceback.format_exc())
            finally:
                state["done"] = True

        def tick():
            dlg.setLabelText(state["msg"])
            dlg.setValue(int(state["frac"] * 100))
            if not state["done"]:
                return
            self._install_timer.stop()
            dlg.close()
            self._show_ssh_support()
            if state["err"]:
                error_dialog(self, "Install failed",
                             "Could not install SSH support: %s" % state["err"][0],
                             det_msg=state["err"][1], show=True)
            elif sshdeps.paramiko_version():
                info_dialog(self, "SSH support installed",
                            "SSH support is ready (paramiko %s). Set Source = ssh "
                            "to use it." % sshdeps.paramiko_version(), show=True)
            else:
                error_dialog(self, "Install incomplete",
                             "The files were installed but paramiko does not load "
                             "yet. Restart Calibre and try again.", show=True)

        self._install_timer = QTimer(self)
        self._install_timer.timeout.connect(tick)
        self._install_timer.start(150)
        threading.Thread(target=work, daemon=True).start()

    def _create_columns(self):
        from calibre.gui2.ui import get_gui
        from calibre.gui2 import error_dialog, info_dialog
        gui = get_gui()
        if gui is None:
            return
        db = gui.library_view.model().db
        existing = db.field_metadata.custom_field_keys()
        created = []
        for key, label, dt, _dc, _on in FIELDS:
            if not self.field_checks[key].isChecked():
                continue
            lookup = self.field_edits[key].text().strip()
            if not lookup.startswith("#") or lookup in existing:
                continue
            try:
                db.create_custom_column(lookup[1:], "Kindle " + label, dt, False)
                created.append(lookup)
            except Exception as e:  # noqa
                error_dialog(self, "Column error",
                             "Could not create %s: %s" % (lookup, e), show=True)
                return
        if created:
            info_dialog(self, "Columns created",
                        "Created: %s\n\nRestart Calibre for them to appear."
                        % ", ".join(created), show=True)
        else:
            info_dialog(self, "Nothing to do",
                        "All ticked columns already exist (or none ticked).",
                        show=True)

    def save_settings(self):
        prefs["source"] = self.source.currentText()
        prefs["usb_documents"] = self.usb_documents.text().strip()
        prefs["ssh_host"] = self.ssh_host.text().strip()
        prefs["ssh_user"] = self.ssh_user.text().strip()
        prefs["ssh_password"] = self.ssh_password.text()
        prefs["ssh_key"] = self.ssh_key.text().strip()
        prefs["dashboard_py"] = self.dashboard_py.text().strip()
        prefs["ccdb_path"] = self.ccdb_path.text().strip()
        prefs["fmcache_path"] = self.fmcache_path.text().strip()
        prefs["match_by_title"] = bool(self.match_by_title.isChecked())
        prefs["auto_on_connect"] = bool(self.auto_on_connect.isChecked())
        on, cols = {}, {}
        for key, *_ in FIELDS:
            on[key] = bool(self.field_checks[key].isChecked())
            val = self.field_edits[key].text().strip()
            cols[key] = val or DEFAULT_COL[key]
        prefs["field_on"] = on
        prefs["field_col"] = cols
