import { useEffect, useState } from "react";
import { getDefaultAssistPaneWidth } from "../appDefaults";

function clampAssistPaneWidth(width: number) {
  const minWidth = 220;
  const maxWidth = Math.min(300, Math.floor(window.innerWidth * 0.28));
  return Math.max(minWidth, Math.min(maxWidth, width));
}

export function useAssistPaneResize() {
  const [assistPaneWidth, setAssistPaneWidth] = useState(getDefaultAssistPaneWidth);
  const [isResizingAssistPane, setIsResizingAssistPane] = useState(false);

  useEffect(() => {
    const clampCurrentWidth = () => setAssistPaneWidth((prev) => clampAssistPaneWidth(prev));

    clampCurrentWidth();
    window.addEventListener("resize", clampCurrentWidth);
    return () => window.removeEventListener("resize", clampCurrentWidth);
  }, []);

  useEffect(() => {
    if (!isResizingAssistPane) return;

    const handleMouseMove = (event: MouseEvent) => {
      setAssistPaneWidth(clampAssistPaneWidth(window.innerWidth - event.clientX));
    };

    const handleMouseUp = () => {
      setIsResizingAssistPane(false);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizingAssistPane]);

  return {
    assistPaneWidth,
    isResizingAssistPane,
    startAssistPaneResize: () => setIsResizingAssistPane(true),
  };
}
