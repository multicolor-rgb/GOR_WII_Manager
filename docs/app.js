(function () {
  "use strict";

  var STORAGE_KEY = "gor-wii-manager-lang";
  var titles = {
    en: "GOR Wii Manager — Wii & GameCube backups on Linux",
    pl: "GOR Wii Manager — kopie gier Wii i GameCube na Linuksie"
  };

  function applyLanguage(lang) {
    if (lang !== "pl") lang = "en";
    document.documentElement.lang = lang;
    document.title = titles[lang];

    var nodes = document.querySelectorAll("[data-en][data-pl]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var value = node.getAttribute("data-" + lang);
      if (value !== null) {
        node.innerHTML = value;
      }
    }

    var toggle = document.getElementById("lang-toggle");
    if (toggle) {
      toggle.textContent = lang === "en" ? "Polski" : "English";
      toggle.setAttribute("aria-label", lang === "en" ? "Przełącz na polski" : "Switch to English");
    }
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (_) {}
  }

  function initialLanguage() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "en" || saved === "pl") return saved;
    } catch (_) {}
    return "en";
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyLanguage(initialLanguage());
    var toggle = document.getElementById("lang-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        applyLanguage(document.documentElement.lang === "en" ? "pl" : "en");
      });
    }
  });
})();
