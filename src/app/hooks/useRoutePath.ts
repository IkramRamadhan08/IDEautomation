import { useCallback, useEffect, useState } from "react";

export function useRoutePath() {
  const [routePath, setRoutePath] = useState(() => {
    if (typeof window === "undefined") return "/";
    return window.location.pathname || "/";
  });

  const navigateTo = useCallback((path: string) => {
    if (typeof window === "undefined") return;
    const nextPath = path || "/";
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
    setRoutePath(nextPath);
  }, []);

  useEffect(() => {
    const handlePopState = () => setRoutePath(window.location.pathname || "/");
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  return { routePath, navigateTo };
}
