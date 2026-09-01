"""
تفعيل Y-ai InOut — مرتبط برمز الجهاز.
(اسم الملف تجنّباً للتعارض مع مكتبة license الافتراضية في Python)
"""
import hashlib
import hmac
import json
import os
import platform
import re
import sys
import uuid
from datetime import datetime

_LICENSE_SECRET = os.environ.get(
    'YAI_LICENSE_SECRET',
    'Y-ai-InOut-2026-License-Key-Change-Before-Distribution',
).encode('utf-8')

VENDOR_PHONE = '07504505340'
VENDOR_NAME = 'الأستاذ Gavan'


def _data_dir():
    if os.environ.get('YAI_DATA_DIR'):
        return os.environ['YAI_DATA_DIR']
    if getattr(sys, 'frozen', False):
        appdata = os.path.join(os.environ.get('APPDATA', ''), 'Y-ai-InOut')
        os.makedirs(appdata, exist_ok=True)
        return appdata
    return os.path.dirname(os.path.abspath(__file__))


def _license_file():
    return os.path.join(_data_dir(), 'yai_license.json')


def _normalize_serial(serial):
    s = (serial or '').strip().upper()
    return re.sub(r'[^A-Z0-9]', '', s)


def _windows_machine_guid():
    if platform.system() != 'Windows':
        return ''
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r'SOFTWARE\Microsoft\Cryptography',
        )
        val, _ = winreg.QueryValueEx(key, 'MachineGuid')
        winreg.CloseKey(key)
        return str(val).strip()
    except OSError:
        return ''


def _get_machine_id():
    guid = _windows_machine_guid()
    if guid:
        return hashlib.sha256(guid.encode('utf-8')).hexdigest()[:8].upper()
    seed = f'{uuid.getnode():012x}|{platform.node()}|{platform.machine()}'
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:8].upper()


def get_machine_request_code():
    mid = _get_machine_id()
    return f'YAI-{mid[:4]}-{mid[4:8]}'


def _sign_client_id(client_id):
    return hmac.new(
        _LICENSE_SECRET,
        client_id.encode('ascii'),
        hashlib.sha256,
    ).hexdigest().upper()[:8]


def format_serial(client_id, sig):
    c = client_id.upper()
    s = sig.upper()
    return f'YAI-{c[:4]}-{c[4:8]}-{s[:4]}-{s[4:8]}'


def _client_id_from_request(request_code):
    raw = _normalize_serial(request_code)
    if raw.startswith('YAI') and len(raw) >= 11:
        return raw[3:11]
    if len(raw) == 8 and re.fullmatch(r'[A-Z0-9]{8}', raw):
        return raw
    return None


def generate_serial_for_request(request_code):
    client_id = _client_id_from_request(request_code)
    if not client_id:
        raise ValueError('رمز الجهاز غير صالح. استخدم الصيغة YAI-XXXX-XXXX')
    return format_serial(client_id, _sign_client_id(client_id))


def validate_license_key(serial):
    raw = _normalize_serial(serial)
    if not re.fullmatch(r'YAI[A-Z0-9]{16}', raw):
        return False, 'صيغة مفتاح الترخيص غير صحيحة.'

    client_id = raw[3:11]
    sig = raw[11:19]
    expected = _sign_client_id(client_id)

    if not hmac.compare_digest(sig, expected):
        return False, 'مفتاح الترخيص غير صالح. تأكد من نسخه كاملاً من المورّد.'

    current = _get_machine_id()
    if client_id != current:
        return False, (
            f'المفتاح لجهاز آخر. رمز هذا الجهاز: {get_machine_request_code()} '
            f'(المعرّف الداخلي: {current})'
        )

    return True, None


def verify_serial(serial):
    ok, _ = validate_license_key(serial)
    return ok


def is_activated():
    path = _license_file()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        serial = _normalize_serial((data or {}).get('serial') or '')
        if not serial:
            return False
        return verify_serial(serial)
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def activate(serial):
    plain = (serial or '').strip()
    if not plain:
        return False, 'يرجى إدخال مفتاح الترخيص.'

    ok, err = validate_license_key(plain)
    if not ok:
        return False, err

    path = _license_file()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

    norm = _normalize_serial(plain)
    payload = {
        'activated': True,
        'serial': norm,
        'machine_id': _get_machine_id(),
        'request_code': get_machine_request_code(),
        'activated_at': datetime.now().isoformat(timespec='seconds'),
    }

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return False, f'تعذر حفظ التفعيل. المسار: {path} — {e}'

    return True, None


def license_file_path():
    return _license_file()


def activation_diagnostics():
    """معلومات للدعم الفني."""
    path = _license_file()
    info = {
        'machine_code': get_machine_request_code(),
        'machine_id': _get_machine_id(),
        'license_path': path,
        'license_exists': os.path.isfile(path),
        'is_activated': is_activated(),
        'data_dir': _data_dir(),
        'frozen': bool(getattr(sys, 'frozen', False)),
    }
    if info['license_exists']:
        try:
            with open(path, encoding='utf-8') as f:
                info['license_content'] = json.load(f)
        except Exception as e:
            info['license_read_error'] = str(e)
    return info
