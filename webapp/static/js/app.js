// PipelineProof: small progressive enhancements (no inline handlers; CSP allows 'self' scripts only)
(function () {
  function copy(btn) {
    var target = btn.hasAttribute("data-copy") ? document.getElementById(btn.getAttribute("data-copy")) : null;
    var text = btn.getAttribute("data-copy-text") || (target && (target.value || target.textContent));
    if (!text) return;
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text); });
    } else {
      fallback(text);
    }
    function fallback(t) {
      var ta = document.createElement("textarea");
      ta.value = t; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* user can copy manually */ }
      document.body.removeChild(ta);
    }
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy], [data-copy-text]");
    if (b) { e.preventDefault(); copy(b); }
  });
  document.addEventListener("change", function (e) {
    var input = e.target;
    if (input.matches && input.matches("input[type=file][data-label]")) {
      var label = document.getElementById(input.getAttribute("data-label"));
      if (label && input.files && input.files[0]) {
        var f = input.files[0];
        label.textContent = f.name + " (" + Math.max(1, Math.round(f.size / 1024)) + " KB)";
      }
    }
  });
  // show a busy state on forms that start long work
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("data-busy")) {
      var btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> ' + form.getAttribute("data-busy"); }
    }
  });
})();
