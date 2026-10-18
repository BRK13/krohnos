/* Runs before first paint: theme from localStorage (or system), and the js flag. */
(function () {
  var d = document.documentElement;
  d.classList.add('js');
  try {
    var t = localStorage.getItem('krohnos-theme');
    d.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
  } catch (e) {}
})();
