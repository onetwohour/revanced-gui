import os, sys, platform, ctypes
from pathlib import Path
from multiprocessing import Process, Queue, freeze_support

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication, QFont
from PySide6.QtCore import Qt

from gui import App
from worker import worker_loop
from utils import setup_pretendard_font

def main():
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == 'win32':
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
        except:
            pass
            
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
    
    q_in = Queue()
    q_out = Queue()
    
    worker = Process(target=worker_loop, args=(q_in, q_out,), daemon=True)
    worker.start()
    
    w = App(q_in, q_out)
    w.show()
    
    app.aboutToQuit.connect(worker.join)
    sys.exit(app.exec())

if __name__ == "__main__":
    freeze_support()
    main()