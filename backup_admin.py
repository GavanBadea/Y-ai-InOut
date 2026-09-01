"""نسخ احتياطي / استعادة / تصفير سنوي — للمدير فقط."""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import gdrive_backup

backup_bp = Blueprint('backup_admin', __name__, url_prefix='/admin/backup')

FIRST_RESET_PASSWORD = 'yara2020'
FIRST_RESET_UNLOCK_KEY = 'first_time_reset_unlocked'

_ROOT_FILES = (
    'requirements.txt',
    'network_config.json',
    'Y-inout.bat',
    'Y-inout-server.bat',
    'Y-inout-client.bat',
)


def _copy_root_python_files(src_dir, dest_dir):
    """نسخ كل ملفات .py في جذر المشروع (ضرورية لتشغيل النسخة المستقلة)."""
    for name in os.listdir(src_dir):
        if not name.endswith('.py'):
            continue
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest_dir, name))

_ROOT_DIRS = ('static', 'templates', 'scripts')

_IGNORE_NAMES = frozenset({'__pycache__', '.git', '.cursor'})


def _admin_only(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get('role') != 'مدير':
            flash('هذه الصفحة للمدير فقط', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return wrapped


def _first_reset_unlocked() -> bool:
    return bool(session.get(FIRST_RESET_UNLOCK_KEY))


def _first_reset_password_ok(pw: str) -> bool:
    return (pw or '').strip().lower() == FIRST_RESET_PASSWORD.lower()


def _ignore_dir(_dir, names):
    return {n for n in names if n in _IGNORE_NAMES or n.endswith('.pyc')}


def _norm_path(p):
    return os.path.normpath(os.path.abspath(p.strip()))


def _valid_backup_root(path):
    return (
        os.path.isdir(path)
        and os.path.isfile(os.path.join(path, 'app.py'))
        and os.path.isfile(os.path.join(path, 'Y_In_Out_DataBase.db'))
    )


def _find_backup_root(extracted):
    if _valid_backup_root(extracted):
        return extracted
    try:
        subs = [
            os.path.join(extracted, name)
            for name in os.listdir(extracted)
            if os.path.isdir(os.path.join(extracted, name))
        ]
    except OSError:
        return None
    for sub in subs:
        if _valid_backup_root(sub):
            return sub
    return None


def _write_readme(dest):
    text = r"""Y-ai InOut — نسخة احتياطية
================================

لتشغيل هذه النسخة كبرنامج مستقل:
1. افتح هذا المجلد (مثلاً D:\...\Y-ai-InOut-backup-...).
2. شغّل Y-inout.bat (وليس Y-inout-client.bat) واترك النافذة السوداء مفتوحة.
3. المتصفح: http://127.0.0.1:8000/login

إن ظهر المتصفح فارغاً: راجع النافذة السوداء — يجب ألا تظهر رسالة خطأ حمراء.
تحتوي النسخة على: البرنامج، قاعدة البيانات، ومجلد uploads (بما فيها المسح الضوئي).
"""
    try:
        with open(os.path.join(dest, 'اقرأني-نسخة-احتياطية.txt'), 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError:
        pass


def create_full_backup(base_dir, db_path, upload_folder, dest_parent):
    dest_parent = _norm_path(dest_parent)
    if not os.path.isdir(dest_parent):
        os.makedirs(dest_parent, exist_ok=True)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(dest_parent, f'Y-ai-InOut-backup-{stamp}')
    os.makedirs(dest, exist_ok=False)

    _copy_root_python_files(base_dir, dest)

    for name in _ROOT_FILES:
        src = os.path.join(base_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))

    for name in _ROOT_DIRS:
        src = os.path.join(base_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, name), ignore=_ignore_dir)

    if os.path.isfile(db_path):
        shutil.copy2(db_path, os.path.join(dest, 'Y_In_Out_DataBase.db'))

    license_file = os.path.join(base_dir, 'yai_license.json')
    if os.path.isfile(license_file):
        shutil.copy2(license_file, os.path.join(dest, 'yai_license.json'))

    if os.path.isdir(upload_folder):
        shutil.copytree(upload_folder, os.path.join(dest, 'uploads'), ignore=_ignore_dir)

    _write_readme(dest)

    check = subprocess.run(
        [sys.executable, '-c', 'import app'],
        cwd=dest,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        err = (check.stderr or check.stdout or '').strip()
        raise OSError(
            f'النسخة نُسخت لكنها غير قابلة للتشغيل: {err[:400]}'
        )

    return dest


def restore_full_backup(base_dir, db_path, upload_folder, source_dir):
    source_dir = _norm_path(source_dir)
    if not _valid_backup_root(source_dir):
        raise ValueError('المجلد المختار ليس نسخة احتياطية صالحة (يجب أن يحتوي app.py و Y_In_Out_DataBase.db).')

    _copy_root_python_files(source_dir, base_dir)

    for name in _ROOT_FILES:
        src = os.path.join(source_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(base_dir, name))

    for name in _ROOT_DIRS:
        src = os.path.join(source_dir, name)
        dst = os.path.join(base_dir, name)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=_ignore_dir)

    src_db = os.path.join(source_dir, 'Y_In_Out_DataBase.db')
    if os.path.isfile(src_db):
        shutil.copy2(src_db, db_path)

    src_lic = os.path.join(source_dir, 'yai_license.json')
    dst_lic = os.path.join(base_dir, 'yai_license.json')
    if os.path.isfile(src_lic):
        shutil.copy2(src_lic, dst_lic)

    src_up = os.path.join(source_dir, 'uploads')
    if os.path.isdir(src_up):
        if os.path.isdir(upload_folder):
            shutil.rmtree(upload_folder)
        shutil.copytree(src_up, upload_folder, ignore=_ignore_dir)
    else:
        os.makedirs(upload_folder, exist_ok=True)
        for sub in ('in', 'out', 'scans', 'archive'):
            os.makedirs(os.path.join(upload_folder, sub), exist_ok=True)


def restore_from_zip(base_dir, db_path, upload_folder, zip_path):
    tmp = tempfile.mkdtemp(prefix='yai_restore_')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(tmp)
        root = _find_backup_root(tmp)
        if not root:
            raise ValueError('ملف ZIP لا يحتوي على نسخة احتياطية صالحة.')
        restore_full_backup(base_dir, db_path, upload_folder, root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def annual_reset(db_path, upload_folder):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute('DELETE FROM Book_Movement')
        conn.execute('DELETE FROM Book_Attachment')
        conn.execute('DELETE FROM Out_tbl')
        conn.execute('DELETE FROM In_tbl')
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('In_tbl','Out_tbl','Book_Movement','Book_Attachment')"
        )
        conn.commit()
    finally:
        conn.close()

    if os.path.isdir(upload_folder):
        for sub in ('in', 'out', 'scans', 'archive'):
            folder = os.path.join(upload_folder, sub)
            if os.path.isdir(folder):
                shutil.rmtree(folder)
            os.makedirs(folder, exist_ok=True)


def first_time_reset(db_path, upload_folder):
    """تصفير كامل للاستخدام لأول مرة: يحذف كل البيانات بما فيها الشعار والأقسام والمستخدمين."""
    annual_reset(db_path, upload_folder)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for table in (
            'Activity_Log',
            'Dept_Book_Seen',
            'Attachment_Ocr_Index',
            'Users',
            'Department',
            'Add_In',
            'Add_Out',
            'organization_tbl',
        ):
            try:
                conn.execute(f'DELETE FROM {table}')
                conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
            except sqlite3.Error:
                pass
        conn.commit()
    finally:
        conn.close()

    org_dir = os.path.join(upload_folder, 'org')
    if os.path.isdir(org_dir):
        shutil.rmtree(org_dir)
    os.makedirs(org_dir, exist_ok=True)


def init_backup_admin(base_dir, db_path, upload_folder):
    backup_bp.base_dir = base_dir
    backup_bp.db_path = db_path
    backup_bp.upload_folder = upload_folder
    gdrive_backup.init_gdrive(base_dir, db_path, upload_folder)
    gdrive_backup.start_scheduler()


@backup_bp.route('/', methods=['GET', 'POST'])
@_admin_only
def backup_page():
    base_dir = backup_bp.base_dir
    db_path = backup_bp.db_path
    upload_folder = backup_bp.upload_folder

    if request.method == 'GET':
        ref = (request.referrer or '').replace('\\', '/')
        if '/admin/backup' not in ref:
            session.pop(FIRST_RESET_UNLOCK_KEY, None)

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'unlock_first_reset':
            pw = (request.form.get('unlock_pw') or '').strip()
            if _first_reset_password_ok(pw):
                session[FIRST_RESET_UNLOCK_KEY] = True
                flash('تم فتح قسم التصفير للاستخدام لأول مرة.', 'success')
            else:
                flash('كلمة المرور غير صحيحة.', 'danger')
            return redirect(url_for('backup_admin.backup_page'))

        if action == 'create_backup':
            dest = request.form.get('backup_path', '').strip()
            if not dest:
                flash('حدد مجلد الحفظ للنسخة الاحتياطية.', 'warning')
            else:
                try:
                    out = create_full_backup(base_dir, db_path, upload_folder, dest)
                    flash(f'تم إنشاء النسخة الاحتياطية بنجاح:\n{out}', 'success')
                except FileExistsError:
                    flash('تعذر إنشاء المجلد (اسم مكرر). أعد المحاولة.', 'danger')
                except OSError as e:
                    flash(f'تعذر حفظ النسخة: {e}', 'danger')

        elif action == 'restore_path':
            src = request.form.get('restore_path', '').strip()
            if not src:
                flash('حدد مسار مجلد النسخة الاحتياطية.', 'warning')
            else:
                try:
                    restore_full_backup(base_dir, db_path, upload_folder, src)
                    flash('تمت الاستعادة من المجلد. أعد تشغيل البرنامج (أغلق Y-inout.bat وشغّله من جديد).', 'success')
                except ValueError as e:
                    flash(str(e), 'danger')
                except OSError as e:
                    flash(f'تعذر الاستعادة: {e}', 'danger')

        elif action == 'restore_zip':
            zf = request.files.get('backup_zip')
            if not zf or not zf.filename:
                flash('اختر ملف ZIP للنسخة الاحتياطية.', 'warning')
            elif not zf.filename.lower().endswith('.zip'):
                flash('يجب أن يكون الملف بصيغة .zip', 'warning')
            else:
                tmp_zip = os.path.join(
                    tempfile.gettempdir(),
                    f'yai_upload_{datetime.now().strftime("%Y%m%d%H%M%S")}.zip',
                )
                try:
                    zf.save(tmp_zip)
                    restore_from_zip(base_dir, db_path, upload_folder, tmp_zip)
                    flash('تمت الاستعادة من الملف. أعد تشغيل البرنامج.', 'success')
                except (ValueError, zipfile.BadZipFile) as e:
                    flash(str(e) if str(e) else 'ملف ZIP تالف.', 'danger')
                except OSError as e:
                    flash(f'تعذر الاستعادة: {e}', 'danger')
                finally:
                    if os.path.isfile(tmp_zip):
                        os.remove(tmp_zip)

        elif action == 'annual_reset':
            confirm = request.form.get('confirm_text', '').strip()
            if confirm != 'تصفير':
                flash('اكتب كلمة «تصفير» للتأكيد.', 'warning')
            elif not request.form.get('confirm_reset'):
                flash('يجب تفعيل خانة التأكيد.', 'warning')
            else:
                try:
                    annual_reset(db_path, upload_folder)
                    flash(
                        'تم التصفير السنوي: حُذفت الكتب الواردة والصادرة والمرفقات. '
                        'بقيت: المستخدمون، معلومات الدائرة، الأقسام، جهات الوارد والصادر.',
                        'success',
                    )
                except OSError as e:
                    flash(f'تعذر التصفير: {e}', 'danger')

        elif action == 'first_time_reset':
            if not _first_reset_unlocked():
                flash('أدخل كلمة المرور لفتح قسم التصفير للاستخدام لأول مرة.', 'warning')
            else:
                confirm = request.form.get('confirm_text', '').strip()
                if confirm != 'اول مرة':
                    flash('اكتب عبارة «اول مرة» للتأكيد.', 'warning')
                elif not request.form.get('confirm_reset'):
                    flash('يجب تفعيل خانة التأكيد.', 'warning')
                else:
                    try:
                        first_time_reset(db_path, upload_folder)
                        session.clear()
                        flash(
                            'تم التصفير للاستخدام لأول مرة. أنشئ حساب المدير الآن.',
                            'success',
                        )
                        return redirect(url_for('setup'))
                    except OSError as e:
                        flash(f'تعذر التصفير: {e}', 'danger')

        elif action == 'gdrive_save':
            cfg = gdrive_backup.load_config()
            cfg['enabled'] = bool(request.form.get('gdrive_enabled'))
            cfg['email'] = request.form.get('email', '').strip()
            cfg['drive_folder'] = request.form.get('drive_folder', '').strip()
            try:
                cfg['hour'] = max(0, min(23, int(request.form.get('hour', 2))))
            except (TypeError, ValueError):
                cfg['hour'] = 2
            if cfg['enabled']:
                if not cfg['email'] or '@' not in cfg['email']:
                    flash('أدخل بريد Google الصحيح قبل تفعيل الرفع التلقائي.', 'warning')
                    return redirect(url_for('backup_admin.backup_page'))
                if not cfg['drive_folder']:
                    flash('حدد مسار مجلد Google Drive على الجهاز.', 'warning')
                    return redirect(url_for('backup_admin.backup_page'))
                try:
                    cfg['drive_folder'] = gdrive_backup.validate_drive_folder(cfg['drive_folder'])
                except ValueError as e:
                    flash(str(e), 'danger')
                    return redirect(url_for('backup_admin.backup_page'))
            gdrive_backup.save_config(cfg)
            flash('تم حفظ إعدادات النسخ الاحتياطي لـ Google Drive.', 'success')

        elif action == 'gdrive_disable':
            cfg = gdrive_backup.load_config()
            cfg['enabled'] = False
            gdrive_backup.save_config(cfg)
            flash('تم إيقاف النسخ اليومي التلقائي.', 'success')

        elif action == 'gdrive_run_now':
            result = gdrive_backup.run_daily_backup_now(force=True)
            flash(result.get('message', 'انتهى'), 'success' if result.get('ok') else 'danger')

        return redirect(url_for('backup_admin.backup_page'))

    in_count = out_count = 0
    try:
        conn = sqlite3.connect(db_path)
        in_count = conn.execute('SELECT COUNT(*) FROM In_tbl').fetchone()[0]
        out_count = conn.execute('SELECT COUNT(*) FROM Out_tbl').fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass

    return render_template(
        'backup_admin.html',
        in_count=in_count,
        out_count=out_count,
        gdrive=gdrive_backup.status_for_template(),
        first_reset_unlocked=_first_reset_unlocked(),
    )