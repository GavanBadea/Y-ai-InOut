from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, send_from_directory, abort, jsonify, make_response, Response)
import json
import sqlite3
import os
import sys
import re
import secrets
import shutil
import socket
import subprocess
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

def _resolve_base_dir():
    return os.environ.get('YAI_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))


def _resolve_resource_dir():
    return os.environ.get('YAI_RESOURCE_DIR') or _resolve_base_dir()


_BASE = _resolve_base_dir()
_RES = _resolve_resource_dir()

app = Flask(
    __name__,
    template_folder=os.path.join(_RES, 'templates'),
    static_folder=os.path.join(_RES, 'static'),
)
app.secret_key = os.environ.get('SESSION_SECRET', 'Y_System_Secret_Key_2024')
# تحديث القوالب فور تعديل الملفات دون الحاجة لإعادة تشغيل دائم
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.jinja_env.cache = None

BASE_DIR     = _BASE
NETWORK_CONFIG_PATH = os.path.join(BASE_DIR, 'network_config.json')


def _load_network_config():
    cfg = {
        'host': '0.0.0.0',
        'port': 8000,
        'threads': 12,
        'server_url': '',
    }
    if os.path.isfile(NETWORK_CONFIG_PATH):
        try:
            with open(NETWORK_CONFIG_PATH, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    cfg['port'] = int(os.environ.get('PORT', cfg.get('port', 8000)))
    cfg['host'] = os.environ.get('HOST', cfg.get('host', '0.0.0.0'))
    cfg['threads'] = int(os.environ.get('THREADS', cfg.get('threads', 12)))
    return cfg


NETWORK = _load_network_config()

app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

DB_PATH      = os.path.join(BASE_DIR, 'Y_In_Out_DataBase.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
SCANS_INBOX_FOLDER = os.path.join(UPLOAD_FOLDER, 'scans')
ARCHIVE_FOLDER = os.path.join(UPLOAD_FOLDER, 'archive')
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'tiff', 'tif', 'bmp'}
LOGO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ORG_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'org')
QR_VIEWER_USERNAME = 'مشاهد-QR'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SCANS_INBOX_FOLDER, exist_ok=True)
os.makedirs(ARCHIVE_FOLDER, exist_ok=True)
os.makedirs(ORG_UPLOAD_FOLDER, exist_ok=True)

# ─── Owner credentials (hardcoded) ───────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def resolve_scans_inbox_file(filename):
    """مسار آمن لملف داخل uploads/scans فقط."""
    if not filename or not isinstance(filename, str):
        return None
    safe = secure_filename(os.path.basename(filename.strip()))
    if not safe or not allowed_file(safe):
        return None
    full = os.path.normpath(os.path.join(SCANS_INBOX_FOLDER, safe))
    inbox_norm = os.path.normpath(SCANS_INBOX_FOLDER)
    if not full.startswith(inbox_norm) or not os.path.isfile(full):
        return None
    return full


def sanitize_scan_category(raw):
    """تطبيع اسم التصنيف لمجلد scans/<category>."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Allow only safe folder names (no slashes, no traversal)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", s):
        return None
    return s


def ensure_unique_dest_path(folder, filename):
    """إذا وُجد ملف بنفس الاسم، يضيف _v2/_v3... قبل الامتداد."""
    base, ext = os.path.splitext(filename)
    ext = ext or ""
    cand = os.path.join(folder, filename)
    if not os.path.exists(cand):
        return cand
    for i in range(2, 100):
        cand = os.path.join(folder, f"{base}_v{i}{ext}")
        if not os.path.exists(cand):
            return cand
    # fallback rare
    return os.path.join(folder, f"{base}_{datetime.now().strftime('%H%M%S')}{ext}")


def newest_valid_scan_file(folder):
    """أحدث ملف مسح صالح (غير فارغ) داخل مجلد محدد."""
    if not os.path.isdir(folder):
        return None
    newest = None
    newest_mtime = -1
    for fn in os.listdir(folder):
        path = os.path.join(folder, fn)
        if not os.path.isfile(path) or not allowed_file(fn):
            continue
        try:
            if os.path.getsize(path) <= 0:
                # تنظيف الكتابة الصفرية (ملف فاسد/فارغ)
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest = path
    return newest


def smart_scan_filename(category, record_id, original_path):
    """[Category]_[ID]_[Timestamp].ext"""
    ext = os.path.splitext(original_path)[1].lower() or ".pdf"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_id = re.sub(r"[^\w\-\.]+", "-", str(record_id).strip())
    safe_id = safe_id.strip("-") or str(record_id)
    return f"{category}_{safe_id}_{ts}{ext}"


def _naps2_subprocess_flags():
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _load_naps2_config():
    """إعدادات NAPS2 من naps2_config.json أو متغيرات البيئة."""
    cfg = {
        "console_path": os.environ.get("NAPS2_CONSOLE_PATH", "").strip(),
        "profile": os.environ.get("NAPS2_PROFILE", "").strip(),
        "driver": os.environ.get("NAPS2_DRIVER", "").strip().lower(),
        "device": os.environ.get("NAPS2_DEVICE", "").strip(),
        "source": os.environ.get("NAPS2_SOURCE", "").strip().lower(),
        "pages": os.environ.get("NAPS2_PAGES", ""),
    }
    path = os.path.join(BASE_DIR, "naps2_config.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("profile", "driver", "device", "source"):
                    val = data.get(key) or data.get(key.upper())
                    if isinstance(val, str) and val.strip():
                        cfg[key] = val.strip().lower() if key in ("driver", "source") else val.strip()
                if "pages" in data and data["pages"] is not None:
                    cfg["pages"] = data["pages"]
                p = (data.get("console_path") or data.get("NAPS2_CONSOLE_PATH") or "").strip()
                if p:
                    cfg["console_path"] = p
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return cfg


def _naps2_source_and_pages(cfg):
    """مصدر الورق (feeder/glass/duplex). pages=تكرار عمليات المسح (ليس عدد أوراق الفيدر)."""
    source = (cfg.get("source") or "feeder").strip().lower()
    if source not in ("glass", "feeder", "duplex"):
        source = "feeder"
    raw_pages = cfg.get("pages")
    if raw_pages in (None, ""):
        pages = 1
    else:
        try:
            pages = int(raw_pages)
        except (TypeError, ValueError):
            pages = 1
    pages = max(1, min(pages, 999))
    return source, pages


def _append_naps2_scan_flags(cmd, cfg, *, include_source=True):
    """
    الفيدر: عملية مسح واحدة (-n 1) تمسح كل الأوراق الموجودة في ADF إلى PDF واحد.
    لا نستخدم -n 50 لأن ذلك يعيد المسح بعد نفاد الورق فيظهر خطأ Epson.
    """
    source, pages = _naps2_source_and_pages(cfg)
    if include_source:
        cmd += ["--source", source]
    if pages > 1:
        cmd += ["-n", str(pages)]
    return cmd


def _naps2_driver_order(cfg):
    """ترتيب تجربة المحركات — TWAIN أوضح للفيدر على Epson."""
    source = (cfg.get("source") or "feeder").strip().lower()
    driver = (cfg.get("driver") or "").lower()
    if driver in ("wia", "twain", "escl"):
        return (driver,)
    if source in ("feeder", "duplex"):
        return ("twain", "wia")
    return ("wia", "twain")


def locate_naps2_console():
    """
    العثور على NAPS2.Console.exe على ويندوز.
    الأولوية: NAPS2_CONSOLE_PATH ثم naps2_config.json ثم مسارات التثبيت الشائعة.
    """
    cfg = _load_naps2_config()
    if cfg["console_path"] and os.path.isfile(cfg["console_path"]):
        return cfg["console_path"]

    local_naps = os.path.join(os.environ.get("LOCALAPPDATA", ""), "NAPS2", "NAPS2.Console.exe")
    bundled = os.path.join(BASE_DIR, "NAPS2", "App", "NAPS2.Console.exe")
    exe_dir = ''
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [
        bundled,
        os.path.join(exe_dir, "NAPS2", "App", "NAPS2.Console.exe") if exe_dir else '',
        os.path.join(os.environ.get("YAI_RESOURCE_DIR", ""), "NAPS2", "App", "NAPS2.Console.exe"),
        local_naps,
        r"C:\Program Files\NAPS2\NAPS2.Console.exe",
        r"C:\Program Files (x86)\NAPS2\NAPS2.Console.exe",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    which = shutil.which("NAPS2.Console.exe") or shutil.which("naps2.console") or shutil.which("naps2.console.exe")
    return which


def _naps2_list_devices(console, driver):
    try:
        proc = subprocess.run(
            [console, "--driver", driver, "--listdevices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=os.path.dirname(console),
            creationflags=_naps2_subprocess_flags(),
            timeout=60,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def build_naps2_scan_command(console, dest):
    """
    يبني أمر المسح: profile من الإعدادات، أو اختيار أول ماسحة (WIA ثم TWAIN).
    يضيف --source feeder (افتراضي) — عملية مسح واحدة لكل أوراق الفيدر.
    يُرجع (cmd, رسالة_خطأ).
    """
    cfg = _load_naps2_config()
    cmd = [console, "-o", dest]

    profile = cfg.get("profile") or ""
    if profile:
        cmd += ["-p", profile]
        _append_naps2_scan_flags(cmd, cfg, include_source=False)
        return cmd, None

    driver = (cfg.get("driver") or "").lower()
    device = (cfg.get("device") or "").strip()

    if driver and device:
        cmd += ["--driver", driver, "--device", device]
        _append_naps2_scan_flags(cmd, cfg)
        return cmd, None

    if driver:
        devices = _naps2_list_devices(console, driver)
        if devices:
            cmd += ["--driver", driver, "--device", devices[0]]
            _append_naps2_scan_flags(cmd, cfg)
            return cmd, None
        return None, (
            f"لم تُعثر على ماسحة بتقنية {driver.upper()}. "
            "تحقق من التوصيل أو حدّد device في naps2_config.json."
        )

    for drv in _naps2_driver_order(cfg):
        devices = _naps2_list_devices(console, drv)
        if devices:
            cmd += ["--driver", drv, "--device", devices[0]]
            _append_naps2_scan_flags(cmd, cfg)
            return cmd, None

    return None, (
        "لم تُعثر على ماسحة ضوئية. وصّل الماسحة وشغّلها، أو أنشئ ملف naps2_config.json مثل:\n"
        '{"driver":"wia","device":"اسم الماسحة","source":"feeder","pages":50}'
    )


def sanitize_archive_department_folder(name, fallback="عام"):
    """اسم مجلد آمن تحت uploads/archive (يدعم العربية، يزيل محارف المسارات)."""
    base = (name or "").strip() or (fallback or "عام")
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(base))
    base = base.strip(" .") or "عام"
    return base[:200]


def archive_year_folder(department_folder_name, year=None):
    y = year or datetime.now().year
    path = os.path.join(ARCHIVE_FOLDER, department_folder_name, str(int(y)))
    os.makedirs(path, exist_ok=True)
    return path


def year_from_sqlite_date(val, default_year=None):
    """سنة من حقل تاريخ قاعدة البيانات (YYYY-...)."""
    y = default_year if default_year is not None else datetime.now().year
    if not val:
        return y
    try:
        s = str(val)[:4]
        if s.isdigit():
            return int(s)
    except (TypeError, ValueError):
        pass
    return y


def mirror_upload_to_staff_archive(source_abs_path, department_display_name, year_val):
    """
    نسخة في uploads/archive/<اسم القسم>/<السنة>/ لرفع ملفات موظفي الأقسام (غير المدير).
    """
    if not source_abs_path or not os.path.isfile(source_abs_path):
        return
    dep_seg = sanitize_archive_department_folder(department_display_name)
    archive_dir = archive_year_folder(dep_seg, int(year_val))
    basename = os.path.basename(source_abs_path)
    dest = ensure_unique_dest_path(archive_dir, basename)
    try:
        shutil.copy2(source_abs_path, dest)
    except OSError:
        pass


def department_name_for_incoming_staff_archive(conn, book, user_dep_id):
    """مجلد الأرشيف للوارد: القسم الحالي للكتاب أو قسم المستخدم."""
    if book and book["Current_Dep_ID"]:
        row_d = conn.execute(
            "SELECT Dep_Name FROM Department WHERE Dep_No=?",
            (book["Current_Dep_ID"],),
        ).fetchone()
        if row_d and row_d["Dep_Name"]:
            return row_d["Dep_Name"]
    if user_dep_id:
        row_d = conn.execute(
            "SELECT Dep_Name FROM Department WHERE Dep_No=?",
            (user_dep_id,),
        ).fetchone()
        if row_d and row_d["Dep_Name"]:
            return row_d["Dep_Name"]
    return "عام"


def filename_safe_token(s, default="X"):
    """جزء آمن لاسم ملف (حروف وأرقام وشرطة وشرطة سفلية)."""
    if s is None:
        return default
    t = re.sub(r"[^\w\-\.]+", "_", str(s).strip(), flags=re.UNICODE)
    t = t.strip("._-") or default
    return t[:120]


def try_update_record_file_path(conn, category, record_id, rel_path):
    """
    تحديث عمود مسار المرفق للسجل (يفضّل attachment_path إن وُجد).
    """
    if not conn:
        return False
    cat = (category or "").lower()
    if cat in ("inbook", "incoming", "in"):
        table = "In_tbl"
        pk_col = "NoBook_In"
    elif cat in ("outbook", "outgoing", "out"):
        table = "Out_tbl"
        pk_col = "NoBook_Out"
    else:
        return False

    try:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return False

    candidate_cols = [
        "attachment_path", "Attachment_Path",
        "file_path", "File_Path", "FILE_PATH",
        "Scan_Path", "scan_path",
    ]
    target_col = next((c for c in candidate_cols if c in cols), None)
    if not target_col:
        return False

    try:
        conn.execute(f"UPDATE {table} SET {target_col}=? WHERE {pk_col}=?", (rel_path, record_id))
        return True
    except Exception:
        return False


def update_book_folder_path(conn, category, record_id, book_folder):
    """يحدّث Folder_Path إن وُجد في الجدول."""
    cat = (category or "").lower()
    if cat in ("inbook", "incoming", "in"):
        table, pk_col = "In_tbl", "NoBook_In"
    elif cat in ("outbook", "outgoing", "out"):
        table, pk_col = "Out_tbl", "NoBook_Out"
    else:
        return False
    try:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "Folder_Path" not in cols:
            return False
        conn.execute(f"UPDATE {table} SET Folder_Path=? WHERE {pk_col}=?", (book_folder, record_id))
        return True
    except Exception:
        return False


def password_is_valid(stored, plain):
    """يقبل كلمة مرور مهاشة (scrypt/pbkdf2) أو نصاً قديماً للتوافق."""
    if not stored or plain is None:
        return False
    s = stored if isinstance(stored, str) else str(stored)
    if s.startswith(('scrypt:', 'pbkdf2:')):
        return check_password_hash(s, plain)
    return s == plain


def hash_password(plain):
    return generate_password_hash(plain)


def make_scan_filename(book_id, dep_name, original_filename):
    """Generate filename like: 10_الادارة.pdf"""
    ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else 'pdf'
    safe_dep = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(dep_name)).strip() or 'عام'
    return f"{book_id}_{safe_dep}.{ext}"


def latest_incoming_book_scan_path(book_id):
    """آخر مرفق مسح في مجلد الكتاب (ليس مرفقات الحركات)."""
    book_folder = os.path.join(UPLOAD_FOLDER, 'in', str(book_id))
    if not os.path.isdir(book_folder):
        return None
    files = [
        fn for fn in os.listdir(book_folder)
        if os.path.isfile(os.path.join(book_folder, fn))
    ]
    if not files:
        return None
    files.sort()
    return os.path.join(book_folder, files[-1])


def attach_scan_to_movement(conn, book_id, move_id, from_dep, source_path):
    """نسخ ملف المسح إلى مجلد الحركة وربطه بـ Book_Movement."""
    dep_name = 'عام'
    if from_dep:
        row = conn.execute('SELECT Dep_Name FROM Department WHERE Dep_No=?', (from_dep,)).fetchone()
        if row:
            dep_name = row['Dep_Name']
    move_folder = os.path.join(UPLOAD_FOLDER, 'in', str(book_id), 'movements', str(move_id))
    os.makedirs(move_folder, exist_ok=True)
    fname = make_scan_filename(book_id, dep_name, os.path.basename(source_path))
    filepath = os.path.join(move_folder, fname)
    shutil.copy2(source_path, filepath)
    rel_path = os.path.relpath(filepath, UPLOAD_FOLDER).replace('\\', '/')
    conn.execute('UPDATE Book_Movement SET Attachment_Path=? WHERE Move_ID=?', (rel_path, move_id))
    return rel_path


def attachment_filename_incoming(no_book_dep, book_internal_id, original_filename):
    """مرفق الوارد: اسم الملف من رقم وارد الدائرة فقط (+ الامتداد)."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "pdf"
    if ext not in ALLOWED_EXTENSIONS:
        ext = "pdf"
    ref = (no_book_dep or "").strip() if no_book_dep else ""
    if not ref:
        base = str(book_internal_id)
    else:
        base = filename_safe_token(ref, str(book_internal_id))
    return f"{base}.{ext}"


def attachment_filename_outgoing(no_book_out_manual, book_internal_id, original_filename):
    """مرفق الصادر: اسم الملف من رقم الصادر اليدوي (صادر الدائرة) فقط (+ الامتداد)."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "pdf"
    if ext not in ALLOWED_EXTENSIONS:
        ext = "pdf"
    ref = (no_book_out_manual or "").strip() if no_book_out_manual else ""
    if not ref:
        base = str(book_internal_id)
    else:
        base = filename_safe_token(ref, str(book_internal_id))
    return f"{base}.{ext}"


def scan_proposed_filename_incoming(book_row, pk):
    """اسم ملف المسح للوارد: رقم وارد الدائرة فقط (مع الامتداد)."""
    return attachment_filename_incoming(book_row["NoBook_Dep"], pk, "scan.pdf")


def scan_proposed_filename_outgoing(book_row, pk):
    """اسم ملف المسح للصادر: رقم صادر الدائرة فقط (مع الامتداد)."""
    return attachment_filename_outgoing(book_row["NoBook_Out_Manual"], pk, "scan.pdf")


def user_may_delete_owned_file(role, user_dep, owner_dep_id):
    """حذف مرفق مجلد الكتاب: المدير دائماً؛ الموظف فقط إن كان رافع الملف من قسمه."""
    if role == "مشاهد":
        return False
    if role == "مدير":
        return True
    if owner_dep_id is None:
        return False
    try:
        return int(user_dep or 0) == int(owner_dep_id)
    except (TypeError, ValueError):
        return str(user_dep) == str(owner_dep_id)


def register_book_file_owner(conn, kind, book_id, rel_path, dep_id):
    """تسجيل قسم رفع الملف (مجلد الكتاب in|out فقط). kind: 'in' | 'out'."""
    rp = (rel_path or "").strip().replace("\\", "/")
    if not rp:
        return
    try:
        conn.execute(
            "INSERT INTO Book_Attachment (kind, book_id, rel_path, dep_id) VALUES (?,?,?,?)",
            (kind, int(book_id), rp, dep_id),
        )
    except Exception:
        try:
            conn.execute("DELETE FROM Book_Attachment WHERE rel_path=?", (rp,))
            conn.execute(
                "INSERT INTO Book_Attachment (kind, book_id, rel_path, dep_id) VALUES (?,?,?,?)",
                (kind, int(book_id), rp, dep_id),
            )
        except Exception:
            pass


def norm_upload_rel(p):
    return (p or "").strip().replace("\\", "/")


def parse_incoming_stored_rel(book_id, rel_path):
    """يُرجع ('root', اسم_الملف) أو ('movement', move_id, اسم_الملف) أو None."""
    p = norm_upload_rel(rel_path)
    exp = f"in/{int(book_id)}/"
    if not p.startswith(exp):
        return None
    tail = p[len(exp) :]
    if not tail or ".." in tail.split("/"):
        return None
    parts = tail.split("/")
    if len(parts) >= 3 and parts[0] == "movements" and parts[1].isdigit():
        fname = "/".join(parts[2:])
        if not fname or ".." in fname:
            return None
        return ("movement", int(parts[1]), fname)
    if len(parts) == 1:
        return ("root", parts[0])
    return None


def parse_outgoing_stored_rel(book_id, rel_path):
    p = norm_upload_rel(rel_path)
    exp = f"out/{int(book_id)}/"
    if not p.startswith(exp):
        return None
    tail = p[len(exp) :]
    if not tail or "/" in tail or ".." in tail:
        return None
    return ("root", tail)


def next_numeric_no_book_dep(conn):
    """تسلسل رقم وارد الدائرة: آخر قيمة رقمية + 1، أو 1."""
    mx = 0
    for row in conn.execute(
        "SELECT NoBook_Dep FROM In_tbl WHERE NoBook_Dep IS NOT NULL AND TRIM(NoBook_Dep) != ''"
    ).fetchall():
        v = (row["NoBook_Dep"] or "").strip()
        if v.isdigit():
            mx = max(mx, int(v))
    return mx + 1


def next_numeric_no_book_out_manual(conn):
    """تسلسل رقم صادر الدائرة (الحقل اليدوي): آخر قيمة رقمية + 1، أو 1."""
    mx = 0
    for row in conn.execute(
        "SELECT NoBook_Out_Manual FROM Out_tbl WHERE NoBook_Out_Manual IS NOT NULL AND TRIM(NoBook_Out_Manual) != ''"
    ).fetchall():
        v = (row["NoBook_Out_Manual"] or "").strip()
        if v.isdigit():
            mx = max(mx, int(v))
    return mx + 1


def ensure_schema():
    """Add new columns to existing tables without breaking anything."""
    conn = get_db()
    new_cols = [
        ("In_tbl",  "NoBook_Dep TEXT"),
        ("In_tbl",  "Date_Dep DATE"),
        ("Out_tbl", "Reply_To_InBook_No INTEGER"),
        ("Out_tbl", "NoBook_Out_Manual TEXT"),
        ("In_tbl",  "attachment_path TEXT"),
        ("Out_tbl", "attachment_path TEXT"),
        ("organization_tbl", "Logo_Path TEXT"),
        ("organization_tbl", "Viewer_QR_Token TEXT"),
        ("organization_tbl", "Email TEXT"),
        ("organization_tbl", "Website TEXT"),
        ("Users", "Dep_ID INTEGER"),
    ]
    for table, col_def in new_cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except Exception:
            pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Book_Attachment (
                Att_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL UNIQUE,
                dep_id INTEGER
            )
            """
        )
    except Exception:
        pass
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Dept_Book_Seen (
                Dep_ID INTEGER NOT NULL,
                Book_In_ID INTEGER NOT NULL,
                Seen_At TEXT DEFAULT (datetime('now','localtime')),
                User_ID INTEGER,
                PRIMARY KEY (Dep_ID, Book_In_ID)
            )
            """
        )
    except Exception:
        pass
    conn.commit()
    conn.close()
    try:
        from activity_log import ensure_activity_log_table
        ensure_activity_log_table(DB_PATH)
    except Exception:
        pass
    try:
        import ocr_attachments
        ocr_attachments.init_ocr(DB_PATH, UPLOAD_FOLDER)
    except Exception:
        pass
    try:
        conn = get_db()
        ensure_admin_department(conn)
        conn.close()
    except Exception:
        pass


def reset_dept_arrival_alert(conn, book_id, dep_id):
    """عند وصول كتاب لقسم: يُلغى «سُبق الفتح» لذلك القسم ليظهر التنبيه من جديد."""
    if not book_id or not dep_id:
        return
    try:
        conn.execute(
            'DELETE FROM Dept_Book_Seen WHERE Book_In_ID=? AND Dep_ID=?',
            (int(book_id), int(dep_id)),
        )
    except Exception:
        pass


def mark_dept_book_seen(conn, book_id, dep_id, user_id=None):
    if not book_id or not dep_id:
        return
    try:
        conn.execute(
            '''INSERT OR REPLACE INTO Dept_Book_Seen (Dep_ID, Book_In_ID, Seen_At, User_ID)
               VALUES (?, ?, datetime('now','localtime'), ?)''',
            (int(dep_id), int(book_id), user_id),
        )
    except Exception:
        pass


def list_unread_dept_books(conn, dep_id, limit=30):
    if not dep_id:
        return []
    return conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com, i.Date_Com
        FROM In_tbl i
        WHERE i.Current_Dep_ID = ?
          AND IFNULL(i.Status, '') = 'في طور العمل'
          AND NOT EXISTS (
            SELECT 1 FROM Dept_Book_Seen s
            WHERE s.Dep_ID = i.Current_Dep_ID AND s.Book_In_ID = i.NoBook_In
          )
        ORDER BY i.NoBook_In DESC
        LIMIT ?
        ''',
        (int(dep_id), int(limit)),
    ).fetchall()


ADMIN_DEP_NAME = 'الادارة'


def _normalize_dep_name(name):
    n = (name or '').strip()
    for a, b in (('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'), ('ة', 'ه')):
        n = n.replace(a, b)
    return n


def dep_name_is_admin(name):
    return _normalize_dep_name(name) == _normalize_dep_name(ADMIN_DEP_NAME)


def ensure_admin_department(conn):
    """قسم الادارة قسم أساسي يُنشأ تلقائياً إن لم يوجد."""
    rows = conn.execute('SELECT Dep_No, Dep_Name FROM Department').fetchall()
    for r in rows:
        if dep_name_is_admin(r['Dep_Name']):
            return r['Dep_No']
    conn.execute('INSERT INTO Department (Dep_Name) VALUES (?)', (ADMIN_DEP_NAME,))
    conn.commit()
    row = conn.execute(
        'SELECT Dep_No FROM Department ORDER BY Dep_No DESC LIMIT 1'
    ).fetchone()
    return row['Dep_No'] if row else None


def get_admin_department_id(conn):
    rows = conn.execute('SELECT Dep_No, Dep_Name FROM Department').fetchall()
    for r in rows:
        if dep_name_is_admin(r['Dep_Name']):
            return r['Dep_No']
    return None


def list_unread_admin_return_books(conn, admin_dep_id, limit=30):
    """كتب أُرجعت من قسم آخر إلى الادارة ولم يفتحها المدير بعد."""
    if not admin_dep_id:
        return []
    return conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com, i.Date_Com
        FROM In_tbl i
        WHERE i.Current_Dep_ID = ?
          AND IFNULL(i.Status, '') = 'في طور العمل'
          AND EXISTS (
            SELECT 1 FROM Book_Movement m
            WHERE m.Book_In_ID = i.NoBook_In
              AND m.Move_ID = (
                SELECT MAX(m2.Move_ID) FROM Book_Movement m2
                WHERE m2.Book_In_ID = i.NoBook_In
              )
              AND m.To_Dep_ID = ?
              AND m.From_Dep_ID IS NOT NULL
              AND m.From_Dep_ID != m.To_Dep_ID
          )
          AND NOT EXISTS (
            SELECT 1 FROM Dept_Book_Seen s
            WHERE s.Dep_ID = i.Current_Dep_ID AND s.Book_In_ID = i.NoBook_In
          )
        ORDER BY i.NoBook_In DESC
        LIMIT ?
        ''',
        (int(admin_dep_id), int(admin_dep_id), int(limit)),
    ).fetchall()


def detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def viewer_access_base_url():
    """رابط الشبكة للموبايل (ليس 127.0.0.1) لرمز QR."""
    configured = (NETWORK.get('server_url') or '').strip().rstrip('/')
    if configured and '127.0.0.1' not in configured and 'localhost' not in configured.lower():
        return configured
    port = int(NETWORK.get('port', 8000))
    return f'http://{detect_lan_ip()}:{port}'


def ensure_qr_viewer_user(conn=None):
    """حساب مشاهد مخصّص لدخول QR — يُنشأ تلقائياً إن لم يوجد."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute(
            'SELECT * FROM Users WHERE Username=?',
            (QR_VIEWER_USERNAME,),
        ).fetchone()
        if row:
            if row['Role'] != 'مشاهد':
                conn.execute(
                    "UPDATE Users SET Role='مشاهد' WHERE User_ID=?",
                    (row['User_ID'],),
                )
                conn.commit()
                row = conn.execute(
                    'SELECT * FROM Users WHERE User_ID=?',
                    (row['User_ID'],),
                ).fetchone()
            return row
        conn.execute(
            "INSERT INTO Users (Username, Password, Role) VALUES (?, ?, 'مشاهد')",
            (QR_VIEWER_USERNAME, hash_password(secrets.token_urlsafe(24))),
        )
        conn.commit()
        return conn.execute(
            'SELECT * FROM Users WHERE Username=?',
            (QR_VIEWER_USERNAME,),
        ).fetchone()
    finally:
        if own:
            conn.close()


def get_or_create_viewer_qr_token(conn, force_new=False):
    org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
    token = ''
    if org:
        try:
            token = (org['Viewer_QR_Token'] or '').strip()
        except (KeyError, IndexError, TypeError):
            token = ''
    if force_new or not token:
        token = secrets.token_urlsafe(24)
        if org:
            conn.execute(
                'UPDATE organization_tbl SET Viewer_QR_Token=? WHERE Organization_No=?',
                (token, org['Organization_No']),
            )
        else:
            conn.execute(
                'INSERT INTO organization_tbl (Name, Information, Address, Phone, Viewer_QR_Token) '
                'VALUES (?, ?, ?, ?, ?)',
                ('', '', '', '', token),
            )
        conn.commit()
    return token


def allowed_logo(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in LOGO_EXTENSIONS


def admin_exists():
    """Return True if at least one admin account is registered."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM Users WHERE Role='مدير'").fetchone()[0]
    conn.close()
    return count > 0


# ═══════════════════════════════════════════════════════════════════════════════
# DECORATORS
# ═══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def login_required_json(f):
    """للمسارات التي تتوقع JSON (المسح) حتى لا يُعاد HTML فيسبب Unexpected token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify(ok=False, error='auth', message='يجب تسجيل الدخول'), 401
        return f(*args, **kwargs)
    return decorated


_MOBILE_UA_RE = re.compile(
    r'(android|iphone|ipad|ipod|mobile|tablet|webos|blackberry|iemobile|opera mini)',
    re.I,
)


def is_mobile_or_tablet():
    """جهاز موبايل أو تابلت حسب User-Agent."""
    ua = request.headers.get('User-Agent', '')
    return bool(_MOBILE_UA_RE.search(ua))


def home_url_for_session():
    """الصفحة الافتراضية بعد الدخول حسب الدور."""
    role = session.get('role')
    if role == 'مدير':
        return url_for('index')
    if role == 'موظف قسم':
        return url_for('incoming_list')
    # مشاهد وغيره: البحث والإحصائيات فقط
    return url_for('search')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'مدير':
            flash('هذه الصفحة للمدير فقط', 'danger')
            return redirect(home_url_for_session())
        return f(*args, **kwargs)
    return decorated


def deny_non_admin_section(message='غير مصرح بالوصول لهذا القسم'):
    """إعادة توجيه الأدوار غير المدير إلى صفحتهم المسموحة."""
    flash(message, 'danger')
    return redirect(home_url_for_session())


@app.context_processor
def inject_scans_inbox_path():
    """مسار مجلد المسودات للمسح (NAPS2) — لعرضه في الواجهة."""
    if not session.get('user_id'):
        return {}
    out = {}
    try:
        out['scans_inbox_full_path'] = os.path.abspath(SCANS_INBOX_FOLDER)
    except Exception:
        out['scans_inbox_full_path'] = SCANS_INBOX_FOLDER
    try:
        conn = get_db()
        out['org'] = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
        conn.close()
    except Exception:
        out['org'] = None
    out['viewer_mobile'] = (
        session.get('role') == 'مشاهد' and is_mobile_or_tablet()
    )
    out['viewer_qr_mode'] = bool(session.get('viewer_qr')) or out['viewer_mobile']
    out['is_admin'] = session.get('role') == 'مدير'
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# LICENSE (تفعيل السريال)
# ═══════════════════════════════════════════════════════════════════════════════

import yai_product_license as yai_license

_LICENSE_EXEMPT_ENDPOINTS = frozenset({
    'license_activate',
    'static',
    'ping',
})


@app.before_request
def require_product_license():
    ep = request.endpoint or ''
    if ep in _LICENSE_EXEMPT_ENDPOINTS or ep.startswith('static'):
        return None
    if not yai_license.is_activated():
        return redirect(url_for('license_activate'))
    return None


@app.route('/license/activate', methods=['GET', 'POST'])
def license_activate():
    if yai_license.is_activated():
        if not admin_exists():
            return redirect(url_for('setup'))
        if 'user_id' in session:
            return redirect(url_for('index'))
        return redirect(url_for('login'))

    error = None
    if request.method == 'POST':
        serial = request.form.get('serial', '')
        ok, err = yai_license.activate(serial)
        if ok:
            flash('تم تفعيل البرنامج بنجاح', 'success')
            if not admin_exists():
                return redirect(url_for('setup'))
            return redirect(url_for('login'))
        error = err

    diag = yai_license.activation_diagnostics()
    return render_template(
        'activate.html',
        error=error,
        machine_code=yai_license.get_machine_request_code(),
        vendor_phone=yai_license.VENDOR_PHONE,
        vendor_name=yai_license.VENDOR_NAME,
        license_path=diag.get('license_path', ''),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and request.args.get('relogin') == '1':
        session.clear()
    if 'user_id' in session:
        return redirect(home_url_for_session())
    # If no admin exists yet → first-time setup
    if not admin_exists():
        return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        conn = get_db()
        user = conn.execute(
            'SELECT * FROM Users WHERE Username=?',
            (username,),
        ).fetchone()
        conn.close()
        if user and password_is_valid(user['Password'], password):
            session['user_id'] = user['User_ID']
            session['username'] = user['Username']
            session['role']     = user['Role']
            session['dep_id']   = user['Dep_ID']
            return redirect(home_url_for_session())
        error = 'اسم المستخدم أو كلمة المرور غير صحيحة'
    return render_template('login.html', error=error)


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """First-time setup: create the first admin account (after license activation)."""
    ensure_schema()
    if admin_exists():
        return redirect(url_for('login'))

    error = None
    if request.method == 'POST':
        new_user = request.form.get('new_username', '').strip()
        new_pass = request.form.get('new_password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        if not new_user or not new_pass:
            error = 'الاسم وكلمة المرور مطلوبان'
        elif len(new_pass) < 4:
            error = 'كلمة المرور يجب أن تكون 4 أحرف على الأقل'
        elif new_pass != confirm:
            error = 'كلمتا المرور غير متطابقتان'
        else:
            conn = get_db()
            exists = conn.execute(
                'SELECT 1 FROM Users WHERE Username=? LIMIT 1', (new_user,)
            ).fetchone()
            if exists:
                conn.close()
                error = 'اسم المستخدم موجود مسبقاً — اختر اسماً آخر'
            else:
                admin_dep_id = ensure_admin_department(conn)
                conn.execute(
                    "INSERT INTO Users (Username, Password, Role, Dep_ID) VALUES (?, ?, 'مدير', ?)",
                    (new_user, hash_password(new_pass), admin_dep_id),
                )
                conn.commit()
                conn.close()
                flash(
                    f'تم إنشاء حساب المدير "{new_user}" وربطه بقسم الادارة — يمكنك الدخول الآن وإنشاء باقي المستخدمين',
                    'success',
                )
                return redirect(url_for('login'))

    return render_template('setup.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
@login_required
def index():
    if session.get('role') != 'مدير':
        return redirect(home_url_for_session())
    conn = get_db()
    total_in    = conn.execute('SELECT COUNT(*) FROM In_tbl').fetchone()[0]
    total_out   = conn.execute('SELECT COUNT(*) FROM Out_tbl').fetchone()[0]
    in_progress = conn.execute("SELECT COUNT(*) FROM In_tbl WHERE Status='في طور العمل'").fetchone()[0]
    completed   = conn.execute("SELECT COUNT(*) FROM In_tbl WHERE Status='تم الانتهاء'").fetchone()[0]

    recent_in = conn.execute('''
        SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        ORDER BY i.NoBook_In DESC LIMIT 8
    ''').fetchall()

    dept_stats = conn.execute('''
        SELECT d.Dep_Name, COUNT(i.NoBook_In) as book_count
        FROM Department d
        LEFT JOIN In_tbl i ON d.Dep_No = i.Current_Dep_ID
        GROUP BY d.Dep_No, d.Dep_Name
        ORDER BY book_count DESC
    ''').fetchall()

    org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
    conn.close()
    return render_template('index.html',
        total_in=total_in, total_out=total_out,
        in_progress=in_progress, completed=completed,
        recent_in=recent_in, dept_stats=dept_stats, org=org)


# ═══════════════════════════════════════════════════════════════════════════════
# INCOMING BOOKS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/incoming')
@login_required
def incoming_list():
    role = session.get('role')
    # المشاهد: لا قائمة كتب — البحث والإحصائيات فقط
    if role == 'مشاهد':
        return deny_non_admin_section('المشاهد يستخدم البحث والإحصائيات فقط')
    conn = get_db()
    dep_id = session.get('dep_id')
    status_filter = request.args.get('status', '')

    base_q = '''
        SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE 1=1
    '''
    params = []
    if role == 'موظف قسم' and dep_id:
        base_q += ' AND i.Current_Dep_ID = ?'
        params.append(dep_id)
    elif role == 'موظف قسم' and not dep_id:
        flash('حسابك غير مربوط بقسم — راجع المدير', 'warning')
        books = []
        conn.close()
        return render_template(
            'in_books.html',
            books=books,
            status_filter=status_filter,
            staff_directed_only=True,
        )
    if status_filter:
        base_q += ' AND i.Status = ?'
        params.append(status_filter)
    base_q += ' ORDER BY i.NoBook_In DESC'

    books = conn.execute(base_q, params).fetchall()
    conn.close()
    return render_template(
        'in_books.html',
        books=books,
        status_filter=status_filter,
        staff_directed_only=(role == 'موظف قسم'),
    )


@app.route('/incoming/add', methods=['GET', 'POST'])
@login_required
def incoming_add():
    if session.get('role') != 'مدير':
        return deny_non_admin_section('إضافة الوارد للمدير فقط')
    conn = get_db()
    if request.method == 'POST':
        no_come    = request.form.get('NoBookCome_In', '').strip()
        date_com   = request.form.get('Date_Com', '')
        subject    = request.form.get('Subject_Com', '').strip()
        add_in_id  = request.form.get('Add_In_ID') or None
        dep_id     = request.form.get('Current_Dep_ID') or None
        no_dep     = request.form.get('NoBook_Dep', '').strip()
        date_dep   = request.form.get('Date_Dep', '')

        cur = conn.execute('''
            INSERT INTO In_tbl
              (NoBookCome_In, Date_Com, Subject_Com, Add_In_ID, Current_Dep_ID,
               NoBook_Dep, Date_Dep, Status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'في طور العمل')
        ''', (no_come, date_com, subject, add_in_id, dep_id, no_dep, date_dep))
        book_id = cur.lastrowid
        if dep_id:
            reset_dept_arrival_alert(conn, book_id, dep_id)

        book_folder = os.path.join(UPLOAD_FOLDER, 'in', str(book_id))
        os.makedirs(book_folder, exist_ok=True)

        conn.commit()
        conn.close()
        try:
            from activity_log import log_activity
            log_activity(
                DB_PATH, 'إضافة', 'وارد',
                ref_no=no_dep or no_come or book_id,
                title=subject,
                details=f'رقم الوارد: {no_come} | وارد الدائرة: {no_dep} | معرف: {book_id}',
            )
        except Exception:
            pass
        flash('تم إضافة الكتاب الوارد بنجاح', 'success')
        return redirect(url_for('incoming_view', id=book_id))

    sources     = conn.execute('SELECT * FROM Add_In ORDER BY In_place').fetchall()
    departments = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    next_dep = next_numeric_no_book_dep(conn)
    conn.close()
    return render_template('in_book_add.html', sources=sources, departments=departments,
                           today=datetime.today().strftime('%Y-%m-%d'), next_dep=next_dep)


@app.route('/incoming/<int:id>')
@login_required
def incoming_view(id):
    conn = get_db()
    book = conn.execute('''
        SELECT i.*, a.In_place as source_name, d.Dep_Name as current_dep_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.NoBook_In = ?
    ''', (id,)).fetchone()

    if not book:
        flash('الكتاب غير موجود', 'danger')
        return redirect(url_for('incoming_list'))

    role     = session.get('role')
    user_dep = session.get('dep_id')

    # موظف القسم: فقط الكتب الموجهة لقسمه
    if role == 'موظف قسم':
        if not user_dep or book['Current_Dep_ID'] != user_dep:
            flash('هذا الكتاب غير موجه لقسمك', 'danger')
            return redirect(url_for('incoming_list'))

    # موظف القسم: فتح الكتاب = اختفاء التنبيه عنه
    if role == 'موظف قسم' and user_dep and book['Current_Dep_ID'] == user_dep:
        mark_dept_book_seen(conn, id, user_dep, session.get('user_id'))
        conn.commit()
    # المدير: فتح كتاب في الادارة = اختفاء تنبيه الاستلام
    if role == 'مدير':
        admin_dep_id = get_admin_department_id(conn)
        if admin_dep_id and book['Current_Dep_ID'] == admin_dep_id:
            mark_dept_book_seen(conn, id, admin_dep_id, session.get('user_id'))
            conn.commit()

    if role == 'مدير':
        can_edit = True
    elif role == 'موظف قسم':
        can_edit = (book['Current_Dep_ID'] == user_dep) and (book['Status'] != 'تم الانتهاء')
    else:
        can_edit = False

    movements = conn.execute('''
        SELECT m.*, fd.Dep_Name as from_dep_name, td.Dep_Name as to_dep_name
        FROM Book_Movement m
        LEFT JOIN Department fd ON m.From_Dep_ID = fd.Dep_No
        LEFT JOIN Department td ON m.To_Dep_ID = td.Dep_No
        WHERE m.Book_In_ID = ?
        ORDER BY m.Move_Date
    ''', (id,)).fetchall()

    book_files = []
    book_folder = os.path.join(UPLOAD_FOLDER, 'in', str(id))
    if os.path.isdir(book_folder):
        for fn in sorted(os.listdir(book_folder)):
            if os.path.isfile(os.path.join(book_folder, fn)):
                book_files.append(fn)

    latest_scan = None
    if book_files:
        latest_scan = f'in/{id}/{book_files[-1]}'
    for m in reversed(movements):
        if m['Attachment_Path']:
            latest_scan = m['Attachment_Path']
            break

    departments = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    sources     = conn.execute('SELECT * FROM Add_In ORDER BY In_place').fetchall()
    own_rows = conn.execute(
        'SELECT rel_path, dep_id FROM Book_Attachment WHERE kind=? AND book_id=?',
        ('in', id),
    ).fetchall()
    owner_by_rel = {r['rel_path']: r['dep_id'] for r in own_rows}
    book_file_delete_ok = {}
    for fn in book_files:
        rp = f'in/{id}/{fn}'
        book_file_delete_ok[fn] = user_may_delete_owned_file(role, user_dep, owner_by_rel.get(rp))
    conn.close()
    return render_template('in_book_view.html',
        book=book, movements=movements, departments=departments,
        book_files=book_files, can_edit=can_edit, latest_scan=latest_scan,
        sources=sources, book_file_delete_ok=book_file_delete_ok)


@app.route('/incoming/<int:id>/edit', methods=['GET', 'POST'])
@admin_required
def incoming_edit(id):
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (id,)).fetchone()
    if not book:
        conn.close()
        flash('الكتاب غير موجود', 'danger')
        return redirect(url_for('incoming_list'))

    if request.method == 'POST':
        no_come  = request.form.get('NoBookCome_In', '').strip()
        date_com = request.form.get('Date_Com', '')
        subject  = request.form.get('Subject_Com', '').strip()
        add_in   = request.form.get('Add_In_ID') or None
        dep_id   = request.form.get('Current_Dep_ID') or None
        no_dep   = request.form.get('NoBook_Dep', '').strip()
        date_dep = request.form.get('Date_Dep', '')
        status   = request.form.get('Status', 'في طور العمل')
        note     = request.form.get('Final_Note', '').strip()

        conn.execute('''
            UPDATE In_tbl SET NoBookCome_In=?, Date_Com=?, Subject_Com=?,
              Add_In_ID=?, Current_Dep_ID=?, NoBook_Dep=?, Date_Dep=?,
              Status=?, Final_Note=?
            WHERE NoBook_In=?
        ''', (no_come, date_com, subject, add_in, dep_id, no_dep, date_dep, status, note, id))
        old_dep = book['Current_Dep_ID']
        if dep_id and str(dep_id) != str(old_dep or ''):
            reset_dept_arrival_alert(conn, id, dep_id)
        conn.commit()
        conn.close()
        try:
            from activity_log import log_activity
            log_activity(
                DB_PATH, 'تعديل', 'وارد',
                ref_no=no_dep or no_come or id,
                title=subject,
                details=f'معرف: {id} | الحالة: {status} | رقم الوارد: {no_come}',
            )
        except Exception:
            pass
        flash('تم تحديث الكتاب بنجاح', 'success')
        return redirect(url_for('incoming_view', id=id))

    sources     = conn.execute('SELECT * FROM Add_In ORDER BY In_place').fetchall()
    departments = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    conn.close()
    return render_template('in_book_edit.html', book=book, sources=sources,
                           departments=departments)


@app.route('/incoming/<int:id>/delete', methods=['POST'])
@admin_required
def incoming_delete(id):
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (id,)).fetchone()
    ref = ''
    title = ''
    if book:
        ref = book['NoBook_Dep'] or book['NoBookCome_In'] or id
        title = book['Subject_Com'] or ''
    conn.execute('DELETE FROM Book_Movement WHERE Book_In_ID=?', (id,))
    conn.execute('DELETE FROM In_tbl WHERE NoBook_In=?', (id,))
    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        log_activity(
            DB_PATH, 'حذف', 'وارد',
            ref_no=ref,
            title=title,
            details=f'حذف كتاب وارد معرف: {id}',
        )
    except Exception:
        pass
    flash('تم حذف الكتاب الوارد', 'success')
    return redirect(url_for('incoming_list'))


@app.route('/incoming/<int:id>/move', methods=['POST'])
@login_required
def incoming_move(id):
    if not session.get('user_id'):
        return redirect(url_for('login'))
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (id,)).fetchone()
    if not book:
        conn.close()
        return redirect(url_for('incoming_list'))

    # Permission check
    role     = session.get('role')
    user_dep = session.get('dep_id')
    if role == 'مشاهد':
        conn.close()
        flash('غير مصرح', 'danger')
        return redirect(url_for('incoming_view', id=id))
    if role == 'موظف قسم' and book['Current_Dep_ID'] != user_dep:
        conn.close()
        flash('هذا الكتاب ليس في قسمك', 'danger')
        return redirect(url_for('incoming_view', id=id))

    from_dep     = user_dep or book['Current_Dep_ID']
    to_dep       = request.form.get('To_Dep_ID') or None
    note         = request.form.get('Action_Note', '').strip()
    is_completed = 1 if request.form.get('Is_Completed') else 0
    mark_final = request.form.get('mark_final_done')
    if mark_final and role != 'مدير':
        mark_final = None

    if mark_final:
        final_note = request.form.get('Final_Note_text', '').strip() or 'تصدير الكتاب'
        if not note:
            note = final_note

    cur = conn.execute('''
        INSERT INTO Book_Movement (Book_In_ID, From_Dep_ID, To_Dep_ID, Action_Note, Is_Completed)
        VALUES (?, ?, ?, ?, ?)
    ''', (id, from_dep, to_dep, note, is_completed))
    move_id = cur.lastrowid

    f = request.files.get('scan_file')
    attach_current = request.form.get('attach_current_scan') == '1'
    if f and f.filename and allowed_file(f.filename):
        dep_name = 'عام'
        if from_dep:
            row = conn.execute('SELECT Dep_Name FROM Department WHERE Dep_No=?', (from_dep,)).fetchone()
            if row:
                dep_name = row['Dep_Name']

        move_folder = os.path.join(UPLOAD_FOLDER, 'in', str(id), 'movements', str(move_id))
        os.makedirs(move_folder, exist_ok=True)
        fname    = make_scan_filename(id, dep_name, f.filename)
        filepath = os.path.join(move_folder, fname)
        f.save(filepath)
        rel_path = os.path.relpath(filepath, UPLOAD_FOLDER)
        conn.execute('UPDATE Book_Movement SET Attachment_Path=? WHERE Move_ID=?', (rel_path, move_id))

        if role not in ('مدير', 'مشاهد'):
            y = year_from_sqlite_date(book['Date_Com'])
            mirror_upload_to_staff_archive(filepath, dep_name, y)
    elif attach_current and role == 'مدير':
        src_path = latest_incoming_book_scan_path(id)
        if src_path:
            attach_scan_to_movement(conn, id, move_id, from_dep, src_path)
        else:
            flash('لا يوجد مرفق مسح حالي لإرفاقه مع الحركة.', 'warning')

    if to_dep:
        conn.execute('UPDATE In_tbl SET Current_Dep_ID=? WHERE NoBook_In=?', (to_dep, id))
        reset_dept_arrival_alert(conn, id, to_dep)
    if mark_final:
        final_note = request.form.get('Final_Note_text', '').strip()
        if not final_note:
            final_note = 'تصدير الكتاب'
        conn.execute("UPDATE In_tbl SET Status='تم الانتهاء', Final_Note=? WHERE NoBook_In=?",
                     (final_note, id))

    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        ref = (book['NoBook_Dep'] or book['NoBookCome_In'] or id) if book else id
        title = (book['Subject_Com'] or '') if book else ''
        log_activity(
            DB_PATH, 'إضافة', 'حركة',
            ref_no=ref,
            title=title,
            details=f'تسجيل حركة #{move_id} على وارد {id} — ملاحظة: {note or "—"}',
        )
    except Exception:
        pass
    flash('تم تسجيل الحركة بنجاح', 'success')
    return redirect(url_for('incoming_view', id=id))


@app.route('/incoming/<int:book_id>/movement/<int:move_id>/delete', methods=['POST'])
@admin_required
def movement_delete(book_id, move_id):
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (book_id,)).fetchone()
    conn.execute('DELETE FROM Book_Movement WHERE Move_ID=? AND Book_In_ID=?', (move_id, book_id))
    # Revert current dep to last remaining movement's To_Dep_ID
    last = conn.execute('''
        SELECT To_Dep_ID FROM Book_Movement
        WHERE Book_In_ID=? AND To_Dep_ID IS NOT NULL
        ORDER BY Move_ID DESC LIMIT 1
    ''', (book_id,)).fetchone()
    if last:
        conn.execute('UPDATE In_tbl SET Current_Dep_ID=? WHERE NoBook_In=?',
                     (last['To_Dep_ID'], book_id))
        reset_dept_arrival_alert(conn, book_id, last['To_Dep_ID'])
    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        ref = (book['NoBook_Dep'] or book['NoBookCome_In'] or book_id) if book else book_id
        title = (book['Subject_Com'] or '') if book else ''
        log_activity(
            DB_PATH, 'حذف', 'حركة',
            ref_no=ref,
            title=title,
            details=f'حذف حركة #{move_id} من وارد {book_id}',
        )
    except Exception:
        pass
    flash('تم حذف الحركة', 'success')
    return redirect(url_for('incoming_view', id=book_id))


@app.route('/incoming/<int:book_id>/movement/<int:move_id>/edit', methods=['POST'])
@admin_required
def movement_edit(book_id, move_id):
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (book_id,)).fetchone()
    to_dep = request.form.get('To_Dep_ID') or None
    note   = request.form.get('Action_Note', '').strip()
    is_completed = 1 if request.form.get('Is_Completed') else 0

    conn.execute('''
        UPDATE Book_Movement SET To_Dep_ID=?, Action_Note=?, Is_Completed=?
        WHERE Move_ID=? AND Book_In_ID=?
    ''', (to_dep, note, is_completed, move_id, book_id))

    if to_dep:
        conn.execute('UPDATE In_tbl SET Current_Dep_ID=? WHERE NoBook_In=?', (to_dep, book_id))
        reset_dept_arrival_alert(conn, book_id, to_dep)

    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        ref = (book['NoBook_Dep'] or book['NoBookCome_In'] or book_id) if book else book_id
        title = (book['Subject_Com'] or '') if book else ''
        log_activity(
            DB_PATH, 'تعديل', 'حركة',
            ref_no=ref,
            title=title,
            details=f'تعديل حركة #{move_id} — ملاحظة: {note or "—"}',
        )
    except Exception:
        pass
    flash('تم تعديل الحركة', 'success')
    return redirect(url_for('incoming_view', id=book_id))


# ═══════════════════════════════════════════════════════════════════════════════
# OUTGOING BOOKS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/outgoing')
@login_required
def outgoing_list():
    if session.get('role') != 'مدير':
        return deny_non_admin_section('الكتب الصادرة للمدير فقط')
    conn = get_db()
    books = conn.execute('''
        SELECT o.*, a.Out_place as dest_name
        FROM Out_tbl o
        LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
        ORDER BY o.NoBook_Out DESC
    ''').fetchall()
    conn.close()
    return render_template('out_books.html', books=books)


@app.route('/outgoing/<int:id>')
@login_required
def outgoing_view(id):
    role = session.get('role')
    # قائمة الصادر للمدير؛ المشاهد يفتح من البحث فقط؛ موظف القسم لا يصل للصادر
    if role == 'موظف قسم':
        return deny_non_admin_section('الكتب الصادرة غير متاحة لموظف القسم')
    if role not in ('مدير', 'مشاهد'):
        return deny_non_admin_section('غير مصرح')
    conn = get_db()
    book = conn.execute('''
        SELECT o.*, a.Out_place as dest_name
        FROM Out_tbl o
        LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
        WHERE o.NoBook_Out = ?
    ''', (id,)).fetchone()
    if not book:
        flash('الكتاب غير موجود', 'danger')
        return redirect(url_for('outgoing_list'))

    book_files = []
    book_folder = os.path.join(UPLOAD_FOLDER, 'out', str(id))
    if os.path.isdir(book_folder):
        for fn in sorted(os.listdir(book_folder)):
            if os.path.isfile(os.path.join(book_folder, fn)):
                book_files.append(fn)

    latest_scan = f'out/{id}/{book_files[-1]}' if book_files else None
    role = session.get('role')
    user_dep = session.get('dep_id')
    own_rows = conn.execute(
        'SELECT rel_path, dep_id FROM Book_Attachment WHERE kind=? AND book_id=?',
        ('out', id),
    ).fetchall()
    owner_by_rel = {r['rel_path']: r['dep_id'] for r in own_rows}
    book_file_delete_ok = {}
    for fn in book_files:
        rp = f'out/{id}/{fn}'
        book_file_delete_ok[fn] = user_may_delete_owned_file(role, user_dep, owner_by_rel.get(rp))
    reply_in_dep = None
    reply_in_subject = None
    if book['Reply_To_InBook_No']:
        rin = conn.execute(
            'SELECT NoBook_Dep, Subject_Com FROM In_tbl WHERE NoBook_In=?',
            (book['Reply_To_InBook_No'],),
        ).fetchone()
        if rin:
            reply_in_dep = (rin['NoBook_Dep'] or '').strip() or None
            reply_in_subject = (rin['Subject_Com'] or '').strip() or None
    dests = conn.execute('SELECT * FROM Add_Out ORDER BY Out_place').fetchall()
    in_books = conn.execute(
        'SELECT NoBook_In, NoBookCome_In, NoBook_Dep, Subject_Com FROM In_tbl ORDER BY NoBook_In DESC'
    ).fetchall()
    conn.close()
    return render_template(
        'out_book_view.html',
        book=book,
        book_files=book_files,
        latest_scan=latest_scan,
        dests=dests,
        book_file_delete_ok=book_file_delete_ok,
        reply_in_dep=reply_in_dep,
        reply_in_subject=reply_in_subject,
        in_books=in_books,
    )


@app.route('/outgoing/add', methods=['GET', 'POST'])
@login_required
def outgoing_add():
    if session.get('role') != 'مدير':
        return deny_non_admin_section('إضافة الصادر للمدير فقط')
    conn = get_db()
    if request.method == 'POST':
        date_out       = request.form.get('Date_Out', datetime.today().strftime('%Y-%m-%d'))
        subject        = request.form.get('Subject', '').strip()
        add_out_id     = request.form.get('Add_Out_ID') or None
        reply_to       = request.form.get('Reply_To_InBook_No', '').strip() or None
        no_out_manual  = request.form.get('NoBook_Out_Manual', '').strip()

        cur = conn.execute(
            '''INSERT INTO Out_tbl (Date_Out, Subject, Add_Out_ID,
                                    Reply_To_InBook_No, NoBook_Out_Manual)
               VALUES (?, ?, ?, ?, ?)''',
            (date_out, subject, add_out_id, reply_to, no_out_manual)
        )
        book_id = cur.lastrowid

        book_folder = os.path.join(UPLOAD_FOLDER, 'out', str(book_id))
        os.makedirs(book_folder, exist_ok=True)

        conn.commit()
        conn.close()
        try:
            from activity_log import log_activity
            log_activity(
                DB_PATH, 'إضافة', 'صادر',
                ref_no=no_out_manual or book_id,
                title=subject,
                details=f'صادر الدائرة: {no_out_manual} | معرف: {book_id} | رد على وارد: {reply_to or "—"}',
            )
        except Exception:
            pass
        flash('تم إضافة الكتاب الصادر بنجاح', 'success')
        return redirect(url_for('outgoing_view', id=book_id))

    dests = conn.execute('SELECT * FROM Add_Out ORDER BY Out_place').fetchall()
    in_books = conn.execute(
        'SELECT NoBook_In, NoBookCome_In, NoBook_Dep, Subject_Com FROM In_tbl ORDER BY NoBook_In DESC'
    ).fetchall()
    next_out_manual = next_numeric_no_book_out_manual(conn)
    last_out_row = conn.execute(
        "SELECT NoBook_Out_Manual FROM Out_tbl "
        "WHERE NoBook_Out_Manual IS NOT NULL AND TRIM(NoBook_Out_Manual) != '' "
        "ORDER BY NoBook_Out DESC LIMIT 1"
    ).fetchone()
    last_out_manual = (last_out_row['NoBook_Out_Manual'] or '').strip() if last_out_row else ''
    conn.close()
    return render_template('out_book_add.html', dests=dests, in_books=in_books,
                           today=datetime.today().strftime('%Y-%m-%d'),
                           next_out_manual=next_out_manual,
                           last_out_manual=last_out_manual)


@app.route('/outgoing/<int:id>/edit', methods=['POST'])
@login_required
def outgoing_edit(id):
    if session.get('role') != 'مدير':
        return deny_non_admin_section('تعديل الصادر للمدير فقط')
    conn = get_db()
    date_out      = request.form.get('Date_Out', '').strip()
    subject       = request.form.get('Subject', '').strip()
    add_out_id    = request.form.get('Add_Out_ID') or None
    reply_to      = request.form.get('Reply_To_InBook_No', '').strip() or None
    no_out_manual = request.form.get('NoBook_Out_Manual', '').strip()

    conn.execute('''
        UPDATE Out_tbl SET Date_Out=?, Subject=?, Add_Out_ID=?,
          Reply_To_InBook_No=?, NoBook_Out_Manual=?
        WHERE NoBook_Out=?
    ''', (date_out, subject, add_out_id, reply_to, no_out_manual, id))

    role = session.get('role')
    f = request.files.get('scan_file')
    if f and f.filename and allowed_file(f.filename):
        book_folder = os.path.join(UPLOAD_FOLDER, 'out', str(id))
        os.makedirs(book_folder, exist_ok=True)
        fname = attachment_filename_outgoing(no_out_manual, id, f.filename)
        out_full = os.path.join(book_folder, fname)
        f.save(out_full)
        conn.execute('UPDATE Out_tbl SET Folder_Path=? WHERE NoBook_Out=?', (book_folder, id))

        if role not in ('مدير', 'مشاهد'):
            dep_name = 'عام'
            did = session.get('dep_id')
            if did:
                row = conn.execute('SELECT Dep_Name FROM Department WHERE Dep_No=?', (did,)).fetchone()
                if row and row['Dep_Name']:
                    dep_name = row['Dep_Name']
            y = year_from_sqlite_date(date_out)
            mirror_upload_to_staff_archive(out_full, dep_name, y)
        register_book_file_owner(conn, 'out', id, f'out/{id}/{fname}', session.get('dep_id'))

    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        log_activity(
            DB_PATH, 'تعديل', 'صادر',
            ref_no=no_out_manual or id,
            title=subject,
            details=f'معرف: {id} | تاريخ: {date_out} | رد على وارد: {reply_to or "—"}',
        )
    except Exception:
        pass
    flash('تم تحديث الكتاب الصادر', 'success')
    return redirect(url_for('outgoing_view', id=id))


@app.route('/outgoing/<int:id>/delete', methods=['POST'])
@admin_required
def outgoing_delete(id):
    conn = get_db()
    book = conn.execute('SELECT * FROM Out_tbl WHERE NoBook_Out=?', (id,)).fetchone()
    ref = ''
    title = ''
    if book:
        ref = book['NoBook_Out_Manual'] or id
        title = book['Subject'] or ''
    conn.execute('DELETE FROM Out_tbl WHERE NoBook_Out=?', (id,))
    conn.commit()
    conn.close()
    try:
        from activity_log import log_activity
        log_activity(
            DB_PATH, 'حذف', 'صادر',
            ref_no=ref,
            title=title,
            details=f'حذف كتاب صادر معرف: {id}',
        )
    except Exception:
        pass
    flash('تم حذف الكتاب الصادر', 'success')
    return redirect(url_for('outgoing_list'))


def _incoming_may_attach_scan_file(book, role, user_dep):
    if role == 'مشاهد':
        return False
    if role == 'موظف قسم' and book['Current_Dep_ID'] != user_dep:
        return False
    return True


@app.route('/incoming/<int:id>/save-scan', methods=['POST'])
@login_required_json
def incoming_save_scan(id):
    """رفع مسح إلى مجلد الكتاب الوارد — رفع مباشر (JSON / FormData)."""
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (id,)).fetchone()
    if not book:
        conn.close()
        return jsonify(ok=False, error='not_found'), 404
    role = session.get('role')
    user_dep = session.get('dep_id')
    if not _incoming_may_attach_scan_file(book, role, user_dep):
        conn.close()
        return jsonify(ok=False, error='forbidden'), 403
    f = request.files.get('file')
    if not f or not f.filename:
        conn.close()
        return jsonify(ok=False, error='no_file'), 400
    if not allowed_file(f.filename):
        conn.close()
        return jsonify(ok=False, error='bad_type'), 400
    book_folder = os.path.join(UPLOAD_FOLDER, 'in', str(id))
    os.makedirs(book_folder, exist_ok=True)
    fname = attachment_filename_incoming(book['NoBook_Dep'], id, f.filename)
    path_saved = os.path.join(book_folder, fname)
    f.save(path_saved)
    if role not in ('مدير', 'مشاهد'):
        dep_arch = department_name_for_incoming_staff_archive(conn, book, user_dep)
        y = year_from_sqlite_date(book['Date_Com'])
        mirror_upload_to_staff_archive(path_saved, dep_arch, y)
    register_book_file_owner(conn, 'in', id, f'in/{id}/{fname}', user_dep)
    conn.execute(
        'UPDATE In_tbl SET Folder_Path=? WHERE NoBook_In=?',
        (book_folder, id),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True, filename=fname, rel_path=f'in/{id}/{fname}')


@app.route('/outgoing/<int:id>/save-scan', methods=['POST'])
@login_required_json
def outgoing_save_scan(id):
    """رفع مسح إلى مجلد الكتاب الصادر — رفع مباشر (JSON / FormData)."""
    if session.get('role') == 'مشاهد':
        return jsonify(ok=False, error='forbidden'), 403
    conn = get_db()
    book = conn.execute('SELECT * FROM Out_tbl WHERE NoBook_Out=?', (id,)).fetchone()
    if not book:
        conn.close()
        return jsonify(ok=False, error='not_found'), 404
    f = request.files.get('file')
    if not f or not f.filename:
        conn.close()
        return jsonify(ok=False, error='no_file'), 400
    if not allowed_file(f.filename):
        conn.close()
        return jsonify(ok=False, error='bad_type'), 400
    book_folder = os.path.join(UPLOAD_FOLDER, 'out', str(id))
    os.makedirs(book_folder, exist_ok=True)
    fname = attachment_filename_outgoing(book['NoBook_Out_Manual'], id, f.filename)
    path_saved = os.path.join(book_folder, fname)
    f.save(path_saved)
    role = session.get('role')
    if role not in ('مدير', 'مشاهد'):
        dep_name = 'عام'
        did = session.get('dep_id')
        if did:
            row = conn.execute('SELECT Dep_Name FROM Department WHERE Dep_No=?', (did,)).fetchone()
            if row and row['Dep_Name']:
                dep_name = row['Dep_Name']
        y = year_from_sqlite_date(book['Date_Out'])
        mirror_upload_to_staff_archive(path_saved, dep_name, y)
    register_book_file_owner(conn, 'out', id, f'out/{id}/{fname}', session.get('dep_id'))
    conn.execute(
        'UPDATE Out_tbl SET Folder_Path=? WHERE NoBook_Out=?',
        (book_folder, id),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True, filename=fname, rel_path=f'out/{id}/{fname}')


@app.route('/api/scans-inbox/list', methods=['GET'])
@login_required_json
def scans_inbox_list():
    """قائمة ملفات جاهزة في uploads/scans (بعد المسح بـ NAPS2 وما شابه)."""
    if session.get("role") != "مدير":
        return jsonify(ok=False, error="forbidden"), 403
    # Optional category: list scans inside uploads/scans/<category>
    category = sanitize_scan_category(request.args.get('category', ''))
    base_dir = SCANS_INBOX_FOLDER
    if category:
        base_dir = os.path.join(SCANS_INBOX_FOLDER, category)
    files = []
    if os.path.isdir(base_dir):
        for fn in os.listdir(base_dir):
            path = os.path.join(base_dir, fn)
            if not os.path.isfile(path) or not allowed_file(fn):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            files.append({'name': fn, 'mtime': mtime})
        files.sort(key=lambda x: -x['mtime'])
    return jsonify(ok=True, files=files)


@app.route('/scan_document/<category>/<record_id>', methods=['POST'])
@login_required_json
def scan_document(category, record_id):
    """
    مسح ضوئي (ورقة أو عدة أوراق من الفيدر) → ملف PDF واحد باسم رقم الوارد/الصادر
    يُحفظ مباشرة في uploads/in/<id>/ أو uploads/out/<id>/ فقط.
    """
    try:
        return _scan_document_impl(category, record_id)
    except Exception as e:
        detail = (str(e) or type(e).__name__).strip()[:240]
        return jsonify(
            ok=False,
            error="server",
            message="حدث خطأ أثناء المسح. تحقق من الماسح وNAPS2 ثم أعد المحاولة."
            + (f" ({detail})" if detail else ""),
        ), 500


def _scan_document_impl(category, record_id):
    if session.get("role") != "مدير":
        return jsonify(
            ok=False,
            error="forbidden",
            message="المسح الضوئي متاح للمدير فقط.",
        ), 403

    cat = sanitize_scan_category(category)
    if not cat:
        return jsonify(ok=False, error="bad_category"), 400

    try:
        pk = int(str(record_id).strip())
    except (TypeError, ValueError):
        return jsonify(ok=False, error="bad_record_id"), 400

    data = request.get_json(silent=True) or {}
    department_name_js = (data.get("department_name") or "").strip()

    conn = get_db()
    try:
        cat_l = cat.lower()
        if cat_l in ("outbook", "outgoing", "out"):
            book = conn.execute("SELECT * FROM Out_tbl WHERE NoBook_Out=?", (pk,)).fetchone()
            if not book:
                return jsonify(
                    ok=False,
                    error="not_found",
                    message="لا يوجد كتاب صادر بهذا الرقم الداخلي — تأكد أنك على صفحة عرض الكتاب وليس إضافة جديدة.",
                ), 404
            proposed = scan_proposed_filename_outgoing(book, pk)
            book_sub = "out"
        elif cat_l in ("inbook", "incoming", "in"):
            book = conn.execute("SELECT * FROM In_tbl WHERE NoBook_In=?", (pk,)).fetchone()
            if not book:
                return jsonify(
                    ok=False,
                    error="not_found",
                    message="لا يوجد كتاب وارد بهذا الرقم الداخلي — تأكد أنك على صفحة عرض الكتاب وليس إضافة جديدة.",
                ), 404
            role = session.get("role")
            user_dep = session.get("dep_id")
            if not _incoming_may_attach_scan_file(book, role, user_dep):
                return jsonify(ok=False, error="forbidden"), 403
            proposed = scan_proposed_filename_incoming(book, pk)
            book_sub = "in"
        else:
            return jsonify(ok=False, error="bad_category"), 400

        book_folder = os.path.join(UPLOAD_FOLDER, book_sub, str(pk))
        os.makedirs(book_folder, exist_ok=True)
        dest = ensure_unique_dest_path(book_folder, proposed)
        if not dest.lower().endswith(".pdf"):
            dest = os.path.splitext(dest)[0] + ".pdf"
    finally:
        conn.close()

    naps2_console = locate_naps2_console()
    if not naps2_console:
        return jsonify(
            ok=False,
            error="naps2_not_found",
            message=(
                "لم يتم العثور على NAPS2.Console.exe. ثبّت NAPS2 من naps2.com ثم اضبط المسار في "
                "naps2_config.json أو متغير النظام NAPS2_CONSOLE_PATH (أعد تشغيل البرنامج)."
            ),
        ), 500

    cmd, build_err = build_naps2_scan_command(naps2_console, dest)
    if build_err:
        return jsonify(ok=False, error="no_scanner", message=build_err), 400

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=os.path.dirname(naps2_console),
            creationflags=_naps2_subprocess_flags(),
            timeout=int(os.environ.get("NAPS2_TIMEOUT_SECONDS", "300")),
        )
    except subprocess.TimeoutExpired:
        try:
            if os.path.exists(dest) and os.path.getsize(dest) <= 0:
                os.remove(dest)
        except OSError:
            pass
        return jsonify(ok=False, error="scan_timeout", message="انتهت مهلة المسح"), 504
    except Exception:
        try:
            if os.path.exists(dest) and os.path.getsize(dest) <= 0:
                os.remove(dest)
        except OSError:
            pass
        return jsonify(ok=False, error="scan_failed"), 500

    if proc.returncode != 0:
        try:
            if os.path.exists(dest) and os.path.getsize(dest) <= 0:
                os.remove(dest)
        except OSError:
            pass
        msg = (proc.stderr or proc.stdout or "").strip()
        return jsonify(ok=False, error="scan_failed", message=msg or "فشل تشغيل NAPS2"), 500

    try:
        if not os.path.exists(dest) or os.path.getsize(dest) <= 0:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            return jsonify(ok=False, error="empty_file", message="تم حذف ملف فارغ بعد عملية المسح"), 500
    except OSError:
        return jsonify(ok=False, error="file_stat_failed"), 500

    rel_path = os.path.relpath(dest, UPLOAD_FOLDER).replace("\\", "/")
    filename = os.path.basename(dest)

    updated_db = False
    conn = get_db()
    try:
        updated_db = try_update_record_file_path(conn, cat, pk, rel_path)
        update_book_folder_path(conn, cat, pk, book_folder)
        bk_kind = "in" if book_sub == "in" else "out"
        register_book_file_owner(conn, bk_kind, pk, rel_path, session.get("dep_id"))
        conn.commit()
    finally:
        conn.close()

    return jsonify(
        ok=True,
        filename=filename,
        rel_path=rel_path,
        message="تم حفظ المسح كملف PDF واحد في مجلد الكتاب.",
        updated_db=updated_db,
    )


@app.route('/incoming/<int:id>/attach-from-scans', methods=['POST'])
@login_required_json
def incoming_attach_from_scans(id):
    """نسخ ملف من uploads/scans إلى مجلد الكتاب الوارد وربطه بالسجل."""
    if session.get("role") != "مدير":
        return jsonify(ok=False, error="forbidden"), 403
    conn = get_db()
    book = conn.execute('SELECT * FROM In_tbl WHERE NoBook_In=?', (id,)).fetchone()
    if not book:
        conn.close()
        return jsonify(ok=False, error='not_found'), 404
    role = session.get('role')
    user_dep = session.get('dep_id')
    if not _incoming_may_attach_scan_file(book, role, user_dep):
        conn.close()
        return jsonify(ok=False, error='forbidden'), 403
    data = request.get_json(silent=True) or {}
    raw_name = data.get('filename') or request.form.get('filename', '')
    src = resolve_scans_inbox_file(raw_name)
    if not src:
        conn.close()
        return jsonify(ok=False, error='bad_file', message='ملف غير موجود أو غير مسموح'), 400
    book_folder = os.path.join(UPLOAD_FOLDER, 'in', str(id))
    os.makedirs(book_folder, exist_ok=True)
    fname = attachment_filename_incoming(book['NoBook_Dep'], id, os.path.basename(src))
    dest = os.path.join(book_folder, fname)
    try:
        shutil.copy2(src, dest)
    except OSError:
        conn.close()
        return jsonify(ok=False, error='copy_failed'), 500
    conn.execute(
        'UPDATE In_tbl SET Folder_Path=? WHERE NoBook_In=?',
        (book_folder, id),
    )
    register_book_file_owner(conn, 'in', id, f'in/{id}/{fname}', None)
    conn.commit()
    conn.close()
    return jsonify(ok=True, filename=fname, rel_path=f'in/{id}/{fname}')


@app.route('/outgoing/<int:id>/attach-from-scans', methods=['POST'])
@login_required_json
def outgoing_attach_from_scans(id):
    """نسخ ملف من uploads/scans إلى مجلد الكتاب الصادر."""
    if session.get("role") != "مدير":
        return jsonify(ok=False, error="forbidden"), 403
    conn = get_db()
    book = conn.execute('SELECT * FROM Out_tbl WHERE NoBook_Out=?', (id,)).fetchone()
    if not book:
        conn.close()
        return jsonify(ok=False, error='not_found'), 404
    data = request.get_json(silent=True) or {}
    raw_name = data.get('filename') or request.form.get('filename', '')
    src = resolve_scans_inbox_file(raw_name)
    if not src:
        conn.close()
        return jsonify(ok=False, error='bad_file', message='ملف غير موجود أو غير مسموح'), 400
    book_folder = os.path.join(UPLOAD_FOLDER, 'out', str(id))
    os.makedirs(book_folder, exist_ok=True)
    fname = attachment_filename_outgoing(book['NoBook_Out_Manual'], id, os.path.basename(src))
    dest = os.path.join(book_folder, fname)
    try:
        shutil.copy2(src, dest)
    except OSError:
        conn.close()
        return jsonify(ok=False, error='copy_failed'), 500
    conn.execute(
        'UPDATE Out_tbl SET Folder_Path=? WHERE NoBook_Out=?',
        (book_folder, id),
    )
    register_book_file_owner(conn, 'out', id, f'out/{id}/{fname}', None)
    conn.commit()
    conn.close()
    return jsonify(ok=True, filename=fname, rel_path=f'out/{id}/{fname}')


@app.route('/incoming/<int:book_id>/book-attachment/delete', methods=['POST'])
@login_required
def incoming_delete_book_attachment(book_id):
    if session.get('role') == 'مشاهد':
        flash('غير مصرح', 'danger')
        return redirect(url_for('incoming_view', id=book_id))
    rel_path = norm_upload_rel(request.form.get('rel_path', ''))
    parsed = parse_incoming_stored_rel(book_id, rel_path)
    if not parsed:
        flash('مسار غير صالح', 'danger')
        return redirect(url_for('incoming_view', id=book_id))
    role = session.get('role')
    uid_dep = session.get('dep_id')
    parts = [p for p in rel_path.split('/') if p and p != '.']
    full_disk = os.path.normpath(os.path.join(UPLOAD_FOLDER, *parts))
    upload_norm = os.path.normpath(UPLOAD_FOLDER)
    if not full_disk.startswith(upload_norm):
        flash('مسار غير صالح', 'danger')
        return redirect(url_for('incoming_view', id=book_id))
    conn = get_db()
    if parsed[0] == 'root':
        row = conn.execute(
            'SELECT dep_id FROM Book_Attachment WHERE kind=? AND book_id=? AND rel_path=?',
            ('in', book_id, rel_path),
        ).fetchone()
        owner = row['dep_id'] if row else None
        if not user_may_delete_owned_file(role, uid_dep, owner):
            conn.close()
            flash('لا يمكنك حذف هذا المرفق — مرفوع من قسم آخر أو غير مسجل.', 'danger')
            return redirect(url_for('incoming_view', id=book_id))
        if os.path.isfile(full_disk):
            try:
                os.remove(full_disk)
            except OSError:
                conn.close()
                flash('تعذر حذف الملف', 'danger')
                return redirect(url_for('incoming_view', id=book_id))
        conn.execute('DELETE FROM Book_Attachment WHERE rel_path=?', (rel_path,))
        try:
            conn.execute(
                'UPDATE In_tbl SET attachment_path=NULL WHERE NoBook_In=? AND attachment_path=?',
                (book_id, rel_path),
            )
        except Exception:
            pass
        conn.commit()
        conn.close()
        flash('تم حذف المرفق', 'success')
        return redirect(url_for('incoming_view', id=book_id))
    move_id, fname = parsed[1], parsed[2]
    m = conn.execute(
        'SELECT * FROM Book_Movement WHERE Move_ID=? AND Book_In_ID=?',
        (move_id, book_id),
    ).fetchone()
    if not m or not m['Attachment_Path']:
        conn.close()
        flash('الحركة أو المرفق غير موجود', 'danger')
        return redirect(url_for('incoming_view', id=book_id))
    if norm_upload_rel(m['Attachment_Path']) != norm_upload_rel(rel_path):
        conn.close()
        flash('تعارض في مسار المرفق', 'danger')
        return redirect(url_for('incoming_view', id=book_id))
    from_dep = m['From_Dep_ID']
    if role != 'مدير':
        try:
            if from_dep is None or int(uid_dep or 0) != int(from_dep):
                conn.close()
                flash('لا يمكنك حذف مرفق حركة أُرفق من قسم آخر.', 'danger')
                return redirect(url_for('incoming_view', id=book_id))
        except (TypeError, ValueError):
            conn.close()
            flash('لا يمكنك حذف هذا المرفق', 'danger')
            return redirect(url_for('incoming_view', id=book_id))
    if os.path.isfile(full_disk):
        try:
            os.remove(full_disk)
        except OSError:
            conn.close()
            flash('تعذر حذف الملف', 'danger')
            return redirect(url_for('incoming_view', id=book_id))
    conn.execute('UPDATE Book_Movement SET Attachment_Path=NULL WHERE Move_ID=?', (move_id,))
    conn.commit()
    conn.close()
    flash('تم حذف مرفق الحركة', 'success')
    return redirect(url_for('incoming_view', id=book_id))


@app.route('/outgoing/<int:book_id>/book-attachment/delete', methods=['POST'])
@login_required
def outgoing_delete_book_attachment(book_id):
    if session.get('role') == 'مشاهد':
        flash('غير مصرح', 'danger')
        return redirect(url_for('outgoing_view', id=book_id))
    rel_path = norm_upload_rel(request.form.get('rel_path', ''))
    parsed = parse_outgoing_stored_rel(book_id, rel_path)
    if not parsed:
        flash('مسار غير صالح', 'danger')
        return redirect(url_for('outgoing_view', id=book_id))
    role = session.get('role')
    uid_dep = session.get('dep_id')
    parts = [p for p in rel_path.split('/') if p and p != '.']
    full_disk = os.path.normpath(os.path.join(UPLOAD_FOLDER, *parts))
    upload_norm = os.path.normpath(UPLOAD_FOLDER)
    if not full_disk.startswith(upload_norm):
        flash('مسار غير صالح', 'danger')
        return redirect(url_for('outgoing_view', id=book_id))
    conn = get_db()
    row = conn.execute(
        'SELECT dep_id FROM Book_Attachment WHERE kind=? AND book_id=? AND rel_path=?',
        ('out', book_id, rel_path),
    ).fetchone()
    owner = row['dep_id'] if row else None
    if not user_may_delete_owned_file(role, uid_dep, owner):
        conn.close()
        flash('لا يمكنك حذف هذا المرفق — مرفوع من قسم آخر أو غير مسجل.', 'danger')
        return redirect(url_for('outgoing_view', id=book_id))
    if os.path.isfile(full_disk):
        try:
            os.remove(full_disk)
        except OSError:
            conn.close()
            flash('تعذر حذف الملف', 'danger')
            return redirect(url_for('outgoing_view', id=book_id))
    conn.execute('DELETE FROM Book_Attachment WHERE rel_path=?', (rel_path,))
    try:
        conn.execute(
            'UPDATE Out_tbl SET attachment_path=NULL WHERE NoBook_Out=? AND attachment_path=?',
            (book_id, rel_path),
        )
    except Exception:
        pass
    conn.commit()
    conn.close()
    flash('تم حذف المرفق', 'success')
    return redirect(url_for('outgoing_view', id=book_id))


# ═══════════════════════════════════════════════════════════════════════════════
# FILE SERVING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    full_path = os.path.normpath(os.path.join(UPLOAD_FOLDER, filename))
    if not full_path.startswith(os.path.normpath(UPLOAD_FOLDER)):
        abort(403)
    directory = os.path.dirname(full_path)
    file_name = os.path.basename(full_path)
    if file_name.lower().endswith('.pdf'):
        resp = send_from_directory(directory, file_name, mimetype='application/pdf')
        resp.headers['Content-Disposition'] = 'inline'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp
    return send_from_directory(directory, file_name)


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS — admin only
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/departments', methods=['GET', 'POST'])
@admin_required
def departments():
    conn = get_db()
    ensure_admin_department(conn)
    if request.method == 'POST':
        name = request.form.get('Dep_Name', '').strip()
        if name:
            if dep_name_is_admin(name) and get_admin_department_id(conn):
                flash('قسم الادارة موجود مسبقاً وهو قسم أساسي', 'warning')
            else:
                conn.execute('INSERT INTO Department (Dep_Name) VALUES (?)', (name,))
                conn.commit()
                flash('تم إضافة القسم', 'success')
    deps = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    core_dep_id = get_admin_department_id(conn)
    conn.close()
    return render_template(
        'departments.html',
        departments=deps,
        core_dep_id=core_dep_id,
    )


@app.route('/departments/<int:id>/delete', methods=['POST'])
@admin_required
def dept_delete(id):
    conn = get_db()
    row = conn.execute('SELECT Dep_Name FROM Department WHERE Dep_No=?', (id,)).fetchone()
    if row and dep_name_is_admin(row['Dep_Name']):
        conn.close()
        flash('لا يمكن حذف قسم الادارة — قسم أساسي', 'warning')
        return redirect(url_for('departments'))
    conn.execute('DELETE FROM Department WHERE Dep_No=?', (id,))
    conn.commit()
    conn.close()
    flash('تم حذف القسم', 'success')
    return redirect(url_for('departments'))


def _xml_esc(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def build_xlsx_bytes(header, rows):
    """قالب Excel بسيط (عمود واحد) دون مكتبات إضافية."""
    cells = [f'<c r="A1" t="inlineStr"><is><t>{_xml_esc(header)}</t></is></c>']
    for i, name in enumerate(rows, start=2):
        cells.append(
            f'<c r="A{i}" t="inlineStr"><is><t>{_xml_esc(name)}</t></is></c>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">' + cells[0] + '</row>'
        + ''.join(f'<row r="{i+2}">{cells[i+1]}</row>' for i in range(len(rows)))
        + '</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="اسماء" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    ctypes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', ctypes)
        zf.writestr('_rels/.rels', root_rels)
        zf.writestr('xl/workbook.xml', workbook)
        zf.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        zf.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


def _xlsx_col_a_names(data):
    """قراءة العمود A من ملف xlsx."""
    ns = {
        'm': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    }
    names = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared = []
        if 'xl/sharedStrings.xml' in zf.namelist():
            root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
            for si in root.findall('m:si', ns):
                texts = [t.text or '' for t in si.findall('.//m:t', ns)]
                shared.append(''.join(texts))
        sheet_name = 'xl/worksheets/sheet1.xml'
        if sheet_name not in zf.namelist():
            sheets = [n for n in zf.namelist() if n.startswith('xl/worksheets/') and n.endswith('.xml')]
            if not sheets:
                return []
            sheet_name = sheets[0]
        root = ET.fromstring(zf.read(sheet_name))
        for c in root.findall('.//m:c', ns):
            ref = c.get('r') or ''
            if not ref.upper().startswith('A') or not ref[1:].isdigit():
                continue
            t = c.get('t')
            v = c.find('m:v', ns)
            is_el = c.find('m:is', ns)
            val = ''
            if t == 's' and v is not None and v.text and v.text.isdigit():
                idx = int(v.text)
                val = shared[idx] if 0 <= idx < len(shared) else ''
            elif t == 'inlineStr' and is_el is not None:
                val = ''.join((n.text or '') for n in is_el.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t'))
            elif v is not None:
                val = v.text or ''
            names.append(val.strip())
    return names


def names_from_excel_upload(file_storage):
    """استخراج الأسماء من CSV أو Excel (العمود الأول)."""
    fname = (file_storage.filename or '').lower()
    raw = file_storage.read()
    if not raw:
        return []
    names = []
    if fname.endswith('.xlsx'):
        try:
            names = _xlsx_col_a_names(raw)
        except Exception:
            names = []
    else:
        text = raw.decode('utf-8-sig', errors='replace')
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if row:
                names.append((row[0] or '').strip())
    skip_headers = {'اسم الجهة', 'الاسم', 'name', 'اسماء', 'الجهة'}
    out = []
    seen = set()
    for n in names:
        if not n or n.strip().lower() in {h.lower() for h in skip_headers}:
            continue
        key = n.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def excel_template_response(filename, header):
    data = build_xlsx_bytes(header, [])
    resp = make_response(data)
    resp.headers['Content-Type'] = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@app.route('/admin/settings')
@admin_required
def admin_settings():
    return render_template('admin_settings.html')


@app.route('/sources', methods=['GET', 'POST'])
@admin_required
def sources():
    conn = get_db()
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        if action == 'import_excel':
            f = request.files.get('excel_file')
            if not f or not f.filename:
                flash('اختر ملف Excel أو CSV.', 'warning')
            else:
                names = names_from_excel_upload(f)
                existing = {
                    (r['In_place'] or '').strip()
                    for r in conn.execute('SELECT In_place FROM Add_In').fetchall()
                }
                added = 0
                for n in names:
                    if n in existing:
                        continue
                    conn.execute('INSERT INTO Add_In (In_place) VALUES (?)', (n,))
                    existing.add(n)
                    added += 1
                conn.commit()
                skipped = len(names) - added
                flash(f'تم استيراد {added} جهة واردة.' + (f' تُجاهل {skipped} مكرر.' if skipped else ''), 'success' if added else 'warning')
        else:
            name = request.form.get('In_place', '').strip()
            if name:
                conn.execute('INSERT INTO Add_In (In_place) VALUES (?)', (name,))
                conn.commit()
                flash('تم إضافة الجهة الواردة', 'success')
    items = conn.execute('SELECT * FROM Add_In ORDER BY In_place').fetchall()
    conn.close()
    return render_template('sources.html', items=items)


@app.route('/sources/excel-template')
@admin_required
def sources_excel_template():
    return excel_template_response('template-sources.xlsx', 'اسم الجهة')


@app.route('/sources/<int:id>/delete', methods=['POST'])
@admin_required
def source_delete(id):
    conn = get_db()
    conn.execute('DELETE FROM Add_In WHERE Add_InNo=?', (id,))
    conn.commit()
    conn.close()
    flash('تم الحذف', 'success')
    return redirect(url_for('sources'))


@app.route('/destinations', methods=['GET', 'POST'])
@admin_required
def destinations():
    conn = get_db()
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        if action == 'import_excel':
            f = request.files.get('excel_file')
            if not f or not f.filename:
                flash('اختر ملف Excel أو CSV.', 'warning')
            else:
                names = names_from_excel_upload(f)
                existing = {
                    (r['Out_place'] or '').strip()
                    for r in conn.execute('SELECT Out_place FROM Add_Out').fetchall()
                }
                added = 0
                for n in names:
                    if n in existing:
                        continue
                    conn.execute('INSERT INTO Add_Out (Out_place) VALUES (?)', (n,))
                    existing.add(n)
                    added += 1
                conn.commit()
                skipped = len(names) - added
                flash(f'تم استيراد {added} جهة صادرة.' + (f' تُجاهل {skipped} مكرر.' if skipped else ''), 'success' if added else 'warning')
        else:
            name = request.form.get('Out_place', '').strip()
            if name:
                conn.execute('INSERT INTO Add_Out (Out_place) VALUES (?)', (name,))
                conn.commit()
                flash('تم إضافة الجهة الصادرة', 'success')
    items = conn.execute('SELECT * FROM Add_Out ORDER BY Out_place').fetchall()
    conn.close()
    return render_template('destinations.html', items=items)


@app.route('/destinations/excel-template')
@admin_required
def destinations_excel_template():
    return excel_template_response('template-destinations.xlsx', 'اسم الجهة')


@app.route('/destinations/<int:id>/delete', methods=['POST'])
@admin_required
def dest_delete(id):
    conn = get_db()
    conn.execute('DELETE FROM Add_Out WHERE Add_OutNo=?', (id,))
    conn.commit()
    conn.close()
    flash('تم الحذف', 'success')
    return redirect(url_for('destinations'))


@app.route('/users', methods=['GET', 'POST'])
@admin_required
def users():
    conn = get_db()
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        if action == 'add':
            username = request.form.get('Username', '').strip()
            password = request.form.get('Password', '').strip()
            dep_id   = request.form.get('Dep_ID') or None
            role     = request.form.get('Role', 'موظف قسم')
            if username and password:
                try:
                    conn.execute(
                        'INSERT INTO Users (Username, Password, Dep_ID, Role) VALUES (?, ?, ?, ?)',
                        (username, hash_password(password), dep_id, role)
                    )
                    conn.commit()
                    flash('تم إضافة المستخدم', 'success')
                except Exception:
                    flash('اسم المستخدم موجود مسبقاً', 'danger')
            else:
                flash('اسم المستخدم وكلمة المرور مطلوبان', 'warning')
        elif action == 'edit':
            uid_raw = request.form.get('user_id')
            try:
                uid = int(uid_raw)
            except (TypeError, ValueError):
                flash('معرّف المستخدم غير صالح', 'danger')
            else:
                password = request.form.get('Password', '').strip()
                dep_id   = request.form.get('Dep_ID') or None
                role     = request.form.get('Role', 'موظف قسم')
                exists = conn.execute('SELECT 1 FROM Users WHERE User_ID=?', (uid,)).fetchone()
                if not exists:
                    flash('المستخدم غير موجود', 'danger')
                else:
                    if password:
                        conn.execute(
                            'UPDATE Users SET Password=?, Dep_ID=?, Role=? WHERE User_ID=?',
                            (hash_password(password), dep_id, role, uid),
                        )
                    else:
                        conn.execute(
                            'UPDATE Users SET Dep_ID=?, Role=? WHERE User_ID=?',
                            (dep_id, role, uid),
                        )
                    conn.commit()
                    flash('تم تحديث المستخدم', 'success')
        conn.close()
        return redirect(url_for('users'))

    all_users   = conn.execute('''
        SELECT u.*, d.Dep_Name FROM Users u
        LEFT JOIN Department d ON u.Dep_ID = d.Dep_No
        ORDER BY u.User_ID
    ''').fetchall()
    departments = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    conn.close()
    return render_template('users.html', users=all_users, departments=departments)


@app.route('/users/<int:id>/delete', methods=['POST'])
@admin_required
def user_delete(id):
    if id == session.get('user_id'):
        flash('لا يمكنك حذف حسابك الخاص', 'danger')
        return redirect(url_for('users'))
    conn = get_db()
    conn.execute('DELETE FROM Users WHERE User_ID=?', (id,))
    conn.commit()
    conn.close()
    flash('تم حذف المستخدم', 'success')
    return redirect(url_for('users'))


@app.route('/organization', methods=['GET', 'POST'])
@admin_required
def organization():
    conn = get_db()
    if request.method == 'POST':
        action = request.form.get('action', 'save_org')

        if action == 'regen_qr':
            get_or_create_viewer_qr_token(conn, force_new=True)
            ensure_qr_viewer_user(conn)
            conn.close()
            flash('تم إنشاء رمز QR جديد. اطبع الملصق الجديد وثبّته في المؤسسة.', 'success')
            return redirect(url_for('organization'))

        if action == 'remove_logo':
            existing = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
            if existing:
                try:
                    old = (existing['Logo_Path'] or '').strip()
                except (KeyError, IndexError, TypeError):
                    old = ''
                if old:
                    old_full = os.path.normpath(os.path.join(UPLOAD_FOLDER, old.replace('/', os.sep)))
                    upload_norm = os.path.normpath(UPLOAD_FOLDER)
                    if old_full.startswith(upload_norm) and os.path.isfile(old_full):
                        try:
                            os.remove(old_full)
                        except OSError:
                            pass
                conn.execute(
                    'UPDATE organization_tbl SET Logo_Path=NULL WHERE Organization_No=?',
                    (existing['Organization_No'],),
                )
                conn.commit()
            conn.close()
            flash('تم حذف شعار المؤسسة', 'success')
            return redirect(url_for('organization'))

        name    = request.form.get('Name', '').strip()
        info    = request.form.get('Information', '').strip()
        address = request.form.get('Address', '').strip()
        phone   = request.form.get('Phone', '').strip()
        email   = request.form.get('Email', '').strip()
        website = request.form.get('Website', '').strip()
        existing = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
        logo_rel = None
        if existing:
            try:
                logo_rel = existing['Logo_Path']
            except (KeyError, IndexError, TypeError):
                logo_rel = None

        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            if not allowed_logo(logo_file.filename):
                conn.close()
                flash('صيغة الشعار غير مدعومة. استخدم PNG أو JPG أو WEBP.', 'warning')
                return redirect(url_for('organization'))
            ext = logo_file.filename.rsplit('.', 1)[1].lower()
            fname = f'logo.{ext}'
            dest = os.path.join(ORG_UPLOAD_FOLDER, fname)
            # احذف ملفات شعار قديمة بامتداد مختلف
            for old_name in os.listdir(ORG_UPLOAD_FOLDER):
                if old_name.lower().startswith('logo.'):
                    try:
                        os.remove(os.path.join(ORG_UPLOAD_FOLDER, old_name))
                    except OSError:
                        pass
            logo_file.save(dest)
            logo_rel = f'org/{fname}'

        if existing:
            conn.execute(
                'UPDATE organization_tbl SET Name=?, Information=?, Address=?, Phone=?, '
                'Email=?, Website=?, Logo_Path=? WHERE Organization_No=?',
                (name, info, address, phone, email, website, logo_rel, existing['Organization_No'])
            )
        else:
            conn.execute(
                'INSERT INTO organization_tbl (Name, Information, Address, Phone, Email, Website, Logo_Path) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (name, info, address, phone, email, website, logo_rel)
            )
        conn.commit()
        get_or_create_viewer_qr_token(conn, force_new=False)
        ensure_qr_viewer_user(conn)
        flash('تم حفظ معلومات الدائرة', 'success')
        conn.close()
        return redirect(url_for('organization'))

    org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
    token = get_or_create_viewer_qr_token(conn, force_new=False)
    ensure_qr_viewer_user(conn)
    conn.close()
    base = viewer_access_base_url()
    qr_url = f'{base}{url_for("viewer_qr_login", token=token)}'
    return render_template(
        'organization.html',
        org=org,
        qr_url=qr_url,
        qr_token=token,
        access_base=base,
    )


@app.route('/organization/qr-print')
@admin_required
def organization_qr_print():
    """صفحة طباعة مستقلة لملصق QR (صورة وليس Canvas فقط)."""
    conn = get_db()
    org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
    token = get_or_create_viewer_qr_token(conn, force_new=False)
    conn.close()
    base = viewer_access_base_url()
    qr_url = f'{base}{url_for("viewer_qr_login", token=token)}'
    org_name = 'Y-ai'
    if org:
        try:
            org_name = (org['Name'] or '').strip() or 'Y-ai'
        except (KeyError, IndexError, TypeError):
            org_name = 'Y-ai'
    return render_template(
        'organization_qr_print.html',
        qr_url=qr_url,
        org_name=org_name,
    )


@app.route('/viewer-qr/<token>')
def viewer_qr_login(token):
    """دخول مشاهد عبر مسح QR من الموبايل داخل المؤسسة (شبكة LAN)."""
    token = (token or '').strip()
    if not token:
        flash('رمز الدخول غير صالح', 'danger')
        return redirect(url_for('login'))
    conn = get_db()
    org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
    stored = ''
    if org:
        try:
            stored = (org['Viewer_QR_Token'] or '').strip()
        except (KeyError, IndexError, TypeError):
            stored = ''
    if not stored or stored != token:
        conn.close()
        flash('رمز QR غير صالح أو تم استبداله. راجع إدارة المؤسسة.', 'danger')
        return redirect(url_for('login'))

    user = ensure_qr_viewer_user(conn)
    conn.close()
    if not user:
        flash('تعذر تجهيز حساب المشاهد', 'danger')
        return redirect(url_for('login'))

    session.clear()
    session['user_id'] = user['User_ID']
    session['username'] = user['Username']
    session['role'] = 'مشاهد'
    session['dep_id'] = user['Dep_ID'] if 'Dep_ID' in user.keys() else None
    session['viewer_qr'] = True
    flash('تم الدخول كمشاهد — يمكنك البحث عن الكتب', 'success')
    return redirect(url_for('search'))


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════════════════════════

def _latest_file_rel_under_uploads(*path_parts):
    """آخر ملف مرفق تحت uploads/<path_parts...> أو None (مسار نسبي بشرطات)."""
    folder = os.path.join(UPLOAD_FOLDER, *path_parts)
    if not os.path.isdir(folder):
        return None
    names = []
    for fn in os.listdir(folder):
        fp = os.path.join(folder, fn)
        if os.path.isfile(fp) and allowed_file(fn):
            names.append(fn)
    if not names:
        return None
    names.sort()
    rel = os.path.join(*path_parts, names[-1])
    return rel.replace("\\", "/")


def _search_apply_date_preset(preset, date_from, date_to):
    """اختصارات التاريخ لصفحة البحث فقط."""
    today = datetime.today().date()
    preset = (preset or '').strip()
    if preset == 'today':
        return today.isoformat(), today.isoformat(), preset
    if preset == 'week':
        start = today - timedelta(days=6)
        return start.isoformat(), today.isoformat(), preset
    if preset == 'month':
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), preset
    if preset == 'year':
        start = today.replace(month=1, day=1)
        return start.isoformat(), today.isoformat(), preset
    return date_from, date_to, preset


def _search_order_sql(kind, sort_by):
    """ترتيب نتائج البحث (وارد/صادر)."""
    sort_by = (sort_by or 'newest').strip()
    if kind == 'in':
        if sort_by == 'oldest':
            return ' ORDER BY i.Date_Com ASC, i.NoBook_In ASC'
        if sort_by == 'number':
            return (
                " ORDER BY CASE WHEN i.NoBook_Dep GLOB '[0-9]*' "
                "THEN CAST(i.NoBook_Dep AS INTEGER) ELSE 999999999 END ASC, i.NoBook_In ASC"
            )
        return ' ORDER BY i.NoBook_In DESC'
    if sort_by == 'oldest':
        return ' ORDER BY o.Date_Out ASC, o.NoBook_Out ASC'
    if sort_by == 'number':
        return (
            " ORDER BY CASE WHEN o.NoBook_Out_Manual GLOB '[0-9]*' "
            "THEN CAST(o.NoBook_Out_Manual AS INTEGER) ELSE 999999999 END ASC, o.NoBook_Out ASC"
        )
    return ' ORDER BY o.NoBook_Out DESC'


@app.route('/search')
@login_required
def search():
    q           = request.args.get('q', '').strip()
    source_id   = request.args.get('source_id', '')
    dep_id      = request.args.get('dep_id', '')
    date_from   = request.args.get('date_from', '')
    date_to     = request.args.get('date_to', '')
    status      = request.args.get('status', '')
    search_type = request.args.get('search_type', 'both')  # in | out | both
    if search_type not in ('in', 'out', 'both'):
        search_type = 'both'
    link_no     = request.args.get('link_no', '').strip()
    dep_no      = request.args.get('dep_no', '').strip()
    sort_by     = request.args.get('sort_by', 'newest').strip() or 'newest'
    date_preset = request.args.get('date_preset', '').strip()
    date_from, date_to, date_preset = _search_apply_date_preset(date_preset, date_from, date_to)

    conn = get_db()

    in_results = []
    out_results = []
    link_in_book = None
    link_out_books = []
    link_out_book = None
    link_in_from_out = None

    if link_no.isdigit():
        nid = int(link_no)
        link_in_book = conn.execute('''
            SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
            FROM In_tbl i
            LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
            LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
            WHERE i.NoBook_In = ?
        ''', (nid,)).fetchone()
        if link_in_book:
            link_out_books = conn.execute('''
                SELECT o.*, a.Out_place as dest_name
                FROM Out_tbl o
                LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
                WHERE o.Reply_To_InBook_No = ?
                ORDER BY o.NoBook_Out DESC
            ''', (nid,)).fetchall()
        link_out_book = conn.execute('''
            SELECT o.*, a.Out_place as dest_name
            FROM Out_tbl o
            LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
            WHERE o.NoBook_Out = ?
        ''', (nid,)).fetchone()
        if link_out_book and link_out_book['Reply_To_InBook_No']:
            link_in_from_out = conn.execute('''
                SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
                FROM In_tbl i
                LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
                LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
                WHERE i.NoBook_In = ?
            ''', (link_out_book['Reply_To_InBook_No'],)).fetchone()

    want_in = search_type in ('in', 'both')
    want_out = search_type in ('out', 'both')
    is_qr_search = bool(session.get('viewer_qr')) or (
        session.get('role') == 'مشاهد' and is_mobile_or_tablet()
    )
    if is_qr_search:
        qr_date = request.args.get('date', '').strip()
        if qr_date:
            date_from = date_to = qr_date
        if search_type not in ('in', 'out'):
            search_type = 'in'
            want_in, want_out = True, False
        else:
            want_in = search_type == 'in'
            want_out = search_type == 'out'
    if (session.get('role') == 'مشاهد' or is_qr_search) and not (q or dep_no or date_from):
        want_in = False
        want_out = False

    in_search_attach = []
    if want_in:
        query = '''
            SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
            FROM In_tbl i
            LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
            LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
            WHERE 1=1
        '''
        params = []
        if q:
            if is_qr_search:
                query += (
                    " AND (i.NoBookCome_In LIKE ? OR i.Subject_Com LIKE ? "
                    "OR CAST(i.NoBook_In AS TEXT) LIKE ? OR i.NoBook_Dep LIKE ? "
                    "OR IFNULL(a.In_place,'') LIKE ? OR IFNULL(d.Dep_Name,'') LIKE ?)"
                )
                params.extend([f'%{q}%'] * 6)
            else:
                query += (
                    " AND (i.NoBookCome_In LIKE ? OR i.Subject_Com LIKE ? "
                    "OR CAST(i.NoBook_In AS TEXT) LIKE ? OR i.NoBook_Dep LIKE ?)"
                )
                params.extend([f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'])
        if dep_no:
            query += " AND i.NoBook_Dep LIKE ?"
            params.append(f'%{dep_no}%')
        if source_id:
            query += " AND i.Add_In_ID = ?"
            params.append(source_id)
        if dep_id:
            query += " AND i.Current_Dep_ID = ?"
            params.append(dep_id)
        if date_from:
            query += " AND i.Date_Com >= ?"
            params.append(date_from)
        if date_to:
            query += " AND i.Date_Com <= ?"
            params.append(date_to)
        if status:
            query += " AND i.Status = ?"
            params.append(status)
        query += _search_order_sql('in', sort_by)
        in_results = conn.execute(query, params).fetchall()

        in_ids = [b["NoBook_In"] for b in in_results]
        out_by_in_id = {}
        if in_ids:
            ph = ",".join("?" * len(in_ids))
            for o in conn.execute(
                f"SELECT NoBook_Out, NoBook_Out_Manual, Reply_To_InBook_No FROM Out_tbl "
                f"WHERE Reply_To_InBook_No IN ({ph}) ORDER BY NoBook_Out DESC",
                in_ids,
            ).fetchall():
                out_by_in_id.setdefault(o["Reply_To_InBook_No"], []).append(o)

        for b in in_results:
            nid = b["NoBook_In"]
            in_rel = _latest_file_rel_under_uploads("in", str(nid))
            outs = []
            for o in out_by_in_id.get(nid, []):
                oid = o["NoBook_Out"]
                outs.append({
                    "NoBook_Out": oid,
                    "NoBook_Out_Manual": o["NoBook_Out_Manual"],
                    "rel_path": _latest_file_rel_under_uploads("out", str(oid)),
                })
            in_search_attach.append({"in_rel": in_rel, "outs": outs})

    out_search_attach = []
    if want_out:
        out_query = '''
            SELECT o.*, a.Out_place as dest_name,
                   i.NoBook_Dep AS reply_in_dep, i.Subject_Com AS reply_in_subject
            FROM Out_tbl o
            LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
            LEFT JOIN In_tbl i ON i.NoBook_In = o.Reply_To_InBook_No
            WHERE 1=1
        '''
        out_params = []
        if q:
            if is_qr_search:
                out_query += (
                    " AND (o.Subject LIKE ? OR CAST(o.NoBook_Out AS TEXT) LIKE ? OR o.NoBook_Out_Manual LIKE ? "
                    "OR CAST(o.Reply_To_InBook_No AS TEXT) LIKE ? OR i.NoBookCome_In LIKE ? OR i.NoBook_Dep LIKE ? "
                    "OR IFNULL(a.Out_place,'') LIKE ?)"
                )
                out_params.extend([f'%{q}%'] * 7)
            else:
                out_query += (
                    " AND (o.Subject LIKE ? OR CAST(o.NoBook_Out AS TEXT) LIKE ? OR o.NoBook_Out_Manual LIKE ? "
                    "OR CAST(o.Reply_To_InBook_No AS TEXT) LIKE ? OR i.NoBookCome_In LIKE ? OR i.NoBook_Dep LIKE ?)"
                )
                out_params.extend([f'%{q}%'] * 6)
        if dep_no:
            out_query += " AND o.NoBook_Out_Manual LIKE ?"
            out_params.append(f'%{dep_no}%')
        if date_from:
            out_query += " AND o.Date_Out >= ?"
            out_params.append(date_from)
        if date_to:
            out_query += " AND o.Date_Out <= ?"
            out_params.append(date_to)
        out_query += _search_order_sql('out', sort_by)
        out_results = conn.execute(out_query, out_params).fetchall()
        for b in out_results:
            out_search_attach.append(_latest_file_rel_under_uploads('out', str(b['NoBook_Out'])))

    sources    = conn.execute('SELECT * FROM Add_In ORDER BY In_place').fetchall()
    all_depts  = conn.execute('SELECT * FROM Department ORDER BY Dep_Name').fetchall()
    total_in   = conn.execute('SELECT COUNT(*) FROM In_tbl').fetchone()[0]
    total_out  = conn.execute('SELECT COUNT(*) FROM Out_tbl').fetchone()[0]
    in_progress= conn.execute("SELECT COUNT(*) FROM In_tbl WHERE Status='في طور العمل'").fetchone()[0]
    completed  = conn.execute("SELECT COUNT(*) FROM In_tbl WHERE Status='تم الانتهاء'").fetchone()[0]

    by_dep = conn.execute('''
        SELECT d.Dep_Name, COUNT(i.NoBook_In) as cnt
        FROM Department d
        LEFT JOIN In_tbl i ON d.Dep_No = i.Current_Dep_ID
        GROUP BY d.Dep_No, d.Dep_Name ORDER BY cnt DESC
    ''').fetchall()

    # كتب متأخرة (في طور العمل أكثر من 14 يوماً)
    overdue_cutoff = (datetime.today().date() - timedelta(days=14)).isoformat()
    overdue_books = conn.execute('''
        SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com, i.Date_Com,
               d.Dep_Name as dep_name,
               CAST(julianday('now') - julianday(i.Date_Com) AS INTEGER) as days_open
        FROM In_tbl i
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.Status = 'في طور العمل'
          AND i.Date_Com IS NOT NULL AND TRIM(i.Date_Com) != ''
          AND i.Date_Com <= ?
        ORDER BY i.Date_Com ASC
        LIMIT 40
    ''', (overdue_cutoff,)).fetchall()
    overdue_count = conn.execute('''
        SELECT COUNT(*) FROM In_tbl
        WHERE Status = 'في طور العمل'
          AND Date_Com IS NOT NULL AND TRIM(Date_Com) != ''
          AND Date_Com <= ?
    ''', (overdue_cutoff,)).fetchone()[0]

    # نشاط الشهر الحالي
    month_start = datetime.today().date().replace(day=1).isoformat()
    top_sources_month = conn.execute('''
        SELECT a.In_place as name, COUNT(i.NoBook_In) as cnt
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        WHERE i.Date_Com >= ?
        GROUP BY i.Add_In_ID
        ORDER BY cnt DESC
        LIMIT 8
    ''', (month_start,)).fetchall()
    top_deps_month = conn.execute('''
        SELECT d.Dep_Name as name, COUNT(i.NoBook_In) as cnt
        FROM In_tbl i
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.Date_Com >= ?
        GROUP BY i.Current_Dep_ID
        ORDER BY cnt DESC
        LIMIT 8
    ''', (month_start,)).fetchall()

    # إحصائيات شهرية لآخر 12 شهراً (رسم بياني)
    monthly_labels = []
    monthly_in = []
    monthly_out = []
    today = datetime.today().date()
    for i in range(11, -1, -1):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12
            y -= 1
        label = f'{y:04d}-{m:02d}'
        monthly_labels.append(label)
        start = f'{label}-01'
        if m == 12:
            end = f'{y + 1}-01-01'
        else:
            end = f'{y:04d}-{m + 1:02d}-01'
        monthly_in.append(conn.execute(
            "SELECT COUNT(*) FROM In_tbl WHERE Date_Com >= ? AND Date_Com < ?",
            (start, end),
        ).fetchone()[0])
        monthly_out.append(conn.execute(
            "SELECT COUNT(*) FROM Out_tbl WHERE Date_Out >= ? AND Date_Out < ?",
            (start, end),
        ).fetchone()[0])

    conn.close()

    chart_data = {
        'labels': monthly_labels,
        'incoming': monthly_in,
        'outgoing': monthly_out,
    }

    return render_template(
        'search.html',
        in_results=in_results, out_results=out_results,
        q=q, sources=sources, all_depts=all_depts,
        source_id=source_id, dep_id=dep_id, date_from=date_from,
        date_to=date_to, status=status, search_type=search_type,
        total_in=total_in, total_out=total_out,
        in_progress=in_progress, completed=completed, by_dep=by_dep,
        link_no=link_no, link_in_book=link_in_book, link_out_books=link_out_books,
        link_out_book=link_out_book, link_in_from_out=link_in_from_out,
        in_search_attach=in_search_attach, out_search_attach=out_search_attach,
        dep_no=dep_no, sort_by=sort_by, date_preset=date_preset,
        overdue_books=overdue_books, overdue_count=overdue_count,
        top_sources_month=top_sources_month, top_deps_month=top_deps_month,
        chart_data=chart_data,
        query_string=request.query_string.decode('utf-8', errors='ignore'),
    )


def _search_collect_for_export():
    """يعيد (in_rows, out_rows, meta) بنفس فلاتر /search للتصدير والطباعة."""
    # استدعاء منطق البحث عبر نفس المعاملات بدون إعادة كتابة كاملة —
    # نعيد استخدام search بتشغيل الاستعلامات مرة أخرى هنا بشكل مختصر.
    q           = request.args.get('q', '').strip()
    source_id   = request.args.get('source_id', '')
    dep_id      = request.args.get('dep_id', '')
    date_from   = request.args.get('date_from', '')
    date_to     = request.args.get('date_to', '')
    status      = request.args.get('status', '')
    search_type = request.args.get('search_type', 'both')
    if search_type not in ('in', 'out', 'both'):
        search_type = 'both'
    dep_no      = request.args.get('dep_no', '').strip()
    sort_by     = request.args.get('sort_by', 'newest').strip() or 'newest'
    date_preset = request.args.get('date_preset', '').strip()
    date_from, date_to, date_preset = _search_apply_date_preset(date_preset, date_from, date_to)

    conn = get_db()
    in_rows, out_rows = [], []
    want_in = search_type in ('in', 'both')
    want_out = search_type in ('out', 'both')

    if want_in:
        query = '''
            SELECT i.*, a.In_place as source_name, d.Dep_Name as dep_name
            FROM In_tbl i
            LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
            LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
            WHERE 1=1
        '''
        params = []
        if q:
            query += (
                " AND (i.NoBookCome_In LIKE ? OR i.Subject_Com LIKE ? "
                "OR CAST(i.NoBook_In AS TEXT) LIKE ? OR i.NoBook_Dep LIKE ?)"
            )
            params.extend([f'%{q}%'] * 4)
        if dep_no:
            query += " AND i.NoBook_Dep LIKE ?"
            params.append(f'%{dep_no}%')
        if source_id:
            query += " AND i.Add_In_ID = ?"
            params.append(source_id)
        if dep_id:
            query += " AND i.Current_Dep_ID = ?"
            params.append(dep_id)
        if date_from:
            query += " AND i.Date_Com >= ?"
            params.append(date_from)
        if date_to:
            query += " AND i.Date_Com <= ?"
            params.append(date_to)
        if status:
            query += " AND i.Status = ?"
            params.append(status)
        query += _search_order_sql('in', sort_by)
        in_rows = conn.execute(query, params).fetchall()

    if want_out:
        out_query = '''
            SELECT o.*, a.Out_place as dest_name,
                   i.NoBook_Dep AS reply_in_dep, i.Subject_Com AS reply_in_subject
            FROM Out_tbl o
            LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
            LEFT JOIN In_tbl i ON i.NoBook_In = o.Reply_To_InBook_No
            WHERE 1=1
        '''
        out_params = []
        if q:
            out_query += (
                " AND (o.Subject LIKE ? OR CAST(o.NoBook_Out AS TEXT) LIKE ? OR o.NoBook_Out_Manual LIKE ? "
                "OR CAST(o.Reply_To_InBook_No AS TEXT) LIKE ? OR i.NoBookCome_In LIKE ? OR i.NoBook_Dep LIKE ?)"
            )
            out_params.extend([f'%{q}%'] * 6)
        if dep_no:
            out_query += " AND o.NoBook_Out_Manual LIKE ?"
            out_params.append(f'%{dep_no}%')
        if date_from:
            out_query += " AND o.Date_Out >= ?"
            out_params.append(date_from)
        if date_to:
            out_query += " AND o.Date_Out <= ?"
            out_params.append(date_to)
        out_query += _search_order_sql('out', sort_by)
        out_rows = conn.execute(out_query, out_params).fetchall()

    conn.close()
    meta = {
        'q': q, 'dep_no': dep_no, 'search_type': search_type,
        'date_from': date_from, 'date_to': date_to, 'status': status,
        'sort_by': sort_by,
    }
    return in_rows, out_rows, meta


@app.route('/search/export')
@login_required
def search_export():
    """تصدير نتائج البحث الحالية كملف Excel (CSV بترميز مناسب)."""
    import csv
    import io
    in_rows, out_rows, meta = _search_collect_for_export()
    buf = io.StringIO()
    buf.write('\ufeff')  # BOM لـ Excel
    w = csv.writer(buf)
    w.writerow(['تقرير البحث — Y-ai InOut', datetime.now().strftime('%Y-%m-%d %H:%M')])
    w.writerow(['نوع البحث', meta['search_type'], 'نص', meta['q'], 'رقم دائرة', meta['dep_no']])
    w.writerow([])
    if meta['search_type'] in ('in', 'both'):
        w.writerow(['=== كتب واردة ==='])
        w.writerow(['رقم وارد الدائرة', 'رقم كتاب الجهة', 'التاريخ', 'الموضوع', 'القسم', 'الحالة', 'الجهة'])
        for b in in_rows:
            w.writerow([
                b['NoBook_Dep'] or '',
                b['NoBookCome_In'] or '',
                b['Date_Com'] or '',
                b['Subject_Com'] or '',
                b['dep_name'] or '',
                b['Status'] or '',
                b['source_name'] or '',
            ])
        w.writerow([])
    if meta['search_type'] in ('out', 'both'):
        w.writerow(['=== كتب صادرة ==='])
        w.writerow(['رقم صادر الدائرة', 'التاريخ', 'الموضوع', 'الجهة', 'وارد مرتبط'])
        for b in out_rows:
            w.writerow([
                b['NoBook_Out_Manual'] or '',
                b['Date_Out'] or '',
                b['Subject'] or '',
                b['dest_name'] or '',
                b['reply_in_dep'] or '',
            ])
    data = buf.getvalue().encode('utf-8')
    filename = f"Y-ai-search-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    resp = make_response(data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@app.route('/search/print')
@login_required
def search_print():
    """تقرير طباعة لنتائج البحث الحالية."""
    in_rows, out_rows, meta = _search_collect_for_export()
    org = None
    try:
        conn = get_db()
        org = conn.execute('SELECT * FROM organization_tbl LIMIT 1').fetchone()
        conn.close()
    except Exception:
        pass
    return render_template(
        'search_print.html',
        in_rows=in_rows,
        out_rows=out_rows,
        meta=meta,
        org=org,
        printed_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# بحث المرفقات (OCR) — قسم مستقل
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/ocr-search', methods=['GET', 'POST'])
@login_required
def ocr_search_page():
    if session.get('role') != 'مدير':
        return deny_non_admin_section('بحث المرفقات OCR للمدير فقط')
    import ocr_attachments
    ocr_attachments.init_ocr(DB_PATH, UPLOAD_FOLDER)
    caps = ocr_attachments.capability_status()
    stats = ocr_attachments.index_stats(DB_PATH)
    state = ocr_attachments.get_index_state()
    q = request.args.get('q', '').strip()
    results = []
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'index':
            force = bool(request.form.get('force'))
            if session.get('role') not in ('مدير', 'موظف قسم'):
                flash('فهرسة المرفقات للمدير أو موظف القسم', 'warning')
            else:
                res = ocr_attachments.start_index_async(force=force)
                flash(res.get('message', ''), 'success' if res.get('ok') else 'warning')
            return redirect(url_for('ocr_search_page', q=q) if q else url_for('ocr_search_page'))
        q = request.form.get('q', '').strip()
        return redirect(url_for('ocr_search_page', q=q))

    if q:
        results = ocr_attachments.search_attachments(DB_PATH, q)

    return render_template(
        'ocr_search.html',
        q=q,
        results=results,
        caps=caps,
        stats=stats,
        state=state,
        can_index=session.get('role') == 'مدير',
    )


@app.route('/api/ocr-index-status')
@login_required
def api_ocr_index_status():
    import ocr_attachments
    ocr_attachments.init_ocr(DB_PATH, UPLOAD_FOLDER)
    return jsonify({
        'state': ocr_attachments.get_index_state(),
        'stats': ocr_attachments.index_stats(DB_PATH),
        'caps': ocr_attachments.capability_status(),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# تنبيهات الأقسام — كتب جديدة لم تُفتح
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/dept-alerts')
@login_required
def api_dept_alerts():
    """كتب لم تُفتح: موظف القسم لقسمه، والمدير لإرجاع الكتب إلى الادارة."""
    role = session.get('role')
    conn = get_db()
    mode = 'dept'
    if role == 'موظف قسم' and session.get('dep_id'):
        rows = list_unread_dept_books(conn, session.get('dep_id'))
    elif role == 'مدير':
        admin_dep_id = ensure_admin_department(conn)
        rows = list_unread_admin_return_books(conn, admin_dep_id)
        mode = 'admin'
    else:
        conn.close()
        return jsonify({'enabled': False, 'count': 0, 'books': []})
    conn.close()
    books = []
    for r in rows:
        books.append({
            'id': r['NoBook_In'],
            'no': r['NoBook_Dep'] or r['NoBookCome_In'] or r['NoBook_In'],
            'subject': r['Subject_Com'] or '—',
            'date': r['Date_Com'] or '',
            'url': url_for('incoming_view', id=r['NoBook_In']),
        })
    return jsonify({
        'enabled': True,
        'mode': mode,
        'count': len(books),
        'books': books,
        'sound_interval_sec': 300,
        'poll_interval_sec': 60,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOG — admin only
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/admin/activity-log')
@admin_required
def activity_log_page():
    from activity_log import search_activity
    q = request.args.get('q', '').strip()
    action_filter = request.args.get('action', '').strip()
    rows = search_activity(DB_PATH, q=q, action=action_filter)
    return render_template(
        'activity_log.html',
        rows=rows,
        q=q,
        action_filter=action_filter,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BACKUP / RESTORE / ANNUAL RESET — admin only
# ═══════════════════════════════════════════════════════════════════════════════

from backup_admin import backup_bp, init_backup_admin

init_backup_admin(BASE_DIR, DB_PATH, UPLOAD_FOLDER)
app.register_blueprint(backup_bp)

from github_update import github_bp, init_github_update

init_github_update(BASE_DIR)
app.register_blueprint(github_bp)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════

from y_ai import y_ai_bp, init_y_ai

init_y_ai(get_db)
app.register_blueprint(y_ai_bp)


@app.route('/ping')
def ping():
    """فحص اتصال الشبكة من أجهزة الأقسام."""
    return jsonify({'ok': True, 'service': 'Y-ai InOut'})


if __name__ == '__main__':
    from run_server import main
    main()
