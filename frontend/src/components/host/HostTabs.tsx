import { useRef } from "react";
import type { KeyboardEvent } from "react";

export type HostTabId = "overview" | "metrics" | "gpu" | "storage" | "users" | "accounts";

export interface HostTabDefinition {
  id: HostTabId;
  label: string;
}

export const HOST_TABS: readonly HostTabDefinition[] = [
  { id: "overview", label: "概览" },
  { id: "metrics", label: "资源" },
  { id: "gpu", label: "GPU" },
  { id: "storage", label: "磁盘空间" },
  { id: "users", label: "用户" },
  { id: "accounts", label: "账户" },
];

export function isHostTabId(value: string | null | undefined): value is HostTabId {
  return value !== null && value !== undefined && HOST_TABS.some((tab) => tab.id === value);
}

export interface HostTabListProps {
  active: HostTabId;
  onChange: (id: HostTabId) => void;
}

/**
 * WAI-ARIA tabs: roving tabindex, Left/Right/Home/End navigation. The active
 * tab is the only one in the tab order, matching the single visible panel.
 */
export function HostTabList({ active, onChange }: HostTabListProps) {
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const activate = (index: number, focus: boolean) => {
    const next = HOST_TABS[index];
    if (!next) return;
    onChange(next.id);
    if (focus) buttonRefs.current[index]?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    switch (event.key) {
      case "ArrowRight":
        nextIndex = (index + 1) % HOST_TABS.length;
        break;
      case "ArrowLeft":
        nextIndex = (index - 1 + HOST_TABS.length) % HOST_TABS.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = HOST_TABS.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    activate(nextIndex, true);
  };

  return (
    <div className="tabs" role="tablist" aria-label="主机详情视图">
      {HOST_TABS.map((tab, index) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(element) => {
              buttonRefs.current[index] = element;
            }}
            type="button"
            role="tab"
            id={`host-tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={selected ? `host-panel-${tab.id}` : undefined}
            tabIndex={selected ? 0 : -1}
            className={`tabs__tab${selected ? " tabs__tab--active" : ""}`}
            onClick={() => activate(index, false)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
