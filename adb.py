from pathlib import Path
from multiprocessing import Queue
from typing import List, Dict, Tuple, Optional

from utils import (
    _run_capture, _has_adb_ok, _find_adb_in_tools
)

_ADB_OVERRIDE: Optional[str] = None
_ADB_EMITTED_PATH: Optional[str] = None

def emit_adb_path_set(out_q: Queue, path: Optional[str], ok: bool = True):
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

def set_adb_override(path: Optional[str]):
    global _ADB_OVERRIDE
    _ADB_OVERRIDE = path

def get_adb_override() -> Optional[str]:
    global _ADB_OVERRIDE
    return _ADB_OVERRIDE

def adb_exec(args: List[str], cwd=None) -> Tuple[int, str, str]:
    global _ADB_OVERRIDE
    if _ADB_OVERRIDE:
        adb_path = _ADB_OVERRIDE
        if Path(adb_path).exists():
            return _run_capture([adb_path] + args, cwd=cwd)
            
    local_tools_adb = _find_adb_in_tools()
    if local_tools_adb and Path(local_tools_adb).exists():
        _ADB_OVERRIDE = local_tools_adb
        return _run_capture([local_tools_adb] + args, cwd=cwd)
        
    adb_path = _has_adb_ok()
    if not adb_path:
        return 127, "", "adb not found"
        
    return _run_capture([adb_path] + args, cwd=cwd)

def adb_shell(serial: str, args: List[str]) -> Tuple[int, str, str]:
    return adb_exec(["-s", serial, "shell"] + args)

def adb_get_model_fallback(serial: str) -> str:
    keys = [
        "ro.product.model",
        "ro.product.name",
        "ro.product.device",
    ]
    for k in keys:
        code, out, err = adb_shell(serial, ["getprop", k])
        val = (out or "").strip()
        if code == 0 and val:
            return val
    code, out, err = adb_shell(serial, ["getprop", "ro.serialno"])
    if code == 0 and (out or "").strip():
        return (out or "").strip()
    return ""

def validate_devices_ready(devs: List[Dict[str, str]], out_q: Queue, context: str) -> bool:
    silent_contexts = {"env_check", "init"}
    silent = context in silent_contexts
    
    if not _has_adb_ok():
        return False

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
        adb_exec(["kill-server"])
        adb_start_server()
        return False
        
    return True

def adb_start_server(out_q: Optional[Queue]=None) -> bool:
    adb_exec(["start-server"])
    code, out, err = adb_exec(["get-state"])
    if code == 0 and ("device" in (out+err).lower()):
        if out_q: out_q.put({"type":"log","text":"[ADB] server ready"})
        return True
    devs, _ = adb_list_devices()
    ok = len(devs) > 0
    return ok

def adb_list_devices() -> Tuple[List[Dict[str,str]], str]:
    code, out, err = adb_exec(["devices", "-l"])
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
            maybe = adb_get_model_fallback(serial)
            model = maybe or product or devname
            
        devices.append({"serial":serial, "model":model, "state":state})
        
    return devices, raw

def adb_install(apk_path: Path, serial: Optional[str], out_q: Queue) -> Tuple[int, str, str]:
    base = ["install", "-r", str(apk_path)]
    if serial:
        return adb_exec(["-s", serial] + base)
    return adb_exec(base)