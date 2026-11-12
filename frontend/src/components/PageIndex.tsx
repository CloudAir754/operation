import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";

export function PageIndex() {
  const location = useLocation();
  const [sections, setSections] = useState<HTMLElement[]>([]);
  const [active, setActive] = useState(0);
  useEffect(() => {
    const root = document.getElementById("page-sections");
    if (!root) return;
    let headings: HTMLElement[] = [];
    const updateActive = () => {
      let current = 0;
      headings.forEach((heading, index) => {
        if (heading.getBoundingClientRect().top <= 150) current = index;
      });
      setActive(current);
    };
    const collect = () => {
      const next = Array.from(root.querySelectorAll<HTMLElement>("h2")).filter(
        (heading) => !heading.closest('[role="dialog"], [hidden]'),
      );
      if (next.length !== headings.length || next.some((item, i) => item !== headings[i])) {
        headings = next;
        setSections(next);
      }
      updateActive();
    };
    collect();
    const observer = new MutationObserver(collect);
    observer.observe(root, { childList: true, subtree: true });
    window.addEventListener("scroll", updateActive, { passive: true });
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", updateActive);
    };
  }, [location.pathname, location.search]);

  return <aside className="page-index" aria-label="本页索引">
    <div className="page-index__heading">本页索引</div>
    <p className="page-index__hint">快速定位功能区域</p>
    <nav aria-label="页内导航">
      {sections.length ? <ol>{sections.map((section, index) => <li key={index}>
        <button type="button" aria-current={active === index ? "location" : undefined}
          onClick={() => {
            section.scrollIntoView({ behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
            section.tabIndex = -1;
            section.focus({ preventScroll: true });
            setActive(index);
          }}>
          <span className="page-index__number">{String(index + 1).padStart(2, "0")}</span>
          {section.textContent}
        </button>
      </li>)}</ol> : <p className="page-index__hint">当前页面暂无分区</p>}
    </nav>
    <button className="page-index__top" type="button" onClick={() => window.scrollTo({ top: 0, behavior: "auto" })}>↑ 返回顶部</button>
  </aside>;
}
