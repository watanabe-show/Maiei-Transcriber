"use strict";
// テーマ（ライト／ダーク）の切り替え。<html data-theme="dark"> を付け外しし、
// 選択を localStorage に保存する。チラつき防止の初期適用は各ページ <head> の
// インラインスクリプトで（このファイル読み込み前に）済ませてある。
// 切り替えボタンは [data-theme-toggle] 属性を持つ要素すべてに自動で結線する。
(function () {
  var root = document.documentElement;
  var btns = Array.prototype.slice.call(document.querySelectorAll("[data-theme-toggle]"));

  function isDark() { return root.getAttribute("data-theme") === "dark"; }

  function sync() {
    var dark = isDark();
    btns.forEach(function (b) {
      b.textContent = dark ? "☀ ライト" : "🌙 ダーク";
      b.setAttribute("aria-pressed", dark ? "true" : "false");
      b.setAttribute("aria-label", dark ? "ライトモードに切り替え" : "ダークモードに切り替え");
    });
  }

  function toggle() {
    var dark = !isDark();
    if (dark) root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* 保存不可でも動く */ }
    sync();
  }

  btns.forEach(function (b) { b.addEventListener("click", toggle); });
  sync();
})();
