import { useId } from "react";
import type { ReactNode } from "react";
import { formatClock } from "../../lib/format";

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  /** Same length as `timestamps`; `null` breaks the line instead of drawing 0. */
  values: Array<number | null>;
}

export interface LineChartProps {
  title: string;
  series: ChartSeries[];
  timestamps: string[];
  /** Fixed upper bound (percent charts pass 100). Auto-scales when omitted. */
  yMax?: number | null;
  yUnit?: string;
  yFormat?: (value: number) => string;
  /** Indices where the counter was reset; the line breaks and a dashed marker is drawn. */
  resetIndices?: number[];
  resetReasons?: Record<number, string | null>;
  ariaLabel: string;
  testId?: string;
  emptyHint?: string;
  caption?: ReactNode;
  height?: number;
}

const WIDTH = 720;
const PADDING = { left: 52, right: 14, top: 12, bottom: 26 };

function niceMax(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const magnitude = 10 ** exponent;
  const normalized = value / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return nice * magnitude;
}

export function LineChart({
  title,
  series,
  timestamps,
  yMax,
  yUnit,
  yFormat,
  resetIndices = [],
  resetReasons = {},
  ariaLabel,
  testId,
  emptyHint = "正在建立基线",
  caption,
  height = 200,
}: LineChartProps) {
  const titleId = useId();
  const count = Math.max(timestamps.length, ...series.map((item) => item.values.length), 0);
  const empty = count < 2 || series.length === 0;

  const values = series.flatMap((item) =>
    item.values.filter((value): value is number => value !== null && Number.isFinite(value)),
  );
  const maxValue = values.length > 0 ? Math.max(...values) : 0;
  const top = yMax ?? niceMax(maxValue);
  const formatValue = yFormat ?? ((value: number) => `${value}${yUnit ?? ""}`);

  const heightPx = Math.max(140, height);
  const plotWidth = WIDTH - PADDING.left - PADDING.right;
  const plotHeight = heightPx - PADDING.top - PADDING.bottom;
  const resetSet = new Set(resetIndices);

  const x = (index: number): number =>
    PADDING.left + (index / Math.max(1, count - 1)) * plotWidth;
  const y = (value: number): number =>
    PADDING.top + plotHeight - (Math.min(top, Math.max(0, value)) / top) * plotHeight;

  const gridLines = [0, 0.25, 0.5, 0.75, 1];
  const xTickIndices =
    count < 4
      ? Array.from({ length: count }, (_, index) => index)
      : [0, Math.floor((count - 1) / 2), count - 1];

  const segmentsFor = (valuesForSeries: Array<number | null>): string[] => {
    const segments: string[] = [];
    let current: string[] = [];
    valuesForSeries.forEach((value, index) => {
      if (value === null || !Number.isFinite(value) || resetSet.has(index)) {
        if (current.length > 0) {
          segments.push(current.join(" "));
          current = [];
        }
        return;
      }
      current.push(`${current.length === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`);
    });
    if (current.length > 0) segments.push(current.join(" "));
    return segments;
  };

  return (
    <figure className="chart" data-testid={testId} aria-labelledby={titleId}>
      <figcaption className="chart__head" id={titleId}>
        <span className="chart__title">{title}</span>
        {yUnit ? <span className="chart__unit">{yUnit}</span> : null}
      </figcaption>

      {empty ? (
        <div
          className="chart-empty"
          role="img"
          aria-label={`${ariaLabel}：${emptyHint}`}
        >
          <span>{emptyHint}</span>
          <span className="chart-empty__hint">至少需要 2 个采样点才能绘制趋势</span>
        </div>
      ) : (
        <>
          <svg
            className="chart__svg"
            viewBox={`0 0 ${WIDTH} ${heightPx}`}
            role="img"
            aria-label={ariaLabel}
            preserveAspectRatio="xMidYMid meet"
          >
            {gridLines.map((fraction) => {
              const lineY = PADDING.top + plotHeight * (1 - fraction);
              return (
                <g key={`grid-${fraction}`}>
                  <line
                    className="chart__grid"
                    x1={PADDING.left}
                    y1={lineY}
                    x2={PADDING.left + plotWidth}
                    y2={lineY}
                  />
                  <text
                    className="chart__axis-label"
                    x={PADDING.left - 8}
                    y={lineY + 4}
                    textAnchor="end"
                  >
                    {formatValue(top * fraction)}
                  </text>
                </g>
              );
            })}

            {xTickIndices.map((index) => (
              <text
                key={`x-${index}`}
                className="chart__axis-label"
                x={x(index)}
                y={heightPx - 8}
                textAnchor={index === 0 ? "start" : index === count - 1 ? "end" : "middle"}
              >
                {formatClock(timestamps[index])}
              </text>
            ))}

            {resetIndices.map((index) => (
              <g key={`reset-${index}`} className="chart__reset-marker">
                <line
                  x1={x(index)}
                  y1={PADDING.top}
                  x2={x(index)}
                  y2={PADDING.top + plotHeight}
                  strokeDasharray="4 3"
                />
                <title>{`重置点 ${formatClock(timestamps[index])}：${
                  resetReasons[index] ?? "计数器重置，已跳过该采样点"
                }`}</title>
              </g>
            ))}

            {series.map((item) =>
              segmentsFor(item.values).map((path, pathIndex) => (
                <path
                  key={`${item.key}-${pathIndex}`}
                  d={path}
                  fill="none"
                  stroke={item.color}
                  strokeWidth={1.8}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              )),
            )}
          </svg>

          <ul className="chart-legend">
            {series.map((item) => {
              const lastValue = [...item.values].reverse().find((value) => value !== null) ?? null;
              return (
                <li key={item.key} className="chart-legend__item">
                  <span className="chart-legend__swatch" style={{ background: item.color }} />
                  <span className="chart-legend__label">{item.label}</span>
                  <span className="chart-legend__value">
                    {lastValue === null ? "N/A" : formatValue(lastValue)}
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}
      {caption ? <p className="chart__caption">{caption}</p> : null}
    </figure>
  );
}
