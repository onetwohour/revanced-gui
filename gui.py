import re, queue
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Union
from multiprocessing import Queue

from PySide6.QtWidgets import (
    QWidget, QFileDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QCheckBox, QProgressBar, QMessageBox,
    QListWidget, QListWidgetItem, QSplitter, QGroupBox, QFormLayout,
    QHeaderView, QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QSizePolicy, QTabWidget, QComboBox, QScrollArea
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor

from utils import _ensure_dir, _clear_form_layout, _try_extract_package_from_apk, _dir_is_empty, CLI_RELEASE_URL, PATCHES_RELEASE_URL

class PatchPickerDialog(QDialog):
    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("패치 선택")
        self.resize(1200, 800)
        self.entries = entries
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit(self); self.search.setPlaceholderText("이름/패키지 검색…")
        btn_sel_all = QPushButton("전체 선택")
        btn_unselect = QPushButton("전체 해제")
        top.addWidget(self.search); top.addWidget(btn_sel_all); top.addWidget(btn_unselect)
        lay.addLayout(top)
        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["사용","Index","Name","Packages"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        lay.addWidget(btns)
        self._all_rows = list(self.entries)
        self._rebuild(self._all_rows)
        self.search.textChanged.connect(self._apply_filter)
        btn_sel_all.clicked.connect(self._select_all)
        btn_unselect.clicked.connect(self._unselect_all)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

    def _rebuild(self, rows):
        self.table.setRowCount(0)
        for e in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QCheckBox()
            chk.setChecked(bool(e.get("enabled")))
            chk.stateChanged.connect(lambda s, entry=e: entry.__setitem__("enabled", s == Qt.Checked))
            cell = QWidget(); h = QHBoxLayout(cell); h.setContentsMargins(4,0,0,0); h.addWidget(chk); h.addStretch()
            self.table.setCellWidget(r, 0, cell)
            it_idx = QTableWidgetItem(str(e.get("index"))); it_name = QTableWidgetItem(e.get("name","")); it_pkgs = QTableWidgetItem(", ".join(e.get("packages",[])))
            self.table.setItem(r,1,it_idx); self.table.setItem(r,2,it_name); self.table.setItem(r,3,it_pkgs)

    def _apply_filter(self):
        q = self.search.text().strip().lower()
        if not q:
            self._rebuild(self._all_rows); return
        rows=[]
        for e in self._all_rows:
            if q in e.get("name","").lower() or any(q in p.lower() for p in e.get("packages",[])):
                rows.append(e)
        self._rebuild(rows)

    def _iter_checkboxes(self):
        for r in range(self.table.rowCount()):
            cell = self.table.cellWidget(r,0)
            chk = cell.findChild(QCheckBox)
            yield r, chk

    def _select_all(self):
        for _, chk in self._iter_checkboxes():
            chk.setChecked(True)

    def _unselect_all(self):
        for _, chk in self._iter_checkboxes():
            chk.setChecked(False)

    def get_enabled(self) -> Tuple[List[int], List[str]]:
        idxs, names = [], []
        for r, chk in self._iter_checkboxes():
            if chk.isChecked():
                idx_item = self.table.item(r,1)
                name_item = self.table.item(r,2)
                try: idxs.append(int(idx_item.text()))
                except: pass
                names.append(name_item.text())
        return idxs, names

class App(QWidget):
    def __init__(self, q_in: Queue, q_out: Queue):
        super().__init__()
        self.setWindowTitle("ReVanced GUI")
        self.resize(1250, 860)
        
        self.out_dir = Path.cwd() / "output"
        _ensure_dir(self.out_dir)
        
        self.cli_jar: Optional[Path] = None
        self.rvp_file: Optional[Path] = None
        self._patches_to_check_on_load: List[str] = []
        
        self._qin = q_in
        self._qout = q_out
        
        self.entries = []
        self._keep_idx = set()
        self._keep_name = set()
        self.reset_select = True
        self.dynamic_option_widgets: Dict[str, QWidget] = {}
        self._auto_list_after_download = False
        
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        
        self._drain_timer = QTimer(self); self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._drain_queues)
        self._drain_timer.start()
        
        QTimer.singleShot(0, self.on_env_check)

    def _create_widgets(self):
        self.tab_widget = QTabWidget()
        self.setup_tab = self._create_setup_tab()
        self.patch_tab = self._create_patch_tab()
        self.adb_tab = self._create_adb_tab()
        
        self.tab_widget.addTab(self.setup_tab, "시작")
        self.tab_widget.addTab(self.patch_tab, "패치")
        self.tab_widget.addTab(self.adb_tab, "ADB")

        self.progress = QProgressBar()
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setRange(0, 1); self.progress.setValue(0)
        
        self.log = QTextEdit(); self.log.setReadOnly(True)

    def _create_setup_tab(self) -> QWidget:
        tab = QWidget()
        setup_layout = QVBoxLayout(tab)
        
        env_box = QGroupBox("1. 환경 점검")
        env_form = QFormLayout()
        self.java_status = QLabel("Java: 미확인")
        self.adb_status_env = QLabel("ADB: 미확인")
        self.btn_env_check = QPushButton("환경 점검")
        self.btn_java = QPushButton("Java 설치")
        self.btn_adb_env = QPushButton("ADB 설치")
        env_form.addRow(self.java_status)
        env_form.addRow(self.adb_status_env)
        h_btn_box = QHBoxLayout(); h_btn_box.addWidget(self.btn_env_check); h_btn_box.addWidget(self.btn_java); h_btn_box.addWidget(self.btn_adb_env)
        env_form.addRow(h_btn_box)
        env_box.setLayout(env_form)
        
        dl_box = QGroupBox("2. ReVanced 구성요소 다운로드")
        dl_lay = QFormLayout()
        self.cli_url_edit = QLineEdit(); self.cli_url_edit.setPlaceholderText(CLI_RELEASE_URL)
        self.rvp_url_edit = QLineEdit(); self.rvp_url_edit.setPlaceholderText(PATCHES_RELEASE_URL)
        self.btn_dl = QPushButton("다운로드")
        dl_lay.addRow("CLI(.jar) URL", self.cli_url_edit)
        dl_lay.addRow("패치(.rvp) URL", self.rvp_url_edit)
        self.cli_path_lbl = QLabel("CLI: 미설정")
        self.btn_pick_cli = QPushButton("파일 선택")
        cli_row_widget = QWidget(); cli_row_layout = QHBoxLayout(cli_row_widget); cli_row_layout.setContentsMargins(0, 0, 0, 0)
        cli_row_layout.addWidget(self.cli_path_lbl, 1); cli_row_layout.addWidget(self.btn_pick_cli)
        dl_lay.addRow(cli_row_widget)
        self.rvp_path_lbl = QLabel("패치 번들: 미설정")
        self.btn_pick_rvp = QPushButton("파일 선택")
        rvp_row_widget = QWidget(); rvp_row_layout = QHBoxLayout(rvp_row_widget); rvp_row_layout.setContentsMargins(0, 0, 0, 0)
        rvp_row_layout.addWidget(self.rvp_path_lbl, 1); rvp_row_layout.addWidget(self.btn_pick_rvp)
        dl_lay.addRow(rvp_row_widget)
        dl_lay.addRow(self.btn_dl)
        dl_box.setLayout(dl_lay)

        in_box = QGroupBox("3. 원본 APK 파일 선택")
        form = QFormLayout()
        self.apk_edit = QLineEdit()
        self.btn_apk = QPushButton("APK 선택")
        apk_row = QHBoxLayout(); apk_row.addWidget(self.apk_edit); apk_row.addWidget(self.btn_apk)
        self.pkg_edit = QLineEdit(); self.pkg_edit.setPlaceholderText("APK 선택 시 자동 감지")
        form.addRow("APK 파일 경로", apk_row)
        form.addRow("패키지명", self.pkg_edit)
        in_box.setLayout(form)
        
        setup_layout.addWidget(env_box)
        setup_layout.addWidget(dl_box)
        setup_layout.addWidget(in_box)
        setup_layout.addStretch(1)
        return tab

    def _create_patch_tab(self) -> QWidget:
        tab = QWidget()
        patch_layout = QVBoxLayout(tab)
        
        patch_box = QGroupBox("4. 패치 목록 설정 및 선택")
        p_lay = QVBoxLayout()
        patch_opts_layout = QHBoxLayout()
        self.include_universal = QCheckBox("유니버설 패치 포함")
        self.exclusive = QCheckBox("선택한 패치만 적용 (권장)"); self.exclusive.setChecked(True)
        patch_opts_layout.addWidget(self.include_universal); patch_opts_layout.addWidget(self.exclusive)
        self.btn_list = QPushButton("패치 목록 새로고침")
        self.list_widget = QListWidget(); self.list_widget.setWordWrap(True); self.list_widget.setUniformItemSizes(False); self.list_widget.setSpacing(2)
        self.list_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.btn_picker = QPushButton("새 창에서 패치 선택하기")
        patch_file_btns = QHBoxLayout()
        self.btn_export = QPushButton("선택 내보내기")
        self.btn_import = QPushButton("선택 불러오기")
        self.btn_preset_clone = QPushButton("프리셋")
        patch_file_btns.addWidget(self.btn_export); patch_file_btns.addWidget(self.btn_import); patch_file_btns.addWidget(self.btn_preset_clone)
        p_lay.addLayout(patch_opts_layout)
        p_lay.addWidget(self.btn_list); p_lay.addWidget(self.list_widget); p_lay.addWidget(self.btn_picker)
        p_lay.addLayout(patch_file_btns)
        patch_box.setLayout(p_lay)

        opt_box = QGroupBox("5. 빌드 옵션")
        opt = QFormLayout()
        self.change_pkg_input = QLineEdit(); self.change_pkg_input.setPlaceholderText('패키지명을 변경하려면 "Change package name" 패치 활성화 필요')
        self.update_perms = QCheckBox("Update permissions 적용")
        self.update_providers = QCheckBox("Update providers 적용")
        self.keystore_edit = QLineEdit()
        self.btn_ks = QPushButton("키스토어 선택")
        ks_row = QHBoxLayout(); ks_row.addWidget(self.keystore_edit); ks_row.addWidget(self.btn_ks)
        self.ks_pass = QLineEdit(); self.ks_pass.setEchoMode(QLineEdit.Password)
        self.alias = QLineEdit()
        self.alias_pass = QLineEdit(); self.alias_pass.setEchoMode(QLineEdit.Password)
        self.tmp_dir_edit = QLineEdit(); self.tmp_dir_edit.setPlaceholderText(r"비워두면 output/work 폴더 사용")
        self.btn_tmp = QPushButton("임시폴더 선택")
        tmp_row = QHBoxLayout(); tmp_row.addWidget(self.tmp_dir_edit); tmp_row.addWidget(self.btn_tmp)
        opt.addRow("임시파일 경로", tmp_row)
        opt.addRow("패키지명 변경", self.change_pkg_input)
        opt.addRow(self.update_perms); opt.addRow(self.update_providers)
        opt.addRow("Keystore", ks_row); opt.addRow("Keystore 비밀번호", self.ks_pass)
        opt.addRow("Key alias", self.alias); opt.addRow("Key 비밀번호", self.alias_pass)
        self.dynamic_options_box = QGroupBox("패치별 세부 옵션")
        self.dynamic_options_layout = QFormLayout()
        self.dynamic_options_box.setLayout(self.dynamic_options_layout)
        self.dynamic_options_scroll_area = QScrollArea()
        self.dynamic_options_scroll_area.setWidgetResizable(True)
        self.dynamic_options_scroll_area.setWidget(self.dynamic_options_box)
        self.dynamic_options_scroll_area.setVisible(False)
        opt_v_layout = QVBoxLayout()
        opt_v_layout.addLayout(opt)
        opt_v_layout.addWidget(self.dynamic_options_scroll_area, 1)
        opt_box.setLayout(opt_v_layout)

        build_box = QGroupBox("6. 빌드 실행")
        b_lay = QVBoxLayout()
        self.btn_build = QPushButton("패치 실행")
        b_lay.addWidget(self.btn_build)
        build_box.setLayout(b_lay)
        
        patch_layout.addWidget(patch_box)
        patch_layout.addWidget(opt_box)
        patch_layout.addWidget(build_box)
        return tab

    def _create_adb_tab(self) -> QWidget:
        tab = QWidget()
        adb_layout = QVBoxLayout(tab)
        
        adb_box = QGroupBox("ADB 설정")
        adb_form = QFormLayout()
        self.adb_status = QLabel("ADB: 미확인")
        self.adb_path_edit = QLineEdit(); self.adb_path_edit.setPlaceholderText(r"예: C:\Android\platform-tools\adb.exe")
        self.btn_adb_browse = QPushButton("찾기")
        adb_row = QHBoxLayout(); adb_row.addWidget(self.adb_path_edit); adb_row.addWidget(self.btn_adb_browse)
        self.btn_adb_install = QPushButton("ADB 설치")
        self.adb_install_check = QCheckBox("빌드 후 ADB로 자동 설치")
        dev_row = QHBoxLayout()
        self.adb_device_edit = QLineEdit()
        self.adb_device_edit.setPlaceholderText("자동 감지 또는 직접 입력")
        self.btn_adb_refresh = QPushButton("ADB 기기 새로고침")
        dev_row.addWidget(self.adb_device_edit)
        dev_row.addWidget(self.btn_adb_refresh)
        adb_form.addRow(self.adb_status)
        adb_form.addRow("ADB 경로", adb_row)
        adb_form.addRow(self.btn_adb_install)
        adb_form.addRow(self.adb_install_check)
        adb_form.addRow("설치 대상 기기", dev_row)
        adb_box.setLayout(adb_form)
        
        adb_layout.addWidget(adb_box)
        adb_layout.addStretch(1)
        return tab

    def _create_layouts(self):
        root = QHBoxLayout(self)
        split = QSplitter()
        
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.tab_widget)
        left_layout.addWidget(self.progress)
        
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("실시간 로그")); right_layout.addWidget(self.log)
        
        split.addWidget(left_panel); split.addWidget(right_panel)
        split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setSizes([720,900])
        root.addWidget(split)
        
    def _connect_signals(self):
        self.btn_env_check.clicked.connect(self.on_env_check)
        self.btn_java.clicked.connect(self.on_java_install)
        self.btn_adb_env.clicked.connect(self.on_adb_install)
        self.btn_dl.clicked.connect(self.on_download)
        self.btn_pick_cli.clicked.connect(self.pick_cli_file)
        self.btn_pick_rvp.clicked.connect(self.pick_rvp_file)
        self.btn_apk.clicked.connect(self.pick_apk)
        
        self.include_universal.checkStateChanged.connect(lambda: self.on_list_patches() if self.cli_jar and self.rvp_file else None)
        self.btn_list.clicked.connect(self.on_list_patches)
        self.btn_picker.clicked.connect(self.open_patch_picker)
        self.btn_export.clicked.connect(self.export_selection)
        self.btn_import.clicked.connect(self.import_selection)
        self.btn_preset_clone.clicked.connect(self.apply_preset)
        self.btn_ks.clicked.connect(self.pick_keystore)
        self.btn_tmp.clicked.connect(self.pick_tmp_dir)
        self.list_widget.itemChanged.connect(self._update_dynamic_options)
        self.btn_build.clicked.connect(self.on_build)
        
        self.btn_adb_browse.clicked.connect(self.pick_adb_path)
        self.btn_adb_install.clicked.connect(self.on_adb_install)
        self.btn_adb_refresh.clicked.connect(self.on_adb_refresh)

    def closeEvent(self, e):
        try:
            self._qin.put({"cmd":"adb_kill"})
            self._qin.put(None)
        except Exception:
            pass
        return super().closeEvent(e)

    def _pb_busy(self):
        self.progress.setRange(0, 0)

    def _pb_idle(self):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

    def _pb_set(self, pct: int):
        self.progress.setRange(0, 100)
        self.progress.setValue(max(0, min(100, int(pct))))

    def _drain_queues(self):
        drained = False
        while True:
            try:
                m = self._qout.get_nowait()
            except queue.Empty:
                break
                
            drained = True
            t = m.get("type")
            
            if t == "log":
                self.log.append(m.get("text",""))
            elif t == "fail":
                QMessageBox.warning(self, "실패", m.get("error","오류"))
                self._pb_idle()
            elif t == "done":
                self._pb_idle()
            elif t == "progress":
                if m.get("phase") == "download":
                    self._pb_set(int(m.get("value", 0)))
            elif t == "env":
                java_ok = m.get("java_ok"); jline = (m.get("java_out","").splitlines()[0] if m.get("java_out") else "")
                self.java_status.setText(f"Java: {'OK' if java_ok else '미설치/버전 불가'} ({jline})")
                adb_ok = m.get("adb_ok")
                self.adb_status_env.setText(f"ADB: {'OK' if adb_ok else '없음'}")
                self.adb_status.setText(f"ADB: {'OK' if adb_ok else '없음'}")
            elif t == "download_ok":
                self.cli_jar = Path(m["cli"]); self.rvp_file = Path(m["rvp"])
                self.cli_path_lbl.setText(f"CLI: {self.cli_jar.name}")
                self.rvp_path_lbl.setText(f"패치 번들: {self.rvp_file.name}")
                if getattr(self, "_auto_list_after_download", False):
                    self._auto_list_after_download = False
                    if self.pkg_edit.text():
                        self.reset_select = True
                        QTimer.singleShot(0, self.on_list_patches)
            elif t == "patches":
                self.entries = m.get("entries",[])
                try:
                    self.list_widget.itemChanged.disconnect(self._update_dynamic_options)
                except RuntimeError:
                    pass
                self.list_widget.clear()
                for e in self.entries:
                    if e.get('index') is None: continue
                    label = f"[{e.get('index')}] {e.get('name','')}"
                    pkgs = e.get("packages",[])
                    if pkgs:
                        label += f"  ({', '.join(pkgs)})"
                    item = QListWidgetItem(label)
                    if self.reset_select:
                        keep = bool(e.get("enabled"))
                    else:
                        keep = (e.get('index') in self._keep_idx) or (e.get('name') in self._keep_name)
                    item.setCheckState(Qt.Checked if keep else Qt.Unchecked)
                    self.list_widget.addItem(item)
                self.reset_select = False
                self.list_widget.itemChanged.connect(self._update_dynamic_options)
                self._update_dynamic_options()
                self.log.append(f"[OK] 패치 목록 불러오기 완료: {self.pkg_edit.text() or 'APK 미지정'}")
                if getattr(self, "_patches_to_check_on_load", []):
                    patches_to_check = set(self._patches_to_check_on_load)
                    for i in range(self.list_widget.count()):
                        item = self.list_widget.item(i)
                        item_name = self._extract_item_name(item.text())
                        if item_name in patches_to_check:
                            item.setCheckState(Qt.Checked)
                    self._patches_to_check_on_load = []
            elif t == "pkg":
                val = m.get("value")
                if val:
                    self.pkg_edit.setText(val)
            elif t == "build_begin":
                self._pb_busy()
            elif t == "build_end":
                self._pb_idle()
            elif t == "build_ok":
                self.log.append(f"[DONE] 빌드 완료 → {m.get('apk')}")
                if self.adb_install_check.isChecked():
                    serial_text = (self.adb_device_edit.text() or "").strip()
                    serial = serial_text.split()[0] if serial_text else ""
                    self._pb_busy()
                    self.log.append(f"[ADB] 설치 시작 (serial={serial or 'auto'})")
                    self._qin.put({"cmd":"adb_install_apk","serial":serial,"apk":m.get('apk')})
                else:
                    self._pb_idle()
            elif t == "adb_devices":
                devs = m.get("devices") or []
                if len(devs) == 1:
                    ser = devs[0].get("serial",""); mdl = devs[0].get("model","")
                    shown = f"{ser}" + (f"  ({mdl})" if mdl else "")
                    self.adb_device_edit.setText(shown)
                    self.log.append(f"[ADB] 1대 연결됨: {shown}")
                elif len(devs) > 1:
                    sers = [ (d.get("serial","") + (f'({d.get("model","")})' if d.get("model") else "")) for d in devs ]
                    self.log.append(f"[ADB] 여러 대 연결됨:\n  - " + "\n  - ".join(sers))
                    if not self.adb_device_edit.text().strip():
                        d0 = devs[0]
                        shown = d0.get("serial","") + (f"  ({d0.get('model','')})" if d0.get("model") else "")
                        self.adb_device_edit.setText(shown)
                else:
                    self.log.append("[ADB] 연결된 디바이스 없음")
            elif t == "adb_install_ok":
                apk = m.get("apk"); ser = m.get("serial","")
                self.log.append(f"[ADB] 설치 완료: {apk} → {ser or 'single-device'}")
                self._pb_idle()
            elif t == "adb_path_set":
                ok = m.get("ok"); p = m.get("path") or ""
                if p:
                    self.adb_path_edit.setText(p)
                    self.log.append(f"[SET] ADB 경로: {p} ({'확인' if ok else '미확인'})")
                    
        if drained:
            self.log.moveCursor(QTextCursor.End)
            self.log.ensureCursorVisible()

    def _remember_selection(self):
        self._keep_idx.clear()
        self._keep_name.clear()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                m = re.match(r'^\[(\d+)\]\s+(.*)$', item.text())
                if m and m.group(1).isdigit():
                    self._keep_idx.add(int(m.group(1)))
                    self._keep_name.add(self._extract_item_name(item.text()))
                else:
                    self._keep_name.add(self._extract_item_name(item.text()))

    def _update_dynamic_options(self, item: Optional[QListWidgetItem] = None):
        _clear_form_layout(self.dynamic_options_layout)
        self.dynamic_option_widgets.clear()
        selected_patch_indices = set()
        for i in range(self.list_widget.count()):
            list_item = self.list_widget.item(i)
            if list_item.checkState() == Qt.Checked:
                match = re.match(r'^\[(\d+)\]', list_item.text())
                if match and match.group(1).isdigit():
                    selected_patch_indices.add(int(match.group(1)))
        found_options = False
        for patch in self.entries:
            patch_index = patch.get("index")
            if patch_index is not None and patch_index in selected_patch_indices and "options" in patch:
                for option in patch['options']:
                    key = option.get('key')
                    if not key: continue
                    if key in {"packageName", "updatePermissions", "updateProviders"}: continue
                    title = option.get('title', key)
                    desc = option.get('description', '')
                    default_val = option.get('default')
                    widget = None
                    custom_option_text = "직접 입력..."
                    if "possible_values" in option:
                        widget = QWidget()
                        widget.setProperty("is_composite", True)
                        layout = QHBoxLayout(widget)
                        layout.setContentsMargins(0, 0, 0, 0)
                        combo = QComboBox()
                        items = option["possible_values"]
                        combo.addItems(items)
                        combo.addItem(custom_option_text)
                        combo.setToolTip(desc)
                        line_edit = QLineEdit()
                        line_edit.setPlaceholderText("사용자 정의 값 입력")
                        line_edit.setVisible(False)
                        layout.addWidget(combo)
                        layout.addWidget(line_edit)
                        is_list_type = False
                        if default_val is not None:
                            if default_val.strip().startswith('[') and default_val.strip().endswith(']'):
                                is_list_type = True
                            found_idx = -1
                            for i, item_text in enumerate(items):
                                if item_text.strip().startswith(default_val):
                                    found_idx = i
                                    break
                            if found_idx != -1:
                                combo.setCurrentIndex(found_idx)
                            else:
                                combo.setCurrentText(custom_option_text)
                                line_edit.setText(default_val)
                                line_edit.setVisible(True)
                        line_edit.setProperty("is_list_type", is_list_type)
                        combo.currentTextChanged.connect(
                            lambda text, le=line_edit, custom_text=custom_option_text: le.setVisible(text == custom_text)
                        )
                        widget.setProperty("combo_widget", combo)
                        widget.setProperty("line_edit_widget", line_edit)
                    else:
                        widget = QLineEdit()
                        widget.setProperty("is_composite", False)
                        widget.setPlaceholderText(desc)
                        if default_val is not None:
                            widget.setText(default_val)
                            if default_val.strip().startswith('[') and default_val.strip().endswith(']'):
                                widget.setProperty("is_list_type", True)
                            else:
                                widget.setProperty("is_list_type", False)
                        else:
                            widget.setPlaceholderText(desc)
                            widget.setProperty("is_list_type", False)
                    if widget:
                        label = title
                        self.dynamic_options_layout.addRow(label, widget)
                        widget_key = f"{patch_index}_{key}"
                        self.dynamic_option_widgets[widget_key] = widget
                        found_options = True
        self.dynamic_options_scroll_area.setVisible(found_options)

    @staticmethod
    def _extract_item_name(item_text: str) -> str:
        txt = re.sub(r'^\s*\[\d+\]\s*', '', item_text).strip()
        m = re.match(r'(.+?)(\s*\(.*\))?$', txt)
        return (m.group(1).strip() if m else txt)

    def on_env_check(self):
        path = (self.adb_path_edit.text() or "").strip()
        self._qin.put({"cmd":"set_adb_path","path":path})
        self._pb_busy()
        self._qin.put({"cmd":"env_check"})

    def on_java_install(self):
        self._pb_busy()
        self._qin.put({"cmd":"install_java"})

    def on_git_install(self):
        self._pb_busy()
        self._qin.put({"cmd":"install_git"})

    def on_adb_install(self):
        path = (self.adb_path_edit.text() or "").strip()
        self._qin.put({"cmd":"set_adb_path","path":path})
        self._pb_busy()
        self.log.append("[RUN] ADB 설치 시작")
        self._qin.put({"cmd":"install_adb"})

    def on_adb_refresh(self):
        path = (self.adb_path_edit.text() or "").strip()
        self._qin.put({"cmd":"set_adb_path","path":path})
        self._pb_busy()
        self._qin.put({"cmd":"adb_devices"})

    def pick_apk(self):
        path, _ = QFileDialog.getOpenFileName(self, "APK 선택", "", "APK (*.apk)")
        if not path: return
        self.apk_edit.setText(path)
        self.change_pkg_input.setPlaceholderText('패키지명을 변경하려면 "Change package name" 패치 활성화 필요')
        self._pb_busy()
        self._qin.put({"cmd":"detect_package","apk":path})

    def pick_cli_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "ReVanced CLI 선택", "", "Java Archive (*.jar)")
        if not path: return
        self.cli_jar = Path(path)
        self.cli_path_lbl.setText(f"CLI: {self.cli_jar.name}")
        self.log.append(f"[OK] CLI 파일 선택됨: {path}")
        if self.cli_jar and self.rvp_file and self.pkg_edit.text():
            self.reset_select = True
            QTimer.singleShot(0, self.on_list_patches)

    def pick_rvp_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "ReVanced Patches 선택", "", "ReVanced Patches (*.rvp)")
        if not path: return
        self.rvp_file = Path(path)
        self.rvp_path_lbl.setText(f"패치 번들: {self.rvp_file.name}")
        self.log.append(f"[OK] RVP 파일 선택됨: {path}")
        if self.cli_jar and self.rvp_file and self.pkg_edit.text():
            self.reset_select = True
            QTimer.singleShot(0, self.on_list_patches)

    def pick_keystore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Keystore 선택", "", "Keystore (*.jks *.keystore *.p12)")
        if path: self.keystore_edit.setText(path)

    def pick_adb_path(self):
        title = "ADB 실행 파일 선택"
        filt = "adb (adb.exe adb);;모든 파일 (*.*)"
        path, _ = QFileDialog.getOpenFileName(self, title, "", filt)
        if path:
            self.adb_path_edit.setText(path)
            self._pb_busy()
            self._qin.put({"cmd": "set_adb_path", "path": path})
            self._qin.put({"cmd": "adb_devices_silent"})

    def on_download(self):
        self._pb_busy()
        self._auto_list_after_download = True
        self._qin.put({
            "cmd":"download_components",
            "out_dir":str(self.out_dir),
            "cli_url": self.cli_url_edit.text(),
            "cli_path": str(self.cli_jar) if self.cli_jar else "",
            "rvp_url": self.rvp_url_edit.text(),
            "rvp_path": str(self.rvp_file) if self.rvp_file else "",
        })

    def on_list_patches(self):
        if not self.cli_jar or not self.rvp_file:
            QMessageBox.information(self, "안내", "먼저 CLI/패치 번들을 다운로드하세요.")
            return
        self._pb_busy()
        self._remember_selection()
        if not self.dynamic_option_widgets: self.reset_select = True
        self._qin.put({
            "cmd":"list_patches",
            "cli":str(self.cli_jar),
            "rvp":str(self.rvp_file),
            "pkg":self.pkg_edit.text(),
            "inc_univ":self.include_universal.isChecked()
        })

    def open_patch_picker(self):
        if not self.entries:
            QMessageBox.information(self, "안내", "먼저 ‘패치 목록 새로고침’을 실행해 주세요.")
            return
        enabled_idx = set()
        enabled_name = set()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                m = re.match(r'^\[(\d+)\]\s+(.*)$', item.text())
                if m:
                    enabled_idx.add(int(m.group(1)))
                enabled_name.add(self._extract_item_name(item.text()))
        for e in self.entries:
            idx = e.get("index")
            nm  = e.get("name","")
            e["enabled"] = (idx in enabled_idx) or (nm in enabled_name)
        dlg = PatchPickerDialog(self.entries, self)
        dlg.showMaximized()
        if dlg.exec() == QDialog.Accepted:
            idxs, names = dlg.get_enabled()
            want = set(idxs)
            try:
                self.list_widget.itemChanged.disconnect(self._update_dynamic_options)
            except RuntimeError:
                pass
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                m = re.match(r'^\[(\d+)\]\s+(.*)$', item.text())
                is_on = False
                if m:
                    is_on = int(m.group(1)) in want
                else:
                    nm = self._extract_item_name(item.text())
                    is_on = any(nm == n or n in nm for n in names)
                item.setCheckState(Qt.Checked if is_on else Qt.Unchecked)
            self.list_widget.itemChanged.connect(self._update_dynamic_options)
            self._update_dynamic_options()

    def export_selection(self):
        path, _ = QFileDialog.getSaveFileName(self, "선택 내보내기", "patch_selection.txt", "Text (*.txt)")
        if not path: return
        idxs=[]
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState()==Qt.Checked:
                m = re.match(r'^\[(\d+)\]\s+', item.text())
                if m: idxs.append(m.group(1))
        with open(path,"w",encoding="utf-8") as f:
            f.write("\n".join(idxs))
        self.log.append(f"[OK] 선택 인덱스 {len(idxs)}개 내보냄 → {path}")

    def import_selection(self):
        path, _ = QFileDialog.getOpenFileName(self, "선택 불러오기", "", "Text (*.txt)")
        if not path: return
        with open(path,"r",encoding="utf-8") as f:
            want=set()
            for line in f:
                line=line.strip()
                if line.isdigit(): want.add(int(line))
        hit=0
        try:
            self.list_widget.itemChanged.disconnect(self._update_dynamic_options)
        except RuntimeError:
            pass
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            m = re.match(r'^\[(\d+)\]\s+', item.text())
            if m and int(m.group(1)) in want:
                item.setCheckState(Qt.Checked); hit+=1
            else:
                item.setCheckState(Qt.Unchecked)
        self.list_widget.itemChanged.connect(self._update_dynamic_options)
        self._update_dynamic_options()
        self.log.append(f"[OK] 불러온 인덱스 {len(want)}개 중 {hit}개 적용")

    def apply_preset(self):
        if self.list_widget.count() == 0:
            QMessageBox.information(self, "안내", "먼저 ‘패치 목록 새로고침’을 실행해 주세요.")
            return
        self._patches_to_check_on_load = []
        base_pkg = self.pkg_edit.text().strip() if hasattr(self, "pkg_edit") else ""
        if not base_pkg:
            apk_path = self.apk_edit.text().strip() if hasattr(self, "apk_edit") else ""
            if apk_path and Path(apk_path).exists():
                try:
                    base_pkg = _try_extract_package_from_apk(Path(apk_path)) or ""
                except Exception:
                    base_pkg = ""
        if False and base_pkg == "com.kakao.talk": # Risk of ban when cloning
            self._patches_to_check_on_load.append('Change package name')
            self._patches_to_check_on_load.append('Ignore Check Package Name')
            if hasattr(self, "update_perms"):
                self.update_perms.setChecked(True)
            if hasattr(self, "update_providers"):
                self.update_providers.setChecked(True)
            if hasattr(self, "include_universal"):
                self.include_universal.setChecked(True)
            self.change_pkg_input.setPlaceholderText(f'e.g. {base_pkg + ".revanced"}')
            self.change_pkg_input.setText(base_pkg + ".revanced")
        else:
            self.update_perms.setChecked(False)
            self.update_providers.setChecked(False)
            self.include_universal.blockSignals(True)
            self.include_universal.setChecked(False)
            self.include_universal.blockSignals(False)
            self.change_pkg_input.setText("")
            if base_pkg: self.change_pkg_input.setPlaceholderText(f'e.g. {base_pkg + ".revanced"}')
            else: self.change_pkg_input.setPlaceholderText('패키지명을 변경하려면 "Change package name" 패치 활성화 필요')
            
        self.exclusive.setChecked(True)
        self.reset_select = True
        QTimer.singleShot(0, self.on_list_patches)
        self.log.append(f"[PRESET] 프리셋 적용: pkg={base_pkg or '(미지정)'}")

    def pick_tmp_dir(self):
        path = QFileDialog.getExistingDirectory(self, "임시폴더 선택", "")
        if not path:
            return
        p_tmp = Path(path).resolve()
        root_dir = Path.cwd().resolve()
        out_dir  = self.out_dir.resolve()
        if p_tmp.exists() and not p_tmp.is_dir():
            QMessageBox.warning(self, "잘못된 경로", f"선택한 경로가 폴더가 아닙니다.\n\n경로: {p_tmp}")
            return
        if p_tmp.exists() and not _dir_is_empty(p_tmp):
            QMessageBox.warning(
                self,
                "비어 있지 않은 폴더",
                f"선택한 폴더가 비어 있지 않습니다.\n\n"
                f"경로: {p_tmp}\n\n"
                "빌드용 임시 폴더는 반드시 비어 있거나 새 폴더여야 합니다."
            )
            return
        if p_tmp == root_dir or p_tmp == out_dir:
            QMessageBox.warning(
                self, "임시폴더 제한",
                f"프로젝트 루트/산출물 폴더는 임시폴더로 사용할 수 없습니다.\n\n경로: {p_tmp}"
            )
            return
        try:
            p_tmp.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "폴더 생성 실패", f"폴더를 만들 수 없습니다.\n\n경로: {p_tmp}\n에러: {e}")
            return
        self.tmp_dir_edit.setText(str(p_tmp))

    def on_build(self):
        if not self.cli_jar or not self.cli_jar.exists():
            QMessageBox.information(self, "안내", "CLI .jar를 먼저 다운로드하세요."); return
        if not self.rvp_file or not self.rvp_file.exists():
            QMessageBox.information(self, "안내", "패치 번들(.rvp)을 먼저 다운로드하세요."); return
        apk_path = self.apk_edit.text().strip()
        if not apk_path or not Path(apk_path).exists():
            QMessageBox.information(self, "안내", "APK 파일을 선택하세요."); return
            
        path = (self.adb_path_edit.text() or "").strip()
        self._qin.put({"cmd":"set_adb_path","path":path})
        
        in_apk = Path(apk_path)
        out_name = in_apk.stem + "-revanced.apk"
        out_apk = (self.out_dir / out_name)
        
        includes_by_idx: List[int] = []
        includes_by_name: List[str] = []
        index_to_option_keys: Dict[int, List[str]] = {}
        name_to_option_keys: Dict[str, List[str]] = {}
        pkgs_map: Dict[Union[int, str], List[str]] = {}
        
        for e in self.entries:
            idx = e.get("index")
            name = e.get("name")
            packages = e.get('packages', [])
            option_keys = [opt['key'] for opt in e.get('options', []) if opt.get('key')]
            if idx is not None:
                pkgs_map[idx] = packages
                if option_keys:
                    index_to_option_keys[idx] = option_keys
            if name:
                pkgs_map[name] = packages
                if option_keys:
                    name_to_option_keys[name] = option_keys
                    
        current_pkg = (self.pkg_edit.text() or "").strip().lower()
        include_universal_checked = self.include_universal.isChecked()
        
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                m = re.match(r'^\[(\d+)\]\s+(.*)$', item.text())
                nm = self._extract_item_name(item.text())
                idx = int(m.group(1)) if m and m.group(1).isdigit() else None
                pkg_list_for_this_patch = []
                identifier = idx if idx is not None else nm
                if identifier in pkgs_map:
                    pkg_list_for_this_patch = pkgs_map[identifier]
                is_universal = (len(pkg_list_for_this_patch) == 0)
                is_for_this_pkg = bool(current_pkg and current_pkg in [p.lower() for p in pkg_list_for_this_patch])
                
                if (include_universal_checked and is_universal) or is_for_this_pkg or not current_pkg:
                    if idx is not None:
                        includes_by_idx.append(idx)
                    elif nm:
                        includes_by_name.append(nm)
                        
        all_options_values: Dict[str, Optional[str]] = {}
        chpkg = self.change_pkg_input.text().strip()
        if chpkg:
            all_options_values["packageName"] = chpkg
        if self.update_perms.isChecked():
            all_options_values["updatePermissions"] = "true"
        if self.update_providers.isChecked():
            all_options_values["updateProviders"] = "true"
            
        if chpkg:
            change_pkg_enabled = False
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.checkState() == Qt.Checked:
                    nm = self._extract_item_name(item.text()).strip().lower()
                    if nm == "change package name":
                        change_pkg_enabled = True; break
            if not change_pkg_enabled:
                self.log.append("[WARN] 패키지 이름을 지정했지만 'Change package name' 패치를 적용하지 않음")
                
        for widget_key, widget in self.dynamic_option_widgets.items():
            value = ""
            is_composite = widget.property("is_composite")
            if is_composite:
                combo = widget.property("combo_widget")
                line_edit = widget.property("line_edit_widget")
                custom_option_text = "직접 입력..."
                if combo.currentText() == custom_option_text:
                    value = line_edit.text().strip()
                    is_list = line_edit.property("is_list_type")
                    if is_list:
                        stripped_value = value.strip().strip('[]').strip()
                        value = f"[{stripped_value}]"
                else:
                    value = combo.currentText().split(' ')[0]
            elif isinstance(widget, QLineEdit):
                value = widget.text().strip()
                is_list = widget.property("is_list_type")
                if is_list:
                    stripped_value = value.strip().strip('[]').strip()
                    value = f"[{stripped_value}]"
            elif isinstance(widget, QComboBox):
                value = widget.currentText().split(' ')[0]
            
            if value:
                try:
                    original_key = widget_key.split('_', 1)[1]
                    all_options_values[original_key] = value
                except IndexError:
                    pass
                    
        cmdline = ["java","-jar",str(self.cli_jar),"patch","-p",str(self.rvp_file),"--purge"]
        if self.exclusive.isChecked():
            cmdline.append("--exclusive")
            
        used_option_keys = set()
        for idx in includes_by_idx:
            cmdline.extend(["--ei", str(idx)])
            if idx in index_to_option_keys:
                for key in index_to_option_keys[idx]:
                    if key in all_options_values:
                        value = all_options_values[key]
                        if value in (None, ""):
                            cmdline.append(f"-O{key}")
                        else:
                            cmdline.append(f"-O{key}={value}")
                        used_option_keys.add(key)
        for name in includes_by_name:
            cmdline.extend(["-e", name])
            
        for key, value in all_options_values.items():
            if key not in used_option_keys:
                if value in (None, ""):
                    cmdline.append(f"-O{key}")
                else:
                    cmdline.append(f"-O{key}={value}")
                    
        keystore = self.keystore_edit.text().strip()
        ks_pass = self.ks_pass.text().strip()
        alias = self.alias.text().strip()
        alias_pass = self.alias_pass.text().strip()
        
        if keystore: cmdline += ["--keystore", str(keystore)]
        if ks_pass: cmdline += ["--keystore-password", ks_pass]
        if alias: cmdline += ["--keystore-entry-alias", alias]
        if alias_pass: cmdline += ["--keystore-entry-password", alias_pass]
        
        tmp_base = self.tmp_dir_edit.text().strip()
        if not tmp_base:
            tmp_base = str(self.out_dir / "work")
        
        p_tmp = Path(tmp_base).resolve()
        if p_tmp.exists() and not p_tmp.is_dir():
            QMessageBox.warning(self, "임시폴더 오류", f"임시파일 경로가 폴더가 아닙니다.\n\n경로: {p_tmp}"); return
        if p_tmp.exists() and not _dir_is_empty(p_tmp):
            QMessageBox.warning(self, "임시폴더 비우기 필요", f"임시파일 경로가 비어 있지 않습니다.\n\n경로: {p_tmp}\n\n폴더를 비우시거나 다른 경로를 지정해 주세요."); return
        root_dir = Path.cwd().resolve()
        out_dir  = self.out_dir.resolve()
        if p_tmp == root_dir or p_tmp == out_dir:
            QMessageBox.warning(self, "임시폴더 제한", f"프로젝트 루트/산출물 폴더는 임시폴더로 사용할 수 없습니다.\n\n경로: {p_tmp}"); return
        if not p_tmp.exists():
            try:
                p_tmp.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "임시폴더 생성 실패", f"경로를 만들 수 없습니다.\n\n경로: {p_tmp}\n에러: {e}"); return
                
        cmdline += ["--temporary-files-path", str(p_tmp), "-o", str(out_apk), str(in_apk)]
        
        self._pb_busy()
        self._qin.put({
            "cmd":"build",
            "cli":str(self.cli_jar), "rvp":str(self.rvp_file), "apk":str(in_apk),
            "out_apk":str(out_apk),
            "exclusive":self.exclusive.isChecked(),
            "includes_by_idx": includes_by_idx,
            "includes_by_name": includes_by_name,
            "options": all_options_values,
            "cmdline": cmdline,
            "keystore":keystore if keystore else "",
            "ks_pass":ks_pass if ks_pass else "",
            "alias":alias if alias else "",
            "alias_pass":alias_pass if alias_pass else "",
            "tmp_base":str(p_tmp),
            "adb_install":self.adb_install_check.isChecked(),
        })
        self.log.append("[RUN] 빌드 시작")