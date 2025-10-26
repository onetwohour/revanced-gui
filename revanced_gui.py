import os, sys, re, shutil, subprocess, platform, tempfile, time, ctypes, stat, queue, urllib.request, zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Union
from multiprocessing import Process, Queue

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
    except:
        pass

import requests
from PySide6.QtWidgets import (
    QApplication, QWidget, QFileDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QCheckBox, QProgressBar, QMessageBox,
    QListWidget, QListWidgetItem, QSplitter, QGroupBox, QFormLayout,
    QHeaderView, QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QSizePolicy, QTabWidget, QComboBox, QScrollArea
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor, QFontDatabase, QFont, QGuiApplication

CLI_RELEASE_URL = 'https://github.com/ReVanced/revanced-cli/releases/latest'
PATCHES_RELEASE_URL = 'https://github.com/ReVanced/revanced-patches/releases/latest'
PLATFORM_TOOLS_WIN_ZIP = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
PLATFORM_TOOLS_MAC_ZIP = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
PLATFORM_TOOLS_LINUX_ZIP = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"

_WIN_NO_WINDOW = 0
if platform.system().lower() == "windows":
    try:
        _WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW
    except Exception:
        _WIN_NO_WINDOW = 0

def _safe_decode(b: bytes, encodings=("utf-8", "cp949", "euc-kr")) -> str:
    for enc in encodings:
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _os_name():
    return platform.system().lower()

def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def _dir_is_empty(p: Path) -> bool:
    if not p.exists():
        return True
    if not p.is_dir():
        return False
    try:
        next(p.iterdir())
        return False
    except StopIteration:
        return True

def _refresh_windows_env_from_registry():
    if _os_name() != "windows":
        return
    try:
        import winreg
        def _read_env(root):
            vals = {}
            with winreg.OpenKey(root, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
                for name in ("Path", "JAVA_HOME"):
                    try:
                        vals[name] = winreg.QueryValueEx(k, name)[0]
                    except FileNotFoundError:
                        pass
            return vals
        sys_vals  = _read_env(winreg.HKEY_LOCAL_MACHINE)
        user_vals = _read_env(winreg.HKEY_CURRENT_USER)
        path_sys  = sys_vals.get("Path", "")
        path_user = user_vals.get("Path", "")
        merged = path_sys + (";" if path_sys and path_user else "") + path_user
        if merged:
            os.environ["PATH"] = merged
        java_home = user_vals.get("JAVA_HOME") or sys_vals.get("JAVA_HOME")
        if java_home:
            os.environ["JAVA_HOME"] = java_home
    except Exception:
        pass

def _iter_windows_java_bins():
    roots = [
        Path(r"C:\Program Files\Eclipse Adoptium"),
        Path(r"C:\Program Files\Java"),
        Path(r"C:\Program Files\Microsoft"),
        Path(r"C:\Program Files\Zulu"),
    ]
    sub_patterns = ["**/jdk*/bin/java.exe", "**/jre*/bin/java.exe"]
    for root in roots:
        if not root.exists():
            continue
        for pat in sub_patterns:
            for p in root.glob(pat):
                low = str(p).lower()
                if "graalvm" in low or "mandrel" in low:
                    continue
                yield p

def _iter_windows_git_bins():
    candidates = [
        Path(r"C:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files\Git\bin\git.exe"),
        Path(r"C:\Program Files (x86)\Git\cmd\git.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\git.exe"),
    ]
    for p in candidates:
        if p.exists():
            yield p

def _prepend_to_path(p: Path):
    s = str(p)
    cur = os.environ.get("PATH", "")
    if s.lower() not in cur.lower():
        os.environ["PATH"] = s + (";" + cur if cur else "")

def _ensure_adb_on_path_posix(extra_dirs: List[Path]):
    cur = os.environ.get("PATH", "")
    for d in extra_dirs:
        if d.exists():
            s = str(d)
            if s not in cur:
                os.environ["PATH"] = s + (":" + cur if cur else "")

def _find_adb_in_tools() -> Optional[str]:
    root = Path.cwd() / "tools"
    if not root.exists():
        return None
    names = ["adb.exe"] if _os_name() == "windows" else ["adb"]
    for name in names:
        for p in root.rglob(name):
            if p.is_file():
                return str(p)
    return None

def _run_capture(cmd, cwd=None, env=None) -> Tuple[int, str, str]:
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_WIN_NO_WINDOW)
    out_b, err_b = p.communicate()
    return p.returncode, _safe_decode(out_b), _safe_decode(err_b)

def _run_stream_worker(cmd, out_q: Queue, cwd=None, env=None) -> int:
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, creationflags=_WIN_NO_WINDOW)
    for raw in iter(proc.stdout.readline, b''):
        if not raw:
            break
        out_q.put({"type":"log","text":_safe_decode(raw).rstrip("\r\n")})
    return proc.wait()

def _is_graalvm_runtime(info_text: str, java_path: Optional[str] = None) -> bool:
    t = (info_text or "").lower()
    if "graalvm" in t or "mandrel" in t:
        return True
    if java_path:
        p = str(java_path).lower()
        if "graalvm" in p or "mandrel" in p:
            return True
    return False

def _has_java_ok() -> Tuple[bool, str, Optional[int]]:
    java_path = _which("java")
    if not java_path and _os_name() == "windows":
        _refresh_windows_env_from_registry()
        java_path = _which("java")
    if not java_path and _os_name() == "windows":
        for p in _iter_windows_java_bins():
            _prepend_to_path(p.parent)
        java_path = _which("java")
    if not java_path:
        return False, "java 미발견", None
    code, out, err = _run_capture([java_path, "-version"])
    text = (out or err or "").strip()
    if _is_graalvm_runtime(text, java_path):
        return False, text + "\n[GraalVM/ Mandrel 감지됨 → 오류 가능성 있음]", None
    m = re.search(r'\bversion "([^"]+)"', text)
    if not m:
        return False, text, None
    ver = m.group(1)
    parts = ver.split(".")
    if parts[0] == "1" and len(parts) > 1:
        major = int(re.match(r"\d+", parts[1]).group(0))
    else:
        major = int(re.match(r"\d+", parts[0]).group(0))
    ok = (17 <= major < 25)
    return ok, text, major if ok else None

def _has_git() -> bool:
    g = _which("git")
    if g:
        return True
    if _os_name() == "windows":
        _refresh_windows_env_from_registry()
        g = _which("git")
        if g:
            return True
        for p in _iter_windows_git_bins():
            _prepend_to_path(p.parent)
        return _which("git") is not None
    return False

_ADB_OVERRIDE: Optional[str] = None
_ADB_EMITTED_PATH: Optional[str] = None

def _emit_adb_path_set(out_q: Queue, path: Optional[str], ok: bool = True):
    global _ADB_EMITTED_PATH
    if not path:
        return
    try:
        newp = str(Path(path).resolve())
        oldp = str(Path(_ADB_EMITTED_PATH).resolve()) if _ADB_EMITTED_PATH else None
    except Exception:
        newp = path
        oldp = _ADB_EMITTED_PATH
    if oldp and oldp == newp:
        return
    _ADB_EMITTED_PATH = newp
    out_q.put({"type": "adb_path_set", "ok": ok, "path": path})

def _ensure_adb_on_path_windows():
    if _os_name() != "windows":
        return
    candidates = [
        Path(os.environ.get("LOCALAPPDATA","")) / "Android" / "Sdk" / "platform-tools",
        Path(r"C:\Android\platform-tools"),
        Path(r"C:\Program Files (x86)\Android\platform-tools"),
        Path(r"C:\Program Files\Android\platform-tools"),
    ]
    for p in candidates:
        if p.exists():
            _prepend_to_path(p)

def _adb_exec(args: List[str], cwd=None) -> Tuple[int, str, str]:
    global _ADB_OVERRIDE
    if _ADB_OVERRIDE:
        adb_path = _ADB_OVERRIDE
        if Path(adb_path).exists():
            return _run_capture([adb_path] + args, cwd=cwd)
    local_tools_adb = _find_adb_in_tools()
    if local_tools_adb and Path(local_tools_adb).exists():
        _ADB_OVERRIDE = local_tools_adb
        return _run_capture([local_tools_adb] + args, cwd=cwd)
    adb_path = _which("adb")
    if not adb_path and _os_name()=="windows":
        _refresh_windows_env_from_registry()
        _ensure_adb_on_path_windows()
        adb_path = _which("adb")
    if not adb_path:
        return 127, "", "adb not found"
    return _run_capture([adb_path] + args, cwd=cwd)

def _adb_shell(serial: str, args: List[str]) -> Tuple[int, str, str]:
    return _adb_exec(["-s", serial, "shell"] + args)

def _adb_get_model_fallback(serial: str) -> str:
    keys = [
        "ro.product.model",
        "ro.product.name",
        "ro.product.device",
    ]
    for k in keys:
        code, out, err = _adb_shell(serial, ["getprop", k])
        val = (out or "").strip()
        if code == 0 and val:
            return val
    code, out, err = _adb_shell(serial, ["getprop", "ro.serialno"])
    if code == 0 and (out or "").strip():
        return (out or "").strip()
    return ""

def _validate_devices_ready(devs: List[Dict[str, str]], out_q: Queue, context: str) -> bool:
    silent_contexts = {"env_check", "init"}
    silent = context in silent_contexts
    if not devs:
        msg = f"[ADB] 연결된 기기가 없습니다. ({context})"
        if silent:
            out_q.put({"type": "log", "text": msg})
        else:
            out_q.put({"type": "fail", "error": msg})
        return False
    bad = [d for d in devs if d.get("state") != "device"]
    if bad:
        lines = []
        for d in bad:
            ser = d.get("serial", "")
            st  = d.get("state", "")
            mdl = d.get("model", "")
            tip = {
                "unauthorized": "디바이스에서 USB 디버깅을 승인해 주세요.",
                "offline": "USB 케이블/드라이버 점검 후 재연결해 주세요.",
                "recovery": "일반 부팅 상태로 전환 후 다시 시도해 주세요.",
                "sideload": "일반 부팅 상태로 전환 후 다시 시도해 주세요.",
                "bootloader": "일반 부팅 상태로 전환 후 다시 시도해 주세요.",
            }.get(st, "")

            lines.append(f" - {ser}  state={st} {f'({mdl})' if mdl else ''}  {tip}")
        msg = "[ADB] 기기 연결 비정상\n" f"(context={context})\n" + "\n".join(lines)
        if silent:
            out_q.put({"type": "log", "text": msg})
        else:
            out_q.put({"type": "fail", "error": msg})
        _adb_exec(["kill-server"])
        _adb_start_server()
        return False
    return True

def _adb_start_server(out_q: Optional[Queue]=None) -> bool:
    _adb_exec(["start-server"])
    code, out, err = _adb_exec(["get-state"])
    if code == 0 and ("device" in (out+err).lower()):
        if out_q: out_q.put({"type":"log","text":"[ADB] server ready"})
        return True
    devs, _ = _adb_list_devices()
    ok = len(devs) > 0
    return ok

def _adb_list_devices() -> Tuple[List[Dict[str,str]], str]:
    code, out, err = _adb_exec(["devices", "-l"])
    raw = (out or "") + (("\n"+err) if err else "")
    devices = []
    valid_states = {"device","unauthorized","offline","recovery","sideload","bootloader"}
    for line in raw.splitlines():
        line = line.strip()
        if (not line) or line.startswith("List of devices"):
            continue
        if line.lower().startswith("adb "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0].strip()
        state  = parts[1].strip()
        if state not in valid_states:
            continue
        if serial.lower() == "adb":
            continue
        model = ""
        product = ""
        devname = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":",1)[1]
            elif token.startswith("product:"):
                product = token.split(":",1)[1]
            elif token.startswith("device:"):
                devname = token.split(":",1)[1]
        if not model:
            maybe = _adb_get_model_fallback(serial)
            model = maybe or product or devname
        devices.append({"serial":serial, "model":model, "state":state})
    return devices, raw

def _adb_install(apk_path: Path, serial: Optional[str], out_q: Queue) -> Tuple[int, str, str]:
    base = ["install", "-r", str(apk_path)]
    if serial:
        return _adb_exec(["-s", serial] + base)
    return _adb_exec(base)

def _winget_install_or_ok(id_str: str, out_q: Queue) -> bool:
    code = _run_stream_worker([
        "winget","install","--id",id_str,"-e","--silent","--accept-package-agreements","--accept-source-agreements","--disable-interactivity","--source","winget"
    ], out_q)
    _refresh_windows_env_from_registry()
    for p in _iter_windows_java_bins():
        _prepend_to_path(p.parent)
    if "java" in id_str.lower():
        ok_now, _, _ = _has_java_ok()
        if ok_now:
            out_q.put({"type":"log","text":"[winget] Java 사용 가능 처리"})
            return True
    if "git" in id_str.lower():
        if _has_git():
            out_q.put({"type":"log","text":"[winget] Git 사용 가능 처리"})
            return True
    return code == 0

def _find_temurin_msi_url(out_q: Queue) -> Optional[str]:
    base = "https://api.adoptium.net/v3/assets/latest/17/hotspot"
    tries = [
        {"architecture": "x64", "image_type": "jdk", "os": "windows", "vendor": "eclipse"},
        {"architecture": "x64", "image_type": "jdk", "os": "windows"},
    ]
    for params in tries:
        try:
            r = requests.get(base, params=params, timeout=30)
            r.raise_for_status()
            assets = r.json()
        except Exception as e:
            out_q.put({"type":"log","text":f"[Adoptium] {e}"})
            continue
        for a in assets:
            for b in a.get("binaries", []):
                inst = b.get("installer") or {}
                link1 = inst.get("link") or ""
                if link1.lower().endswith(".msi"):
                    return link1
                pkg = b.get("package") or {}
                link2 = pkg.get("link") or ""
                if link2.lower().endswith(".msi"):
                    return link2
    return None

def _download_file(url: str, dest_path: Path, out_q: Queue, target_key: str, retries: int=3):
    _ensure_dir(dest_path.parent)
    for attempt in range(1, retries+1):
        try:
            with requests.get(url, stream=True, timeout=(5, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length', 0))
                done = 0
                with open(dest_path, 'wb') as f:
                    for chunk in r.iter_content(1024*64):
                        if not chunk:
                            continue
                        f.write(chunk); done += len(chunk)
                        if total:
                            pct = int(done * 100 / total)
                            out_q.put({"type":"progress","phase":"download","target":target_key,"value":pct,"done":done,"total":total})
                        else:
                            if done % (1024*1024) == 0:
                                out_q.put({"type":"log","text":f"[DL] {done} bytes"})
            out_q.put({"type":"log","text":f"[OK] {dest_path.name} → {dest_path}"})
            return
        except Exception as e:
            out_q.put({"type":"log","text":f"[DL RETRY {attempt}/{retries}] {e}"})
            time.sleep(1.0 * attempt)
    out_q.put({"type":"log","text":"[DL] 다운로드 실패"})

def _safe_extractall(zf: zipfile.ZipFile, dest_dir: Path):
    dest_dir = dest_dir.resolve()
    for member in zf.infolist():
        out_path = (dest_dir / member.filename).resolve()
        if not str(out_path).startswith(str(dest_dir)):
            raise RuntimeError(f"Zip entry escapes target dir: {member.filename}")
        if member.is_dir():
            out_path.mkdir(parents=True, exist_ok=True)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, 'r') as src, open(out_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)

def _download_and_extract_zip(url: str, dest_dir: Path, out_q: Queue) -> Optional[Path]:
    _ensure_dir(dest_dir)
    tmp_zip = dest_dir / "tmp_download.zip"
    _download_file(url, tmp_zip, out_q, target_key="adb-zip")
    try:
        with zipfile.ZipFile(tmp_zip, 'r') as z:
            _safe_extractall(z, dest_dir)
        out_q.put({"type":"log","text":f"[OK] ZIP 압축 해제 → {dest_dir}"})
        return dest_dir
    finally:
        try: tmp_zip.unlink(missing_ok=True)
        except Exception: pass

def _make_executable(p: Path):
    try:
        mode = os.stat(p).st_mode
        os.chmod(p, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass

def _get_latest_release(url: str):
    GITHUB_REGEX = re.compile(r'^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?')
    match = GITHUB_REGEX.match(url)
    if match:
        owner = match.group('owner')
        repo = match.group('repo')
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    else:
        api_url = url
    r = requests.get(api_url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get('tag_name') or '', data.get('assets') or []

def _asset_download_url(asset: dict) -> str:
    for k in ("browser_download_url", "browser_url", "html_url", "url"):
        v = asset.get(k)
        if v and isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return ""

def _pick_cli_jar_download_url(assets):
    jar_assets = [a for a in assets if str(a.get('name','')).lower().endswith('.jar')]
    cli_jars = [a for a in jar_assets if 'cli' in str(a.get('name','')).lower()]
    for cand in (cli_jars or jar_assets):
        url = _asset_download_url(cand)
        if url:
            name = cand.get('name') or os.path.basename(url.split("?")[0]) or 'revanced-cli.jar'
            return url, name
    return None, None

def _pick_patches_rvp_download_url(assets):
    rvp_assets = [a for a in assets if str(a.get('name','')).lower().endswith('.rvp')]
    for cand in rvp_assets:
        url = _asset_download_url(cand)
        if url:
            name = cand.get('name') or os.path.basename(url.split("?")[0]) or 'patches.rvp'
            return url, name
    return None, None

def _run_cli_list_patches(cli_jar: Path, rvp_path: Path) -> str:
    code, out, err = _run_capture(["java","-jar",str(cli_jar),"list-patches","--with-packages","--with-versions","--with-options",str(rvp_path)])
    if code != 0:
        raise RuntimeError(f"list-patches 실패\n{err or out}")
    return out

def _clear_form_layout(form_layout: QFormLayout):
    while form_layout.count():
        item = form_layout.takeAt(0)
        w = item.widget()
        l = item.layout()
        if w:
            w.deleteLater()
        if l:
            while l.count():
                ci = l.takeAt(0)
                if ci.widget():
                    ci.widget().deleteLater()

def _parse_patches(text: str):
    entries = []
    for blk in re.split(r'\n{2,}', text.strip()):
        if not blk.strip():
            continue
        patch_dict = {}
        main_info_text = blk
        options_text = None
        options_match = re.search(r'(?m)^\s*Options:\s*$', blk)
        if options_match:
            main_info_text = blk[:options_match.start()].strip()
            options_text = blk[options_match.end():].strip()
        mi = re.search(r'(?mi)^\s*(?:정보:\s*)?Index:\s*(\d+)\s*$', main_info_text)
        mn = re.search(r'(?mi)^\s*Name:\s*(.+?)\s*$', main_info_text)
        md = re.search(r'(?ms)^\s*Description:\s*(.+?)(?=\n\s*(?:[A-Z][a-z]+:|\Z))', main_info_text)
        me = re.search(r'(?mi)^\s*Enabled:\s*(true|false)\s*$', main_info_text)
        mp = re.search(r'(?ms)^(?:Packages?|Compatible packages?):\s*(.+?)(?:\n[A-Z][A-Za-z ]+?:|\Z)', main_info_text) or re.search(r'(?ms)^(?:Packages?|Compatible packages?):\s*(.+?)(?:\n[A-Z][A-Za-z ]+?:|\Z)', blk)
        pkgs = []
        if mp:
            body = mp.group(1)
            pkgs = re.findall(r'\b[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+\b', body)
        if not mn:
            continue
        patch_dict = {
            "index": int(mi.group(1)) if mi else None,
            "name": mn.group(1).strip(),
            "description": md.group(1).strip() if md else None,
            "enabled": (me and me.group(1).lower() == "true"),
            "packages": pkgs
        }
        if options_text:
            patch_dict["options"] = []
            option_sub_blocks = re.split(r'(?m)(?=\n\s*Title:)', options_text)
            for opt_block in option_sub_blocks:
                opt_block = opt_block.strip()
                if not opt_block:
                    continue
                opt_dict = {}
                m_title = re.search(r'(?m)^\s*Title:\s*(.+)', opt_block)
                m_opt_desc = re.search(r'(?ms)^\s*Description:\s*(.+?)(?=\n\s*(?:[A-Z][a-z]+:|\Z))', opt_block)
                m_req = re.search(r'(?m)^\s*Required:\s*(.+)', opt_block)
                m_key = re.search(r'(?m)^\s*Key:\s*(.+)', opt_block)
                m_type = re.search(r'(?m)^\s*Type:\s*(.+)', opt_block)
                m_default = re.search(r'(?m)^\s*Default:\s*([^\n\r]+)', opt_block)
                if m_title: opt_dict['title'] = m_title.group(1).strip()
                if m_opt_desc: opt_dict['description'] = m_opt_desc.group(1).strip()
                if m_req: opt_dict['required'] = (m_req.group(1).strip().lower() == 'true')
                if m_key: opt_dict['key'] = m_key.group(1).strip()
                if m_type: opt_dict['type'] = m_type.group(1).strip()
                if m_default: opt_dict['default'] = m_default.group(1).strip()
                m_pv = re.search(r'(?ms)^\s*Possible values:\s*\n(.+?)(?=\n\s*(?:[A-Z][a-z]+:|\Z))', opt_block)
                if m_pv:
                    pv_text = m_pv.group(1)
                    pv_list = [line.strip() for line in pv_text.splitlines() if line.strip()]
                    opt_dict['possible_values'] = pv_list
                if opt_dict:
                    patch_dict["options"].append(opt_dict)
        entries.append(patch_dict)
    return entries

def _find_aapt_bins() -> List[Path]:
    bins = []
    for name in ("aapt","aapt.exe","aapt2","aapt2.exe"):
        p = shutil.which(name)
        if p:
            bins.append(Path(p))
    roots = []
    for k in ("ANDROID_HOME","ANDROID_SDK_ROOT"):
        v = os.environ.get(k)
        if v:
            roots.append(Path(v))
    if _os_name() == "windows":
        local = Path(os.environ.get("LOCALAPPDATA",""))/"Android"/"Sdk"
        progx = Path("C:/Program Files (x86)/Android/android-sdk")
        prog = Path("C:/Program Files/Android/android-sdk")
        for r in (local, progx, prog):
            if r.exists():
                roots.append(r)
    for r in roots:
        bt = r/"build-tools"
        if not bt.exists():
            continue
        for sub in sorted(bt.glob("*/")):
            for nm in ("aapt.exe","aapt2.exe","aapt","aapt2"):
                p = sub/nm
                if p.exists():
                    bins.append(p)
    seen = set(); uniq=[]
    for p in bins:
        s = str(p).lower()
        if s not in seen:
            seen.add(s); uniq.append(p)
    return uniq

def _run_badging_with(bin_path: Path, apk_path: Path) -> Optional[str]:
    code, out, err = _run_capture([str(bin_path),"dump","badging",str(apk_path)])
    txt = out or err or ""
    m = re.search(r"package:\s+name='([^']+)'", txt)
    return m.group(1) if m else None

def _try_extract_package_from_apk(apk_path: Path) -> Optional[str]:
    try:
        from apkutils2 import APK
        a = APK(str(apk_path))
        pkg = a.get_manifest()["@package"]
        if pkg:
            return pkg
    except Exception:
        pass
    for bin_path in _find_aapt_bins():
        pkg = _run_badging_with(bin_path, apk_path)
        if pkg:
            return pkg
    for nm in (["aapt"],["aapt2"]):
        code, out, err = _run_capture(nm+["dump","badging",str(apk_path)])
        m = re.search(r"package:\s+name='([^']+)'",(out or err or ""))
        if m:
            return m.group(1)
    return None

def _chmod_writable(p: Path):
    try:
        os.chmod(str(p), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except Exception:
        pass

def _safe_rmtree_force(path: Path, max_retries: int = 10, wait_sec: float = 0.5):
    if not path.exists():
        return True
    for _ in range(max_retries):
        try:
            for root, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    fp = Path(root)/name
                    _chmod_writable(fp)
                    try: fp.unlink()
                    except Exception: pass
                for name in dirs:
                    dp = Path(root)/name
                    _chmod_writable(dp)
                    try: dp.rmdir()
                    except Exception: pass
            _chmod_writable(path)
            path.rmdir()
            if not path.exists():
                return True
        except Exception:
            pass
        time.sleep(wait_sec)
    tomb = path.parent/(path.name+".delete_pending_"+datetime.now().strftime("%Y%m%d%H%M%S"))
    try:
        os.replace(str(path), str(tomb))
        path = tomb
    except Exception:
        pass
    if _os_name() == "windows":
        try:
            MoveFileExW = ctypes.windll.kernel32.MoveFileExW
            MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
            MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)
            return True
        except Exception:
            pass
    return not path.exists()

def _win_set_not_content_indexed(path: Path):
    if _os_name() != "windows":
        return
    try:
        FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x2000
        ctypes.windll.kernel32.SetFileAttributesW(ctypes.c_wchar_p(str(path)), FILE_ATTRIBUTE_NOT_CONTENT_INDEXED)
    except Exception:
        pass

def worker_loop(in_q: Queue, out_q: Queue):
    global _ADB_OVERRIDE
    while True:
        msg = in_q.get()
        if msg is None:
            break
        cmd = msg.get("cmd")
        try:
            if cmd == "set_adb_path":
                path = (msg.get("path") or "").strip()
                if path and Path(path).exists():
                    code, _, _ = _run_capture([path, "version"])
                    _ADB_OVERRIDE = path if code == 0 else None
                    _emit_adb_path_set(out_q, path, ok=(code==0))
                else:
                    _ADB_OVERRIDE = None if not path else path
                    _emit_adb_path_set(out_q, path, Path(path).exists())
                out_q.put({"type":"done"})
            elif cmd == "env_check":
                ok, out, _ = _has_java_ok()
                adb_ok = False
                adb_path = None
                if _ADB_OVERRIDE and Path(_ADB_OVERRIDE).exists():
                    adb_path = _ADB_OVERRIDE
                    adb_ok = True
                else:
                    tools_adb = _find_adb_in_tools()
                    if tools_adb and Path(tools_adb).exists():
                        _ADB_OVERRIDE = tools_adb
                        adb_path = tools_adb
                        adb_ok = True
                        try:
                            _prepend_to_path(Path(tools_adb).parent)
                        except Exception:
                            pass
                    else:
                        if _os_name() == "windows":
                            _refresh_windows_env_from_registry()
                            _ensure_adb_on_path_windows()
                        adb_path = _which("adb")
                        adb_ok = adb_path is not None
                out_q.put({"type":"env","java_ok":ok,"java_out":out,"git_ok":_has_git(),"adb_ok":adb_ok})
                if adb_path:
                    try:
                        _emit_adb_path_set(out_q, adb_path, True)
                    except NameError:
                        out_q.put({"type":"adb_path_set","ok":True,"path":adb_path})
                _adb_start_server(out_q)
                devs, raw = _adb_list_devices()
                if _validate_devices_ready(devs, out_q, context="env_check"):
                    out_q.put({"type":"adb_devices","devices":devs,"raw":raw})
                out_q.put({"type":"done"})
            elif cmd == "install_java":
                if _os_name()=="windows" and _which("winget"):
                    out_q.put({"type":"log","text":"winget Temurin 17 실행"})
                    ok_by_winget = _winget_install_or_ok("EclipseAdoptium.Temurin.17.JDK", out_q)
                    _refresh_windows_env_from_registry()
                    for p in _iter_windows_java_bins():
                        _prepend_to_path(p.parent)
                    ok_now, _, _ = _has_java_ok()
                    if ok_by_winget or ok_now:
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                        continue
                    out_q.put({"type":"log","text":"winget로 Java 감지 실패 → MSI 시도"})
                if _os_name()=="windows":
                    msi_url = _find_temurin_msi_url(out_q)
                    if not msi_url:
                        out_q.put({"type":"fail","error":"MSI url not found"}); out_q.put({"type":"done"}); continue
                    msi_path = Path(tempfile.gettempdir())/"temurin17.msi"
                    _download_file(msi_url, msi_path, out_q, target_key="java-msi")
                    code = _run_stream_worker(["msiexec","/i",str(msi_path),"/qn"], out_q)
                    if code==0:
                        _refresh_windows_env_from_registry()
                        for p in _iter_windows_java_bins(): _prepend_to_path(p.parent)
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":f"msiexec code={code}"}); out_q.put({"type":"done"})
                elif _os_name()=="darwin" and _which("brew"):
                    code = _run_stream_worker(["brew","install","--cask","temurin17"], out_q)
                    if code == 0:
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":f"brew code={code}"})
                        out_q.put({"type":"done"})
                else:
                    pkg_cmds = [
                        ["bash","-lc","sudo apt-get update && sudo apt-get install -y openjdk-17-jdk"],
                        ["bash","-lc","sudo dnf install -y java-17-openjdk"],
                        ["bash","-lc","sudo pacman -S --noconfirm jdk17-openjdk"]
                    ]
                    ok=False
                    for c in pkg_cmds:
                        if _which(c[0]) or c[0]=="bash":
                            code = _run_stream_worker(c, out_q)
                            if code==0: ok=True; break
                    if ok:
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":"java 설치 실패"})
                        out_q.put({"type":"done"})
            elif cmd == "install_git":
                if _os_name()=="windows" and _which("winget"):
                    code = _run_stream_worker(["winget","install","--id","Git.Git","-e","--silent","--accept-package-agreements","--accept-source-agreements","--disable-interactivity","--source","winget"], out_q)
                    if code==0 or _has_git():
                        _refresh_windows_env_from_registry()
                        for p in _iter_windows_git_bins(): _prepend_to_path(p.parent)
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":f"winget git code={code}"}); out_q.put({"type":"done"})
                elif _os_name()=="darwin" and _which("brew"):
                    code = _run_stream_worker(["brew","install","git"], out_q)
                    if code == 0:
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":"brew git 실패"})
                        out_q.put({"type":"done"})
                else:
                    ok=False
                    for c in (["bash","-lc","sudo apt-get update && sudo apt-get install -y git"],["bash","-lc","sudo dnf install -y git"],["bash","-lc","sudo pacman -S --noconfirm git"]):
                        code = _run_stream_worker(c, out_q)
                        if code==0: ok=True; break
                    if ok:
                        out_q.put({"type":"done"})
                        in_q.put({"cmd":"env_check"})
                    else:
                        out_q.put({"type":"fail","error":"git 설치 실패"})
                        out_q.put({"type":"done"})
            elif cmd == "install_adb":
                ok = False
                auto_path = None
                try:
                    existing_adb = None
                    if _ADB_OVERRIDE and Path(_ADB_OVERRIDE).exists():
                        existing_adb = _ADB_OVERRIDE
                    else:
                        found = _find_adb_in_tools()
                        if found and Path(found).exists():
                            existing_adb = found
                        else:
                            sys_adb = _which("adb")
                            if sys_adb and Path(sys_adb).exists():
                                existing_adb = sys_adb
                    if existing_adb:
                        out_q.put({"type":"log","text":f"[SKIP] ADB 이미 존재: {existing_adb}"})
                        _ADB_OVERRIDE = existing_adb
                        auto_path = existing_adb
                        ok = True
                    else:
                        if _os_name() == "windows":
                            base = Path.cwd() / "tools" / "platform-tools-win"
                            extract_root = _download_and_extract_zip(PLATFORM_TOOLS_WIN_ZIP, base, out_q)
                            if extract_root:
                                p_adb = next((extract_root.glob("platform-tools/adb.exe")), None)
                                if p_adb and p_adb.exists():
                                    _prepend_to_path(p_adb.parent)
                                    _ADB_OVERRIDE = str(p_adb)
                                    auto_path = str(p_adb)
                                    ok = True
                                    out_q.put({"type":"log","text":f"[SET] ADB 경로 설정: {p_adb}"})
                        elif _os_name() == "darwin":
                            base = Path.cwd() / "tools" / "platform-tools-mac"
                            extract_root = _download_and_extract_zip(PLATFORM_TOOLS_MAC_ZIP, base, out_q)
                            if extract_root:
                                p_adb = next((extract_root.glob("platform-tools/adb")), None)
                                if p_adb and p_adb.exists():
                                    _make_executable(p_adb)
                                    _ensure_adb_on_path_posix([p_adb.parent])
                                    _ADB_OVERRIDE = str(p_adb)
                                    auto_path = str(p_adb)
                                    ok = True
                                    out_q.put({"type":"log","text":f"[SET] ADB 경로 설정: {p_adb}"})
                        else:
                            base = Path.cwd() / "tools" / "platform-tools-linux"
                            extract_root = _download_and_extract_zip(PLATFORM_TOOLS_LINUX_ZIP, base, out_q)
                            if extract_root:
                                p_adb = next((extract_root.glob("platform-tools/adb")), None)
                                if p_adb and p_adb.exists():
                                    _make_executable(p_adb)
                                    _ensure_adb_on_path_posix([p_adb.parent])
                                    _ADB_OVERRIDE = str(p_adb)
                                    auto_path = str(p_adb)
                                    ok = True
                                    out_q.put({"type":"log","text":f"[SET] ADB 경로 설정: {p_adb}"})
                except Exception as e:
                    out_q.put({"type":"fail","error":f"ADB 설치 중 예외: {e}"})
                    out_q.put({"type":"done"})
                    continue
                if ok:
                    if auto_path:
                        _emit_adb_path_set(out_q, auto_path, True)
                    _adb_start_server(out_q)
                    out_q.put({"type":"log","text":"[ADB] 설치 완료 및 서버 확인"})
                    in_q.put({"cmd":"env_check"})
                else:
                    out_q.put({"type":"fail","error":"ADB 설치 실패"})
                    out_q.put({"type":"done"})
            elif cmd == "download_components":
                out_dir = Path(msg["out_dir"])
                _ensure_dir(out_dir)
                user_cli_url = (msg.get("cli_url") or "").strip().rstrip("/")
                user_rvp_url = (msg.get("rvp_url") or "").strip().rstrip("/")
                if user_cli_url and not user_cli_url.endswith(("latest", "releases")):
                    url_cli = user_cli_url
                    name_cli = os.path.basename(url_cli.split("?")[0]) or "revanced-cli.jar"
                else:
                    _, assets_cli = _get_latest_release(CLI_RELEASE_URL)
                    url_cli, name_cli = _pick_cli_jar_download_url(assets_cli)
                    if not url_cli:
                        out_q.put({"type":"fail","error":"CLI .jar 없음"}); out_q.put({"type":"done"}); continue
                cli_path = out_dir / name_cli
                _download_file(url_cli, cli_path, out_q, target_key="cli")
                if user_rvp_url and not user_rvp_url.endswith(("latest", "releases")):
                    url_rvp = user_rvp_url
                    name_rvp = os.path.basename(url_rvp.split("?")[0]) or "patches.rvp"
                else:
                    _, assets_rvp = _get_latest_release(PATCHES_RELEASE_URL)
                    url_rvp, name_rvp = _pick_patches_rvp_download_url(assets_rvp)
                    if not url_rvp:
                        out_q.put({"type":"fail","error":".rvp 없음"}); out_q.put({"type":"done"}); continue
                rvp_path = out_dir / name_rvp
                _download_file(url_rvp, rvp_path, out_q, target_key="rvp")
                out_q.put({"type":"download_ok","cli":str(cli_path),"rvp":str(rvp_path)})
                out_q.put({"type":"done"})
            elif cmd == "detect_package":
                apk = Path(msg["apk"])
                pkg = _try_extract_package_from_apk(apk)
                out_q.put({"type":"pkg","value":pkg})
                out_q.put({"type":"done"})
            elif cmd == "list_patches":
                cli = Path(msg["cli"])
                rvp = Path(msg["rvp"])
                text = _run_cli_list_patches(cli, rvp)
                entries = _parse_patches(text)
                pkg = (msg.get("pkg") or "").strip().lower()
                inc_univ = bool(msg.get("inc_univ"))
                def filter_rows(allow_universal: bool):
                    rows=[]
                    for e in entries:
                        if not e.get("index", None): continue
                        pkgs=[p.lower() for p in e.get("packages",[])]
                        is_univ=(len(pkgs)==0)
                        if not pkg:
                            if (not is_univ) or (is_univ and allow_universal):
                                rows.append(e)
                        else:
                            if (pkg in pkgs) or (is_univ and allow_universal):
                                rows.append(e)
                    return rows
                has_pkg_info = any(e.get("packages") for e in entries)
                if pkg and has_pkg_info:
                    rows = filter_rows(inc_univ)
                    if not rows:
                        rows = filter_rows(True)
                else:
                    rows = filter_rows(inc_univ)
                out_q.put({"type":"patches","entries":rows})
                out_q.put({"type":"done"})
            elif cmd == "build":
                cli = Path(msg["cli"]); rvp = Path(msg["rvp"]); apk = Path(msg["apk"])
                out_apk = Path(msg["out_apk"])
                exclusive = bool(msg.get("exclusive"))
                includes_by_idx = msg.get("includes_by_idx") or []
                includes_by_name = msg.get("includes_by_name") or []
                options = msg.get("options") or {}
                keystore = Path(msg["keystore"]) if msg.get("keystore") else None
                ks_pass = msg.get("ks_pass") or None
                alias = msg.get("alias") or None
                alias_pass = msg.get("alias_pass") or None
                tmp_base = Path(msg["tmp_base"])
                _ensure_dir(tmp_base)
                tmp_path = tmp_base / datetime.now().strftime("tmp-%Y%m%d-%H%M%S")
                _ensure_dir(tmp_path)
                _win_set_not_content_indexed(tmp_path)
                cmdline = msg.get("cmdline")
                if not cmdline:
                    cmdline = ["java","-jar",str(cli),"patch","-p",str(rvp),"--purge"]
                    if exclusive: cmdline.append("--exclusive")
                    for i in includes_by_idx: cmdline += ["--ei", str(i)]
                    for n in includes_by_name: cmdline += ["-e", n]
                    for k,v in options.items():
                        if v in (None,"", "true"): cmdline.append(f"-O{k}")
                        else: cmdline.append(f"-O{k}={v}")
                    if keystore: cmdline += ["--keystore", str(keystore)]
                    if ks_pass: cmdline += ["--keystore-password", ks_pass]
                    if alias: cmdline += ["--keystore-entry-alias", alias]
                    if alias_pass: cmdline += ["--keystore-entry-password", alias_pass]
                    cmdline += ["--temporary-files-path", str(tmp_path), "-o", str(out_apk), str(apk)]
                out_q.put({"type":"build_begin"})
                out_q.put({"type":"log","text":"[CMD] " + " ".join(f"\"{c}\"" if " " in c else c for c in cmdline)})
                try:
                    code = _run_stream_worker(cmdline, out_q)
                    if code == 0:
                        out_q.put({"type":"build_ok","apk":str(out_apk)})
                    else:
                        out_q.put({"type":"fail","error":f"패치 실패 code={code}"})
                finally:
                    try:
                        _safe_rmtree_force(tmp_base)
                    finally:
                        out_q.put({"type":"log","text":"[CLEAN] 임시파일 정리 완료"})
                    out_q.put({"type":"build_end"})
                    out_q.put({"type":"done"})
            elif cmd == "adb_devices":
                _adb_start_server(out_q)
                devs, raw = _adb_list_devices()
                if _validate_devices_ready(devs, out_q, context="adb_devices"):
                    out_q.put({"type":"adb_devices","devices":devs,"raw":raw})
                out_q.put({"type":"done"})
            elif cmd == "adb_devices_silent":
                _adb_start_server(out_q)
                devs, raw = _adb_list_devices()
                if _validate_devices_ready(devs, out_q, context="init"):
                    out_q.put({"type": "adb_devices", "devices": devs, "raw": raw})
                out_q.put({"type":"done"})
            elif cmd == "adb_install_apk":
                out_q.put({"type":"log","text":"[ADB] 설치중..."})
                apk_path = Path(msg.get("apk",""))
                serial = (msg.get("serial") or "").strip() or None
                if not apk_path.exists():
                    out_q.put({"type":"fail","error":"APK 경로가 유효하지 않습니다."})
                    out_q.put({"type":"done"})
                    continue
                _adb_start_server(out_q)
                devs, _ = _adb_list_devices()
                if not _validate_devices_ready(devs, out_q, context="install"):
                    out_q.put({"type":"done"})
                    continue
                if serial is None and len(devs) > 1:
                    out_q.put({"type":"fail","error":"여러 대 연결됨. 설치할 디바이스 시리얼을 지정해 주세요."})
                    out_q.put({"type":"done"})
                    continue
                code, out, err = _adb_install(apk_path, serial, out_q)
                txt = (out + err)
                if code == 0 and ("success" in txt.lower()):
                    out_q.put({"type":"log","text":"[ADB] 설치 성공"})
                    out_q.put({"type":"adb_install_ok","apk":str(apk_path),"serial":serial or (devs[0]["serial"] if devs else "")})
                else:
                    out_q.put({"type":"fail","error":f"ADB 설치 실패 (code={code})\n{txt.strip()}"})
                out_q.put({"type":"done"})
            elif cmd == "adb_kill":
                _adb_exec(["kill-server"])
                out_q.put({"type":"done"})
            else:
                out_q.put({"type":"fail","error":"unknown command"}); out_q.put({"type":"done"})
        except Exception as e:
            out_q.put({"type":"fail","error":str(e)})
            out_q.put({"type":"done"})

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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ReVanced GUI")
        self.resize(1250, 860)
        self.out_dir = Path.cwd() / "output"
        _ensure_dir(self.out_dir)
        self.cli_jar: Optional[Path] = None
        self.rvp_file: Optional[Path] = None
        self._patches_to_check_on_load: List[str] = []
        self._qin: Queue = Queue()
        self._qout: Queue = Queue()
        self._worker = Process(target=worker_loop, args=(self._qin, self._qout,), daemon=True)
        self._worker.start()
        self._drain_timer = QTimer(self); self._drain_timer.setInterval(50); self._drain_timer.timeout.connect(self._drain_queues); self._drain_timer.start()
        root = QHBoxLayout(self)
        split = QSplitter()
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        tab_widget = QTabWidget()
        left_layout.addWidget(tab_widget)
        setup_tab = QWidget()
        setup_layout = QVBoxLayout(setup_tab)
        tab_widget.addTab(setup_tab, "시작")
        patch_tab = QWidget()
        patch_layout = QVBoxLayout(patch_tab)
        tab_widget.addTab(patch_tab, "패치")
        adb_tab = QWidget()
        adb_layout = QVBoxLayout(adb_tab)
        tab_widget.addTab(adb_tab, "ADB")
        env_box = QGroupBox("1. 환경 점검")
        env_form = QFormLayout()
        self.java_status = QLabel("Java: 미확인")
        self.adb_status_env = QLabel("ADB: 미확인")
        # self.git_status = QLabel("Git: 미확인")
        btn_env_check = QPushButton("환경 점검"); btn_env_check.clicked.connect(self.on_env_check)
        btn_java = QPushButton("Java 설치"); btn_java.clicked.connect(self.on_java_install)
        # btn_git = QPushButton("Git 설치"); btn_git.clicked.connect(self.on_git_install)
        btn_adb_env = QPushButton("ADB 설치")
        btn_adb_env.clicked.connect(self.on_adb_install)
        env_form.addRow(self.java_status)
        env_form.addRow(self.adb_status_env)
        # env_form.addRow(self.git_status)
        h_btn_box = QHBoxLayout(); h_btn_box.addWidget(btn_env_check); h_btn_box.addWidget(btn_java); h_btn_box.addWidget(btn_adb_env);# h_btn_box.addWidget(btn_git)
        env_form.addRow(h_btn_box)
        env_box.setLayout(env_form)
        setup_layout.addWidget(env_box)
        dl_box = QGroupBox("2. ReVanced 구성요소 다운로드")
        dl_lay = QFormLayout()
        self.cli_url_edit = QLineEdit(); self.cli_url_edit.setPlaceholderText(CLI_RELEASE_URL)
        self.rvp_url_edit = QLineEdit(); self.rvp_url_edit.setPlaceholderText(PATCHES_RELEASE_URL)
        btn_dl = QPushButton("다운로드"); btn_dl.clicked.connect(self.on_download)
        dl_lay.addRow("CLI(.jar) URL", self.cli_url_edit)
        dl_lay.addRow("패치(.rvp) URL", self.rvp_url_edit)
        self.cli_path_lbl = QLabel("CLI: 미설정")
        btn_pick_cli = QPushButton("파일 선택")
        btn_pick_cli.clicked.connect(self.pick_cli_file)
        cli_row_widget = QWidget()
        cli_row_layout = QHBoxLayout(cli_row_widget)
        cli_row_layout.setContentsMargins(0, 0, 0, 0)
        cli_row_layout.addWidget(self.cli_path_lbl, 1)
        cli_row_layout.addWidget(btn_pick_cli)
        dl_lay.addRow(cli_row_widget)
        self.rvp_path_lbl = QLabel("패치 번들: 미설정")
        btn_pick_rvp = QPushButton("파일 선택")
        btn_pick_rvp.clicked.connect(self.pick_rvp_file)
        rvp_row_widget = QWidget()
        rvp_row_layout = QHBoxLayout(rvp_row_widget)
        rvp_row_layout.setContentsMargins(0, 0, 0, 0)
        rvp_row_layout.addWidget(self.rvp_path_lbl, 1)
        rvp_row_layout.addWidget(btn_pick_rvp)
        dl_lay.addRow(rvp_row_widget)
        dl_lay.addRow(btn_dl)
        dl_box.setLayout(dl_lay)
        setup_layout.addWidget(dl_box)
        self._auto_list_after_download = False
        in_box = QGroupBox("3. 원본 APK 파일 선택")
        form = QFormLayout()
        self.apk_edit = QLineEdit()
        btn_apk = QPushButton("APK 선택"); btn_apk.clicked.connect(self.pick_apk)
        apk_row = QHBoxLayout(); apk_row.addWidget(self.apk_edit); apk_row.addWidget(btn_apk)
        self.pkg_edit = QLineEdit(); self.pkg_edit.setPlaceholderText("APK 선택 시 자동 감지")
        form.addRow("APK 파일 경로", apk_row)
        form.addRow("패키지명", self.pkg_edit)
        in_box.setLayout(form)
        setup_layout.addWidget(in_box)
        setup_layout.addStretch(1)
        patch_box = QGroupBox("4. 패치 목록 설정 및 선택")
        p_lay = QVBoxLayout()
        patch_opts_layout = QHBoxLayout()
        self.include_universal = QCheckBox("유니버설 패치 포함")
        self.include_universal.checkStateChanged.connect(lambda: self.on_list_patches() if self.cli_jar and self.rvp_file else None)
        self.exclusive = QCheckBox("선택한 패치만 적용 (권장)"); self.exclusive.setChecked(True)
        patch_opts_layout.addWidget(self.include_universal); patch_opts_layout.addWidget(self.exclusive)
        self.btn_list = QPushButton("패치 목록 새로고침"); self.btn_list.clicked.connect(self.on_list_patches)
        self.list_widget = QListWidget(); self.list_widget.setWordWrap(True); self.list_widget.setUniformItemSizes(False); self.list_widget.setSpacing(2)
        self.list_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.btn_picker = QPushButton("새 창에서 패치 선택하기"); self.btn_picker.clicked.connect(self.open_patch_picker)
        patch_file_btns = QHBoxLayout()
        self.btn_export = QPushButton("선택 내보내기"); self.btn_export.clicked.connect(self.export_selection)
        self.btn_import = QPushButton("선택 불러오기"); self.btn_import.clicked.connect(self.import_selection)
        self.btn_preset_clone = QPushButton("프리셋"); self.btn_preset_clone.clicked.connect(self.apply_preset)
        patch_file_btns.addWidget(self.btn_export); patch_file_btns.addWidget(self.btn_import); patch_file_btns.addWidget(self.btn_preset_clone)
        p_lay.addLayout(patch_opts_layout)
        p_lay.addWidget(self.btn_list); p_lay.addWidget(self.list_widget); p_lay.addWidget(self.btn_picker)
        p_lay.addLayout(patch_file_btns)
        patch_box.setLayout(p_lay)
        patch_layout.addWidget(patch_box)
        opt_box = QGroupBox("5. 빌드 옵션")
        opt = QFormLayout()
        self.change_pkg_input = QLineEdit(); self.change_pkg_input.setPlaceholderText('패키지명을 변경하려면 "Change package name" 패치 활성화 필요')
        self.update_perms = QCheckBox("Update permissions 적용")
        self.update_providers = QCheckBox("Update providers 적용")
        self.keystore_edit = QLineEdit()
        btn_ks = QPushButton("키스토어 선택"); btn_ks.clicked.connect(self.pick_keystore)
        ks_row = QHBoxLayout(); ks_row.addWidget(self.keystore_edit); ks_row.addWidget(btn_ks)
        self.ks_pass = QLineEdit(); self.ks_pass.setEchoMode(QLineEdit.Password)
        self.alias = QLineEdit()
        self.alias_pass = QLineEdit(); self.alias_pass.setEchoMode(QLineEdit.Password)
        self.tmp_dir_edit = QLineEdit(); self.tmp_dir_edit.setPlaceholderText(r"비워두면 output/work 폴더 사용")
        btn_tmp = QPushButton("임시폴더 선택")
        btn_tmp.clicked.connect(self.pick_tmp_dir)
        tmp_row = QHBoxLayout(); tmp_row.addWidget(self.tmp_dir_edit); tmp_row.addWidget(btn_tmp)
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
        patch_layout.addWidget(opt_box)
        build_box = QGroupBox("6. 빌드 실행")
        b_lay = QVBoxLayout()
        self.btn_build = QPushButton("패치 실행"); self.btn_build.clicked.connect(self.on_build)
        b_lay.addWidget(self.btn_build)
        build_box.setLayout(b_lay)
        patch_layout.addWidget(build_box)
        adb_box = QGroupBox("ADB 설정")
        adb_form = QFormLayout()
        self.adb_status = QLabel("ADB: 미확인")
        self.adb_path_edit = QLineEdit(); self.adb_path_edit.setPlaceholderText(r"예: C:\Android\platform-tools\adb.exe")
        btn_adb_browse = QPushButton("찾기")
        btn_adb_browse.clicked.connect(self.pick_adb_path)
        adb_row = QHBoxLayout(); adb_row.addWidget(self.adb_path_edit); adb_row.addWidget(btn_adb_browse)
        btn_adb_install = QPushButton("ADB 설치")
        btn_adb_install.clicked.connect(self.on_adb_install)
        self.adb_install = QCheckBox("빌드 후 ADB로 자동 설치")
        dev_row = QHBoxLayout()
        self.adb_device_edit = QLineEdit()
        self.adb_device_edit.setPlaceholderText("자동 감지 또는 직접 입력")
        btn_adb_refresh = QPushButton("ADB 기기 새로고침")
        btn_adb_refresh.clicked.connect(self.on_adb_refresh)
        dev_row.addWidget(self.adb_device_edit)
        dev_row.addWidget(btn_adb_refresh)
        adb_form.addRow(self.adb_status)
        adb_form.addRow("ADB 경로", adb_row)
        adb_form.addRow(btn_adb_install)
        adb_form.addRow(self.adb_install)
        adb_form.addRow("설치 대상 기기", dev_row)
        adb_box.setLayout(adb_form)
        adb_layout.addWidget(adb_box)
        adb_layout.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setRange(0, 1); self.progress.setValue(0)
        left_layout.addWidget(self.progress)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self.log = QTextEdit(); self.log.setReadOnly(True)
        right_layout.addWidget(QLabel("실시간 로그")); right_layout.addWidget(self.log)
        split.addWidget(left_panel); split.addWidget(right_panel)
        split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setSizes([720,900])
        root.addWidget(split)
        self.entries = []
        self._keep_idx = set()
        self._keep_name = set()
        self.reset_select = True
        self.dynamic_option_widgets: Dict[str, QWidget] = {}
        self.list_widget.itemChanged.connect(self._update_dynamic_options)
        QTimer.singleShot(0, self.on_env_check)

    def _pb_busy(self):
        self.progress.setRange(0, 0)

    def _pb_idle(self):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

    def _pb_set(self, pct: int):
        self.progress.setRange(0, 100)
        self.progress.setValue(max(0, min(100, int(pct))))

    def closeEvent(self, e):
        try:
            self._qin.put({"cmd":"adb_kill"})
        except Exception:
            pass
        try:
            self._qin.put(None)
        except Exception:
            pass
        try:
            if self._worker.is_alive():
                self._worker.join(timeout=0.5)
        except Exception:
            pass
        return super().closeEvent(e)

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
                # self.git_status.setText(f"Git: {'OK' if m.get('git_ok') else '없음'}")
                adb_ok = m.get("adb_ok")
                if hasattr(self, "adb_status_env") and self.adb_status_env is not None:
                    self.adb_status_env.setText(f"ADB: {'OK' if adb_ok else '없음'}")
                if hasattr(self, "adb_status") and self.adb_status is not None:
                    self.adb_status.setText(f"ADB: {'OK' if adb_ok else '없음'}")
                self._pb_idle()
            elif t == "download_ok":
                self.cli_jar = Path(m["cli"]); self.rvp_file = Path(m["rvp"])
                self.cli_path_lbl.setText(f"CLI: {self.cli_jar.name}")
                self.rvp_path_lbl.setText(f"패치 번들: {self.rvp_file.name}")
                self._pb_idle()
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
                self._pb_idle()
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
                if self.adb_install.isChecked():
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
                    ser = devs[0].get("serial","")
                    mdl = devs[0].get("model","")
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
            "rvp_url": self.rvp_url_edit.text(),
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
            self.change_pkg_input.setText(base_pkg + ".revanced")
        else:
            if hasattr(self, "update_perms"):
                self.update_perms.setChecked(False)
            if hasattr(self, "update_providers"):
                self.update_providers.setChecked(False)
            if hasattr(self, "include_universal"):
                self.include_universal.blockSignals(True)
                self.include_universal.setChecked(False)
                self.include_universal.blockSignals(False)
            self.change_pkg_input.setText("")
            if base_pkg: self.change_pkg_input.setPlaceholderText(f'e.g. {base_pkg + ".revanced"}')
            else: self.change_pkg_input.setPlaceholderText('패키지명을 변경하려면 "Change package name" 패치 활성화 필요')
        if hasattr(self, "exclusive"):
            self.exclusive.setChecked(True)
        self.reset_select = True
        QTimer.singleShot(0, self.on_list_patches)
        if hasattr(self, "log"):
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
            QMessageBox.information(self, "안내", "CLI .jar를 먼저 다운로드하세요.")
            return
        if not self.rvp_file or not self.rvp_file.exists():
            QMessageBox.information(self, "안내", "패치 번들(.rvp)을 먼저 다운로드하세요.")
            return
        apk_path = self.apk_edit.text().strip()
        if not apk_path or not Path(apk_path).exists():
            QMessageBox.information(self, "안내", "APK 파일을 선택하세요.")
            return
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
                if (include_universal_checked and is_universal) or is_for_this_pkg:
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
                        change_pkg_enabled = True
                        break
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
            QMessageBox.warning(self, "임시폴더 오류", f"임시파일 경로가 폴더가 아닙니다.\n\n경로: {p_tmp}")
            return
        if p_tmp.exists() and not _dir_is_empty(p_tmp):
            QMessageBox.warning(
                self,
                "임시폴더 비우기 필요",
                f"임시파일 경로가 비어 있지 않습니다.\n\n경로: {p_tmp}\n\n"
                "폴더를 비우시거나 다른 경로를 지정해 주세요."
            )
            return
        root_dir = Path.cwd().resolve()
        out_dir  = self.out_dir.resolve()
        if p_tmp == root_dir or p_tmp == out_dir:
            QMessageBox.warning(
                self, "임시폴더 제한",
                f"프로젝트 루트/산출물 폴더는 임시폴더로 사용할 수 없습니다.\n\n경로: {p_tmp}"
            )
            return
        if not p_tmp.exists():
            try:
                p_tmp.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "임시폴더 생성 실패", f"경로를 만들 수 없습니다.\n\n경로: {p_tmp}\n에러: {e}")
                return
        cmdline += ["--temporary-files-path", str(p_tmp), "-o", str(out_apk), str(in_apk)]
        self._pb_busy()
        self._qin.put({
            "cmd":"build",
            "cli":str(self.cli_jar),
            "rvp":str(self.rvp_file),
            "apk":str(in_apk),
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
            "adb_install":self.adb_install.isChecked(),
        })
        self.log.append("[RUN] 빌드 시작")

def setup_pretendard_font(font_storage_dir: Path) -> Optional[str]:
    font_url = "https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/public/variable/PretendardVariable.ttf"
    font_filename = "PretendardVariable.ttf"
    font_path = font_storage_dir / font_filename
    font_storage_dir.mkdir(parents=True, exist_ok=True)
    if not font_path.exists():
        try:
            with urllib.request.urlopen(font_url) as response, open(font_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        except Exception as e:
            return None
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id == -1:
        return None
    family_names = QFontDatabase.applicationFontFamilies(font_id)
    if not family_names:
        return None
    return True

def main():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    if platform.system().lower() == "windows":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
    app = QApplication(sys.argv)
    font_dir = Path.cwd() / "output" / "fonts"
    if setup_pretendard_font(font_dir):
        app.setFont(QFont("Pretendard Variable SemiBold", 11))
    w = App()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()