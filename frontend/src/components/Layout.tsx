import { PageIndex } from "./PageIndex";
import { Link, NavLink, Outlet } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  hint: string;
  icon: JSX.Element;
  end?: boolean;
}

function HostIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" focusable="false">
      <rect x="2.5" y="3.5" width="15" height="5" rx="1.2" fill="none" stroke="currentColor" />
      <rect x="2.5" y="11.5" width="15" height="5" rx="1.2" fill="none" stroke="currentColor" />
      <circle cx="5.5" cy="6" r="0.9" fill="currentColor" />
      <circle cx="5.5" cy="14" r="0.9" fill="currentColor" />
    </svg>
  );
}

function TaskIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" focusable="false">
      <path d="M4 5.5h12M4 10h12M4 14.5h7" stroke="currentColor" strokeWidth="1.4" fill="none" />
      <circle cx="15.5" cy="14.5" r="1.4" fill="currentColor" />
    </svg>
  );
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "主机总览", hint: "清单与连接状态", icon: <HostIcon />, end: true },
  { to: "/tasks", label: "维护任务", hint: "进度与实时输出", icon: <TaskIcon /> },
];

export function Layout() {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="brand" to="/">
          <span className="brand__mark" aria-hidden="true">
            LP
          </span>
          <span className="brand__text">
            <strong>LabOpsPanel</strong>
            <small>本机运维控制台</small>
          </span>
        </Link>

        <nav className="nav" aria-label="主导航">
          <p className="nav__heading">运维</p>
          <ul className="nav__list">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => `nav__link${isActive ? " nav__link--active" : ""}`}
                >
                  <span className="nav__icon">{item.icon}</span>
                  <span className="nav__label">
                    <span className="nav__title">{item.label}</span>
                    <span className="nav__hint">{item.hint}</span>
                  </span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="sidebar__footer">
          <p className="sidebar__status">
            <span className="status-dot status-dot--ok" aria-hidden="true" />
            本地模式 · 仅回环监听
          </p>
          <p className="sidebar__note">通过 Windows OpenSSH 管理 Linux 主机</p>
        </div>
      </aside>

      <div className="app-main">
        <div className="topbar">
          <span className="topbar__label">本地控制台</span>
          <span className="topbar__meta">API · 127.0.0.1:8765</span>
        </div>
        <main id="main-content" className="page">
          <div className="page-with-index">
            <div id="page-sections" className="page-sections"><Outlet /></div>
            <PageIndex />
          </div>
        </main>
      </div>
    </div>
  );
}
