/* GXSUITE-T2: wire refresh button only. Status pill is missing. */
(function () {
  const btn = document.querySelector('[data-testid="refresh-btn"]');
  if (btn) {
    btn.addEventListener("click", function () {
      btn.textContent = "Refreshed";
    });
  }
})();
