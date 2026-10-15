try {
  document.documentElement.dataset.theme =
    localStorage.getItem("qa-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
} catch (_) {
  /* Storage can be disabled. */
}
