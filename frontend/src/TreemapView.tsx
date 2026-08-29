import { DownloadOutlined } from '@ant-design/icons'
import { TreemapChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { Button, InputNumber, Select, Space, Typography } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ScanNode } from './types'

echarts.use([TreemapChart, TooltipComponent, CanvasRenderer])

interface ChartNode {
  name: string
  value: number
  path: string
  type: string
  logicalSize: number
  allocatedSize: number
  fileCount: number
  owner: string
  group: string
  mode: string
  flags: string
  itemStyle: { color?: string }
  children: ChartNode[]
}

function ownerColor(uid: number | null): string {
  if (uid === null) return '#829ab1'
  return `hsl(${Math.abs(uid * 47) % 360} 48% 48%)`
}

function chartNode(node: ScanNode): ChartNode {
  const flags = [
    node.unreadable && '无权访问',
    node.changed_during_scan && '扫描中变化',
    node.cross_filesystem && '跨文件系统',
    node.hardlink_duplicate && '硬链接重复',
    node.world_writable && '其他用户可写',
  ].filter(Boolean).join('、') || '无'
  return {
    name: node.name,
    value: Math.max(node.allocated_size, node.logical_size, 1),
    path: node.path,
    type: node.type,
    logicalSize: node.logical_size,
    allocatedSize: node.allocated_size,
    fileCount: node.file_count,
    owner: node.owner ?? String(node.owner_uid ?? 'N/A'),
    group: node.group ?? String(node.group_gid ?? 'N/A'),
    mode: node.mode === null ? 'N/A' : node.mode.toString(8).padStart(4, '0'),
    flags,
    itemStyle: { color: node.world_writable ? '#cf1322' : node.unreadable ? '#829ab1' : ownerColor(node.owner_uid) },
    children: node.children.map(chartNode).sort((left, right) => right.allocatedSize - left.allocatedSize),
  }
}

function filterTree(node: ScanNode, ownerUid: string, minimumBytes: number, isRoot = false): ScanNode | null {
  const children = node.children
    .map(child => filterTree(child, ownerUid, minimumBytes))
    .filter((child): child is ScanNode => child !== null)
    .sort((left, right) => right.allocated_size - left.allocated_size)
  const ownerMatches = ownerUid === 'all' || String(node.owner_uid) === ownerUid
  const sizeMatches = node.type !== 'file' || node.allocated_size >= minimumBytes
  if (node.type !== 'directory' && (!ownerMatches || !sizeMatches)) return null
  if (!isRoot && node.type === 'directory' && children.length === 0 && (!ownerMatches || node.file_count > 0)) return null
  if (node.type !== 'directory') return { ...node, children }
  return {
    ...node,
    children,
    logical_size: children.reduce((total, child) => total + child.logical_size, 0),
    allocated_size: children.reduce((total, child) => total + child.allocated_size, 0),
    file_count: children.reduce((total, child) => total + child.file_count, 0),
  }
}

function collectOwners(node: ScanNode, result = new Map<string, string>()): Map<string, string> {
  if (node.owner_uid !== null) result.set(String(node.owner_uid), `${node.owner ?? '未知用户'} · UID ${node.owner_uid}`)
  node.children.forEach(child => collectOwners(child, result))
  return result
}

function csvSummary(root: ScanNode, scannedAt?: string, expiresAt?: string): string {
  const rows: string[][] = [['扫描时间', scannedAt ?? 'N/A'], ['过期时间', expiresAt ?? 'N/A'], [], ['路径', '类型', '逻辑大小', '实际占用', '文件数', 'UID', 'GID', '权限', '标记']]
  const visit = (node: ScanNode) => {
    const flags = [node.unreadable && 'unreadable', node.changed_during_scan && 'changed', node.cross_filesystem && 'cross-filesystem', node.hardlink_duplicate && 'hardlink-duplicate', node.world_writable && 'world-writable'].filter(Boolean).join('|')
    rows.push([node.path, node.type, String(node.logical_size), String(node.allocated_size), String(node.file_count), String(node.owner_uid ?? ''), String(node.group_gid ?? ''), node.mode === null ? '' : node.mode.toString(8), flags])
    node.children.forEach(visit)
  }
  visit(root)
  return rows.map(row => row.map(value => `"${value.replaceAll('"', '""')}"`).join(',')).join('\n')
}

export default function TreemapView({ root, scannedAt, expiresAt }: { root: ScanNode, scannedAt?: string, expiresAt?: string }) {
  const element = useRef<HTMLDivElement>(null)
  const [ownerUid, setOwnerUid] = useState('all')
  const [minimumMiB, setMinimumMiB] = useState(0)
  const owners = useMemo(() => [...collectOwners(root)].map(([value, label]) => ({ value, label })), [root])
  const filtered = useMemo(() => filterTree(root, ownerUid, minimumMiB * 1024 * 1024, true) ?? { ...root, children: [] }, [root, ownerUid, minimumMiB])

  useEffect(() => {
    if (!element.current) return
    const chart = echarts.init(element.current)
    chart.setOption({
      tooltip: {
        renderMode: 'richText',
        formatter: (params: { data: ChartNode }) => {
          const data = params.data
          return `${data.path}\n类型：${data.type}\n实际占用：${data.allocatedSize.toLocaleString()} B\n逻辑大小：${data.logicalSize.toLocaleString()} B\n文件：${data.fileCount}\n所有者：${data.owner}\n所属组：${data.group}\n权限：${data.mode}\n标记：${data.flags}`
        },
      },
      series: [{
        type: 'treemap',
        roam: true,
        nodeClick: 'zoomToNode',
        sort: 'desc',
        breadcrumb: { show: true },
        data: [chartNode(filtered)],
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
  }, [filtered])

  const exportSummary = () => {
    const blob = new Blob([`\uFEFF${csvSummary(filtered, scannedAt, expiresAt)}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `labopspanel-scan-${Date.now()}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  return <Space direction="vertical" className="scan-status">
    <Space wrap>
      <Typography.Text>所有者筛选</Typography.Text>
      <Select value={ownerUid} onChange={setOwnerUid} className="owner-filter" options={[{ value: 'all', label: '全部所有者' }, ...owners]} />
      <Typography.Text>仅显示至少</Typography.Text>
      <InputNumber min={0} max={1024 * 1024} value={minimumMiB} onChange={value => setMinimumMiB(value ?? 0)} addonAfter="MiB" />
      <Button icon={<DownloadOutlined />} onClick={exportSummary}>导出当前摘要</Button>
    </Space>
    <Typography.Text type="secondary">面积按实际占用优先；颜色按所有者区分，红色表示其他用户可写，灰色表示无权访问。可点击下钻并使用面包屑返回。</Typography.Text>
    <div ref={element} className="treemap" aria-label="目录与文件占用 treemap" />
  </Space>
}
