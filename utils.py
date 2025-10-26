import os, re, shutil, subprocess, platform, time, ctypes, stat, urllib.request, zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional
from multiprocessing import Queue

import requests
from PySide6.QtWidgets import QFormLayout

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

def _has_adb_ok() -> str:
    adb_path = _which("adb")
    if not adb_path and _os_name()=="windows":
        _refresh_windows_env_from_registry()
        _ensure_adb_on_path_windows()
        adb_path = _which("adb")
    return adb_path

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

def setup_pretendard_font(font_storage_dir: Path) -> Optional[str]:
    from PySide6.QtGui import QFontDatabase
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