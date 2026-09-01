"""
تشغيل Y-ai InOut بدون نافذة طرفية — للنسخة المُحزّمة (exe).
"""
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser

if getattr(sys, 'frozen', False):
    INSTALL_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, '_MEIPASS', INSTALL_DIR)
else:
    INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = INSTALL_DIR


def _setup_paths():
    if getattr(sys, 'frozen', False):
        appdata = os.path.join(os.environ.get('APPDATA', INSTALL_DIR), 'Y-ai-InOut')
        os.makedirs(appdata, exist_ok=True)
        os.environ['YAI_DATA_DIR'] = appdata
        os.environ['YAI_RESOURCE_DIR'] = RESOURCE_DIR

        db_name = 'Y_In_Out_DataBase.db'
        db_dst = os.path.join(appdata, db_name)
        if not os.path.isfile(db_dst):
            db_src = os.path.join(INSTALL_DIR, db_name)
            if os.path.isfile(db_src):
                shutil.copy2(db_src, db_dst)

        cfg = 'network_config.json'
        cfg_dst = os.path.join(appdata, cfg)
        if not os.path.isfile(cfg_dst):
            cfg_src = os.path.join(RESOURCE_DIR, cfg)
            if not os.path.isfile(cfg_src):
                cfg_src = os.path.join(INSTALL_DIR, cfg)
            if os.path.isfile(cfg_src):
                shutil.copy2(cfg_src, cfg_dst)

        uploads = os.path.join(appdata, 'uploads')
        for sub in ('in', 'out', 'scans', 'archive'):
            os.makedirs(os.path.join(uploads, sub), exist_ok=True)

        os.chdir(appdata)
    else:
        os.environ['YAI_DATA_DIR'] = INSTALL_DIR
        os.environ['YAI_RESOURCE_DIR'] = RESOURCE_DIR
        os.chdir(INSTALL_DIR)


_setup_paths()


def _detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def _run_server():
    from run_server import main
    main()


def start_server():
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()


def open_browser(port=8000):
    time.sleep(2.5)
    webbrowser.open(f'http://127.0.0.1:{port}/license/activate')


def show_control_window(port=8000):
    import tkinter as tk
    from tkinter import messagebox

    lan = _detect_lan_ip()
    root = tk.Tk()
    root.title('Y-ai InOut')
    root.geometry('420x220')
    root.resizable(False, False)

    try:
        ico = os.path.join(INSTALL_DIR, 'Y-ai-Icon.ico')
        if not os.path.isfile(ico):
            ico = os.path.join(RESOURCE_DIR, 'Y-ai-Icon.ico')
        if os.path.isfile(ico):
            root.iconbitmap(ico)
    except Exception:
        pass

    tk.Label(root, text='Y-ai InOut — يعمل الآن', font=('Segoe UI', 12, 'bold')).pack(pady=(16, 8))
    tk.Label(
        root,
        text=f'على هذا الجهاز:\nhttp://127.0.0.1:{port}/login\n\nمن أجهزة WiFi:\nhttp://{lan}:{port}/login',
        justify='center',
        font=('Segoe UI', 10),
    ).pack(pady=4)

    def on_open():
        webbrowser.open(f'http://127.0.0.1:{port}/login')

    def on_quit():
        if messagebox.askyesno('إغلاق', 'إغلاق البرنامج وإيقاف الخادم؟'):
            root.destroy()
            os._exit(0)

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=12)
    tk.Button(btn_frame, text='فتح البرنامج', width=14, command=on_open).pack(side=tk.LEFT, padx=6)
    tk.Button(btn_frame, text='إغلاق', width=10, command=on_quit).pack(side=tk.LEFT, padx=6)

    root.protocol('WM_DELETE_WINDOW', on_quit)
    root.mainloop()


def main():
    start_server()
    threading.Thread(target=open_browser, daemon=True).start()
    show_control_window()


if __name__ == '__main__':
    main()
