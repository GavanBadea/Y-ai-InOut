"""Y-ai — مساعد ذكاء اصطناعي محلي (دون إنترنت) لنظام الصادر والوارد."""
import json
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request, session

y_ai_bp = Blueprint('y_ai', __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OVERDUE_DAYS = int(os.environ.get('Y_AI_OVERDUE_DAYS', '14'))

_db_getter = None


def init_y_ai(get_db_func):
    global _db_getter
    _db_getter = get_db_func


def _db():
    if _db_getter is None:
        raise RuntimeError('Y-ai: لم يتم تهيئة get_db')
    return _db_getter()


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'ok': False, 'error': 'يجب تسجيل الدخول أولاً.'}), 401
        return f(*args, **kwargs)
    return decorated


def admin_api_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'مدير':
            return jsonify({'ok': False, 'error': 'هذا الإجراء للمدير فقط.'}), 403
        return f(*args, **kwargs)
    return decorated


def _row_dict(row):
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _val(v, default='—'):
    s = (str(v).strip() if v is not None else '')
    return s if s else default


def _fetch_incoming_book(conn, book_id):
    return conn.execute(
        '''
        SELECT i.*, a.In_place AS source_name, d.Dep_Name AS current_dep_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.NoBook_In = ?
        ''',
        (book_id,),
    ).fetchone()


def _fetch_outgoing_book(conn, book_id):
    return conn.execute(
        '''
        SELECT o.*, a.Out_place AS dest_name
        FROM Out_tbl o
        LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
        WHERE o.NoBook_Out = ?
        ''',
        (book_id,),
    ).fetchone()


def _fetch_movements(conn, book_id, limit=12):
    rows = conn.execute(
        '''
        SELECT m.Move_ID, m.Action_Note, m.Move_Date, m.Is_Completed,
               fd.Dep_Name AS from_dep_name, td.Dep_Name AS to_dep_name
        FROM Book_Movement m
        LEFT JOIN Department fd ON m.From_Dep_ID = fd.Dep_No
        LEFT JOIN Department td ON m.To_Dep_ID = td.Dep_No
        WHERE m.Book_In_ID = ?
        ORDER BY m.Move_Date DESC
        LIMIT ?
        ''',
        (book_id, limit),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _book_context_in(conn, book_id):
    book = _fetch_incoming_book(conn, book_id)
    if not book:
        return None
    data = _row_dict(book)
    data['movements'] = _fetch_movements(conn, book_id)
    return {'type': 'incoming', 'book': data}


def _book_context_out(conn, book_id):
    book = _fetch_outgoing_book(conn, book_id)
    if not book:
        return None
    data = _row_dict(book)
    if data.get('Reply_To_InBook_No'):
        rin = _fetch_incoming_book(conn, data['Reply_To_InBook_No'])
        if rin:
            data['reply_incoming'] = {
                'NoBook_In': rin['NoBook_In'],
                'NoBookCome_In': rin['NoBookCome_In'],
                'NoBook_Dep': rin['NoBook_Dep'],
                'Subject_Com': rin['Subject_Com'],
            }
    return {'type': 'outgoing', 'book': data}


def _overdue_incoming(conn, limit=20):
    cutoff = (datetime.today() - timedelta(days=OVERDUE_DAYS)).strftime('%Y-%m-%d')
    rows = conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com,
               i.Date_Com, i.Date_Dep, i.Status, a.In_place AS source_name,
               d.Dep_Name AS current_dep_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.Status = 'في طور العمل'
          AND (
            (i.Date_Dep IS NOT NULL AND TRIM(i.Date_Dep) != '' AND i.Date_Dep < ?)
            OR (
              (i.Date_Dep IS NULL OR TRIM(i.Date_Dep) = '')
              AND i.Date_Com IS NOT NULL AND TRIM(i.Date_Com) != '' AND i.Date_Com < ?
            )
          )
        ORDER BY COALESCE(NULLIF(TRIM(i.Date_Dep), ''), i.Date_Com) ASC
        LIMIT ?
        ''',
        (cutoff, cutoff, limit),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _search_books(conn, query, limit=15):
    q = (query or '').strip()
    if not q:
        return {'incoming': [], 'outgoing': []}
    like = f'%{q}%'
    incoming = conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.NoBookCome_In, i.Subject_Com,
               i.Date_Com, i.Status, a.In_place AS source_name
        FROM In_tbl i
        LEFT JOIN Add_In a ON i.Add_In_ID = a.Add_InNo
        WHERE i.NoBookCome_In LIKE ? OR i.Subject_Com LIKE ?
           OR CAST(i.NoBook_In AS TEXT) LIKE ? OR i.NoBook_Dep LIKE ?
        ORDER BY i.NoBook_In DESC LIMIT ?
        ''',
        (like, like, like, like, limit),
    ).fetchall()
    outgoing = conn.execute(
        '''
        SELECT o.NoBook_Out, o.NoBook_Out_Manual, o.Subject, o.Date_Out,
               a.Out_place AS dest_name
        FROM Out_tbl o
        LEFT JOIN Add_Out a ON o.Add_Out_ID = a.Add_OutNo
        WHERE o.Subject LIKE ? OR CAST(o.NoBook_Out AS TEXT) LIKE ?
           OR o.NoBook_Out_Manual LIKE ?
        ORDER BY o.NoBook_Out DESC LIMIT ?
        ''',
        (like, like, like, limit),
    ).fetchall()
    return {
        'incoming': [_row_dict(r) for r in incoming],
        'outgoing': [_row_dict(r) for r in outgoing],
    }


def _books_by_department(conn):
    rows = conn.execute(
        '''
        SELECT d.Dep_Name, COUNT(i.NoBook_In) AS cnt
        FROM Department d
        LEFT JOIN In_tbl i ON d.Dep_No = i.Current_Dep_ID
        GROUP BY d.Dep_No, d.Dep_Name
        ORDER BY cnt DESC, d.Dep_Name
        '''
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _system_snapshot(conn):
    org = conn.execute('SELECT Name, Information FROM organization_tbl LIMIT 1').fetchone()
    return {
        'organization': _row_dict(org) if org else None,
        'totals': {
            'incoming': conn.execute('SELECT COUNT(*) FROM In_tbl').fetchone()[0],
            'outgoing': conn.execute('SELECT COUNT(*) FROM Out_tbl').fetchone()[0],
            'in_progress': conn.execute(
                "SELECT COUNT(*) FROM In_tbl WHERE Status='في طور العمل'"
            ).fetchone()[0],
            'completed': conn.execute(
                "SELECT COUNT(*) FROM In_tbl WHERE Status='تم الانتهاء'"
            ).fetchone()[0],
        },
        'overdue_threshold_days': OVERDUE_DAYS,
    }


def _recent_incoming(conn, limit=5):
    rows = conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.Subject_Com, i.Status, i.Date_Com
        FROM In_tbl i
        ORDER BY i.NoBook_In DESC LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _recent_outgoing(conn, limit=5):
    rows = conn.execute(
        '''
        SELECT o.NoBook_Out, o.NoBook_Out_Manual, o.Subject, o.Date_Out
        FROM Out_tbl o
        ORDER BY o.NoBook_Out DESC LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _incoming_without_outgoing_reply(conn, limit=15):
    """وارد «في طور العمل» دون صادر رد مسجّل."""
    rows = conn.execute(
        '''
        SELECT i.NoBook_In, i.NoBook_Dep, i.Subject_Com, d.Dep_Name AS dep_name
        FROM In_tbl i
        LEFT JOIN Department d ON i.Current_Dep_ID = d.Dep_No
        WHERE i.Status = 'في طور العمل'
          AND NOT EXISTS (
            SELECT 1 FROM Out_tbl o WHERE o.Reply_To_InBook_No = i.NoBook_In
          )
        ORDER BY i.NoBook_In DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    return [_row_dict(r) for r in rows]


# أسئلة محددة مسبقاً — Y-ai داخلي 100% (لا إنترنت، لا Ollama، لا API)
PREDEFINED_QUESTIONS = [
    {'id': 'stats_all', 'label': 'إحصائيات عامة', 'examples': ['ما هي الإحصائيات؟', 'ملخص عام']},
    {'id': 'incoming_count', 'label': 'عدد الكتب الواردة', 'examples': ['كم عدد الوارد؟', 'عدد الكتب الواردة']},
    {'id': 'outgoing_count', 'label': 'عدد الكتب الصادرة', 'examples': ['كم عدد الصادر؟', 'عدد الكتب الصادرة']},
    {'id': 'in_progress', 'label': 'كتب في طور العمل', 'examples': ['كم في طور العمل؟', 'قيد المتابعة']},
    {'id': 'completed', 'label': 'كتب منتهية', 'examples': ['كم منتهية؟', 'تم الانتهاء']},
    {'id': 'departments', 'label': 'الكتب حسب القسم', 'examples': ['كل قسم كم كتاب؟', 'محتويات الأقسام']},
    {'id': 'overdue', 'label': 'الكتب المتأخرة', 'examples': ['ما هي الكتب المتأخرة؟']},
    {'id': 'pending_reply', 'label': 'وارد بلا رد صادر', 'examples': ['وارد بدون صادر', 'بانتظار الرد']},
    {'id': 'recent', 'label': 'آخر الكتب', 'examples': ['آخر الكتب', 'أحدث المعاملات']},
    {'id': 'guide', 'label': 'مرشد البرنامج', 'examples': ['ارشدني', 'كيف أضيف وارد؟', 'أين الأقسام؟']},
    {'id': 'help', 'label': 'مساعدة', 'examples': ['ماذا يمكنك؟', 'الأسئلة المتاحة']},
]


def _normalize_ar(text):
    t = (text or '').strip().lower()
    t = re.sub(r'[؟?!.,،؛:\-\(\)\[\]«»"]+', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def _resolve_intent(user_message, action=None):
    """يحدد نية السؤال من action أو من نص الرسالة."""
    if action in (
        'summarize', 'overdue', 'search', 'stats', 'stats_all', 'incoming_count',
        'outgoing_count', 'in_progress', 'completed', 'departments',
        'pending_reply', 'recent', 'help', 'guide',
    ):
        return action
    if action == 'stats':
        return 'stats_all'

    msg = _normalize_ar(user_message)
    if not msg:
        return None

    rules = [
        ('guide', (
            r'ارشد', r'أرشد', r'وين', r'أين', r'اين', r'كيف\s*أ', r'كيف\s*ا',
            r'العملية', r'عمل\s*داخل', r'القسم\s*التالي', r'دليل', r'مرشد',
            r'كيف\s*أضيف', r'كيف\s*اسجل', r'كيف\s*أوجه', r'كيف\s*ابحث',
        )),
        ('help', (
            r'مساعدة', r'ماذا\s*تستطيع', r'ماذا\s*يمكن', r'الأسئلة\s*المتاح',
            r'ساعدني', r'help',
        )),
        ('departments', (
            r'حسب\s*القسم', r'كل\s*قسم', r'الأقسام', r'محتويات\s*القسم',
            r'كتب\s*القسم', r'توزيع\s*القسم',
        )),
        ('pending_reply', (
            r'بدون\s*صادر', r'بلا\s*صادر', r'بانتظار\s*الرد', r'لم\s*يرد',
            r'وارد\s*بلا',
        )),
        ('recent', (
            r'آخر\s*الكتب', r'أحدث', r'آخر\s*معامل', r'الكتب\s*الأخير',
        )),
        ('overdue', (r'متأخر', r'تأخرت', r'تأخير')),
        ('incoming_count', (
            r'عدد\s*الوارد', r'كم\s*وارد', r'الكتب\s*الواردة', r'إجمالي\s*الوارد',
        )),
        ('outgoing_count', (
            r'عدد\s*الصادر', r'كم\s*صادر', r'الكتب\s*الصادرة', r'إجمالي\s*الصادر',
        )),
        ('in_progress', (
            r'في\s*طور\s*العمل', r'قيد\s*المتابعة', r'قيد\s*العمل', r'جاري\s*العمل',
        )),
        ('completed', (
            r'منته', r'تم\s*الانتهاء', r'مكتمل', r'أنجز',
        )),
        ('stats_all', (
            r'إحصاء', r'إحصائية', r'إحصائيات', r'ملخص\s*عام', r'نظرة\s*عام',
        )),
        ('summarize', (r'لخص', r'ملخص\s*الكتاب', r'تلخيص')),
        ('search', (r'ابحث', r'بحث\s*عن', r'اعثر', r'جد\s*معامل')),
    ]

    for intent, patterns in rules:
        for pat in patterns:
            if re.search(pat, msg):
                return intent

    if any(w in msg for w in ('كم', 'عدد', 'إجمالي')):
        return 'stats_all'
    if any(w in msg for w in ('بحث', 'ابحث')):
        return 'search'
    if any(w in msg for w in ('كيف', 'وين', 'أين', 'اين', 'ارشد', 'أرشد', 'مرشد', 'دليل')):
        return 'guide'

    return None


def _format_guide(ctx):
    role = ((ctx.get('user') or {}).get('role')) or ''
    page = (ctx.get('page') or {}).get('endpoint') or ''
    lines = [
        'أنا مرشد Y-ai داخل البرنامج — أرشدك إلى أين تذهب وكيف تتم العملية، بالإضافة إلى الإحصائيات والبحث.',
        '',
    ]
    if role == 'مدير':
        lines.extend([
            'مسارات العمل (مدير):',
            '• كتاب وارد جديد: المراسلات ← الكتب الواردة ← إضافة كتاب وارد، ثم امسح أو ارفع المرفق.',
            '• توجيه كتاب لقسم: افتح الكتاب الوارد ← «توجيه إلى القسم» ← سجّل الحركة. للمدير: يمكن تفعيل «ارفاق الكتاب الحالي».',
            '• إرجاع إلى الادارة: وجّه الكتاب إلى قسم «الادارة» — يظهر تنبيه استلام للمدير.',
            '• كتاب صادر جديد: المراسلات ← الكتب الصادرة ← إضافة. يظهر بجانب الرقم اليدوي آخر صادر مسجّل.',
            '• بحث داخل المرفقات: من القائمة الجانبية «بحث المرفقات OCR».',
            '• الإعدادات: القائمة الجانبية ← «الإعدادات — المدير» ثم مربعات الأوامر:',
            '  الأقسام، جهات الوارد/الصادر (يمكن رفع Excel)، المستخدمون، النسخ الاحتياطي، تسجيل الحركات، QR موبايل، تحديث GitHub.',
            '• تحديث GitHub: من مربعات الإعدادات — يطلب كلمة مرور ويمكن إظهارها أو إخفاؤها.',
            '• دخول المشاهد من الهاتف: إعدادات المدير ← دخول المشاهد QR موبايل ← اطبع/اعرض الرمز.',
        ])
    elif role == 'موظف قسم':
        lines.extend([
            'مسارات العمل (موظف قسم):',
            '• الكتب الموجهة لقسمك: القائمة الجانبية ← «الكتب الموجهة لي». يظهر جرس وتنبيه عند وصول كتاب جديد.',
            '• بعد الفتح يختفي التنبيه. يمكنك تسجيل إجراء أو توجيه الكتاب إن كان في قسمك.',
            '• البحث والإحصائيات: من القائمة الجانبية.',
        ])
    else:
        lines.extend([
            'مسارات العمل (مشاهد):',
            '• البحث: اكتب رقماً أو موضوعاً. في QR اختر وارد أو صادر، ثم الموضوع/الرقم والتاريخ.',
        ])
    if page:
        lines.extend(['', f'أنت الآن في شاشة: {page}'])
    lines.extend([
        '',
        'اسأل مثلاً: «كيف أضيف صادر؟» · «أين جهات الوارد؟» · «أرشدني إلى التوجيه»',
        'وللإحصائيات: «كم عدد الوارد؟» أو اضغط الأزرار السريعة.',
    ])
    return '\n'.join(lines)


def _format_help():
    lines = [
        'Y-ai — مساعد داخلي (يعمل دون إنترنت ولا يحتاج Ollama أو برامج إضافية).',
        'يقرأ بيانات نظامكم فقط. الأسئلة المتاحة:',
        '',
    ]
    for i, q in enumerate(PREDEFINED_QUESTIONS, 1):
        ex = q['examples'][0] if q.get('examples') else ''
        lines.append(f'{i}. {q["label"]}' + (f' — مثال: «{ex}»' if ex else ''))
    lines.extend([
        '',
        '• «أرشدني» — دليل أين القسم وكيف تتم العملية.',
        '• «لخص هذا الكتاب» — من صفحة كتاب محدد.',
        '• «ابحث عن …» — برقم أو موضوع أو جهة.',
        '• أو اضغط الأزرار السريعة أعلى نافذة Y-ai.',
    ])
    return '\n'.join(lines)


def _format_incoming_count(ctx):
    n = (ctx.get('snapshot') or {}).get('totals', {}).get('incoming', 0)
    return f'عدد الكتب الواردة في النظام: {n} كتاب.'


def _format_outgoing_count(ctx):
    n = (ctx.get('snapshot') or {}).get('totals', {}).get('outgoing', 0)
    return f'عدد الكتب الصادرة في النظام: {n} كتاب.'


def _format_in_progress_count(ctx):
    n = (ctx.get('snapshot') or {}).get('totals', {}).get('in_progress', 0)
    return f'عدد الكتب الواردة «في طور العمل»: {n} كتاب.'


def _format_completed_count(ctx):
    n = (ctx.get('snapshot') or {}).get('totals', {}).get('completed', 0)
    return f'عدد الكتب الواردة «تم الانتهاء»: {n} كتاب.'


def _format_departments(ctx):
    rows = ctx.get('by_department') or []
    if not rows:
        return 'لا توجد أقسام مسجّلة أو لا توجد كتب مرتبطة بها.'
    lines = ['عدد الكتب الواردة حسب القسم:', '']
    for i, row in enumerate(rows, 1):
        lines.append(f'{i}. {_val(row.get("Dep_Name"))}: {row.get("cnt", 0)} كتاب')
    total = sum(int(r.get('cnt') or 0) for r in rows)
    lines.extend(['', f'المجموع (حسب القسم الحالي): {total}'])
    return '\n'.join(lines)


def _format_pending_reply(ctx):
    items = ctx.get('pending_reply') or []
    if not items:
        return 'لا توجد كتب واردة «في طور العمل» بدون صادر رد مسجّل — أو تمت متابعتها جميعاً.'
    lines = [
        f'كتب واردة في طور العمل بلا صادر رد (أول {len(items)}):',
        '',
    ]
    for i, row in enumerate(items, 1):
        lines.append(
            f'{i}. وارد {_val(row.get("NoBook_Dep"))} | '
            f'موضوع: {_val(row.get("Subject_Com"))[:45]} | '
            f'قسم: {_val(row.get("dep_name"))}'
        )
    return '\n'.join(lines)


def _format_recent(ctx):
    inc = ctx.get('recent_incoming') or []
    out = ctx.get('recent_outgoing') or []
    lines = ['آخر المعاملات المسجّلة:', '']
    if inc:
        lines.append('— آخر وارد:')
        for i, row in enumerate(inc, 1):
            lines.append(
                f'{i}. رقم {_val(row.get("NoBook_Dep"))} | '
                f'{_val(row.get("Subject_Com"))[:40]} | حالة: {_val(row.get("Status"))}'
            )
        lines.append('')
    if out:
        lines.append('— آخر صادر:')
        for i, row in enumerate(out, 1):
            lines.append(
                f'{i}. رقم {_val(row.get("NoBook_Out_Manual"))} | '
                f'{_val(row.get("Subject"))[:40]}'
            )
    if not inc and not out:
        return 'لا توجد كتب مسجّلة بعد.'
    return '\n'.join(lines)


def build_context_payload(action, page_ctx, user_message):
    conn = _db()
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'user': {
            'username': session.get('username'),
            'role': session.get('role'),
        },
        'page': page_ctx or {},
        'snapshot': _system_snapshot(conn),
    }

    book_type = (page_ctx or {}).get('bookType')
    book_id = (page_ctx or {}).get('bookId')
    if book_id is not None:
        try:
            book_id = int(book_id)
        except (TypeError, ValueError):
            book_id = None

    if book_type == 'in' and book_id:
        payload['current_book'] = _book_context_in(conn, book_id)
    elif book_type == 'out' and book_id:
        payload['current_book'] = _book_context_out(conn, book_id)

    intent = _resolve_intent(user_message, action)
    if intent == 'overdue' or action == 'overdue':
        payload['overdue_incoming'] = _overdue_incoming(conn)
    elif intent == 'search' or action == 'search':
        payload['search_results'] = _search_books(conn, user_message)
    elif intent == 'summarize' or action == 'summarize':
        if not payload.get('current_book') and book_id and book_type == 'in':
            payload['current_book'] = _book_context_in(conn, book_id)
    elif intent == 'departments' or action == 'departments':
        payload['by_department'] = _books_by_department(conn)
    elif intent == 'pending_reply' or action == 'pending_reply':
        payload['pending_reply'] = _incoming_without_outgoing_reply(conn)
    elif intent == 'recent' or action == 'recent':
        payload['recent_incoming'] = _recent_incoming(conn)
        payload['recent_outgoing'] = _recent_outgoing(conn)
    elif intent in ('stats_all', 'stats') or action in ('stats', 'stats_all'):
        payload['overdue_incoming'] = _overdue_incoming(conn, limit=5)
        payload['by_department'] = _books_by_department(conn)

    conn.close()
    return payload


# ─── محرك الرد المحلي (بدون إنترنت) ───────────────────────────────────────────

def _format_summarize(ctx):
    cur = ctx.get('current_book')
    if not cur:
        return (
            'لا يتوفر كتاب محدد في الصفحة الحالية. '
            'يُرجى فتح صفحة كتاب وارد أو صادر ثم إعادة طلب التلخيص.'
        )

    b = cur.get('book') or {}
    if cur.get('type') == 'incoming':
        lines = [
            'ملخص الكتاب الوارد:',
            f'• الرقم الداخلي: {_val(b.get("NoBook_In"))}',
            f'• رقم وارد الدائرة: {_val(b.get("NoBook_Dep"))}',
            f'• رقم كتاب الجهة: {_val(b.get("NoBookCome_In"))}',
            f'• التاريخ: {_val(b.get("Date_Com"))}',
            f'• الجهة الواردة: {_val(b.get("source_name"))}',
            f'• القسم الحالي: {_val(b.get("current_dep_name"))}',
            f'• الحالة: {_val(b.get("Status"))}',
            f'• الموضوع: {_val(b.get("Subject_Com"))}',
        ]
        moves = b.get('movements') or []
        if moves:
            lines.append(f'• عدد حركات التوجيه: {len(moves)}')
            last = moves[0]
            lines.append(
                f'• آخر إجراء: {_val(last.get("Action_Note"))} '
                f'({_val(last.get("Move_Date"))[:10]})'
            )
        return '\n'.join(lines)

    lines = [
        'ملخص الكتاب الصادر:',
        f'• الرقم الداخلي: {_val(b.get("NoBook_Out"))}',
        f'• رقم الصادر اليدوي: {_val(b.get("NoBook_Out_Manual"))}',
        f'• التاريخ: {_val(b.get("Date_Out"))}',
        f'• الجهة الصادرة إليها: {_val(b.get("dest_name"))}',
        f'• الموضوع: {_val(b.get("Subject"))}',
    ]
    reply = b.get('reply_incoming')
    if reply:
        lines.append(
            f'• مرتبط بوارد رقم: {_val(reply.get("NoBook_In"))} — '
            f'{_val(reply.get("Subject_Com"))}'
        )
    return '\n'.join(lines)


def _format_overdue(ctx):
    items = ctx.get('overdue_incoming') or []
    days = (ctx.get('snapshot') or {}).get('overdue_threshold_days', OVERDUE_DAYS)
    if not items:
        return (
            f'لا توجد كتب واردة متأخرة وفق معيار المتابعة ({days} يوماً) '
            'والحالة «في طور العمل».'
        )
    lines = [
        f'الكتب الواردة المتأخرة (أكثر من {days} يوماً ولم تُغلق):',
        f'العدد: {len(items)}',
        '',
    ]
    for i, row in enumerate(items, 1):
        lines.append(
            f'{i}. وارد {_val(row.get("NoBook_Dep"))} | '
            f'جهة: {_val(row.get("NoBookCome_In"))} | '
            f'{_val(row.get("source_name"))} | '
            f'موضوع: {_val(row.get("Subject_Com"))[:60]} | '
            f'قسم: {_val(row.get("current_dep_name"))} | '
            f'تاريخ: {_val(row.get("Date_Dep") or row.get("Date_Com"))}'
        )
    return '\n'.join(lines)


def _format_search(ctx, query):
    res = ctx.get('search_results') or {}
    inc = res.get('incoming') or []
    out = res.get('outgoing') or []
    q = (query or '').strip()
    if not q:
        return 'يُرجى إدخال كلمة أو رقم للبحث.'
    if not inc and not out:
        return f'لم يُعثر على معاملة مطابقة لـ «{q}» في قاعدة البيانات.'

    lines = [f'نتائج البحث عن «{q}»:', '']
    if inc:
        lines.append(f'— وارد ({len(inc)}):')
        for i, row in enumerate(inc, 1):
            lines.append(
                f'{i}. رقم {_val(row.get("NoBook_In"))} | '
                f'وارد دائرة {_val(row.get("NoBook_Dep"))} | '
                f'{_val(row.get("Subject_Com"))[:50]} | '
                f'حالة: {_val(row.get("Status"))}'
            )
        lines.append('')
    if out:
        lines.append(f'— صادر ({len(out)}):')
        for i, row in enumerate(out, 1):
            lines.append(
                f'{i}. رقم {_val(row.get("NoBook_Out"))} | '
                f'{_val(row.get("Subject"))[:50]} | '
                f'جهة: {_val(row.get("dest_name"))}'
            )
    return '\n'.join(lines)


def _format_stats(ctx):
    snap = ctx.get('snapshot') or {}
    totals = snap.get('totals') or {}
    org = snap.get('organization') or {}
    org_name = _val(org.get('Name'), 'الدائرة')
    overdue = ctx.get('overdue_incoming')
    overdue_n = len(overdue) if overdue is not None else None

    lines = [
        f'إحصائيات عامة — {org_name}:',
        f'• عدد الكتب الواردة: {totals.get("incoming", 0)}',
        f'• عدد الكتب الصادرة: {totals.get("outgoing", 0)}',
        f'• في طور العمل: {totals.get("in_progress", 0)}',
        f'• تم الانتهاء: {totals.get("completed", 0)}',
    ]
    if overdue_n is not None:
        lines.append(f'• متأخرة (أكثر من {snap.get("overdue_threshold_days", OVERDUE_DAYS)} يوماً): {overdue_n}')
    dept = ctx.get('by_department') or []
    if dept:
        top = dept[:5]
        lines.append('')
        lines.append('أكثر الأقسام (وارد):')
        for row in top:
            if int(row.get('cnt') or 0) > 0:
                lines.append(f'  — {_val(row.get("Dep_Name"))}: {row.get("cnt")} كتاب')
    lines.append('')
    lines.append('لتفاصيل كل قسم: «الكتب حسب القسم»')
    return '\n'.join(lines)


def _reply_for_intent(ctx, intent, user_message):
    if intent == 'help':
        return _format_help()
    if intent == 'guide':
        return _format_guide(ctx)
    if intent == 'incoming_count':
        return _format_incoming_count(ctx)
    if intent == 'outgoing_count':
        return _format_outgoing_count(ctx)
    if intent == 'in_progress':
        return _format_in_progress_count(ctx)
    if intent == 'completed':
        return _format_completed_count(ctx)
    if intent == 'departments':
        return _format_departments(ctx)
    if intent == 'pending_reply':
        return _format_pending_reply(ctx)
    if intent == 'recent':
        return _format_recent(ctx)
    if intent == 'stats_all':
        return _format_stats(ctx)
    if intent == 'summarize':
        return _format_summarize(ctx)
    if intent == 'overdue':
        return _format_overdue(ctx)
    if intent == 'search':
        return _format_search(ctx, user_message)
    return None


def _format_general(ctx, user_message, action):
    intent = _resolve_intent(user_message, action)
    if intent:
        reply = _reply_for_intent(ctx, intent, user_message)
        if reply:
            return reply

    msg = (user_message or '').strip()
    cur = ctx.get('current_book')
    if cur:
        return (
            _format_summarize(ctx)
            + '\n\n'
            + 'أسئلة سريعة: «إحصائيات عامة»، «الكتب حسب القسم»، «ما هي الكتب المتأخرة؟»'
        )

    return _format_help()


def respond_local(context_payload, user_message, action=None):
    """يرد من قاعدة البيانات محلياً — لا يتطلب إنترنتاً ولا Ollama ولا API."""
    ctx = dict(context_payload or {})
    intent = _resolve_intent(user_message, action)

    if intent == 'overdue' and 'overdue_incoming' not in ctx:
        conn = _db()
        ctx['overdue_incoming'] = _overdue_incoming(conn)
        conn.close()
    if intent == 'departments' and 'by_department' not in ctx:
        conn = _db()
        ctx['by_department'] = _books_by_department(conn)
        conn.close()
    if intent == 'pending_reply' and 'pending_reply' not in ctx:
        conn = _db()
        ctx['pending_reply'] = _incoming_without_outgoing_reply(conn)
        conn.close()
    if intent == 'recent' and 'recent_incoming' not in ctx:
        conn = _db()
        ctx['recent_incoming'] = _recent_incoming(conn)
        ctx['recent_outgoing'] = _recent_outgoing(conn)
        conn.close()
    if intent == 'search' and 'search_results' not in ctx:
        conn = _db()
        ctx['search_results'] = _search_books(conn, user_message)
        conn.close()
    if intent == 'stats_all' and 'overdue_incoming' not in ctx:
        conn = _db()
        ctx['overdue_incoming'] = _overdue_incoming(conn, limit=5)
        ctx['by_department'] = _books_by_department(conn)
        conn.close()

    return _format_general(ctx, user_message, action)


@y_ai_bp.route('/api/y-ai/config/status', methods=['GET'])
@api_login_required
def y_ai_config_status():
    return jsonify({
        'ok': True,
        'configured': True,
        'provider': 'local',
        'mode': 'offline',
        'engine': 'builtin',
        'requires_internet': False,
        'requires_ollama': False,
        'questions': PREDEFINED_QUESTIONS,
        'is_admin': session.get('role') == 'مدير',
    })


@y_ai_bp.route('/api/y-ai/config', methods=['POST'])
@api_login_required
@admin_api_required
def y_ai_config_save():
    return jsonify({
        'ok': True,
        'configured': True,
        'message': 'Y-ai يعمل محلياً داخل البرنامج ولا يحتاج مفتاح API أو إنترنت.',
    })


@y_ai_bp.route('/api/y-ai/chat', methods=['POST'])
@api_login_required
def y_ai_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()
    action = (data.get('action') or '').strip() or None
    page_ctx = data.get('page') or {}

    if action in ('summarize', 'overdue') and not user_message:
        user_message = {
            'summarize': 'لخص هذا الكتاب',
            'overdue': 'ما هي الكتب المتأخرة؟',
        }.get(action, user_message)

    silent_actions = (
        'overdue', 'stats', 'stats_all', 'incoming_count', 'outgoing_count',
        'in_progress', 'completed', 'departments', 'pending_reply', 'recent', 'help', 'guide',
    )
    if not user_message and action in silent_actions:
        user_message = {
            'stats': 'إحصائيات عامة',
            'stats_all': 'إحصائيات عامة',
            'incoming_count': 'كم عدد الكتب الواردة؟',
            'outgoing_count': 'كم عدد الكتب الصادرة؟',
            'in_progress': 'كم كتاب في طور العمل؟',
            'completed': 'كم كتاب تم الانتهاء؟',
            'departments': 'الكتب حسب القسم',
            'pending_reply': 'وارد بلا رد صادر',
            'recent': 'آخر الكتب',
            'help': 'مساعدة',
            'guide': 'أرشدني',
        }.get(action, user_message)

    if not user_message and action not in silent_actions:
        return jsonify({'ok': False, 'error': 'يرجى إدخال رسالة.'}), 400

    if action == 'summarize' and not page_ctx.get('bookId'):
        return jsonify({
            'ok': False,
            'error': 'افتح صفحة كتاب وارد أو صادر محدد لتفعيل «لخص هذا الكتاب».',
        }), 400

    try:
        context_payload = build_context_payload(action, page_ctx, user_message)
        reply = respond_local(context_payload, user_message, action)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'تعذر معالجة الطلب: {e}'}), 500

    return jsonify({
        'ok': True,
        'reply': reply,
        'context_meta': {
            'has_current_book': bool(context_payload.get('current_book')),
            'overdue_count': len(context_payload.get('overdue_incoming') or []),
            'engine': 'local',
        },
    })
