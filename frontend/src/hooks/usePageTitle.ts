import { useEffect } from "react";

const APP_NAME = "LabOpsPanel";

export function usePageTitle(title: string): void {
  useEffect(() => {
    document.title = `${title} · ${APP_NAME}`;
    return () => {
      document.title = APP_NAME;
    };
  }, [title]);
}
