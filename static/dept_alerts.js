/**
 * تنبيهات موظف القسم: كتب واردة وصلت ولم تُفتح بعد.
 * - شارة داخل البرنامج
 * - إشعار متصفح
 * - صوت هادئ كل 5 دقائق حتى الفتح
 */
(function () {
  'use strict';

  var POLL_MS = 60000;
  var SOUND_MS = 5 * 60 * 1000;
  var lastSoundAt = 0;
  var lastCount = -1;
  var panelOpen = false;
  var STORE_COUNT = 'yai-dept-last-count';
  var STORE_SOUND = 'yai-dept-last-sound';

  try {
    var sc = sessionStorage.getItem(STORE_COUNT);
    if (sc !== null) lastCount = parseInt(sc, 10);
    var ss = sessionStorage.getItem(STORE_SOUND);
    if (ss) lastSoundAt = parseInt(ss, 10) || 0;
  } catch (e) {}

  function el(id) { return document.getElementById(id); }

  function playSoftChime() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      var now = ctx.currentTime;
      function tone(freq, start, dur, vol) {
        var o = ctx.createOscillator();
        var g = ctx.createGain();
        o.type = 'sine';
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, now + start);
        g.gain.exponentialRampToValueAtTime(vol, now + start + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, now + start + dur);
        o.connect(g);
        g.connect(ctx.destination);
        o.start(now + start);
        o.stop(now + start + dur + 0.05);
      }
      // نغمة قصيرة هادئة (نقرتان خفيفتان)
      tone(523.25, 0, 0.18, 0.045);
      tone(659.25, 0.16, 0.22, 0.035);
      setTimeout(function () {
        try { ctx.close(); } catch (e) {}
      }, 800);
    } catch (e) { /* ignore */ }
  }

  function ensureNotifyPermission() {
    if (!('Notification' in window)) return Promise.resolve('denied');
    if (Notification.permission === 'granted' || Notification.permission === 'denied') {
      return Promise.resolve(Notification.permission);
    }
    return Notification.requestPermission();
  }

    function showBrowserNotify(count, books, mode) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    if (!count) return;
    var first = books && books[0];
    var title = mode === 'admin'
      ? 'كتاب تم استلامه'
      : 'كتاب جديد في قسمك';
    var body = count === 1 && first
      ? ('رقم ' + first.no + ' — ' + first.subject)
      : ('لديك ' + count + ' كتاباً لم تُفتح بعد');
    try {
      var n = new Notification(title, {
        body: body,
        tag: 'yai-dept-unread',
        renotify: true,
        silent: true,
        dir: 'rtl',
        lang: 'ar',
      });
      n.onclick = function () {
        window.focus();
        if (first && first.url) window.location.href = first.url;
        n.close();
      };
    } catch (e) { /* ignore */ }
  }

  function renderPanel(data) {
    var list = el('dept-alert-list');
    var empty = el('dept-alert-empty');
    var badge = el('dept-alert-badge');
    var wrap = el('dept-alert-wrap');
    if (!list || !badge || !wrap) return;

    var count = data.count || 0;
    if (count > 0) {
      badge.hidden = false;
      badge.textContent = count > 99 ? '99+' : String(count);
      wrap.classList.add('has-alerts');
    } else {
      badge.hidden = true;
      badge.textContent = '0';
      wrap.classList.remove('has-alerts');
    }

    list.innerHTML = '';
    if (!count) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    (data.books || []).forEach(function (b) {
      var a = document.createElement('a');
      a.className = 'dept-alert-item';
      a.href = b.url;
      a.innerHTML =
        '<div class="dept-alert-item__no">' + escapeHtml(String(b.no)) + '</div>' +
        '<div class="dept-alert-item__sub">' + escapeHtml(b.subject || '') + '</div>' +
        (b.date ? '<div class="dept-alert-item__date">' + escapeHtml(b.date) + '</div>' : '');
      list.appendChild(a);
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function maybeAlert(data) {
    var count = data.count || 0;
    var now = Date.now();
    if (count <= 0) {
      lastCount = 0;
      try { sessionStorage.setItem(STORE_COUNT, '0'); } catch (e) {}
      return;
    }
    var isNew = lastCount >= 0 && count > lastCount;
    var dueSound = !lastSoundAt || (now - lastSoundAt) >= SOUND_MS;
    if (isNew || dueSound || lastCount === -1) {
      playSoftChime();
      showBrowserNotify(count, data.books, data.mode);
      lastSoundAt = now;
    }
    lastCount = count;
    try {
      sessionStorage.setItem(STORE_COUNT, String(lastCount));
      sessionStorage.setItem(STORE_SOUND, String(lastSoundAt));
    } catch (e) {}
  }

  function fetchAlerts() {
    return fetch('/api/dept-alerts', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.enabled) return;
        if (data.poll_interval_sec) POLL_MS = data.poll_interval_sec * 1000;
        if (data.sound_interval_sec) SOUND_MS = data.sound_interval_sec * 1000;
        renderPanel(data);
        maybeAlert(data);
      })
      .catch(function () { /* ignore */ });
  }

  function init() {
    var wrap = el('dept-alert-wrap');
    if (!wrap) return;
    var btn = el('dept-alert-btn');
    var panel = el('dept-alert-panel');
    var enableBtn = el('dept-alert-enable-notify');

    if (btn && panel) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        panelOpen = !panelOpen;
        panel.hidden = !panelOpen;
        if (panelOpen) {
          ensureNotifyPermission();
          fetchAlerts();
        }
      });
      document.addEventListener('click', function () {
        if (!panelOpen) return;
        panelOpen = false;
        panel.hidden = true;
      });
      panel.addEventListener('click', function (e) { e.stopPropagation(); });
    }

    if (enableBtn) {
      enableBtn.addEventListener('click', function () {
        ensureNotifyPermission().then(function (p) {
          var st = el('dept-alert-perm-status');
          if (st) {
            st.textContent = p === 'granted'
              ? 'إشعارات المتصفح مفعّلة'
              : (p === 'denied' ? 'تم رفض الإشعارات من المتصفح' : 'بانتظار الإذن');
          }
          if (p === 'granted') playSoftChime();
        });
      });
    }

    fetchAlerts();
    setInterval(fetchAlerts, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
