import { useEffect, useState } from "react";
import type { AppTheme } from "../appDefaults";

export function useAppTheme() {
  const [appTheme, setAppTheme] = useState<AppTheme>(() => {
    if (typeof window === "undefined") return "light";
    const saved = window.localStorage.getItem("appora-theme");
    return saved === "dark" || saved === "light" ? saved : "light";
  });

  useEffect(() => {
    document.documentElement.dataset.appTheme = appTheme;
    window.localStorage.setItem("appora-theme", appTheme);
  }, [appTheme]);

  const toggleAppTheme = () => setAppTheme((theme) => theme === "dark" ? "light" : "dark");

  return { appTheme, toggleAppTheme };
}
