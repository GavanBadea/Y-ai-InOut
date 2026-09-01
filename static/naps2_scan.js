/**
 * تحديث قائمة uploads/scans وربط ملف بالكتاب الحالي.
 */
(function () {
  function parseJsonResponse(r) {
    return r.text().then(function (t) {
      var j = null;
      try {
        j = t ? JSON.parse(t) : {};
      } catch (e) {
        var msg = 'تعذر قراءة رد الخادم';
        if (r.status === 401) msg = 'يجب تسجيل الدخول ثم إعادة المحاولة';
        else if (r.status === 404) msg = 'مسار المسح غير موجود — أعد تشغيل البرنامج بعد التحديث';
        else if (r.status >= 500) msg = 'خطأ في الخادم أثناء المسح';
        else if ((t || '').charAt(0) === '<') msg = 'الخادم أعاد صفحة وليس بيانات المسح';
        throw new Error(msg);
      }
      return { r: r, j: j || {} };
    });
  }

  function fmtTime(ts) {
    if (!ts) return '';
    try {
      return new Date(ts * 1000).toLocaleString('ar');
    } catch (e) {
      return '';
    }
  }

  function wirePanel(panel) {
    var listUrl = panel.getAttribute('data-list-url');
    var attachUrl = panel.getAttribute('data-attach-url');
    var category = panel.getAttribute('data-category') || '';
    var sel = panel.querySelector('[data-naps2-select]');
    var btnRef = panel.querySelector('[data-naps2-refresh]');
    var btnAtt = panel.querySelector('[data-naps2-attach]');
    var st = panel.querySelector('[data-naps2-status]');
    var confirm = panel.querySelector('[data-naps2-confirm]');
    var runBtn = panel.querySelector('[data-naps2-run]');

    function setStatus(t, err) {
      if (!st) return;
      st.textContent = t || '';
      st.classList.toggle('text-danger', !!err);
    }

    function setConfirm(html, isErr) {
      if (!confirm) return;
      confirm.innerHTML = html || '';
      confirm.classList.toggle('text-danger', !!isErr);
    }

    function guessCategory() {
      if (category) return category;
      if (attachUrl && attachUrl.indexOf('/incoming/') >= 0) return 'InBook';
      if (attachUrl && attachUrl.indexOf('/outgoing/') >= 0) return 'OutBook';
      return 'InBook';
    }

    /**
     * رقم السجل الداخلي فقط (NoBook_In / NoBook_Out) — لا نستخدم أرقام الجهة أو وارد الدائرة
     * كمعرف للمسح؛ وإلا يعيد الخادم not_found.
     */
    function getPrimaryRecordIdForScan() {
      var pk = (panel.getAttribute('data-record-pk') || '').trim();
      if (pk) return pk;
      var root = panel.closest('form') || document;
      var candidates = ['input[name="NoBook_In"]', 'input[name="NoBook_Out"]'];
      for (var i = 0; i < candidates.length; i++) {
        var el = root.querySelector(candidates[i]) || document.querySelector(candidates[i]);
        if (!el) continue;
        var v = (el.value || '').trim();
        if (v) return v;
      }
      return '';
    }

    function getRelationInboundId() {
      var inp = document.querySelector('input[name="Reply_To_InBook_No"]');
      if (inp && (inp.value || '').trim()) return (inp.value || '').trim();
      return (panel.getAttribute('data-relation-id') || '').trim();
    }

    function getDepartmentNameForScan() {
      var fromPanel = (panel.getAttribute('data-department-name') || '').trim();
      if (fromPanel) return fromPanel;
      var sel = document.querySelector('select[name="Current_Dep_ID"]');
      if (sel && sel.options && sel.selectedIndex >= 0) {
        var t = (sel.options[sel.selectedIndex].text || '').trim();
        if (t && t.indexOf('—') !== 0) return t;
      }
      return '';
    }

    function listUrlWithCategory() {
      var cat = guessCategory();
      if (!listUrl) return '';
      if (listUrl.indexOf('?') >= 0) return listUrl + '&category=' + encodeURIComponent(cat);
      return listUrl + '?category=' + encodeURIComponent(cat);
    }

    function refreshList() {
      if (!sel) return;
      var u = listUrlWithCategory();
      if (!u) return;
      setStatus('جاري التحديث…');
      fetch(u, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(parseJsonResponse)
        .then(function (x) {
          var j = x.j;
          if (!j.ok) throw new Error(j.message || 'فشل القائمة');
          sel.innerHTML = '<option value="">— اختر ملفاً —</option>';
          (j.files || []).forEach(function (f) {
            var o = document.createElement('option');
            o.value = f.name;
            o.textContent = f.name + (f.mtime ? ' — ' + fmtTime(f.mtime) : '');
            sel.appendChild(o);
          });
          setStatus('عدد الملفات: ' + (j.files ? j.files.length : 0));
        })
        .catch(function (e) {
          setStatus(e.message || String(e), true);
        });
    }

    function finalizeLatestScan() {
      var cat = guessCategory();
      var rid = getPrimaryRecordIdForScan();
      if (!rid) {
        setConfirm('احفظ الكتاب أولاً ليُنشأ رقمٌ داخلي، ثم أعد المحاولة.', true);
        return Promise.resolve(null);
      }
      if (!/^\d+$/.test(String(rid))) {
        setConfirm('رقم الكتاب الداخلي يجب أن يكون أرقاماً فقط.', true);
        return Promise.resolve(null);
      }

      var linkDb = !!attachUrl; // only when we have a real record page
      var url = '/scan_document/' + encodeURIComponent(cat) + '/' + encodeURIComponent(rid);
      if (linkDb) url += '?link_db=1';

      var depId = '';
      var depSel = document.querySelector('select[name="Current_Dep_ID"]');
      if (depSel && depSel.value) depId = depSel.value;
      var depName = getDepartmentNameForScan();
      var relationId = getRelationInboundId();

      setConfirm('جاري المسح عبر السيرفر…', false);
      return fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          dep: depId,
          department_name: depName,
          relation_id: relationId,
        }),
      })
        .then(parseJsonResponse)
        .then(function (x) {
          if (!x.r.ok || !x.j.ok) throw new Error(x.j.message || x.j.error || 'فشل الربط');
          var rel = x.j.rel_path || '';
          var fn = x.j.filename || '';
          if (rel) {
            var href = '/uploads/' + rel;
            setConfirm(
              '<a href="' + href + '" target="_blank" class="text-decoration-none">' +
                '<span style="display:inline-flex;align-items:center;gap:6px">' +
                  '<span style="width:18px;height:18px;display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(220,38,38,.25);border-radius:4px;color:#dc2626;font-weight:800">PDF</span>' +
                  '<span style="color:var(--text-sub)">' + (fn || rel) + '</span>' +
                '</span>' +
              '</a>',
              false
            );
            refreshList();
            if (attachUrl) window.location.reload();
          } else {
            setConfirm('تم الحفظ، لكن لم يصل مسار الملف.', false);
          }
          return x.j;
        })
        .catch(function (e) {
          setConfirm((e && e.message) ? e.message : String(e), true);
          return null;
        });
    }

    // (كان هناك Polling لالتقاط ملف محفوظ يدوياً؛ لم نعد نحتاجه بعد تفعيل المسح عبر السيرفر)

    if (sel) {
      sel.addEventListener('change', function () {
        if (btnAtt) btnAtt.disabled = !sel.value;
      });
    }

    if (btnRef) btnRef.addEventListener('click', refreshList);

    if (btnAtt && attachUrl) {
      btnAtt.addEventListener('click', function () {
        var fn = sel.value;
        if (!fn) return;
        setStatus('جاري الربط…');
        fetch(attachUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ filename: fn }),
        })
          .then(parseJsonResponse)
          .then(function (x) {
            if (!x.r.ok || !x.j.ok) throw new Error(x.j.message || x.j.error || 'رفض الخادم');
            setStatus('تم الربط: ' + (x.j.rel_path || x.j.filename));
            window.location.reload();
          })
          .catch(function (e) {
            setStatus(e.message || String(e), true);
          });
      });
    }

    if (runBtn) {
      runBtn.addEventListener('click', function (e) {
        if (e && e.preventDefault) e.preventDefault();
        setConfirm('', false);
        finalizeLatestScan();
      });
    }

    var saveUrl = panel.getAttribute('data-save-scan-url');
    var btnUpload = panel.querySelector('[data-naps2-upload-file]');
    var inpUpload = panel.querySelector('[data-naps2-upload-input]');
    if (btnUpload && inpUpload && saveUrl) {
      btnUpload.addEventListener('click', function () {
        if (window.pickFileForInput) {
          window.pickFileForInput(inpUpload);
        } else {
          inpUpload.click();
        }
      });
      inpUpload.addEventListener('change', function () {
        if (!inpUpload.files || !inpUpload.files[0]) return;
        var fd = new FormData();
        fd.append('file', inpUpload.files[0]);
        setStatus('جاري رفع الملف…');
        fetch(saveUrl, {
          method: 'POST',
          credentials: 'same-origin',
          body: fd,
        })
          .then(parseJsonResponse)
          .then(function (x) {
            if (!x.r.ok || !x.j.ok) throw new Error(x.j.message || x.j.error || 'فشل الرفع');
            setStatus('تم رفع الملف: ' + (x.j.filename || ''));
            window.location.reload();
          })
          .catch(function (e) {
            setStatus(e.message || String(e), true);
          });
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-naps2-panel]').forEach(wirePanel);
  });

  /** لحقول رفع الملفات في نفس الصفحة. */
  window.onScanFileSelected = function (input, badgeId, labelId) {
    var badge = document.getElementById(badgeId);
    var label = document.getElementById(labelId);
    if (!badge || !label) return;
    if (input.files && input.files[0]) {
      label.textContent = input.files[0].name;
      badge.style.display = '';
    }
  };
})();
