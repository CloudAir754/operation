import { TreemapChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'
import type { ScanNode } from './types'

echarts.use([TreemapChart, TooltipComponent, CanvasRenderer])

interface ChartNode {
  name: string
  value: number
  path: string
  logicalSize: number
  allocatedSize: number
  fileCount: number
  owner: string
  mode: string
  itemStyle: { color?: string }
  children: ChartNode[]
}

function chartNode(node: ScanNode): ChartNode {
  return {
    name: node.name,
    value: Math.max(node.allocated_size, node.logical_size, 1),
    path: node.path,
    logicalSize: node.logical_size,
    allocatedSize: node.allocated_size,
    fileCount: node.file_count,
    owner: node.owner ?? String(node.owner_uid ?? 'N/A'),
    mode: node.mode === null ? 'N/A' : node.mode.toString(8).padStart(4, '0'),
    itemStyle: { color: node.unreadable ? '#829ab1' : node.world_writable ? '#cf1322' : undefined },
    children: node.children.map(chartNode),
  }
}

export default function TreemapView({ root }: { root: ScanNode }) {
  const element = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!element.current) return
    const chart = echarts.init(element.current)
    chart.setOption({
      tooltip: {
        renderMode: 'richText',
        formatter: (params: { data: ChartNode }) => {
          const data = params.data
          return `${data.path}\n实际占用：${data.allocatedSize.toLocaleString()} B\n逻辑大小：${data.logicalSize.toLocaleString()} B\n文件：${data.fileCount}\n所有者：${data.owner}\n权限：${data.mode}`
        },
      },
      series: [{
        type: 'treemap',
        roam: true,
        nodeClick: 'zoomToNode',
        breadcrumb: { show: true },
        data: [chartNode(root)],
        upperLabel: { show: true, height: 24 },
        label: { show: true, formatter: '{b}' },
      }],
    })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(element.current)
    return () => {
      observer.disconnect()
      chart.dispose()
    }
  }, [root])

  return <div ref={element} className="treemap" aria-label="home 目录占用 treemap" />
}
