/* ─── Theme Management ─── */
const THEME_KEY = 'yos_theme';
const VALID_THEMES = ['light', 'dark', 'glass'];

function applyTheme(theme) {
  if (!VALID_THEMES.includes(theme)) theme = 'light';
  document.documentElement.setAttribute('data-theme', theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (e) { /* private mode */ }
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}

/* ─── Sidebar Management ─── */
const SIDEBAR_KEY = 'yos_sidebar';

function getSidebar() {
  return document.getElementById('sidebar');
}

function applySidebarState(collapsed) {
  const sb = getSidebar();
  if (!sb) return;
  if (collapsed) {
    sb.classList.add('collapsed');
    localStorage.setItem(SIDEBAR_KEY, 'collapsed');
  } else {
    sb.classList.remove('collapsed');
    localStorage.setItem(SIDEBAR_KEY, 'open');
  }
  updateToggleIcon();
}

function toggleSidebar() {
  const sb = getSidebar();
  if (!sb) return;
  const isCollapsed = sb.classList.contains('collapsed');
  applySidebarState(!isCollapsed);
}

function updateToggleIcon() {
  const sb = getSidebar();
  const btn = document.getElementById('sidebar-toggle-btn');
  if (!sb || !btn) return;
  const collapsed = sb.classList.contains('collapsed');
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  btn.title = collapsed ? 'توسيع القائمة' : 'طي القائمة';
  const icon = btn.querySelector('i');
  if (icon) {
    icon.className = collapsed ? 'fas fa-angles-left' : 'fas fa-angles-right';
  }
  document.querySelectorAll('.sidebar-link .nav-label').forEach(function (label) {
    const link = label.closest('.sidebar-link');
    if (!link) return;
    link.title = collapsed ? label.textContent.trim() : '';
  });
}

/* ─── Init ─── */
document.addEventListener('DOMContentLoaded', function () {

  /* Apply saved theme */
  const savedTheme = localStorage.getItem(THEME_KEY) || 'light';
  applyTheme(savedTheme);

  /* Theme buttons */
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', function () {
      applyTheme(this.dataset.theme);
    });
  });

  /* Apply saved sidebar state */
  const savedSidebar = localStorage.getItem(SIDEBAR_KEY);
  if (savedSidebar === 'collapsed') {
    applySidebarState(true);
  } else {
    applySidebarState(false);
  }

  /* Sidebar toggle button */
  const toggleBtn = document.getElementById('sidebar-toggle-btn');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleSidebar();
    });
  }

  /* Mobile: close sidebar on overlay click */
  document.addEventListener('click', function (e) {
    const sb = getSidebar();
    if (!sb) return;
    if (window.innerWidth <= 768) {
      if (!sb.contains(e.target) && sb.classList.contains('mobile-open')) {
        sb.classList.remove('mobile-open');
      }
    }
  });

  /* Mobile hamburger */
  const mobileBtn = document.getElementById('mobile-menu-btn');
  if (mobileBtn) {
    mobileBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      const sb = getSidebar();
      if (sb) sb.classList.toggle('mobile-open');
    });
  }

  /* Clickable rows */
  document.querySelectorAll('.clickable-row').forEach(function (row) {
    row.addEventListener('click', function () {
      window.location.href = this.dataset.href;
    });
  });

  /* Sidebar nav buttons (prevents browser link preview on hover) */
  document.querySelectorAll('.js-nav-link[data-nav-url]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const url = this.dataset.navUrl || this.getAttribute('data-nav-url');
      if (url) window.location.href = url;
    });
  });

  /* File input drop zone label */
  document.querySelectorAll('input[type="file"]').forEach(function (input) {
    input.addEventListener('change', function () {
      if (this.files.length > 0) {
        const zone = this.closest('.scan-wrap')?.querySelector('.scan-drop-zone')
                  || document.querySelector('.scan-drop-zone');
        const label = zone?.querySelector('.drop-filename');
        if (label) label.textContent = this.files[0].name;
      }
    });
  });

  initUploadPickerMemory();
  initAttachmentSidePanel();

  /* Scanner device preference */
  const scannerInput = document.getElementById('scanner-device-name');
  if (scannerInput) {
    scannerInput.value = localStorage.getItem('scannerDevice') || '';
    scannerInput.addEventListener('change', function () {
      localStorage.setItem('scannerDevice', this.value);
    });
  }

  /* Confirm delete */
  document.querySelectorAll('form.confirm-delete').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!confirm('هل أنت متأكد من الحذف؟ لا يمكن التراجع عن هذا الإجراء.')) {
        e.preventDefault();
      }
    });
  });

});

/* ─── تذكّر مجلد اختيار الملفات (File System Access API) ─── */
const UPLOAD_FS_DB = 'yos_upload_fs_v1';
const UPLOAD_FS_STORE = 'handles';
const UPLOAD_FS_KEY = 'lastPicker';
let lastUploadPickerHandle = null;

const uploadAcceptTypes = [{
  description: 'PDF / صور',
  accept: {
    'application/pdf': ['.pdf'],
    'image/*': ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp']
  }
}];

function openUploadFsDb() {
  return new Promise(function (resolve, reject) {
    const req = indexedDB.open(UPLOAD_FS_DB, 1);
    req.onupgradeneeded = function () {
      req.result.createObjectStore(UPLOAD_FS_STORE);
    };
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { reject(req.error); };
  });
}

async function loadLastUploadPickerHandle() {
  if (!('indexedDB' in window)) return null;
  try {
    const db = await openUploadFsDb();
    return await new Promise(function (resolve) {
      const tx = db.transaction(UPLOAD_FS_STORE, 'readonly');
      const g = tx.objectStore(UPLOAD_FS_STORE).get(UPLOAD_FS_KEY);
      g.onsuccess = function () { resolve(g.result || null); };
      g.onerror = function () { resolve(null); };
    });
  } catch (e) {
    return null;
  }
}

async function saveLastUploadPickerHandle(handle) {
  if (!handle || !('indexedDB' in window)) return;
  try {
    const db = await openUploadFsDb();
    const tx = db.transaction(UPLOAD_FS_STORE, 'readwrite');
    tx.objectStore(UPLOAD_FS_STORE).put(handle, UPLOAD_FS_KEY);
  } catch (e) { /* ignore */ }
}

async function pickFileForInput(input) {
  if (!input) return;
  if (window.showOpenFilePicker) {
    try {
      const opts = { multiple: false, types: uploadAcceptTypes };
      if (lastUploadPickerHandle) opts.startIn = lastUploadPickerHandle;
      const handles = await window.showOpenFilePicker(opts);
      const handle = handles[0];
      const file = await handle.getFile();
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      lastUploadPickerHandle = handle;
      await saveLastUploadPickerHandle(handle);
      return;
    } catch (e) {
      if (e && e.name === 'AbortError') return;
    }
  }
  input.click();
}

window.pickFileForInput = pickFileForInput;

function initUploadPickerMemory() {
  loadLastUploadPickerHandle().then(function (h) {
    lastUploadPickerHandle = h;
  });
  document.querySelectorAll('.yos-pick-upload-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const id = btn.getAttribute('data-target-input');
      pickFileForInput(id ? document.getElementById(id) : null);
    });
  });
}

/* ─── معاينة المرفق في مستعرض جانبي ─── */
const ATTACHMENT_PDFJS_VERSION = '3.11.174';
const ATTACHMENT_PDFJS_CMAP_URL =
  'https://cdn.jsdelivr.net/npm/pdfjs-dist@' + ATTACHMENT_PDFJS_VERSION + '/cmaps/';
const ATTACHMENT_PDFJS_STANDARD_FONTS_URL =
  'https://cdn.jsdelivr.net/npm/pdfjs-dist@' + ATTACHMENT_PDFJS_VERSION + '/standard_fonts/';

let attachmentPdfDoc = null;
let attachmentPdfPageNum = 1;
let attachmentPreviewUrl = null;

function initAttachmentSidePanel() {
  const panel = document.getElementById('attachment-side-panel');
  const frame = document.getElementById('attachment-side-frame');
  const pdfWrap = document.getElementById('attachment-side-pdf-wrap');
  const pdfCanvas = document.getElementById('attachment-side-pdf-canvas');
  const img = document.getElementById('attachment-side-img');
  const btnPrint = panel ? panel.querySelector('[data-attachment-print]') : null;
  if (!panel || !frame) return;

  function setPrintEnabled(on) {
    if (btnPrint) btnPrint.disabled = !on;
  }

  function hideViewers() {
    frame.style.display = 'none';
    frame.removeAttribute('src');
    if (pdfWrap) pdfWrap.style.display = 'none';
    attachmentPdfDoc = null;
    attachmentPdfPageNum = 1;
    if (img) {
      img.style.display = 'none';
      img.removeAttribute('src');
    }
  }

  function closePanel() {
    panel.classList.remove('is-open');
    panel.setAttribute('aria-hidden', 'true');
    attachmentPreviewUrl = null;
    setPrintEnabled(false);
    hideViewers();
  }

  function printAttachment() {
    if (!attachmentPreviewUrl) return;
    const url = attachmentPreviewUrl;
    const lower = url.split('?')[0].toLowerCase();
    const isImg = /\.(png|jpe?g|gif|webp|bmp|tiff?)$/.test(lower);

    if (isImg && img && img.src) {
      const w = window.open('', '_blank');
      if (!w) return;
      w.document.write(
        '<html dir="rtl"><head><title>طباعة</title><style>body{margin:0;text-align:center}img{max-width:100%}</style></head><body><img src="' +
          img.src.replace(/"/g, '&quot;') +
          '" onload="window.print()"></body></html>'
      );
      w.document.close();
      return;
    }

    const isPdf = /\.pdf$/.test(lower);
    if (isPdf || (frame && frame.style.display !== 'none' && frame.src)) {
      const printFrame = document.createElement('iframe');
      printFrame.className = 'attachment-print-frame';
      printFrame.setAttribute('aria-hidden', 'true');
      printFrame.src = url;
      document.body.appendChild(printFrame);
      printFrame.onload = function () {
        try {
          printFrame.contentWindow.focus();
          printFrame.contentWindow.print();
        } catch (e) {
          window.open(url, '_blank');
        }
        setTimeout(function () {
          printFrame.remove();
        }, 2000);
      };
      return;
    }

    if (attachmentPdfDoc && pdfCanvas && pdfWrap && pdfWrap.style.display !== 'none') {
      const w = window.open('', '_blank');
      if (!w) return;
      w.document.write(
        '<html dir="rtl"><head><title>طباعة</title><style>body{margin:0;text-align:center}img{max-width:100%}</style></head><body><img src="' +
          pdfCanvas.toDataURL('image/png') +
          '" onload="window.print()"></body></html>'
      );
      w.document.close();
      return;
    }

    const printFrame = document.createElement('iframe');
    printFrame.className = 'attachment-print-frame';
    printFrame.setAttribute('aria-hidden', 'true');
    printFrame.src = url;
    document.body.appendChild(printFrame);
    printFrame.onload = function () {
      try {
        printFrame.contentWindow.focus();
        printFrame.contentWindow.print();
      } catch (e) {
        window.open(url, '_blank');
      }
      setTimeout(function () {
        printFrame.remove();
      }, 2000);
    };
  }

  function openPanelShell() {
    panel.classList.add('is-open');
    panel.setAttribute('aria-hidden', 'false');
  }

  function openWithViewer(mode, src) {
    hideViewers();
    openPanelShell();
    if (mode === 'img' && img) {
      img.style.display = 'block';
      img.src = src;
    } else {
      frame.style.display = 'block';
      frame.src = src;
    }
  }

  function updatePdfPageInfo() {
    const info = panel.querySelector('[data-pdf-page-info]');
    if (!info || !attachmentPdfDoc) return;
    info.textContent = attachmentPdfPageNum + ' / ' + attachmentPdfDoc.numPages;
  }

  function renderPdfPage() {
    if (!attachmentPdfDoc || !pdfCanvas) return Promise.resolve();
    return attachmentPdfDoc.getPage(attachmentPdfPageNum).then(function (page) {
      const parent = pdfCanvas.parentElement;
      const maxW = parent ? parent.clientWidth - 16 : 600;
      const baseVp = page.getViewport({ scale: 1 });
      const scale = Math.min(2, Math.max(0.5, maxW / baseVp.width));
      const outputScale = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale: scale * outputScale });
      pdfCanvas.width = viewport.width;
      pdfCanvas.height = viewport.height;
      pdfCanvas.style.width = Math.floor(viewport.width / outputScale) + 'px';
      pdfCanvas.style.height = Math.floor(viewport.height / outputScale) + 'px';
      return page.render({
        canvasContext: pdfCanvas.getContext('2d', { alpha: false }),
        viewport: viewport,
      }).promise;
    }).then(updatePdfPageInfo);
  }

  function showPdfJs(url) {
    if (typeof pdfjsLib === 'undefined' || !pdfWrap || !pdfCanvas) {
      openWithViewer('frame', url);
      return;
    }
    hideViewers();
    openPanelShell();
    pdfWrap.style.display = 'flex';
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/' + ATTACHMENT_PDFJS_VERSION + '/pdf.worker.min.js';

    fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('تعذر تحميل الملف');
        return r.arrayBuffer();
      })
      .then(function (buf) {
        return pdfjsLib.getDocument({
          data: buf,
          cMapUrl: ATTACHMENT_PDFJS_CMAP_URL,
          cMapPacked: true,
          standardFontDataUrl: ATTACHMENT_PDFJS_STANDARD_FONTS_URL,
          useSystemFonts: true,
        }).promise;
      })
      .then(function (pdf) {
        attachmentPdfDoc = pdf;
        attachmentPdfPageNum = 1;
        return renderPdfPage();
      })
      .catch(function () {
        hideViewers();
        openWithViewer('frame', url);
      });
  }

  function showPreview(url) {
    attachmentPreviewUrl = url || null;
    setPrintEnabled(!!attachmentPreviewUrl);
    const lower = (url || '').split('?')[0].toLowerCase();
    const isImg = /\.(png|jpe?g|gif|webp|bmp|tiff?)$/.test(lower);
    const isPdf = /\.pdf$/.test(lower);

    if (isPdf) {
      showPdfJs(url);
      return;
    }
    if (isImg) {
      openWithViewer('img', url);
      return;
    }
    openWithViewer('frame', url);
  }

  const btnPrev = panel.querySelector('[data-pdf-prev]');
  const btnNext = panel.querySelector('[data-pdf-next]');
  if (btnPrev) {
    btnPrev.addEventListener('click', function () {
      if (!attachmentPdfDoc || attachmentPdfPageNum <= 1) return;
      attachmentPdfPageNum -= 1;
      renderPdfPage();
    });
  }
  if (btnNext) {
    btnNext.addEventListener('click', function () {
      if (!attachmentPdfDoc || attachmentPdfPageNum >= attachmentPdfDoc.numPages) return;
      attachmentPdfPageNum += 1;
      renderPdfPage();
    });
  }

  document.body.addEventListener('click', function (e) {
    const link = e.target.closest('.js-attachment-side-view');
    if (!link || !link.href) return;
    e.preventDefault();
    e.stopPropagation();
    showPreview(link.href);
  });

  panel.querySelectorAll('[data-attachment-side-close]').forEach(function (el) {
    el.addEventListener('click', closePanel);
  });

  if (btnPrint) {
    btnPrint.addEventListener('click', printAttachment);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && panel.classList.contains('is-open')) closePanel();
  });
}
