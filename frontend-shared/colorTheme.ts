export type ColorTheme = "system" | "light" | "dark";

export const nextColorTheme = {
  system: "light",
  light: "dark",
  dark: "system",
} as const satisfies Record<ColorTheme, ColorTheme>;

export function watchColorTheme(readTheme: () => ColorTheme): () => void {
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  function apply() {
    const theme = readTheme();
    document.documentElement.dataset.theme =
      theme === "system" ? (system.matches ? "dark" : "light") : theme;
  }
  apply();
  system.addEventListener("change", apply);
  return () => system.removeEventListener("change", apply);
}

export function readColorTheme(key: string): ColorTheme {
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark" || saved === "system")
      return saved;
  } catch (error) {
    if (!(error instanceof DOMException) || error.name !== "SecurityError")
      throw error;
  }
  return "system";
}

export function saveColorTheme(key: string, theme: ColorTheme): boolean {
  try {
    localStorage.setItem(key, theme);
    return true;
  } catch (error) {
    if (
      !(error instanceof DOMException) ||
      !["SecurityError", "QuotaExceededError"].includes(error.name)
    )
      throw error;
    return false;
  }
}
