import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { HostDetailPage } from "./pages/HostDetailPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TasksPage } from "./pages/TasksPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="hosts/:id" element={<HostDetailPage />} />
        <Route path="hosts/:id/settings" element={<SettingsPage />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

function NotFoundPage() {
  return (
    <div className="not-found">
      <p className="not-found__code">404</p>
      <h1>页面不存在</h1>
      <p className="muted">请检查地址，或返回主机总览继续操作。</p>
      <Link className="btn btn--primary" to="/">
        返回主机总览
      </Link>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppRoutes />
    </BrowserRouter>
  );
}
