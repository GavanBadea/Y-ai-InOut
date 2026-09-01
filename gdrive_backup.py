"""نسخ احتياطي يومي عبر مجلد Google Drive لسطح المكتب — للمدير فقط.

الآلية:
1) يثبّت المستخدم تطبيق Google Drive على الجهاز ويسجّل دخوله بالجيميل.
2) يحدد في البرنامج مسار مجلد المزامنة + الإيميل + تفعيل كل 24 ساعة.
3) البرنامج ينشئ ZIP للمشروع كاملاً داخل ذلك المجلد.
4) يحذف أي نسخة سابقة بنفس النمط من نفس المجلد؛ تطبيق Drive يزامن الحذف إلى السحابة تلقائياً.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime
from typing import Any

CONFIG_NAME = 'gdrive_backup_config.json'
BACKUP_PREFIX = 'Y-ai-InOut-Drive-'
BACKUP_FIXED = 'Y-ai-InOut-Drive-Full.zip'
SKIP_DIR_NAMES = frozenset({
    '__pycache__', '.git', '.cursor', 'backups_gdrive',
    'الى العميل', 'dist', 'build', 'node_modules',
})

_lock = threading.Lock()
_scheduler_started = False
_base_dir = ''
_db_path = ''
_upload_folder = ''


def init_gdrive(base_dir: str, db_path: str, upload_folder: str) -> None:
    global _base_dir, _db_path, _upload_folder
    _base_dir = base_dir
    _db_path = db_path
    _upload_folder = upload_folder


def _config_path() -> str:
    return os.path.join(_base_dir, CONFIG_NAME)


def default_config() -> dict[str, Any]:
    return {
        'enabled': False,
        'email': '',
        'drive_folder': '',
        'hour': 2,
        'last_run_date': '',
        'last_run_at': '',
        'last_status': '',
        'last_file': '',
        'last_error': '',
        'last_deleted': '',
    }


def load_config() -> dict[str, Any]:
    cfg = default_config()
    path = _config_path()
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, json.JSONDecodeError):
        pass
    # توافق مع الإعدادات القديمة إن وُجدت
    if not cfg.get('email') and cfg.get('account_email'):
        cfg['email'] = cfg.get('account_email') or ''
    try:
        cfg['hour'] = max(0, min(23, int(cfg.get('hour', 2))))
    except (TypeError, ValueError):
        cfg['hour'] = 2
    cfg['enabled'] = bool(cfg.get('enabled'))
    cfg['email'] = (cfg.get('email') or '').strip()
    cfg['drive_folder'] = (cfg.get('drive_folder') or '').strip()
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    path = _config_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _norm_folder(path: str) -> str:
    return os.path.normpath(os.path.abspath((path or '').strip()))


def validate_drive_folder(path: str) -> str:
    folder = _norm_folder(path)
    if not folder or not os.path.isdir(folder):
        raise ValueError('مجلد Google Drive غير موجود. ثبّت Drive ثم أنشئ مجلداً داخل «ملفاتي» وحدّد مساره هنا.')
    # منع الكتابة داخل مجلد المشروع نفسه إلا إن كان مجلد Drive منفصلاً
    base = _norm_folder(_base_dir)
    try:
        if os.path.commonpath([folder, base]) == base and folder == base:
            raise ValueError('لا تختر مجلد البرنامج نفسه. اختر مجلداً داخل Google Drive.')
    except ValueError as e:
        if 'commonpath' not in str(type(e)).lower() and 'لا تختر' in str(e):
            raise
    return folder


def suggest_drive_folders() -> list[str]:
    """اقتراحات مسارات شائعة لمجلد Google Drive على Windows."""
    found: list[str] = []
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'Google Drive'),
        os.path.join(home, 'GoogleDrive'),
        os.path.join(home, 'My Drive'),
        r'G:\My Drive',
        r'H:\My Drive',
    ]
    # Google Drive for Desktop غالباً تحت:
    # C:\Users\<user>\Google Drive\My Drive
    # أو C:\Users\<user>\AppData\...\Google\DriveFS — لكن الأخير غير مفيد كنسخ يدوي
    for p in candidates:
        if os.path.isdir(p) and p not in found:
            found.append(p)
    my_drive = os.path.join(home, 'Google Drive', 'My Drive')
    if os.path.isdir(my_drive) and my_drive not in found:
        found.insert(0, my_drive)
    return found


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith('.egg-info')


def create_full_project_zip(dest_zip: str) -> str:
    """ZIP كامل للمشروع: الكود + قاعدة البيانات + uploads."""
    os.makedirs(os.path.dirname(dest_zip) or '.', exist_ok=True)
    base = _base_dir
    with zipfile.ZipFile(dest_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
            # لا تُضمّن مجلد الوجهة إن كان داخل المشروع
            rel_root = os.path.relpath(root, base)
            if rel_root != '.' and BACKUP_PREFIX in rel_root.replace('\\', '/'):
                continue
            for name in files:
                if name.endswith(('.pyc', '.pyo')):
                    continue
                if name.startswith(BACKUP_PREFIX) and name.endswith('.zip'):
                    continue
                if name == CONFIG_NAME:
                    continue
                full = os.path.join(root, name)
                # تجنّب تضمين الملف الوجهة أثناء إنشائه
                try:
                    if os.path.samefile(full, dest_zip):
                        continue
                except OSError:
                    pass
                arc = os.path.relpath(full, base).replace('\\', '/')
                zf.write(full, arcname=arc)
    return dest_zip


def list_previous_backups(folder: str) -> list[str]:
    out = []
    try:
        for name in os.listdir(folder):
            if name.startswith(BACKUP_PREFIX) and name.lower().endswith('.zip'):
                out.append(os.path.join(folder, name))
    except OSError:
        pass
    return out


def delete_previous_backups(folder: str, keep: str | None = None) -> list[str]:
    """حذف النسخ السابقة من مجلد Drive المحلي → يزامن Drive الحذف إلى السحابة."""
    deleted = []
    keep_norm = os.path.normcase(os.path.abspath(keep)) if keep else None
    for path in list_previous_backups(folder):
        if keep_norm and os.path.normcase(os.path.abspath(path)) == keep_norm:
            continue
        try:
            os.remove(path)
            deleted.append(os.path.basename(path))
        except OSError:
            pass
    return deleted


def run_daily_backup_now(force: bool = False) -> dict[str, Any]:
    """إنشاء نسخة كاملة داخل مجلد Drive وحذف السابقة."""
    with _lock:
        cfg = load_config()
        today = datetime.now().strftime('%Y-%m-%d')
        if not force and cfg.get('last_run_date') == today and cfg.get('last_status') == 'ok':
            return {'ok': True, 'skipped': True, 'message': 'تمت النسخة لهذا اليوم مسبقاً.'}
        if not force and not cfg.get('enabled'):
            return {'ok': False, 'message': 'النسخ التلقائي غير مفعّل.'}

        email = (cfg.get('email') or '').strip()
        folder_raw = (cfg.get('drive_folder') or '').strip()
        if not email or '@' not in email:
            return {'ok': False, 'message': 'أدخل بريد Google المرتبط بتطبيق Drive على هذا الجهاز.'}
        if not folder_raw:
            return {'ok': False, 'message': 'حدد مسار مجلد Google Drive على الجهاز.'}

        try:
            folder = validate_drive_folder(folder_raw)
        except ValueError as e:
            cfg['last_status'] = 'error'
            cfg['last_error'] = str(e)
            save_config(cfg)
            return {'ok': False, 'message': str(e)}

        tmp_zip = os.path.join(
            _base_dir,
            'backups_gdrive',
            f'_tmp-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip',
        )
        try:
            os.makedirs(os.path.dirname(tmp_zip), exist_ok=True)
            create_full_project_zip(tmp_zip)

            # احذف كل النسخ السابقة أولاً (مزامنة الحذف إلى السحابة)
            deleted = delete_previous_backups(folder)

            final_name = BACKUP_FIXED
            final_path = os.path.join(folder, final_name)
            if os.path.isfile(final_path):
                try:
                    os.remove(final_path)
                    deleted.append(final_name)
                except OSError:
                    pass

            shutil.move(tmp_zip, final_path)

            cfg['last_run_date'] = today
            cfg['last_run_at'] = datetime.now().isoformat(timespec='seconds')
            cfg['last_status'] = 'ok'
            cfg['last_file'] = final_name
            cfg['last_error'] = ''
            cfg['last_deleted'] = '، '.join(deleted) if deleted else 'لا يوجد'
            save_config(cfg)

            msg = (
                f'تم حفظ النسخة الكاملة في مجلد Drive: {final_name}. '
                f'حُذفت السابقة: {cfg["last_deleted"]}. '
                f'تطبيق Google Drive سيزامن الملف ويحذف القديم من السحابة تلقائياً.'
            )
            return {'ok': True, 'message': msg, 'file': final_path, 'deleted': deleted}
        except Exception as e:
            cfg['last_status'] = 'error'
            cfg['last_error'] = str(e)[:400]
            save_config(cfg)
            try:
                if os.path.isfile(tmp_zip):
                    os.remove(tmp_zip)
            except OSError:
                pass
            return {'ok': False, 'message': str(e)}


def _last_success_at(cfg: dict[str, Any]) -> datetime | None:
    raw = (cfg.get('last_run_at') or '').strip()
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    day = (cfg.get('last_run_date') or '').strip()
    if day and cfg.get('last_status') == 'ok':
        try:
            h = int(cfg.get('hour', 2))
            return datetime.strptime(day, '%Y-%m-%d').replace(hour=h, minute=0, second=0)
        except ValueError:
            pass
    return None


def _backup_is_due(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    """مستحق إن لم تنجح نسخة منذ ~20 ساعة، أو فُوّت اليوم لأن الجهاز كان مطفأ."""
    if not cfg.get('enabled'):
        return False
    if not (cfg.get('email') or '').strip() or not (cfg.get('drive_folder') or '').strip():
        return False
    now = now or datetime.now()
    last = _last_success_at(cfg)
    if last is None:
        return True
    if cfg.get('last_status') != 'ok':
        return True
    if (now - last).total_seconds() >= 20 * 3600:
        return True
    today = now.strftime('%Y-%m-%d')
    if cfg.get('last_run_date') != today and now.hour >= int(cfg.get('hour', 2)):
        return True
    return False


def _scheduler_loop() -> None:
    # انتظر قليلاً حتى يقلع تطبيق Google Drive ويظهر المجلد بعد تشغيل الجهاز
    time.sleep(60)
    while True:
        try:
            cfg = load_config()
            if _backup_is_due(cfg):
                run_daily_backup_now(force=False)
        except Exception as e:
            try:
                cfg = load_config()
                cfg['last_status'] = 'error'
                cfg['last_error'] = f'المجدول: {e}'[:400]
                save_config(cfg)
            except Exception:
                pass
        time.sleep(120)


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, name='yai-gdrive-backup', daemon=True)
    t.start()


def status_for_template() -> dict[str, Any]:
    cfg = load_config()
    folder = (cfg.get('drive_folder') or '').strip()
    folder_ok = bool(folder and os.path.isdir(folder))
    return {
        'enabled': bool(cfg.get('enabled')),
        'email': cfg.get('email') or '',
        'drive_folder': folder,
        'folder_ok': folder_ok,
        'hour': int(cfg.get('hour', 2)),
        'suggestions': suggest_drive_folders(),
        'last_run_date': cfg.get('last_run_date') or '',
        'last_run_at': cfg.get('last_run_at') or '',
        'last_status': cfg.get('last_status') or '',
        'last_file': cfg.get('last_file') or '',
        'last_error': cfg.get('last_error') or '',
        'last_deleted': cfg.get('last_deleted') or '',
        'backup_name': BACKUP_FIXED,
    }
