import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const THEME_STORAGE_KEY = "telepilot-theme";
const LEGACY_THEME_STORAGE_KEY = "telebot-theme";
const LIGHT_THEME_COLOR = "#F2F0EC";
const DARK_THEME_COLOR = "#1C1C1E";
// 两套主题都用 default：让 iOS 用 theme-color 填状态栏底色、按亮度自动选文字色。
// 不用 black-translucent——那模式下 iOS 强制白字并自绘一层深色半透明 scrim 保证可读，
// 那层 scrim 是系统画的、CSS 删不掉，就是浅色主题顶部那条灰带的真身。
const STATUS_BAR_STYLE = "default";

interface ThemeContextValue {
  theme: Theme;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function isTheme(value: string | null): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

function getSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(theme: Theme): ResolvedTheme {
  return theme === "system" ? getSystemTheme() : theme;
}

function applyResolvedTheme(resolvedTheme: ResolvedTheme) {
  const root = document.documentElement;
  const chromeColor = resolvedTheme === "dark" ? DARK_THEME_COLOR : LIGHT_THEME_COLOR;

  root.classList.toggle("dark", resolvedTheme === "dark");
  root.style.colorScheme = resolvedTheme;
  root.style.backgroundColor = chromeColor;
  document.body.style.backgroundColor = chromeColor;

  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) {
    meta.content = chromeColor;
  }

  const statusBarMeta = document.querySelector<HTMLMetaElement>(
    'meta[name="apple-mobile-web-app-status-bar-style"]',
  );
  if (statusBarMeta) {
    statusBarMeta.content = STATUS_BAR_STYLE;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    if (typeof window === "undefined") return "system";
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
      || window.localStorage.getItem(LEGACY_THEME_STORAGE_KEY);
    if (isTheme(stored)) {
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, stored);
      } catch {
        // 浏览器隐私模式下 localStorage 可能不可写；读取到的旧主题仍可用于本次会话。
      }
    }
    return isTheme(stored) ? stored : "system";
  });
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() => resolveTheme(theme));

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");

    const update = () => {
      const nextResolved = resolveTheme(theme);
      setResolvedTheme(nextResolved);
      applyResolvedTheme(nextResolved);
    };

    update();
    const legacyMedia = media as MediaQueryList & {
      addListener?: (listener: () => void) => void;
      removeListener?: (listener: () => void) => void;
    };

    if (legacyMedia.addEventListener) {
      legacyMedia.addEventListener("change", update);
      return () => legacyMedia.removeEventListener("change", update);
    }

    legacyMedia.addListener?.(update);
    return () => legacyMedia.removeListener?.(update);
  }, [theme]);

  const setTheme = (nextTheme: Theme) => {
    setThemeState(nextTheme);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
      window.localStorage.removeItem(LEGACY_THEME_STORAGE_KEY);
    } catch {
      // 浏览器隐私模式下 localStorage 可能不可写；本次会话仍保持主题切换。
    }
  };

  const value = useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within ThemeProvider");
  }
  return context;
}
