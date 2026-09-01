"""تحديث البرنامج من مستودع GitHub — واجهة أمامية و/أو خلفية (للمدير فقط)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from functools import wraps
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import (
    Blueprint,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

CONFIG_NAME = 'github_update_config.json'
UNLOCK_PASSWORD = 'yara2020'
SESSION_UNLOCK_KEY = 'github_update_unlocked'

# لا تُستبدل عند التحديث
_PRESERVE_NAMES = frozenset({
    'Y_In_Out_DataBase.db',
    'uploads',
    'network_config.json',
    'gdrive_backup_config.json',
    'github_update_config.json',
    'yai_license.json',
    'naps2_config.json',
    '__pycache__',
    '.git',
    '.cursor',
    '.env',
    'الى العميل',
    'مفاتيح التوليد',
    'backups_gdrive',
})

_FRONTEND_DIRS = ('static', 'templates')
_BACKEND_ROOT_EXTS = ('.py',)
_BACKEND_ROOT_FILES = (
    'requirements.txt',
    'Y-inout.bat',
    'Y-inout-server.bat',
    'Y-inout-client.bat',
)
_BACKEND_DIRS = ('scripts',)

github_bp = Blueprint('github_update', __name__, url_prefix='/admin/github-update')


def _admin_only(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get('role') != 'مدير':
            flash('هذه الصفحة للمدير فقط', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return wrapped


def _github_unlocked() -> bool:
    return bool(session.get(SESSION_UNLOCK_KEY))


def _unlock_password_ok(pw: str) -> bool:
    return (pw or '').strip().lower() == UNLOCK_PASSWORD.lower()


def init_github_update(base_dir: str):
    github_bp.base_dir = base_dir


def _config_path() -> str:
    return os.path.join(github_bp.base_dir, CONFIG_NAME)


def default_config() -> dict[str, Any]:
    return {
        'repo_url': '',
        'owner': '',
        'repo': '',
        'branch': 'main',
        'token': '',
        'last_sha': '',
        'last_update': '',
        'last_scope': '',
        'last_message': '',
        'dismissed_sha': '',
        'auto_check': True,
    }


def load_config() -> dict[str, Any]:
    cfg = default_config()
    path = _config_path()
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: data.get(k, cfg[k]) for k in cfg})
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    path = _config_path()
    out = default_config()
    out.update(cfg)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def parse_repo_url(url: str) -> tuple[str, str]:
    """استخراج owner/repo من رابط أو صيغة مختصرة."""
    text = (url or '').strip().rstrip('/')
    if text.endswith('.git'):
        text = text[:-4]
    m = re.search(r'github\.com[/:]([^/\s]+)/([^/\s]+)', text, re.I)
    if m:
        return m.group(1), m.group(2)
    parts = [p for p in text.replace('\\', '/').split('/') if p]
    if len(parts) == 2 and 'github.com' not in parts[0].lower():
        return parts[0], parts[1]
    raise ValueError('رابط المستودع غير صالح. استخدم: https://github.com/المالك/المستودع')


def _api_headers(token: str = '') -> dict[str, str]:
    h = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Y-ai-InOut-Updater',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _http_get_json(url: str, token: str = '') -> Any:
    req = Request(url, headers=_api_headers(token))
    with urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _http_download(url: str, dest: str, token: str = '') -> None:
    req = Request(url, headers=_api_headers(token))
    with urlopen(req, timeout=180) as resp, open(dest, 'wb') as out:
        shutil.copyfileobj(resp, out)


def fetch_remote_info(cfg: dict[str, Any]) -> dict[str, Any]:
    owner = cfg.get('owner') or ''
    repo = cfg.get('repo') or ''
    branch = (cfg.get('branch') or 'main').strip() or 'main'
    token = (cfg.get('token') or '').strip()
    if not owner or not repo:
        raise ValueError('احفظ إعدادات المستودع أولاً')

    url = f'https://api.github.com/repos/{owner}/{repo}/commits/{branch}'
    try:
        data = _http_get_json(url, token)
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        if e.code == 404:
            raise ValueError(
                f'لم يُعثر على الفرع أو المستودع ({owner}/{repo} @ {branch}). '
                'تحقق من الرابط أو التوكن للمستودعات الخاصة.'
            ) from e
        if e.code in (401, 403):
            raise ValueError('رفض GitHub الوصول — تحقق من التوكن والصلاحيات.') from e
        raise ValueError(f'خطأ GitHub HTTP {e.code}: {body}') from e
    except URLError as e:
        raise ValueError(f'تعذّر الاتصال بـ GitHub: {e.reason}') from e

    commit = data.get('commit') or {}
    msg = ((commit.get('message') or '').strip().split('\n')[0])[:200]
    author = ((commit.get('author') or {}).get('name')) or ''
    date = ((commit.get('author') or {}).get('date')) or ''
    sha = (data.get('sha') or '')[:40]
    local_sha = (cfg.get('last_sha') or '')[:40]
    return {
        'sha': sha,
        'short_sha': sha[:7] if sha else '',
        'message': msg,
        'author': author,
        'date': date,
        'branch': branch,
        'owner': owner,
        'repo': repo,
        'update_available': bool(sha) and sha != local_sha,
        'local_sha': local_sha,
        'local_short': local_sha[:7] if local_sha else '—',
    }


def _find_project_root(extracted: str) -> str | None:
    if os.path.isfile(os.path.join(extracted, 'app.py')):
        return extracted
    try:
        names = os.listdir(extracted)
    except OSError:
        return None
    for name in names:
        sub = os.path.join(extracted, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, 'app.py')):
            return sub
    return None


def _safe_copy_tree(src: str, dest: str) -> int:
    """نسخ مجلد مع استبدال المحتويات، مع تجاهل __pycache__."""
    count = 0
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)

    def _ignore(_dir, names):
        return {n for n in names if n in ('__pycache__', '.git') or n.endswith('.pyc')}

    shutil.copytree(src, dest, ignore=_ignore)
    for _root, _dirs, files in os.walk(dest):
        count += len(files)
    return count


def _apply_frontend(src_root: str, dest_root: str) -> list[str]:
    done = []
    for name in _FRONTEND_DIRS:
        s = os.path.join(src_root, name)
        d = os.path.join(dest_root, name)
        if not os.path.isdir(s):
            continue
        n = _safe_copy_tree(s, d)
        done.append(f'{name}/ ({n} ملف)')
    return done


def _apply_backend(src_root: str, dest_root: str) -> list[str]:
    done = []
    for name in os.listdir(src_root):
        if name in _PRESERVE_NAMES:
            continue
        src = os.path.join(src_root, name)
        if not os.path.isfile(src):
            continue
        if name.endswith(_BACKEND_ROOT_EXTS) or name in _BACKEND_ROOT_FILES:
            shutil.copy2(src, os.path.join(dest_root, name))
            done.append(name)

    for name in _BACKEND_DIRS:
        s = os.path.join(src_root, name)
        d = os.path.join(dest_root, name)
        if os.path.isdir(s):
            n = _safe_copy_tree(s, d)
            done.append(f'{name}/ ({n} ملف)')
    return done


def _pip_install_requirements(base_dir: str) -> str:
    req = os.path.join(base_dir, 'requirements.txt')
    if not os.path.isfile(req):
        return 'لا يوجد requirements.txt'
    try:
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', req],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode == 0:
            return 'تم تثبيت/تحديث المتطلبات من requirements.txt'
        err = (r.stderr or r.stdout or '')[-400:]
        return f'pip انتهى بخطأ: {err}'
    except Exception as e:
        return f'تعذّر تشغيل pip: {e}'


def download_and_apply(
    cfg: dict[str, Any],
    scope: str,
    install_deps: bool = False,
) -> dict[str, Any]:
    """
    scope: frontend | backend | both
    """
    if scope not in ('frontend', 'backend', 'both'):
        raise ValueError('نطاق التحديث غير صالح')

    owner = cfg.get('owner') or ''
    repo = cfg.get('repo') or ''
    branch = (cfg.get('branch') or 'main').strip() or 'main'
    token = (cfg.get('token') or '').strip()
    if not owner or not repo:
        raise ValueError('احفظ إعدادات المستودع أولاً')

    info = fetch_remote_info(cfg)
    zip_url = f'https://api.github.com/repos/{owner}/{repo}/zipball/{branch}'
    base_dir = github_bp.base_dir
    applied: list[str] = []
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix='yai-gh-upd-') as tmp:
        zip_path = os.path.join(tmp, 'repo.zip')
        try:
            _http_download(zip_url, zip_path, token)
        except HTTPError as e:
            raise ValueError(f'فشل تنزيل الحزمة من GitHub (HTTP {e.code})') from e
        except URLError as e:
            raise ValueError(f'تعذّر تنزيل التحديث: {e.reason}') from e

        extract_dir = os.path.join(tmp, 'extract')
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)

        src_root = _find_project_root(extract_dir)
        if not src_root:
            raise ValueError('الحزمة لا تحتوي على app.py — تأكد من المستودع الصحيح')

        if scope in ('frontend', 'both'):
            applied.extend(_apply_frontend(src_root, base_dir))
        if scope in ('backend', 'both'):
            applied.extend(_apply_backend(src_root, base_dir))
            if install_deps:
                notes.append(_pip_install_requirements(base_dir))

    if not applied:
        raise ValueError('لم يُنسخ أي ملف — تحقق من محتوى المستودع')

    cfg['last_sha'] = info['sha']
    cfg['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cfg['last_scope'] = scope
    cfg['last_message'] = info.get('message') or ''
    save_config(cfg)

    notes.append('سيُعاد تشغيل البرنامج الآن لتفعيل التحديث.')
    return {
        'applied': applied,
        'notes': notes,
        'info': info,
        'scope': scope,
    }


def schedule_restart_after_update() -> None:
    """بعد نجاح التطبيق: عملية جديدة ثم إغلاق الحالية. لا يُستدعى إلا من زر التحديث."""

    def _restart():
        time.sleep(1.4)
        try:
            exe = sys.executable
            if getattr(sys, 'frozen', False):
                inner = f'ping 127.0.0.1 -n 3 >nul & start "" "{exe}"'
            else:
                parts = [exe, os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
                quoted = ' '.join(f'"{p}"' for p in parts)
                inner = f'ping 127.0.0.1 -n 3 >nul & {quoted}'
            kwargs = {'shell': True, 'close_fds': True}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(inner, **kwargs)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_restart, name='yai-gh-restart', daemon=True).start()


_RESTARTING_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>إعادة تشغيل Y-ai InOut</title>
  <style>
    body{font-family:Tahoma,Arial,sans-serif;background:#0f172a;color:#e2e8f0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{text-align:center;max-width:28rem;padding:2rem;line-height:1.7}
    h1{font-size:1.25rem;margin:0 0 0.75rem}
    p{color:#94a3b8;margin:0}
  </style>
</head>
<body>
  <div class="box">
    <h1>تم تطبيق التحديث</h1>
    <p>جاري إعادة تشغيل البرنامج. انتظر لحظات ثم تُفتح صفحة الدخول.</p>
  </div>
  <script>
  (function () {
    var n = 0;
    var t = setInterval(function () {
      n += 1;
      fetch('/login', { credentials: 'omit', cache: 'no-store' })
        .then(function (r) {
          if (r && r.status) {
            clearInterval(t);
            location.href = '/login';
          }
        })
        .catch(function () {});
      if (n > 50) clearInterval(t);
    }, 1500);
  })();
  </script>
</body>
</html>
"""


def try_git_pull(base_dir: str) -> tuple[bool, str]:
    """اختياري: إن وُجد .git استخدم git pull."""
    git_dir = os.path.join(base_dir, '.git')
    if not os.path.isdir(git_dir):
        return False, 'المجلد ليس مستودع git محلي'
    try:
        r = subprocess.run(
            ['git', 'pull', '--ff-only'],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        if r.returncode == 0:
            return True, out or 'git pull نجح'
        return False, out or 'git pull فشل'
    except FileNotFoundError:
        return False, 'أمر git غير متوفر على الجهاز'
    except Exception as e:
        return False, str(e)


def status_for_template() -> dict[str, Any]:
    cfg = load_config()
    return {
        'repo_url': cfg.get('repo_url') or '',
        'branch': cfg.get('branch') or 'main',
        'token_set': bool((cfg.get('token') or '').strip()),
        'last_sha': (cfg.get('last_sha') or '')[:7] or '—',
        'last_update': cfg.get('last_update') or '—',
        'last_scope': cfg.get('last_scope') or '—',
        'last_message': cfg.get('last_message') or '',
        'has_git': os.path.isdir(os.path.join(github_bp.base_dir, '.git')),
        'is_frozen': getattr(sys, 'frozen', False),
        'auto_check': bool(cfg.get('auto_check', True)),
    }


# كاش فحص GitHub حتى لا يُستدعى الـ API في كل طلب
_remote_cache: dict[str, Any] = {'at': 0.0, 'payload': None, 'error': None}
_CACHE_TTL_SEC = 8 * 60


def _build_notify_payload(cfg: dict[str, Any], remote: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    configured = bool(cfg.get('owner') and cfg.get('repo') and cfg.get('repo_url'))
    dismissed = (cfg.get('dismissed_sha') or '')[:40]
    remote_sha = (remote or {}).get('sha') or ''
    update_available = bool(remote and remote.get('update_available'))
    show_badge = (
        configured
        and bool(cfg.get('auto_check', True))
        and update_available
        and remote_sha
        and remote_sha != dismissed
    )
    return {
        'ok': error is None,
        'error': error,
        'configured': configured,
        'auto_check': bool(cfg.get('auto_check', True)),
        'show_badge': show_badge,
        'update_available': update_available,
        'remote': remote,
        'page_url': '/admin/github-update/',
        'is_frozen': getattr(sys, 'frozen', False),
    }


def get_update_status(force: bool = False) -> dict[str, Any]:
    """فحص التحديث مع كاش قصير للواجهة العلوية."""
    import time

    cfg = load_config()
    now = time.time()
    if (
        not force
        and _remote_cache['payload'] is not None
        and (now - float(_remote_cache['at'])) < _CACHE_TTL_SEC
    ):
        return _remote_cache['payload']

    if not (cfg.get('owner') and cfg.get('repo') and cfg.get('repo_url')):
        payload = _build_notify_payload(cfg, None, None)
        _remote_cache.update({'at': now, 'payload': payload, 'error': None})
        return payload

    if not cfg.get('auto_check', True) and not force:
        payload = _build_notify_payload(cfg, None, None)
        payload['update_available'] = False
        payload['show_badge'] = False
        _remote_cache.update({'at': now, 'payload': payload, 'error': None})
        return payload

    try:
        remote = fetch_remote_info(cfg)
        payload = _build_notify_payload(cfg, remote, None)
        _remote_cache.update({'at': now, 'payload': payload, 'error': None})
        return payload
    except ValueError as e:
        payload = _build_notify_payload(cfg, None, str(e))
        _remote_cache.update({'at': now, 'payload': payload, 'error': str(e)})
        return payload


def dismiss_update(sha: str = '') -> None:
    cfg = load_config()
    target = (sha or '').strip()
    if not target:
        cached = (_remote_cache.get('payload') or {}).get('remote') or {}
        target = (cached.get('sha') or '')[:40]
    if not target:
        st = get_update_status(force=True)
        target = ((st.get('remote') or {}).get('sha') or '')[:40]
    cfg['dismissed_sha'] = target
    save_config(cfg)
    # إبطال الكاش لإخفاء الشارة فوراً
    _remote_cache['at'] = 0
    _remote_cache['payload'] = None


@github_bp.route('/api/status')
@_admin_only
def api_status():
    from flask import jsonify
    force = request.args.get('force') in ('1', 'true', 'yes')
    return jsonify(get_update_status(force=force))


@github_bp.route('/api/dismiss', methods=['POST'])
@_admin_only
def api_dismiss():
    from flask import jsonify
    data = request.get_json(silent=True) or {}
    sha = (data.get('sha') or request.form.get('sha') or '').strip()
    dismiss_update(sha)
    return jsonify({'ok': True, **get_update_status(force=True)})


@github_bp.route('/api/apply', methods=['POST'])
@_admin_only
def api_apply():
    from flask import jsonify
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    scope = (data.get('scope') or request.form.get('scope') or 'both').strip()
    install_deps = bool(data.get('install_deps') or request.form.get('install_deps') == '1')
    if getattr(sys, 'frozen', False) and scope in ('backend', 'both'):
        if scope == 'backend':
            return jsonify({
                'ok': False,
                'error': 'النسخة المجمّعة لا تدعم تحديث الخلفية من هنا.',
            }), 400
        scope = 'frontend'
    try:
        result = download_and_apply(cfg, scope, install_deps=install_deps)
        _remote_cache['at'] = 0
        _remote_cache['payload'] = None
        schedule_restart_after_update()
        return jsonify({
            'ok': True,
            'restart': True,
            'applied': result['applied'],
            'notes': result.get('notes') or [],
            'info': result.get('info'),
            'scope': scope,
        })
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@github_bp.route('/', methods=['GET', 'POST'])
@_admin_only
def update_page():
    if request.method == 'GET' and request.args.get('gate') == '1':
        session.pop(SESSION_UNLOCK_KEY, None)
    if request.method == 'GET':
        ref = (request.referrer or '').replace('\\', '/')
        if '/admin/github-update' not in ref:
            session.pop(SESSION_UNLOCK_KEY, None)

    if request.method == 'POST' and (request.form.get('action') or '').strip() == 'unlock':
        pw = (request.form.get('unlock_pw') or '').strip()
        if _unlock_password_ok(pw):
            session[SESSION_UNLOCK_KEY] = True
            flash('تم فتح قسم تحديث GitHub.', 'success')
            return redirect(url_for('github_update.update_page'))
        flash('كلمة المرور غير صحيحة.', 'danger')

    if not _github_unlocked():
        return render_template('github_update_unlock.html')

    cfg = load_config()
    remote = None
    check_error = None

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()

        if action == 'save_settings':
            repo_url = (request.form.get('repo_url') or '').strip()
            branch = (request.form.get('branch') or 'main').strip() or 'main'
            token = (request.form.get('token') or '').strip()
            keep_token = request.form.get('keep_token') == '1'
            try:
                owner, repo = parse_repo_url(repo_url)
            except ValueError as e:
                flash(str(e), 'danger')
                return redirect(url_for('github_update.update_page'))
            cfg['repo_url'] = repo_url
            cfg['owner'] = owner
            cfg['repo'] = repo
            cfg['branch'] = branch
            cfg['auto_check'] = request.form.get('auto_check') == '1'
            if request.form.get('clear_token') == '1':
                cfg['token'] = ''
            elif token:
                cfg['token'] = token
            elif not keep_token:
                cfg['token'] = ''
            # عند أول ربط: ثبّت SHA الحالي كأساس حتى لا تظهر أيقونة إلا لتحديث جديد
            if not cfg.get('last_sha'):
                try:
                    info = fetch_remote_info(cfg)
                    cfg['last_sha'] = info.get('sha') or ''
                    cfg['last_message'] = info.get('message') or ''
                    cfg['dismissed_sha'] = cfg['last_sha']
                except ValueError:
                    pass
            save_config(cfg)
            _remote_cache['at'] = 0
            _remote_cache['payload'] = None
            flash(f'تم حفظ إعدادات المستودع: {owner}/{repo} ({branch})', 'success')
            return redirect(url_for('github_update.update_page'))

        if action == 'check':
            try:
                remote = fetch_remote_info(cfg)
                _remote_cache['at'] = 0
                _remote_cache['payload'] = None
                if remote['update_available']:
                    flash('يتوفر تحديث جديد على GitHub — ستظهر أيقونة التحديث في الشريط العلوي.', 'info')
                else:
                    flash('النسخة الحالية مطابقة لآخر التزام على الفرع.', 'success')
            except ValueError as e:
                check_error = str(e)
                flash(check_error, 'danger')
            return render_template(
                'github_update.html',
                cfg=status_for_template(),
                remote=remote,
                check_error=check_error,
            )

        if action == 'apply':
            scope = (request.form.get('scope') or 'both').strip()
            install_deps = request.form.get('install_deps') == '1'
            if getattr(sys, 'frozen', False) and scope in ('backend', 'both'):
                flash(
                    'هذه النسخة مجمّعة (exe). حدّث الواجهة الأمامية فقط، '
                    'أو ثبّت النسخة المصدرية لتحديث الـ backend.',
                    'warning',
                )
                if scope == 'backend':
                    return redirect(url_for('github_update.update_page'))
                scope = 'frontend'
            try:
                result = download_and_apply(cfg, scope, install_deps=install_deps)
                _remote_cache['at'] = 0
                _remote_cache['payload'] = None
                schedule_restart_after_update()
                return make_response(_RESTARTING_HTML)
            except ValueError as e:
                flash(str(e), 'danger')
            except Exception as e:
                flash(f'فشل التحديث: {e}', 'danger')
            return redirect(url_for('github_update.update_page'))

        if action == 'git_pull':
            ok, msg = try_git_pull(github_bp.base_dir)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('github_update.update_page'))

        flash('إجراء غير معروف', 'warning')
        return redirect(url_for('github_update.update_page'))

    return render_template(
        'github_update.html',
        cfg=status_for_template(),
        remote=remote,
        check_error=check_error,
    )
