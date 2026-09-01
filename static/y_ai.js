/**
 * Y-ai — مساعد عائم للكتب الصادرة والواردة
 */
(function () {
  'use strict';

  var root = document.getElementById('y-ai-assistant');
  if (!root) return;

  var fab = root.querySelector('[data-yai-fab]');
  var panel = root.querySelector('[data-yai-panel]');
  var closeBtn = root.querySelector('[data-yai-close]');
  var messagesEl = root.querySelector('[data-yai-messages]');
  var inputEl = root.querySelector('[data-yai-input]');
  var sendBtn = root.querySelector('[data-yai-send]');
  var chipSummarize = root.querySelector('[data-yai-chip-summarize]');
  var chipOverdue = root.querySelector('[data-yai-chip-overdue]');
  var chipSearch = root.querySelector('[data-yai-chip-search]');
  var setupEl = root.querySelector('[data-yai-setup]');
  var apiKeyInput = root.querySelector('[data-yai-api-key]');
  var saveConfigBtn = root.querySelector('[data-yai-save-config]');
  var setupHint = root.querySelector('[data-yai-setup-hint]');

  var history = [];
  var busy = false;
  var apiConfigured = true;
  var drag = { active: false, moved: false, startX: 0, startY: 0, startLeft: 0, startBottom: 0 };

  function detectPageContext() {
    var path = window.location.pathname || '';
    var m = path.match(/\/incoming\/(\d+)/);
    if (m) {
      return { endpoint: 'incoming_view', bookType: 'in', bookId: parseInt(m[1], 10) };
    }
    m = path.match(/\/outgoing\/(\d+)/);
    if (m) {
      return { endpoint: 'outgoing_view', bookType: 'out', bookId: parseInt(m[1], 10) };
    }
    return {
      endpoint: root.getAttribute('data-endpoint') || '',
      bookType: null,
      bookId: null,
    };
  }

  function hasCurrentBook() {
    var ctx = detectPageContext();
    return !!(ctx.bookId && (ctx.bookType === 'in' || ctx.bookType === 'out'));
  }

  function updateChips() {
    if (chipSummarize) {
      chipSummarize.disabled = !hasCurrentBook();
      chipSummarize.title = chipSummarize.disabled
        ? 'افتح صفحة كتاب محدد لتفعيل التلخيص'
        : 'تلخيص الكتاب الحالي';
    }
  }

  function updateSetupVisibility() {
    if (!setupEl) return;
    if (apiConfigured) {
      setupEl.classList.add('yai-setup--done');
    } else {
      setupEl.classList.remove('yai-setup--done');
    }
  }

  function refreshConfigStatus() {
    return fetch('/api/y-ai/config/status', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (x) {
        if (x.ok && x.j && x.j.ok) {
          apiConfigured = x.j.mode === 'offline' || x.j.provider === 'local' || !!x.j.configured;
        } else {
          apiConfigured = true;
        }
        updateSetupVisibility();
        return apiConfigured;
      })
      .catch(function () {
        apiConfigured = true;
        updateSetupVisibility();
        return true;
      });
  }

  function saveApiKey() {
    if (!apiKeyInput || !saveConfigBtn) return;
    var key = (apiKeyInput.value || '').trim();
    if (!key) {
      if (setupHint) {
        setupHint.textContent = 'أدخل مفتاح OpenAI أولاً.';
        setupHint.classList.add('yai-setup__hint--err');
      }
      return;
    }
    saveConfigBtn.disabled = true;
    if (setupHint) {
      setupHint.textContent = 'جاري الحفظ…';
      setupHint.classList.remove('yai-setup__hint--err');
    }
    fetch('/api/y-ai/config', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ OPENAI_API_KEY: key, Y_AI_PROVIDER: 'openai', Y_AI_MODEL: 'gpt-4o' }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (x) {
        if (!x.ok || !x.j.ok) throw new Error((x.j && x.j.error) || 'فشل الحفظ');
        apiConfigured = true;
        apiKeyInput.value = '';
        updateSetupVisibility();
        if (setupHint) {
          setupHint.textContent = x.j.message || 'تم حفظ المفتاح. يمكنك السؤال الآن.';
          setupHint.classList.remove('yai-setup__hint--err');
        }
        appendMessage('bot', 'تم ضبط مفتاح OpenAI بنجاح. يمكنك طرح سؤالك الآن.');
      })
      .catch(function (e) {
        if (setupHint) {
          setupHint.textContent = (e && e.message) ? e.message : 'تعذر حفظ المفتاح.';
          setupHint.classList.add('yai-setup__hint--err');
        }
      })
      .finally(function () {
        saveConfigBtn.disabled = false;
      });
  }

  function openPanel() {
    root.classList.add('is-open');
    root.setAttribute('aria-expanded', 'true');
    if (messagesEl && !messagesEl.childElementCount) {
      showWelcomeMessage();
    }
    refreshConfigStatus().then(function () {
      if (!apiConfigured && setupEl) {
        if (apiKeyInput) apiKeyInput.focus();
      } else if (inputEl) {
        inputEl.focus();
      }
    });
  }

  function clearConversation() {
    history = [];
    if (messagesEl) messagesEl.innerHTML = '';
  }

  function showWelcomeMessage() {
    appendMessage(
      'bot',
      'مرحباً، أنا Y-ai — مرشد داخلي ومساعد بيانات نظامكم.\n' +
        'اسأل: «كيف أضيف وارد؟» · «أين الإعدادات؟» · «أرشدني» أو استخدم الأزرار السريعة للإحصائيات والبحث.'
    );
  }

  function closePanel() {
    root.classList.remove('is-open');
    root.setAttribute('aria-expanded', 'false');
    clearConversation();
  }

  function appendMessage(role, text, extraClass) {
    if (!messagesEl) return;
    var div = document.createElement('div');
    div.className = 'yai-msg yai-msg--' + (role === 'user' ? 'user' : 'bot') + (extraClass ? ' ' + extraClass : '');
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function setBusy(on) {
    busy = on;
    if (sendBtn) sendBtn.disabled = on;
    if (inputEl) inputEl.disabled = on;
    root.querySelectorAll('.yai-chip').forEach(function (c) {
      c.disabled = on || (c === chipSummarize && !hasCurrentBook());
    });
  }

  function sendChat(action, message) {
    if (busy) return;
    var text = (message || (inputEl && inputEl.value) || '').trim();
    if (!text && action !== 'overdue') return;

    if (action === 'summarize' && !hasCurrentBook()) {
      appendMessage('bot', 'افتح صفحة كتاب وارد أو صادر محدد لتفعيل «لخص هذا الكتاب».');
      return;
    }

    if (text) {
      appendMessage('user', text);
      history.push({ role: 'user', content: text });
    }
    if (inputEl) inputEl.value = '';

    var typing = appendMessage('bot', 'جاري المعالجة…', 'yai-msg--typing');
    setBusy(true);

    fetch('/api/y-ai/chat', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        message: text,
        action: action || null,
        page: detectPageContext(),
        history: history.slice(-8),
      }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (x) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        if (!x.ok || !x.j.ok) {
          throw new Error((x.j && x.j.error) || 'فشل الطلب');
        }
        var reply = x.j.reply || '';
        appendMessage('bot', reply);
        history.push({ role: 'assistant', content: reply });
      })
      .catch(function (e) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        var msg = (e && e.message) ? e.message : 'حدث خطأ غير متوقع.';
        if (/OPENAI_API_KEY|ANTHROPIC_API_KEY|y_ai_config\.json|متغيرات البيئة/.test(msg)) {
          msg =
            'يُرجى إغلاق البرنامج ثم تشغيله من جديد (Y-inout.bat) لتفعيل Y-ai المحلي دون مفتاح API.';
        }
        appendMessage('bot', msg);
      })
      .finally(function () {
        setBusy(false);
        updateChips();
      });
  }

  function onFabClick(e) {
    if (drag.moved) {
      drag.moved = false;
      return;
    }
    if (root.classList.contains('is-open')) {
      closePanel();
    } else {
      openPanel();
    }
  }

  function clampPosition() {
    var rect = root.getBoundingClientRect();
    var pad = 8;
    var maxLeft = window.innerWidth - rect.width - pad;
    var maxBottom = window.innerHeight - rect.height - pad;
    var style = window.getComputedStyle(root);
    var left = parseFloat(style.left) || 24;
    var bottom = parseFloat(style.bottom) || 24;
    if (left < pad) left = pad;
    if (bottom < pad) bottom = pad;
    if (left > maxLeft) left = Math.max(pad, maxLeft);
    if (bottom > maxBottom) bottom = Math.max(pad, maxBottom);
    root.style.left = left + 'px';
    root.style.bottom = bottom + 'px';
    root.style.right = 'auto';
    root.style.top = 'auto';
  }

  function startDrag(clientX, clientY) {
    var style = window.getComputedStyle(root);
    drag.active = true;
    drag.moved = false;
    drag.startX = clientX;
    drag.startY = clientY;
    drag.startLeft = parseFloat(style.left) || 24;
    drag.startBottom = parseFloat(style.bottom) || 24;
  }

  function moveDrag(clientX, clientY) {
    if (!drag.active) return;
    var dx = clientX - drag.startX;
    var dy = clientY - drag.startY;
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) drag.moved = true;
    root.style.left = drag.startLeft + dx + 'px';
    root.style.bottom = drag.startBottom - dy + 'px';
    root.style.right = 'auto';
    root.style.top = 'auto';
  }

  function endDrag() {
    if (!drag.active) return;
    drag.active = false;
    clampPosition();
  }

  if (fab) {
    fab.addEventListener('click', onFabClick);
    fab.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      fab.setPointerCapture(e.pointerId);
      startDrag(e.clientX, e.clientY);
    });
    fab.addEventListener('pointermove', function (e) {
      moveDrag(e.clientX, e.clientY);
    });
    fab.addEventListener('pointerup', endDrag);
    fab.addEventListener('pointercancel', endDrag);
  }

  if (closeBtn) closeBtn.addEventListener('click', closePanel);

  if (saveConfigBtn) saveConfigBtn.addEventListener('click', saveApiKey);
  if (apiKeyInput) {
    apiKeyInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        saveApiKey();
      }
    });
  }

  if (sendBtn) {
    sendBtn.addEventListener('click', function () {
      sendChat(null, null);
    });
  }

  if (inputEl) {
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat(null, null);
      }
    });
  }

  if (chipSummarize) {
    chipSummarize.addEventListener('click', function () {
      openPanel();
      sendChat('summarize', 'لخص هذا الكتاب');
    });
  }
  if (chipOverdue) {
    chipOverdue.addEventListener('click', function () {
      openPanel();
      sendChat('overdue', 'ما هي الكتب المتأخرة؟');
    });
  }
  if (chipSearch) {
    chipSearch.addEventListener('click', function () {
      openPanel();
      var q = (inputEl && inputEl.value.trim()) || '';
      if (!q) {
        appendMessage('bot', 'اكتب كلمة البحث في الحقل أدناه ثم اضغط إرسال.');
        if (inputEl) inputEl.focus();
        return;
      }
      sendChat('search', q);
    });
  }

  root.querySelectorAll('[data-yai-chip-action]').forEach(function (chip) {
    chip.addEventListener('click', function () {
      openPanel();
      var act = chip.getAttribute('data-yai-chip-action');
      var labels = {
        stats_all: 'إحصائيات عامة',
        departments: 'الكتب حسب القسم',
        overdue: 'ما هي الكتب المتأخرة؟',
        pending_reply: 'وارد بلا رد صادر',
        recent: 'آخر الكتب',
        help: 'مساعدة',
        guide: 'أرشدني',
      };
      sendChat(act, labels[act] || act);
    });
  });

  updateChips();
  updateSetupVisibility();
  refreshConfigStatus();

  window.addEventListener('resize', clampPosition);
})();
