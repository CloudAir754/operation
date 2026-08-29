import { Alert, Button, Card, Space, Steps, Table, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { loadTasks } from './api'
import type { PersistentTask } from './types'

const RECOVERY_TARGETS: Record<string, { href: string, label: string }> = {
  disk_initialization: { href: '#disk-initialization-panel', label: '前往恢复磁盘任务' },
  file_transfer_cleanup: { href: '#file-transfer-panel', label: '前往重新核对文件' },
  home_migration_execute: { href: '#home-migration-panel', label: '前往继续或回退' },
  home_migration_preflight: { href: '#home-migration-panel', label: '前往重新预检' },
  home_migration_rollback: { href: '#home-migration-panel', label: '前往核对回退' },
  home_migration_cleanup: { href: '#home-migration-panel', label: '前往核对清理' },
  file_transfer_preflight: { href: '#file-transfer-panel', label: '前往重新预检' },
  file_transfer_copy: { href: '#file-transfer-panel', label: '前往重新预检' },
  create_users: { href: '#user-import-panel', label: '前往重新导入' },
}

function stateColor(state: PersistentTask['state']): string {
  if (state === 'succeeded') return 'green'
  if (state === 'failed') return 'red'
  if (state === 'interrupted' || state === 'rollback_pending') return 'orange'
  if (state === 'cancelled' || state === 'rolled_back') return 'default'
  return 'blue'
}

export default function TaskPanel({ token }: { token: string }) {
  const [tasks, setTasks] = useState<PersistentTask[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const refresh = () => {
      void loadTasks(token).then(items => {
        if (!active) return
        setTasks(items)
        setError('')
      }).catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : '刷新任务列表失败。')
      })
    }
    refresh()
    const timer = window.setInterval(refresh, 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [token])

  return <Card id="task-panel" title="任务" extra={<Tag>{tasks.length} 条最近记录</Tag>}>
    <Typography.Paragraph type="secondary">展示本次数据库中的预检、执行步骤、失败原因和中断恢复入口；涉及破坏性操作时仍需在对应工具中重新核对身份并确认。</Typography.Paragraph>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    <Table<PersistentTask>
      rowKey="id"
      size="small"
      dataSource={tasks}
      pagination={{ pageSize: 10 }}
      columns={[
        { title: '类型', dataIndex: 'kind', key: 'kind' },
        { title: '目标', dataIndex: 'target', key: 'target', ellipsis: true },
        { title: '状态', dataIndex: 'state', key: 'state', render: value => <Tag color={stateColor(value)}>{value}</Tag> },
        { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', render: value => new Date(value).toLocaleString() },
        { title: '恢复 / 重试', key: 'recovery', render: (_, task) => {
          const target = RECOVERY_TARGETS[task.kind]
          const observingHome = task.kind === 'home_migration_execute' && task.state === 'succeeded'
          if (!target || (!observingHome && !['failed', 'interrupted', 'rollback_pending'].includes(task.state))) return '—'
          return <Button size="small" href={target.href}>{target.label}</Button>
        } },
      ]}
      expandable={{ expandedRowRender: task => <Space direction="vertical" className="scan-status">
        <Steps direction="vertical" size="small" items={task.steps.map(step => ({ title: step.name, description: step.detail ? JSON.stringify(step.detail) : undefined, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />
        {task.error && <Alert type="error" showIcon message={task.error} />}
      </Space> }}
    />
  </Card>
}
