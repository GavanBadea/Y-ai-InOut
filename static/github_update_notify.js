/**
 * تنبيه تحديث GitHub للمدير:
 * - فحص دوري
 * - أيقونة + شارة عند توفر تحديث
 * - لوحة معلومات + تحديث الآن / ليس الآن
 */
(function () {
  'use strict';

  var POLL_MS = 5 * 60 * 1000;
  var STATUS_URL = '/admin/github-update/api/status';
  var DISMISS_URL = '/admin/github-update/api/dismiss';
  var APPLY_URL = '/admin/github-update/api/apply';
  var lastShaShown = '';
  var panelOpen = false;
  var applying = false;

  function el(id) { return document.getElementById(id); }

  function setHidden(node, hidden) {
    if (!node) return;
    if (hidden) node.setAttribute('hidden', '');
    else node.removeAttribute('hidden');
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function render(data) {
    var wrap = el('gh-update-wrap');
    var badge = el('gh-update-badge');
    var body = el('gh-update-body');
    var empty = el('gh-update-empty');
    if (!wrap || !body) return;

    var show = !!(data && data.show_badge && data.remote);
    wrap.classList.toggle('has-update', show);
    setHidden(badge, !show);

    if (!data || !data.configured) {
      setHidden(empty, false);
      empty.textContent = 'لم يُضبط مستودع GitHub بعد — افتح صفحة التحديث.';
      body.innerHTML = '';
      return;
    }

    if (data.error && !data.remote) {
      setHidden(empty, false);
      empty.textContent = data.error;
      body.innerHTML = '';
      return;
    }

    if (!data.update_available || !data.remote) {
      setHidden(empty, false);
      empty.textContent = show
        ? 'يتوفر تحديث'
        : 'لا يوجد تحديث جديد حالياً.';
      if (!show) {
        body.innerHTML = '';
        return;
      }
    }

    var r = data.remote || {};
    setHidden(empty, true);
    body.innerHTML =
      '<div class="gh-update-meta">' +
        '<div><span class="text-muted">المستودع</span><div dir="ltr" class="fw-700">' +
          esc(r.owner) + '/' + esc(r.repo) + '@' + esc(r.branch) +
        '</div></div>' +
        '<div><span class="text-muted">الإصدار البعيد</span><div dir="ltr"><code>' +
          esc(r.short_sha || r.sha) + '</code></div></div>' +
        '<div><span class="text-muted">المحلي</span><div dir="ltr"><code>' +
          esc(r.local_short || '—') + '</code></div></div>' +
        '<div><span class="text-muted">التاريخ</span><div>' +
          esc((r.date || '').slice(0, 19) || '—') +
          (r.author ? ' — ' + esc(r.author) : '') +
        '</div></div>' +
        '<div class="gh-update-msg"><span class="text-muted">ماذا في التحديث؟</span>' +
          '<div class="fw-700 mt-1">' + esc(r.message || '—') + '</div></div>' +
      '</div>' +
      '<div class="gh-update-actions mt-3">' +
        '<label class="form-label small mb-1">نطاق التحديث</label>' +
        '<select id="gh-update-scope" class="form-select form-select-sm mb-2">' +
          '<option value="both">واجهة + خلفية</option>' +
          '<option value="frontend">واجهة أمامية فقط</option>' +
          '<option value="backend">خلفية فقط</option>' +
        '</select>' +
        '<div class="d-flex flex-wrap gap-2">' +
          '<button type="button" class="btn btn-sm btn-success flex-grow-1" id="gh-update-apply">' +
            '<i class="fas fa-download me-1"></i>تحديث الآن</button>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" id="gh-update-later">' +
            'ليس الآن</button>' +
        '</div>' +
        '<a class="btn btn-sm btn-link w-100 mt-2" href="/admin/github-update/?gate=1">فتح صفحة الإعدادات</a>' +
        '<div class="small text-muted mt-2" id="gh-update-status-msg"></div>' +
      '</div>';

    var applyBtn = el('gh-update-apply');
    var laterBtn = el('gh-update-later');
    if (laterBtn) {
      laterBtn.addEventListener('click', function () {
        dismiss(r.sha || '');
      });
    }
    if (applyBtn) {
      applyBtn.addEventListener('click', function () {
        applyUpdate();
      });
    }
  }

  function setStatusMsg(text, isError) {
    var n = el('gh-update-status-msg');
    if (!n) return;
    n.textContent = text || '';
    n.style.color = isError ? '#ef4444' : 'var(--text-sub)';
  }

  function dismiss(sha) {
    fetch(DISMISS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ sha: sha || '' }),
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        panelOpen = false;
        setHidden(el('gh-update-panel'), true);
        render(data);
      })
      .catch(function () {
        setStatusMsg('تعذّر تأجيل التحديث', true);
      });
  }

  function applyUpdate() {
    if (applying) return;
    var scopeEl = el('gh-update-scope');
    var scope = scopeEl ? scopeEl.value : 'both';
    if (!window.confirm('تطبيق التحديث الآن (' + scope + ') ثم إعادة تشغيل البرنامج؟ يُفضّل نسخة احتياطية. سينقطع اتصال الأجهزة الأخرى حتى يعود الخادم.')) return;
    applying = true;
    setStatusMsg('جاري التنزيل والتطبيق...', false);
    var applyBtn = el('gh-update-apply');
    if (applyBtn) applyBtn.disabled = true;

    fetch(APPLY_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ scope: scope, install_deps: false }),
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        applying = false;
        if (!res.ok || !res.j.ok) {
          setStatusMsg((res.j && res.j.error) || 'فشل التحديث', true);
          if (applyBtn) applyBtn.disabled = false;
          return;
        }
        setStatusMsg('تم التطبيق. جاري إعادة تشغيل البرنامج... انتظر ثم ستُفتح صفحة الدخول.', false);
        var n = 0;
        var t = setInterval(function () {
          n += 1;
          fetch('/login', { credentials: 'omit', cache: 'no-store' })
            .then(function (r) {
              if (r && r.status) {
                clearInterval(t);
                window.location.href = '/login';
              }
            })
            .catch(function () {});
          if (n > 50) {
            clearInterval(t);
            setStatusMsg('أُعيد التشغيل. حدّث الصفحة يدوياً إن لم تُفتح صفحة الدخول.', false);
          }
        }, 1500);
      })
      .catch(function () {
        applying = false;
        if (applyBtn) applyBtn.disabled = false;
        setStatusMsg('تعذّر الاتصال أثناء التحديث', true);
      });
  }

  function poll(force) {
    var url = STATUS_URL + (force ? '?force=1' : '');
    fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('status');
        return r.json();
      })
      .then(function (data) {
        render(data);
        if (data.show_badge && data.remote && data.remote.sha && data.remote.sha !== lastShaShown) {
          lastShaShown = data.remote.sha;
          // افتح اللوحة تلقائياً عند ظهور تحديث جديد
          if (!panelOpen) {
            panelOpen = true;
            setHidden(el('gh-update-panel'), false);
          }
        }
      })
      .catch(function () { /* صامت */ });
  }

  function init() {
    var wrap = el('gh-update-wrap');
    var btn = el('gh-update-btn');
    var panel = el('gh-update-panel');
    if (!wrap || !btn || !panel) return;

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      panelOpen = !panelOpen;
      setHidden(panel, !panelOpen);
      if (panelOpen) poll(true);
    });

    document.addEventListener('click', function (e) {
      if (!panelOpen) return;
      if (wrap.contains(e.target)) return;
      panelOpen = false;
      setHidden(panel, true);
    });

    poll(true);
    setInterval(function () { poll(false); }, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
