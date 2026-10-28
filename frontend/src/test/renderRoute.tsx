import { render } from "@testing-library/react";
import type { RenderResult } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactElement } from "react";

export interface RenderRouteOptions {
  route: string;
  path: string;
}

/** Render a page inside a MemoryRouter with the route params it expects. */
export function renderRoute(ui: ReactElement, { route, path }: RenderRouteOptions): RenderResult {
  return render(
    <MemoryRouter
      initialEntries={[route]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path={path} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}
