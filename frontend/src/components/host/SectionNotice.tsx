import type { SectionStatus } from "../../api/types";
import { SECTION_STATUS_LABELS } from "../../lib/labels";

export const SECTION_STATUS_DESCRIPTIONS: Record<SectionStatus, string> = {
  ok: "采集成功，但主机没有返回该部分数据。",
  permission_denied: "当前 SSH 用户没有读取该信息的权限。",
  unavailable: "该主机不支持这项信息采集。",
  error: "采集该部分信息时发生错误。",
};

export interface SectionNoticeProps {
  status: SectionStatus;
  error: string | null;
  testId?: string;
  title?: string;
  requiresSudo?: boolean;
}

/** Inline explanation for a probe section that did not return usable data. */
export function SectionNotice({ status, error, testId, title, requiresSudo }: SectionNoticeProps) {
  return (
    <div className="section-notice" role="status" data-testid={testId}>
      <p className="section-notice__title">
        {title ? `${title} · ` : null}
        {SECTION_STATUS_LABELS[status]}
      </p>
      <p className="section-notice__text">{SECTION_STATUS_DESCRIPTIONS[status]}</p>
      {requiresSudo ? (
        <p className="section-notice__text">该部分数据需要 sudo 权限或额外能力。</p>
      ) : null}
      {error ? <p className="section-notice__error">原因：{error}</p> : null}
    </div>
  );
}
