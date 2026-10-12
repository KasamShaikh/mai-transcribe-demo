/* Theme switch: "copilot" (default, purple-blue) or "classic" (burgundy), remembered per browser. */
(function () {
  var KEY = "mai-theme";
  var root = document.documentElement;

  function get() {
    try { return localStorage.getItem(KEY) || "copilot"; } catch (e) { return "copilot"; }
  }
  function apply(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch (e) {}
  }

  // Apply immediately (script is in <head>) so the page never flashes the wrong colours.
  apply(get());

  // Add the floating toggle once the body exists.
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-toggle";
    function label() {
      var next = get() === "copilot" ? "Classic" : "Copilot";
      btn.innerHTML = '<span class="swatch"></span>Theme: switch to ' + next;
    }
    btn.addEventListener("click", function () {
      apply(get() === "copilot" ? "classic" : "copilot");
      label();
    });
    label();
    document.body.appendChild(btn);
  });
})();
