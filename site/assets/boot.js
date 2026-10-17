/* Runs before first paint: theme from localStorage (or system), and the js flag. */
(function () {
  var d = document.documentElement;
  d.classList.add('js');
  try {
    var t = localStorage.getItem('krohnos-theme');
    if (t === 'dark' || t === 'light') d.setAttribute('data-theme', t);
  } catch (e) {}
})();
