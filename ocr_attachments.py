"""بحث داخل المرفقات (نصي + OCR للممسوحات) — محلي بدون إنترنت بعد التثبيت.

يعتمد اختيارياً على:
- pymupdf (fitz): قراءة PDF ورسم الصفحات
- pytesseract + برنامج Tesseract مع ara ويفضّل fas (أو script/Arabic)
  للأحرف الكوردية المتصلة: پ چ ڤ گ ژ
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Optional

_TESS_CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tesseract_sorani.cfg')
_ocr_lang_cached: Optional[str] = None
_ocr_lang_candidates_cached: Optional[list[str]] = None
_tess_ok_cached: Optional[bool] = None
_caps_cache: Optional[dict[str, Any]] = None
_caps_cache_at: float = 0.0
_tess_hidden_patched = False
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
OCR_PROFILE = 'v4-kurdish-fold'
_OCR_WORKERS = max(1, min(3, (os.cpu_count() or 2)))
_OCR_MAX_PAGES = 16
_OCR_RENDER_SCALE = 1.85
_OCR_MAX_EDGE = 1600
_SORANI_LETTERS = 'پچڤگژ'
# بدائل شائعة: سوراني ↔ أقرب حرف عربي/فارسي بعد OCR
_FOLD_TABLE = str.maketrans({
    'پ': 'ب', 'چ': 'ج', 'ڤ': 'ف', 'گ': 'ك', 'ک': 'ك',
    'ژ': 'ز', 'ۆ': 'و', 'ێ': 'ي', 'ی': 'ي', 'ى': 'ي',
    'ە': 'ه', 'ة': 'ه', 'ڵ': 'ل', 'ڕ': 'ر',
    'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا',
    'ؤ': 'و', 'ئ': 'ي',
})
_HARAKAT_RE = re.compile(r'[\u064B-\u065F\u0670\u06D6-\u06ED]')
_DIGIT_TABLE = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
# تطويل، شرطات، كل أنواع الفراغات — ئاگـــادارى و ژیا  ن تُعاملان ككلمة واحدة
_GAP_RE = re.compile(
    r'[\s_\-\u0640\u00AD\u00A0\u1680\u180E\u2000-\u200B\u2028\u2029\u202F\u205F\u3000'
    r'\u2010-\u2015\u2212\u2E3A\u2E3B\uFE31\uFE32\uFE58\uFE63\uFF0D]+',
)

_lock = threading.Lock()
_index_state = {
    'running': False,
    'done': 0,
    'total': 0,
    'current': '',
    'message': '',
    'started_at': '',
    'finished_at': '',
    'ocr_available': False,
    'pymupdf_available': False,
}

_UPLOAD_FOLDER = ''
_DB_PATH = ''
_write_lock = threading.Lock()


def _is_tesseract_cmd(cmd: Any) -> bool:
    if isinstance(cmd, (list, tuple)) and cmd:
        return 'tesseract' in os.path.basename(str(cmd[0])).lower()
    if isinstance(cmd, str):
        return 'tesseract' in cmd.lower()
    return False


def _hide_win_console(kwargs: dict) -> dict:
    if os.name != 'nt':
        return kwargs
    out = dict(kwargs)
    out['creationflags'] = out.get('creationflags', 0) | _CREATE_NO_WINDOW
    if hasattr(subprocess, 'STARTUPINFO'):
        si = out.get('startupinfo') or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        out['startupinfo'] = si
    return out


def _patch_tesseract_no_console() -> None:
    """منع ظهور نافذة CMD عند كل استدعاء لـ tesseract.exe."""
    global _tess_hidden_patched
    if _tess_hidden_patched:
        return
    _tess_hidden_patched = True
    if os.name != 'nt':
        return
    orig_run = subprocess.run
    orig_check = subprocess.check_output
    orig_popen = subprocess.Popen

    def run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        if _is_tesseract_cmd(cmd):
            kwargs = _hide_win_console(kwargs)
        return orig_run(*args, **kwargs)

    def check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args')
        if _is_tesseract_cmd(cmd):
            kwargs = _hide_win_console(kwargs)
        return orig_check(*args, **kwargs)

    class Popen(orig_popen):
        def __init__(self, *args, **kwargs):
            cmd = args[0] if args else kwargs.get('args')
            if _is_tesseract_cmd(cmd):
                kwargs = _hide_win_console(kwargs)
            super().__init__(*args, **kwargs)

    subprocess.run = run
    subprocess.check_output = check_output
    subprocess.Popen = Popen
    try:
        import pytesseract.pytesseract as pt
        orig_sa = pt.subprocess_args

        def subprocess_args(*a, **k):
            return _hide_win_console(orig_sa(*a, **k))

        pt.subprocess_args = subprocess_args
    except Exception:
        pass


def init_ocr(db_path: str, upload_folder: str) -> None:
    global _DB_PATH, _UPLOAD_FOLDER, _ocr_lang_cached, _ocr_lang_candidates_cached
    global _tess_ok_cached, _caps_cache, _caps_cache_at
    _DB_PATH = db_path
    _UPLOAD_FOLDER = upload_folder
    _ocr_lang_cached = None
    _ocr_lang_candidates_cached = None
    _tess_ok_cached = None
    _caps_cache = None
    _caps_cache_at = 0.0
    _patch_tesseract_no_console()
    ensure_ocr_table(db_path)
    st = capability_status()
    _index_state['ocr_available'] = st['tesseract']
    _index_state['pymupdf_available'] = st['pymupdf']


def _configure_tesseract_cmd() -> str:
    """تعيين مسار tesseract.exe على ويندوز إن لم يكن في PATH."""
    _patch_tesseract_no_console()
    try:
        import pytesseract
    except Exception:
        return ''

    current = getattr(pytesseract.pytesseract, 'tesseract_cmd', None) or 'tesseract'
    # إن وُجد مسبقاً ومسار صالح
    if current and current != 'tesseract' and os.path.isfile(current):
        return current

    # مسارات شائعة لنسخ UB-Mannheim / تثبيت المستخدم
    local = os.environ.get('LOCALAPPDATA', '')
    user_profile = os.environ.get('USERPROFILE', '')
    candidates = [
        os.environ.get('TESSERACT_CMD', ''),
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        os.path.join(local, 'Programs', 'Tesseract-OCR', 'tesseract.exe') if local else '',
        os.path.join(local, 'Tesseract-OCR', 'tesseract.exe') if local else '',
        os.path.join(user_profile, 'AppData', 'Local', 'Programs', 'Tesseract-OCR', 'tesseract.exe')
        if user_profile else '',
    ]

    # سجل ويندوز إن وُجد
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (
                r'SOFTWARE\Tesseract-OCR',
                r'SOFTWARE\WOW6432Node\Tesseract-OCR',
            ):
                try:
                    with winreg.OpenKey(root, sub) as key:
                        for name in ('Path', 'InstallDir', 'InstallPath'):
                            try:
                                val, _ = winreg.QueryValueEx(key, name)
                                if val:
                                    exe = os.path.join(str(val), 'tesseract.exe')
                                    candidates.append(exe)
                                    candidates.append(str(val) if str(val).lower().endswith('.exe') else '')
                            except OSError:
                                pass
                except OSError:
                    pass
    except Exception:
        pass

    seen = set()
    for path in candidates:
        if not path:
            continue
        path = os.path.normpath(path)
        if path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return path

    # آخر محاولة: which
    try:
        import shutil
        found = shutil.which('tesseract')
        if found and os.path.isfile(found):
            pytesseract.pytesseract.tesseract_cmd = found
            return found
    except Exception:
        pass

    return current or 'tesseract'


def _tessdata_dirs() -> list[str]:
    dirs: list[str] = []
    cmd = _configure_tesseract_cmd()
    if cmd and os.path.isfile(cmd):
        dirs.append(os.path.join(os.path.dirname(cmd), 'tessdata'))
    prefix = (os.environ.get('TESSDATA_PREFIX') or '').strip()
    if prefix:
        dirs.append(prefix)
        dirs.append(os.path.join(prefix, 'tessdata'))
    out = []
    seen = set()
    for d in dirs:
        n = os.path.normpath(d)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _traineddata_stems() -> set[str]:
    stems: set[str] = set()
    for d in _tessdata_dirs():
        if not os.path.isdir(d):
            continue
        try:
            for name in os.listdir(d):
                low = name.lower()
                if low.endswith('.traineddata'):
                    stems.add(low[: -len('.traineddata')])
        except OSError:
            pass
    return stems


def _installed_tess_langs() -> list[str]:
    try:
        import pytesseract
        _configure_tesseract_cmd()
        return list(pytesseract.get_languages(config='')) or []
    except Exception:
        return []


def _available_lang_map() -> dict[str, str]:
    """الاسم كما يعرفه Tesseract (من --list-langs أو ملفات tessdata)."""
    raw = _installed_tess_langs()
    lower_map = {x.lower(): x for x in raw}
    for stem in _traineddata_stems():
        lower_map.setdefault(stem, stem)
    return lower_map


def _persian_lang_code() -> Optional[str]:
    """fas هو الكود الرسمي. per/far إن وُجد ملف traineddata."""
    m = _available_lang_map()
    for key in ('fas', 'per', 'far', 'farsi', 'persian'):
        if key in m:
            return m[key]
    return None


def _ocr_lang() -> str:
    """fas (أو per/far) + ara. لا ندمج script/Arabic إلا إذا وُجد فعلاً — الدمج الخاطئ يوقف Tesseract."""
    global _ocr_lang_cached
    if _ocr_lang_cached:
        return _ocr_lang_cached
    m = _available_lang_map()
    parts: list[str] = []
    pers = _persian_lang_code()
    if pers:
        parts.append(pers)
    if 'ara' in m:
        parts.append(m['ara'])
    _ocr_lang_cached = '+'.join(parts) if parts else 'ara'
    return _ocr_lang_cached


def _ocr_lang_candidates() -> list[str]:
    """لغة واحدة مركّبة للسرعة (fas+ara إن وُجدت)."""
    global _ocr_lang_candidates_cached
    if _ocr_lang_candidates_cached:
        return _ocr_lang_candidates_cached
    primary = _ocr_lang()
    _ocr_lang_candidates_cached = [primary]
    return _ocr_lang_candidates_cached


def _ocr_tess_config(psm: str, extra: bool = True) -> str:
    """LSTM فقط (أسرع من oem 3) + PSM."""
    bits = ['--oem 1', f'--psm {psm}']
    if extra:
        bits.append('-c preserve_interword_spaces=1')
    return ' '.join(bits)


def _search_query_variants(q: str) -> list[str]:
    q = (q or '').strip()
    if not q:
        return []
    folded = _fold_for_search(q)
    out = [q]
    if folded and folded not in out:
        out.append(folded)
    return out


def _fold_for_search(s: str) -> str:
    """طي الحروف + حذف التطويل والفراغات كلها حتى ئاگـــادارى = ئاگادارى و ژیان = ژیا  ن."""
    s = unicodedata.normalize('NFKC', s or '')
    s = s.translate(_DIGIT_TABLE)
    for ch in ('\u200c', '\u200d', '\u200e', '\u200f', '\ufeff'):
        s = s.replace(ch, '')
    s = _HARAKAT_RE.sub('', s)
    s = s.translate(_FOLD_TABLE)
    return _GAP_RE.sub('', s)


def _norm_ocr_text(s: str) -> str:
    return _fold_for_search(s)


def _compact_letters(s: str) -> str:
    return _GAP_RE.sub('', s or '')


def _text_contains_query(text: str, query: str) -> bool:
    hay = _fold_for_search(text)
    needle = _fold_for_search(query)
    return bool(needle and hay and needle in hay)


def _count_query_hits(text: str, query: str) -> int:
    hay = _fold_for_search(text)
    needle = _fold_for_search(query)
    if not hay or not needle:
        return 0
    return hay.count(needle)


def capability_status() -> dict[str, Any]:
    global _caps_cache, _caps_cache_at
    now = time.time()
    if _caps_cache is not None and (now - _caps_cache_at) < 90:
        return _caps_cache
    info = {
        'pymupdf': False,
        'tesseract': False,
        'pillow': False,
        'tesseract_cmd': '',
        'langs': [],
        'message': '',
    }
    try:
        import fitz  # noqa: F401
        info['pymupdf'] = True
    except Exception:
        pass
    try:
        from PIL import Image  # noqa: F401
        info['pillow'] = True
    except Exception:
        pass
    try:
        import pytesseract
        cmd = _configure_tesseract_cmd()
        ver = pytesseract.get_tesseract_version()
        info['tesseract'] = True
        info['tesseract_cmd'] = str(cmd)
        info['tesseract_version'] = str(ver)
        try:
            info['langs'] = list(pytesseract.get_languages(config=''))
        except Exception:
            info['langs'] = []
        langs_l = [x.lower() for x in info['langs']]
        stems = _traineddata_stems()
        info['ocr_lang'] = _ocr_lang()
        pers = _persian_lang_code()
        if 'ara' not in langs_l and 'ara' not in stems and not pers:
            info['message'] = (
                'Tesseract موجود لكن لا توجد ara ولا fas. '
                'ثبّت Arabic و Persian/Farsi (الملف fas.traineddata وليس far).'
            )
            _caps_cache = info
            _caps_cache_at = now
            return info
        if not pers:
            info['persian_missing'] = True
    except Exception as e:
        info['tesseract_error'] = str(e)[:200]

    if not info['pymupdf']:
        info['message'] = 'ثبّت المكتبة: pip install pymupdf'
    elif not info['tesseract']:
        info['message'] = (
            'البحث في PDF النصي يعمل. للممسوحات ثبّت Tesseract OCR '
            'مع لغة العربية (ara) و Persian (fas) ثم: pip install pytesseract Pillow.'
        )
    elif info.get('persian_missing'):
        info['message'] = (
            'Tesseract يعمل بدون fas.traineddata. ثبّت Persian (كود fas وليس far) '
            'من مثبّت Tesseract، أعد تشغيل البرنامج، ثم «إعادة فهرسة كاملة».'
        )
    else:
        info['message'] = (
            f'جاهز. لغة OCR: {info.get("ocr_lang") or _ocr_lang()}. '
            'للبحث داخل المرفق اضغط «إعادة فهرسة كاملة» ثم ابحث هنا.'
        )
    _caps_cache = info
    _caps_cache_at = now
    return info


def ensure_ocr_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Attachment_Ocr_Index (
                Rel_Path TEXT PRIMARY KEY,
                Kind TEXT,
                Book_ID INTEGER,
                File_MTime REAL,
                File_Size INTEGER,
                Text_Content TEXT,
                Method TEXT,
                Indexed_At TEXT,
                Error TEXT
            )
            """
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_ocr_text ON Attachment_Ocr_Index(Text_Content)'
        )
        try:
            conn.execute('ALTER TABLE Attachment_Ocr_Index ADD COLUMN Ocr_Profile TEXT')
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def get_index_state() -> dict[str, Any]:
    with _lock:
        return dict(_index_state)


def _set_state(**kwargs) -> None:
    with _lock:
        _index_state.update(kwargs)


def _list_attachment_files(upload_folder: str) -> list[dict[str, Any]]:
    files = []
    for kind in ('in', 'out'):
        root = os.path.join(upload_folder, kind)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
                if ext not in ('pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp'):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, upload_folder).replace('\\', '/')
                # uploads/in/12/file.pdf → book_id = 12
                parts = rel.split('/')
                book_id = None
                if len(parts) >= 2 and parts[1].isdigit():
                    book_id = int(parts[1])
                files.append({
                    'rel_path': rel,
                    'full_path': full,
                    'kind': kind,
                    'book_id': book_id,
                    'mtime': st.st_mtime,
                    'size': st.st_size,
                })
    return files


def _ocr_fix_orientation(img):
    """تدوير خفيف بدون OSD (OSD يفتح Tesseract إضافياً ويبطئ الفهرسة)."""
    try:
        w, h = img.size
        if w > h * 1.25:
            return img.rotate(90, expand=True)
    except Exception:
        pass
    return img


def _ocr_preprocess(img, binarize: bool = True):
    """اتجاه ثم تكبير ثم تباين، واختياري أبيض/أسود للنقاط الثلاثية."""
    from PIL import Image, ImageOps

    _configure_tesseract_cmd()
    if img.mode not in ('L', 'RGB', 'RGBA'):
        img = img.convert('RGB')
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg

    img = _ocr_fix_orientation(img)

    gray = ImageOps.grayscale(img)
    w, h = gray.size
    longest = max(w, h) or 1
    if longest > _OCR_MAX_EDGE:
        scale = _OCR_MAX_EDGE / longest
        gray = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)
    elif longest < 1100:
        scale = min(1.5, 1100 / longest)
        gray = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    if not binarize:
        return gray
    bw = gray.point(lambda p: 255 if p > 172 else 0)
    return bw.convert('L')


def _ocr_score(txt: str) -> int:
    arabic = sum(1 for ch in txt if '\u0600' <= ch <= '\u06FF')
    sorani = sum(1 for ch in txt if ch in _SORANI_LETTERS)
    return arabic * 2 + sorani * 12 + len(txt)


def _ocr_image(img) -> str:
    """تمريرة Tesseract واحدة لكل صفحة."""
    import pytesseract
    _configure_tesseract_cmd()
    use_lang = _ocr_lang()
    gray = _ocr_preprocess(img, binarize=False)
    try:
        return (pytesseract.image_to_string(
            gray, lang=use_lang, config=_ocr_tess_config('6', extra=True),
            timeout=45,
        ) or '').strip()
    except Exception:
        return ''


def _page_image_for_ocr(page):
    """رسم الصفحة مباشرة إلى صورة (بدون PNG وسيط)."""
    import fitz
    from PIL import Image

    pix = page.get_pixmap(matrix=fitz.Matrix(_OCR_RENDER_SCALE, _OCR_RENDER_SCALE), alpha=False)
    return Image.frombytes('RGB', [pix.width, pix.height], pix.samples)


def _extract_text_pymupdf(full_path: str, use_ocr: bool) -> tuple[str, str]:
    """يعيد (نص, method)."""
    import fitz

    doc = fitz.open(full_path)
    chunks = []
    method = 'text'
    ocr_pages = 0
    try:
        for page in doc:
            text = (page.get_text('text') or '').strip()
            arabic_text = sum(1 for ch in text if '\u0600' <= ch <= '\u06FF')
            sorani_text = sum(1 for ch in text if ch in _SORANI_LETTERS)
            has_img = False
            try:
                has_img = bool(page.get_images())
            except Exception:
                has_img = True
            need_ocr = bool(use_ocr) and ocr_pages < _OCR_MAX_PAGES and (
                len(text) < 80
                or arabic_text < 15
                or (has_img and sorani_text == 0 and arabic_text < 40)
            )
            if need_ocr:
                try:
                    img = _page_image_for_ocr(page)
                    ocr_txt = _ocr_image(img)
                    ocr_pages += 1
                    parts = []
                    if text:
                        parts.append(text)
                    if ocr_txt:
                        parts.append(ocr_txt)
                        method = 'ocr'
                    if parts:
                        chunks.append('\n'.join(parts))
                except Exception:
                    if text:
                        chunks.append(text)
            elif text:
                chunks.append(text)
    finally:
        doc.close()
    return '\n'.join(chunks).strip(), method


def _extract_text_image(full_path: str) -> tuple[str, str]:
    from PIL import Image
    img = Image.open(full_path)
    txt = _ocr_image(img)
    return txt.strip(), 'ocr'


def _tesseract_ready() -> bool:
    global _tess_ok_cached
    if _tess_ok_cached is not None:
        return _tess_ok_cached
    try:
        _configure_tesseract_cmd()
        import pytesseract
        pytesseract.get_tesseract_version()
        _tess_ok_cached = True
    except Exception:
        _tess_ok_cached = False
    return bool(_tess_ok_cached)


def extract_file_text(full_path: str) -> tuple[str, str, str]:
    """(text, method, error)."""
    ext = full_path.rsplit('.', 1)[-1].lower()
    try:
        if ext == 'pdf':
            try:
                import fitz  # noqa: F401
            except Exception:
                return '', '', 'pymupdf غير مثبّت'
            text, method = _extract_text_pymupdf(full_path, use_ocr=_tesseract_ready())
            return text, method, ''
        if ext in ('png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp'):
            if not _tesseract_ready():
                return '', '', 'Tesseract غير مثبّت لصور المرفقات'
            text, method = _extract_text_image(full_path)
            return text, method, ''
        return '', '', 'صيغة غير مدعومة'
    except Exception as e:
        return '', '', str(e)[:300]


def _index_row_is_fresh(row, item: dict[str, Any], force: bool) -> bool:
    if force or not row:
        return False
    try:
        old_profile = row['Ocr_Profile']
    except (KeyError, IndexError, TypeError):
        old_profile = row[2] if len(row) > 2 else None
    try:
        old_text = (row['Text_Content'] or '').strip()
    except (KeyError, IndexError, TypeError):
        old_text = ''
    same_file = abs((row[0] or 0) - item['mtime']) < 0.01 and (row[1] or 0) == item['size']
    return bool(same_file and old_profile == OCR_PROFILE and old_text)


def _fetch_index_row(conn: sqlite3.Connection, rel_path: str):
    try:
        return conn.execute(
            'SELECT File_MTime, File_Size, Ocr_Profile, Text_Content FROM Attachment_Ocr_Index WHERE Rel_Path=?',
            (rel_path,),
        ).fetchone()
    except sqlite3.OperationalError:
        return conn.execute(
            'SELECT File_MTime, File_Size FROM Attachment_Ocr_Index WHERE Rel_Path=?',
            (rel_path,),
        ).fetchone()


def _write_index_row(conn: sqlite3.Connection, item: dict[str, Any], text: str, method: str, err: str) -> None:
    vals = (
        item['rel_path'],
        item['kind'],
        item['book_id'],
        item['mtime'],
        item['size'],
        text or '',
        method or '',
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        err or '',
        OCR_PROFILE,
    )
    try:
        conn.execute(
            '''INSERT OR REPLACE INTO Attachment_Ocr_Index
               (Rel_Path, Kind, Book_ID, File_MTime, File_Size, Text_Content, Method, Indexed_At, Error, Ocr_Profile)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            vals,
        )
    except sqlite3.OperationalError:
        conn.execute(
            '''INSERT OR REPLACE INTO Attachment_Ocr_Index
               (Rel_Path, Kind, Book_ID, File_MTime, File_Size, Text_Content, Method, Indexed_At, Error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            vals[:-1],
        )
    conn.commit()


def index_one_file(conn: sqlite3.Connection, item: dict[str, Any], force: bool = False) -> str:
    row = _fetch_index_row(conn, item['rel_path'])
    if _index_row_is_fresh(row, item, force):
        return 'skip'
    text, method, err = extract_file_text(item['full_path'])
    _write_index_row(conn, item, text, method, err)
    return 'ocr' if method == 'ocr' else ('ok' if text else 'empty')


def run_index(force: bool = False, limit: Optional[int] = None) -> None:
    if _index_state.get('running'):
        return
    _patch_tesseract_no_console()
    files = _list_attachment_files(_UPLOAD_FOLDER)
    if limit:
        files = files[: int(limit)]
    _set_state(
        running=True,
        done=0,
        total=len(files),
        current='',
        message='جاري الفهرسة...',
        started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        finished_at='',
    )
    conn = sqlite3.connect(_DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        todo = []
        skipped = 0
        for item in files:
            row = _fetch_index_row(conn, item['rel_path'])
            if _index_row_is_fresh(row, item, force):
                skipped += 1
            else:
                todo.append(item)
        _set_state(done=skipped)

        def _ocr_job(item: dict[str, Any]):
            return item, extract_file_text(item['full_path'])

        workers = 1 if len(todo) < 2 else _OCR_WORKERS
        if todo and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_ocr_job, item) for item in todo]
                done_n = skipped
                for fut in as_completed(futs):
                    item = {'rel_path': ''}
                    try:
                        item, (text, method, err) = fut.result()
                        _set_state(current=item['rel_path'])
                        with _write_lock:
                            _write_index_row(conn, item, text, method, err)
                    except Exception as e:
                        try:
                            with _write_lock:
                                conn.execute(
                                    '''INSERT OR REPLACE INTO Attachment_Ocr_Index
                                       (Rel_Path, Kind, Book_ID, File_MTime, File_Size, Text_Content, Method, Indexed_At, Error)
                                       VALUES (?, ?, ?, ?, ?, '', '', ?, ?)''',
                                    (
                                        item.get('rel_path', ''), item.get('kind'), item.get('book_id'),
                                        item.get('mtime'), item.get('size'),
                                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        str(e)[:300],
                                    ),
                                )
                                conn.commit()
                        except Exception:
                            pass
                    done_n += 1
                    _set_state(done=done_n)
        else:
            for i, item in enumerate(todo, start=1):
                _set_state(done=skipped + i - 1, current=item['rel_path'])
                try:
                    index_one_file(conn, item, force=True)
                except Exception as e:
                    try:
                        conn.execute(
                            '''INSERT OR REPLACE INTO Attachment_Ocr_Index
                               (Rel_Path, Kind, Book_ID, File_MTime, File_Size, Text_Content, Method, Indexed_At, Error)
                               VALUES (?, ?, ?, ?, ?, '', '', ?, ?)''',
                            (
                                item['rel_path'], item['kind'], item['book_id'],
                                item['mtime'], item['size'],
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                str(e)[:300],
                            ),
                        )
                        conn.commit()
                    except Exception:
                        pass
                _set_state(done=skipped + i)
        _set_state(message=f'اكتملت فهرسة {len(files)} ملف')
    finally:
        conn.close()
        _set_state(
            running=False,
            current='',
            finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )


def start_index_async(force: bool = False) -> dict[str, Any]:
    if _index_state.get('running'):
        return {'ok': False, 'message': 'الفهرسة قيد التشغيل حالياً'}
    _patch_tesseract_no_console()
    t = threading.Thread(
        target=run_index,
        kwargs={'force': force},
        name='yai-ocr-index',
        daemon=True,
    )
    t.start()
    return {'ok': True, 'message': 'بدأت فهرسة المرفقات في الخلفية'}


def index_stats(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        total = conn.execute('SELECT COUNT(*) FROM Attachment_Ocr_Index').fetchone()[0]
        with_text = conn.execute(
            "SELECT COUNT(*) FROM Attachment_Ocr_Index WHERE IFNULL(Text_Content,'') != ''"
        ).fetchone()[0]
        with_err = conn.execute(
            "SELECT COUNT(*) FROM Attachment_Ocr_Index WHERE IFNULL(Error,'') != ''"
        ).fetchone()[0]
        return {'total': total, 'with_text': with_text, 'with_error': with_err}
    except Exception:
        return {'total': 0, 'with_text': 0, 'with_error': 0}
    finally:
        conn.close()


def _snippet(text: str, query: str, radius: int = 70) -> str:
    if not text:
        return ''
    hay_orig = text
    hay_norm = _norm_ocr_text(text)
    idx = -1
    needle_len = 0
    for v in _search_query_variants(query):
        n = _norm_ocr_text(v)
        if n:
            idx = hay_norm.find(n)
            if idx >= 0:
                needle_len = len(n)
                break
    if idx < 0:
        low = text.lower()
        q = (query or '').lower()
        idx2 = low.find(q)
        if idx2 < 0:
            for line in text.splitlines():
                if line.strip():
                    return line.strip()[:160]
            return text[:160]
        start = max(0, idx2 - radius)
        end = min(len(text), idx2 + len(query) + radius)
        snip = hay_orig[start:end].replace('\n', ' ').strip()
        if start > 0:
            snip = '…' + snip
        if end < len(text):
            snip = snip + '…'
        return snip
    start = max(0, idx - radius)
    end = min(len(hay_norm), idx + needle_len + radius)
    snip = hay_norm[start:end].strip()
    if start > 0:
        snip = '…' + snip
    if end < len(hay_norm):
        snip = snip + '…'
    return snip


def search_attachments(db_path: str, query: str, limit: int = 80) -> list[dict[str, Any]]:
    """بحث كلمة أو عبارة داخل نص المرفق المفهرس (وليس موضوع الكتاب)."""
    q = (query or '').strip()
    if not q:
        return []
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''SELECT Rel_Path, Kind, Book_ID, Text_Content, Method, Indexed_At, Error
               FROM Attachment_Ocr_Index
               WHERE IFNULL(Text_Content,'') != ''
               ORDER BY Indexed_At DESC''',
        ).fetchall()

        grouped: dict[tuple[str, Any], dict[str, Any]] = {}
        order: list[tuple[str, Any]] = []

        for r in rows:
            text_content = r['Text_Content'] or ''
            if not _text_contains_query(text_content, q):
                continue

            kind = (r['Kind'] or '').strip()
            book_id = r['Book_ID']
            if not book_id:
                key = (kind or '?', r['Rel_Path'])
            else:
                key = (kind, int(book_id))

            rel = r['Rel_Path'] or ''
            attach_name = os.path.basename(rel.replace('\\', '/')) or rel
            hits_in_file = _count_query_hits(text_content, q)
            snip = _snippet(text_content, q)

            if key not in grouped:
                book_title = ''
                book_no = ''
                book_url = ''
                party_name = ''
                book_date = ''

                if book_id and kind == 'in':
                    b = conn.execute(
                        '''SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com, i.Date_Com,
                                  a.In_place AS party_name
                           FROM In_tbl i
                           LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
                           WHERE i.NoBook_In=?''',
                        (book_id,),
                    ).fetchone()
                    if b:
                        book_no = b['NoBook_Dep'] or b['NoBookCome_In'] or b['NoBook_In']
                        book_title = b['Subject_Com'] or ''
                        book_date = b['Date_Com'] or ''
                        party_name = b['party_name'] or ''
                        book_url = f'/incoming/{book_id}'
                elif book_id and kind == 'out':
                    b = conn.execute(
                        '''SELECT o.NoBook_Out, o.NoBook_Out_Manual, o.Subject, o.Date_Out,
                                  a.Out_place AS party_name
                           FROM Out_tbl o
                           LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
                           WHERE o.NoBook_Out=?''',
                        (book_id,),
                    ).fetchone()
                    if b:
                        book_no = b['NoBook_Out_Manual'] or b['NoBook_Out']
                        book_title = b['Subject'] or ''
                        book_date = b['Date_Out'] or ''
                        party_name = b['party_name'] or ''
                        book_url = f'/outgoing/{book_id}'

                grouped[key] = {
                    'rel_path': rel,
                    'kind': kind,
                    'kind_label': 'وارد' if kind == 'in' else ('صادر' if kind == 'out' else kind),
                    'book_id': book_id,
                    'book_no': book_no,
                    'book_title': book_title,
                    'book_date': book_date,
                    'party_name': party_name,
                    'party_label': 'الجهة الواردة' if kind == 'in' else 'الجهة الصادرة',
                    'attachment_name': attach_name,
                    'attachment_names': [attach_name] if attach_name else [],
                    'attachments_count': 1,
                    'match_count': hits_in_file or 1,
                    'book_url': book_url,
                    'method': r['Method'] or '',
                    'snippet': snip,
                    'indexed_at': r['Indexed_At'] or '',
                }
                order.append(key)
            else:
                item = grouped[key]
                item['match_count'] = (item.get('match_count') or 0) + (hits_in_file or 1)
                item['attachments_count'] = (item.get('attachments_count') or 1) + 1
                names = item.setdefault('attachment_names', [])
                if attach_name and attach_name not in names:
                    names.append(attach_name)
                if len(names) > 1:
                    item['attachment_name'] = f'{names[0]} (+{len(names) - 1})'

        return [grouped[k] for k in order[: int(limit)]]
    finally:
        conn.close()
