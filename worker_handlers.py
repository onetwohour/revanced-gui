import os
import tempfile
from pathlib import Path
from multiprocessing import Queue
from datetime import datetime

from utils import (
    _has_java_ok, _has_git, _which, _refresh_windows_env_from_registry,
    _iter_windows_java_bins, _prepend_to_path, _iter_windows_git_bins,
    _find_adb_in_tools, _ensure_adb_on_path_windows, _winget_install_or_ok,
    _find_temurin_msi_url, _download_file, _run_stream_worker,
    _ensure_adb_on_path_posix, _download_and_extract_zip, _make_executable,
    _get_latest_release, _pick_cli_jar_download_url, _pick_patches_rvp_download_url,
    _try_extract_package_from_apk, _run_cli_list_patches, _parse_patches,
    _ensure_dir, _win_set_not_content_indexed, _safe_rmtree_force,
    CLI_RELEASE_URL, PATCHES_RELEASE_URL, PLATFORM_TOOLS_WIN_ZIP,
    PLATFORM_TOOLS_MAC_ZIP, PLATFORM_TOOLS_LINUX_ZIP, _os_name
)

from adb import (
    set_adb_override, get_adb_override, emit_adb_path_set, adb_start_server,
    adb_list_devices, validate_devices_ready, adb_install, adb_exec
)

def handle_set_adb_path(msg: dict, out_q: Queue):
    path = (msg.get("path") or "").strip()
    if path and Path(path).exists():
        set_adb_override(path)
        code, _, _ = adb_exec(["version"])
        emit_adb_path_set(out_q, path, ok=(code==0))
    else:
        set_adb_override(None if not path else path)
        emit_adb_path_set(out_q, path, Path(path).exists())

def handle_env_check(msg: dict, out_q: Queue):
    ok, out, _ = _has_java_ok()
    adb_ok = False
    adb_path = None
    
    current_override = get_adb_override()
    if current_override and Path(current_override).exists():
        adb_path = current_override
        adb_ok = True
    else:
        tools_adb = _find_adb_in_tools()
        if tools_adb and Path(tools_adb).exists():
            set_adb_override(tools_adb)
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
        emit_adb_path_set(out_q, adb_path, True)
        
    adb_start_server(out_q)
    devs, raw = adb_list_devices()
    if validate_devices_ready(devs, out_q, context="env_check"):
        out_q.put({"type":"adb_devices","devices":devs,"raw":raw})

def handle_install_java(msg: dict, out_q: Queue):
    if _os_name()=="windows" and _which("winget"):
        out_q.put({"type":"log","text":"winget Temurin 17 실행"})
        ok_by_winget = _winget_install_or_ok("EclipseAdoptium.Temurin.17.JDK", out_q)
        _refresh_windows_env_from_registry()
        for p in _iter_windows_java_bins():
            _prepend_to_path(p.parent)
        ok_now, _, _ = _has_java_ok()
        if ok_by_winget or ok_now:
            return
        out_q.put({"type":"log","text":"winget로 Java 감지 실패 → MSI 시도"})

    if _os_name()=="windows":
        msi_url = _find_temurin_msi_url(out_q)
        if not msi_url:
            out_q.put({"type":"fail","error":"MSI url not found"}); return
        msi_path = Path(tempfile.gettempdir())/"temurin17.msi"
        _download_file(msi_url, msi_path, out_q, target_key="java-msi")
        code = _run_stream_worker(["msiexec","/i",str(msi_path),"/qn"], out_q)
        if code==0:
            _refresh_windows_env_from_registry()
            for p in _iter_windows_java_bins(): _prepend_to_path(p.parent)
        else:
            out_q.put({"type":"fail","error":f"msiexec code={code}"})
    elif _os_name()=="darwin" and _which("brew"):
        code = _run_stream_worker(["brew","install","--cask","temurin17"], out_q)
        if code != 0:
            out_q.put({"type":"fail","error":f"brew code={code}"})
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
        if not ok:
            out_q.put({"type":"fail","error":"java 설치 실패"})

def handle_install_git(msg: dict, out_q: Queue):
    if _os_name()=="windows" and _which("winget"):
        code = _run_stream_worker(["winget","install","--id","Git.Git","-e","--silent","--accept-package-agreements","--accept-source-agreements","--disable-interactivity","--source","winget"], out_q)
        if code==0 or _has_git():
            _refresh_windows_env_from_registry()
            for p in _iter_windows_git_bins(): _prepend_to_path(p.parent)
        else:
            out_q.put({"type":"fail","error":f"winget git code={code}"})
    elif _os_name()=="darwin" and _which("brew"):
        code = _run_stream_worker(["brew","install","git"], out_q)
        if code != 0:
            out_q.put({"type":"fail","error":"brew git 실패"})
    else:
        ok=False
        for c in (["bash","-lc","sudo apt-get update && sudo apt-get install -y git"],["bash","-lc","sudo dnf install -y git"],["bash","-lc","sudo pacman -S --noconfirm git"]):
            code = _run_stream_worker(c, out_q)
            if code==0: ok=True; break
        if not ok:
            out_q.put({"type":"fail","error":"git 설치 실패"})

def handle_install_adb(msg: dict, out_q: Queue):
    ok = False
    auto_path = None
    try:
        existing_adb = None
        current_override = get_adb_override()
        if current_override and Path(current_override).exists():
            existing_adb = current_override
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
            set_adb_override(existing_adb)
            auto_path = existing_adb
            ok = True
        else:
            if _os_name() == "windows":
                base = Path.cwd() / "tools" / "platform-tools-win"
                url = PLATFORM_TOOLS_WIN_ZIP
                adb_exe = "platform-tools/adb.exe"
            elif _os_name() == "darwin":
                base = Path.cwd() / "tools" / "platform-tools-mac"
                url = PLATFORM_TOOLS_MAC_ZIP
                adb_exe = "platform-tools/adb"
            else:
                base = Path.cwd() / "tools" / "platform-tools-linux"
                url = PLATFORM_TOOLS_LINUX_ZIP
                adb_exe = "platform-tools/adb"

            extract_root = _download_and_extract_zip(url, base, out_q)
            if extract_root:
                p_adb = next((extract_root.glob(adb_exe)), None)
                if p_adb and p_adb.exists():
                    if _os_name() != "windows":
                        _make_executable(p_adb)
                        _ensure_adb_on_path_posix([p_adb.parent])
                    else:
                         _prepend_to_path(p_adb.parent)
                    set_adb_override(str(p_adb))
                    auto_path = str(p_adb)
                    ok = True
                    out_q.put({"type":"log","text":f"[SET] ADB 경로 설정: {p_adb}"})
                    
    except Exception as e:
        out_q.put({"type":"fail","error":f"ADB 설치 중 예외: {e}"})
        return
        
    if ok:
        if auto_path:
            emit_adb_path_set(out_q, auto_path, True)
        adb_start_server(out_q)
        out_q.put({"type":"log","text":"[ADB] 설치 완료 및 서버 확인"})
    else:
        out_q.put({"type":"fail","error":"ADB 설치 실패"})

def handle_download_components(msg: dict, out_q: Queue):
    out_dir = Path(msg["out_dir"])
    _ensure_dir(out_dir)
    user_cli_url = (msg.get("cli_url") or "").strip().rstrip("/")
    user_rvp_url = (msg.get("rvp_url") or "").strip().rstrip("/")

    cli_path = (msg.get("cli_path") or "").strip()
    rvp_path = (msg.get("rvp_path") or "").strip()
    
    if user_cli_url or not cli_path:
        if user_cli_url and not user_cli_url.endswith(("latest", "releases")):
            url_cli = user_cli_url
            name_cli = os.path.basename(url_cli.split("?")[0]) or "revanced-cli.jar"
        else:
            _, assets_cli = _get_latest_release(CLI_RELEASE_URL)
            url_cli, name_cli = _pick_cli_jar_download_url(assets_cli)
            if not url_cli:
                out_q.put({"type":"fail","error":"CLI .jar 없음"}); return
                
        cli_path = out_dir / name_cli
        _download_file(url_cli, cli_path, out_q, target_key="cli")
    
    if user_rvp_url or not rvp_path:
        if user_rvp_url and not user_rvp_url.endswith(("latest", "releases")):
            url_rvp = user_rvp_url
            name_rvp = os.path.basename(url_rvp.split("?")[0]) or "patches.rvp"
        else:
            _, assets_rvp = _get_latest_release(PATCHES_RELEASE_URL)
            url_rvp, name_rvp = _pick_patches_rvp_download_url(assets_rvp)
            if not url_rvp:
                out_q.put({"type":"fail","error":".rvp 없음"}); return
                
        rvp_path = out_dir / name_rvp
        _download_file(url_rvp, rvp_path, out_q, target_key="rvp")
    
    out_q.put({"type":"download_ok","cli":str(cli_path),"rvp":str(rvp_path)})

def handle_detect_package(msg: dict, out_q: Queue):
    apk = Path(msg["apk"])
    pkg = _try_extract_package_from_apk(apk)
    out_q.put({"type":"pkg","value":pkg})

def handle_list_patches(msg: dict, out_q: Queue):
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

def handle_build(msg: dict, out_q: Queue):
    cli = Path(msg["cli"]); rvp = Path(msg["rvp"]); apk = Path(msg["apk"])
    out_apk = Path(msg["out_apk"])
    tmp_base = Path(msg["tmp_base"])
    _ensure_dir(tmp_base)
    tmp_path = tmp_base / datetime.now().strftime("tmp-%Y%m%d-%H%M%S")
    _ensure_dir(tmp_path)
    _win_set_not_content_indexed(tmp_path)
    
    cmdline = msg.get("cmdline")
    
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

def handle_adb_devices(msg: dict, out_q: Queue):
    adb_start_server(out_q)
    devs, raw = adb_list_devices()
    if validate_devices_ready(devs, out_q, context="adb_devices"):
        out_q.put({"type":"adb_devices","devices":devs,"raw":raw})

def handle_adb_devices_silent(msg: dict, out_q: Queue):
    adb_start_server(out_q)
    devs, raw = adb_list_devices()
    if validate_devices_ready(devs, out_q, context="init"):
        out_q.put({"type": "adb_devices", "devices": devs, "raw": raw})

def handle_adb_install_apk(msg: dict, out_q: Queue):
    out_q.put({"type":"log","text":"[ADB] 설치중..."})
    apk_path = Path(msg.get("apk",""))
    serial = (msg.get("serial") or "").strip() or None
    
    if not apk_path.exists():
        out_q.put({"type":"fail","error":"APK 경로가 유효하지 않습니다."}); return
        
    adb_start_server(out_q)
    devs, _ = adb_list_devices()
    if not validate_devices_ready(devs, out_q, context="install"):
        return
        
    if serial is None and len(devs) > 1:
        out_q.put({"type":"fail","error":"여러 대 연결됨. 설치할 디바이스 시리얼을 지정해 주세요."}); return
        
    code, out, err = adb_install(apk_path, serial, out_q)
    txt = (out + err)
    
    if code == 0 and ("success" in txt.lower()):
        out_q.put({"type":"log","text":"[ADB] 설치 성공"})
        out_q.put({"type":"adb_install_ok","apk":str(apk_path),"serial":serial or (devs[0]["serial"] if devs else "")})
    else:
        out_q.put({"type":"fail","error":f"ADB 설치 실패 (code={code})\n{txt.strip()}"})

def handle_adb_kill(msg: dict, out_q: Queue):
    adb_exec(["kill-server"])