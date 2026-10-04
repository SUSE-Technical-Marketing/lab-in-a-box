// Theme: "auto" follows the OS; the toggle cycles auto → light → dark. Stored per browser.
(function () {
  var KEY = 'lab-builder-theme', root = document.documentElement;
  function apply(t) { if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme'); }
  var saved = null; try { saved = localStorage.getItem(KEY); } catch (e) {}
  apply(saved);
  function label(t) { return t === 'light' ? 'Light' : t === 'dark' ? 'Dark' : 'Auto'; }
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('themeToggle'); if (!btn) return;
    var cur = saved || 'auto';
    btn.querySelector('.t-label').textContent = label(cur);
    btn.addEventListener('click', function () {
      cur = cur === 'auto' ? 'light' : cur === 'light' ? 'dark' : 'auto';
      apply(cur); btn.querySelector('.t-label').textContent = label(cur);
      try { localStorage.setItem(KEY, cur); } catch (e) {}
      document.dispatchEvent(new CustomEvent('themechange'));
    });
  });
})();
