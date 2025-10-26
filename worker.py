from multiprocessing import Queue

from worker_handlers import (
    handle_set_adb_path,
    handle_env_check,
    handle_install_java,
    handle_install_git,
    handle_install_adb,
    handle_download_components,
    handle_detect_package,
    handle_list_patches,
    handle_build,
    handle_adb_devices,
    handle_adb_devices_silent,
    handle_adb_install_apk,
    handle_adb_kill
)

HANDLERS = {
    "set_adb_path": handle_set_adb_path,
    "env_check": handle_env_check,
    "install_java": handle_install_java,
    "install_git": handle_install_git,
    "install_adb": handle_install_adb,
    "download_components": handle_download_components,
    "detect_package": handle_detect_package,
    "list_patches": handle_list_patches,
    "build": handle_build,
    "adb_devices": handle_adb_devices,
    "adb_devices_silent": handle_adb_devices_silent,
    "adb_install_apk": handle_adb_install_apk,
    "adb_kill": handle_adb_kill,
}

def worker_loop(in_q: Queue, out_q: Queue):
    while True:
        msg = in_q.get()
        if msg is None:
            break
            
        cmd = msg.get("cmd")
        handler = HANDLERS.get(cmd)
        
        try:
            if handler:
                handler(msg, out_q)
                if cmd in ("install_java", "install_git", "install_adb"):
                     in_q.put({"cmd":"env_check"})
            else:
                out_q.put({"type":"fail","error":f"unknown command: {cmd}"})
        except Exception as e:
            import traceback
            out_q.put({"type":"fail","error":f"Worker Error ({cmd}):\n{traceback.format_exc()}"})
        
        out_q.put({"type":"done"})