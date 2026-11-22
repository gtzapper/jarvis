// Applies the saved theme before the page draws. The choice is per browser (kept in localStorage).
(function () {
  var THEMES = [
    { id: "system", name: "System", bg: "linear-gradient(135deg,#f4f5f7 50%,#0f1216 50%)", accent: "#3b82f6" },
    { id: "light", name: "Light", bg: "#f4f5f7", panel: "#ffffff", accent: "#2563eb" },
    { id: "dark", name: "Dark", bg: "#0f1216", panel: "#171b21", accent: "#3b82f6" },
    { id: "midnight", name: "Midnight", bg: "#070b14", panel: "#0d1424", accent: "#22d3ee" },
    { id: "ember", name: "Ember", bg: "#140f0c", panel: "#1c1612", accent: "#f97316" },
    { id: "forest", name: "Forest", bg: "#0c130f", panel: "#121b16", accent: "#22c55e" },
    { id: "contrast", name: "High contrast", bg: "#000000", panel: "#000000", accent: "#ffd400" }
  ];
  var BG = { light: "#f4f5f7", dark: "#0f1216", midnight: "#070b14", ember: "#140f0c", forest: "#0c130f", contrast: "#000000" };
  var dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try { return localStorage.getItem("jarvis.theme") || "system"; } catch (e) { return "system"; }
  }
  function apply(id) {
    var real = id === "system" ? (dark && dark.matches ? "dark" : "light") : id;
    if (!BG[real]) real = "dark";
    document.documentElement.setAttribute("data-theme", real);
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i++) { metas[i].setAttribute("content", BG[real]); metas[i].removeAttribute("media"); }
  }
  apply(saved());
  if (dark && dark.addEventListener) dark.addEventListener("change", function () { if (saved() === "system") apply("system"); });
  window.JarvisTheme = {
    list: THEMES,
    get: saved,
    set: function (id) { try { localStorage.setItem("jarvis.theme", id); } catch (e) {} apply(id); }
  };
})();
