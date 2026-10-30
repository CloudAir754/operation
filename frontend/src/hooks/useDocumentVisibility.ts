import { useEffect, useState } from "react";

/** `true` unless the browser explicitly reports a hidden document. */
export function isDocumentVisible(): boolean {
  if (typeof document === "undefined") return true;
  return document.visibilityState !== "hidden";
}

/**
 * Tracks `document.visibilityState`. Polling surfaces use this to stop hitting
 * the backend while the tab is in the background.
 */
export function useDocumentVisibility(): boolean {
  const [visible, setVisible] = useState(isDocumentVisible);

  useEffect(() => {
    const update = () => setVisible(isDocumentVisible());
    document.addEventListener("visibilitychange", update);
    // Safari keeps the listener silent when the page is restored from the
    // back/forward cache, so re-check on focus as well.
    window.addEventListener("focus", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("focus", update);
    };
  }, []);

  return visible;
}
