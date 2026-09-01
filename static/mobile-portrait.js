/**
 * كشف الوضع العمودي — Chrome Android / iOS
 */
(function () {
  'use strict';

  function viewportWidth() {
    if (window.visualViewport && window.visualViewport.width) {
      return window.visualViewport.width;
    }
    return document.documentElement.clientWidth || window.innerWidth || 0;
  }

  function viewportHeight() {
    if (window.visualViewport && window.visualViewport.height) {
      return window.visualViewport.height;
    }
    return document.documentElement.clientHeight || window.innerHeight || 0;
  }

  function isPortraitMobile() {
    var w = viewportWidth();
    var h = viewportHeight();
    if (w <= 767) return true;
    if (window.matchMedia('(max-width: 991px) and (orientation: portrait)').matches) {
      return true;
    }
    if (window.matchMedia('(max-width: 991px) and (max-aspect-ratio: 1/1)').matches) {
      return true;
    }
    return w <= 991 && h > w;
  }

  function applyPortraitClass() {
    document.documentElement.classList.toggle('is-portrait-mobile', isPortraitMobile());
  }

  applyPortraitClass();
  window.addEventListener('resize', applyPortraitClass);
  window.addEventListener('orientationchange', function () {
    setTimeout(applyPortraitClass, 50);
    setTimeout(applyPortraitClass, 300);
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', applyPortraitClass);
  }
  document.addEventListener('DOMContentLoaded', applyPortraitClass);
})();
