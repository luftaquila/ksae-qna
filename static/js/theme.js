const media = window.matchMedia("(prefers-color-scheme: dark)");

function preferredTheme() {
  return localStorage.getItem("theme") || (media.matches ? "dark" : "light");
}

function updateThemeMeta() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  const paper = getComputedStyle(document.documentElement).getPropertyValue("--color-paper").trim();
  if (paper) meta.setAttribute("content", paper);
}

function updateButton(button, theme) {
  if (!button) return;
  const next = theme === "dark" ? "라이트" : "다크";
  button.setAttribute("aria-label", `${next} 테마로 전환`);
  button.dataset.theme = theme;
}

export function setTheme(theme, button = null, persist = true) {
  document.documentElement.dataset.theme = theme;
  if (persist) localStorage.setItem("theme", theme);
  updateButton(button, theme);
  updateThemeMeta();
}

export function initTheme(button) {
  setTheme(preferredTheme(), button, false);

  button?.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || preferredTheme();
    setTheme(current === "dark" ? "light" : "dark", button, true);
  });

  media.addEventListener("change", (event) => {
    if (!localStorage.getItem("theme")) {
      setTheme(event.matches ? "dark" : "light", button, false);
    }
  });
}
