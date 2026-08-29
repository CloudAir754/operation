import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, InputNumber, Select, Space, Steps, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { cleanupHomeMigration, executeHomeMigration, loadHomeMigrationStatus, loadTask, loadTasks, resumeHomeMigration, rollbackHomeMigration, startHomeMigrationPreflight } from './api'
import type { HomeMigrationStatus, PersistentTask, UserAccount } from './types'

interface Props {
  token: string
  privileged: boolean
  users: UserAccount[]
}

const terminalStates = new Set(['succeeded', 'failed', 'cancelled', 'interrupted', 'rolled_back'])

function bytes(value: number | undefined): string {
  if (value === undefined) return 'N/A'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export default function HomeMigrationPanel({ token, privileged, users }: Props) {
  const [username, setUsername] = useState('')
  const [targetParent, setTargetParent] = useState('')
  const [margin, setMargin] = useState(10)
  const [verifyContents, setVerifyContents] = useState(false)
  const [task, setTask] = useState<PersistentTask | null>(null)
  const [migrationId, setMigrationId] = useState('')
  const [executionTaskId, setExecutionTaskId] = useState('')
  const [migrationUsername, setMigrationUsername] = useState('')
  const [status, setStatus] = useState<HomeMigrationStatus | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!privileged) return
    void loadTasks(token).then(async tasks => {
      const recent = tasks.find(item => item.kind === 'home_migration_execute' && (item.state === 'interrupted' || item.state === 'succeeded'))
      if (!recent) return
      const restoredMigrationId = typeof recent.parameters.migration_id === 'string' ? recent.parameters.migration_id : ''
      if (!restoredMigrationId) return
      const preflight = await loadTask(token, restoredMigrationId)
      const restoredUsername = typeof preflight.parameters.username === 'string' ? preflight.parameters.username : ''
      setTask(recent)
      setExecutionTaskId(recent.id)
      setMigrationId(restoredMigrationId)
      setMigrationUsername(restoredUsername)
    }).catch(() => undefined)
  }, [privileged, token])

  useEffect(() => {
    if (!task || terminalStates.has(task.state)) return
    const timer = window.setInterval(() => {
      void loadTask(token, task.id).then(setTask).catch(reason => {
        setError(reason instanceof Error ? reason.message : '刷新 home 迁移任务失败。')
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [task, token])

  useEffect(() => {
    if (!migrationId || !privileged) return
    const refresh = () => {
      void loadHomeMigrationStatus(token, migrationId).then(setStatus).catch(() => undefined)
    }
    refresh()
    const timer = window.setInterval(refresh, 2000)
    return () => window.clearInterval(timer)
  }, [migrationId, privileged, token])

  async function preflight() {
    setError('')
    try {
      const next = await startHomeMigrationPreflight(token, username, targetParent, margin, verifyContents)
      setMigrationId(next.id)
      setMigrationUsername(username)
      setExecutionTaskId('')
      setStatus(null)
      setConfirmation('')
      setTask(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'home 迁移预检失败。')
    }
  }

  async function execute() {
    setError('')
    try {
      const next = await executeHomeMigration(token, migrationId, confirmation)
      setExecutionTaskId(next.id)
      setConfirmation('')
      setTask(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'home 迁移执行失败。')
    }
  }

  async function resume() {
    setError('')
    try {
      setTask(await resumeHomeMigration(token, executionTaskId, confirmation))
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'home 迁移恢复失败。')
    }
  }

  async function rollback() {
    setError('')
    try {
      setTask(await rollbackHomeMigration(token, migrationId, confirmation))
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'home 迁移回退失败。')
    }
  }

  async function cleanup() {
    setError('')
    try {
      setTask(await cleanupHomeMigration(token, migrationId, confirmation))
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '原 home 备份清理失败。')
    }
  }

  const preflightSucceeded = task?.kind === 'home_migration_preflight' && task.state === 'succeeded'
  const interrupted = task?.kind === 'home_migration_execute' && task.state === 'interrupted'
  const observing = status?.phase === 'observing'
  const busy = Boolean(task && !terminalStates.has(task.state))
  const expectedExecute = `迁移home:${migrationUsername}`
  const expectedResume = `继续home迁移:${migrationId}`
  const expectedRollback = `回退home:${migrationUsername}`
  const expectedCleanup = `清理原home备份:${migrationUsername}`

  return <Card title="整个 home 跨盘迁移" extra={<Tag color="red">仅 ext4 → ext4</Tag>}>
    <Alert className="notice" type="warning" showIcon message="迁移会锁定账号并终止该 UID 的全部会话、服务和进程。原 home 在观察期完整保留，不会自动清理。" />
    <Form layout="vertical">
      <Space wrap align="start">
        <Form.Item label="本机用户"><Select className="home-user-select" value={username || undefined} onChange={setUsername} options={users.map(user => ({ label: `${user.username} (UID ${user.uid})`, value: user.username }))} /></Form.Item>
        <Form.Item label="目标父目录"><Input value={targetParent} onChange={event => setTargetParent(event.target.value)} placeholder="/data2/users" /></Form.Item>
        <Form.Item label="安全余量 (%)"><InputNumber min={5} max={100} value={margin} onChange={value => setMargin(value ?? 10)} /></Form.Item>
      </Space>
      <Checkbox checked={verifyContents} onChange={event => setVerifyContents(event.target.checked)}>执行较慢的全量内容校验</Checkbox>
      <div className="notice"><Button type="primary" disabled={!privileged || busy || !username || !targetParent} onClick={() => void preflight()}>预检 home、空间与 inode</Button></div>
    </Form>
    {!privileged && <Alert className="notice" type="info" showIcon message="安装或升级可信特权助手后才可使用 home 迁移。" />}
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {status && <Descriptions className="notice" size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: 'phase', label: 'root 日志阶段', children: <Tag color={status.phase === 'observing' ? 'green' : status.phase === 'cleaned' ? 'blue' : 'orange'}>{status.phase}</Tag> },
      { key: 'source', label: '逻辑 home', children: status.source },
      { key: 'destination', label: '目标 home', children: status.destination },
      { key: 'required', label: '预计占用 + 余量', children: `${bytes(status.required_bytes)} + ${bytes(status.safety_margin_bytes)}` },
    ]} />}
    {task && <Card className="notice" size="small" title={`home 迁移任务 ${task.id}`} extra={<Tag color={task.state === 'succeeded' ? 'green' : task.state === 'failed' ? 'red' : task.state === 'interrupted' ? 'orange' : 'blue'}>{task.state}</Tag>}>
      <Steps size="small" items={task.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : step.state === 'interrupted' ? 'error' : 'wait' }))} />
      {task.error && <Alert className="notice" type={interrupted ? 'warning' : 'error'} showIcon message={task.error} />}
    </Card>}
    {(preflightSucceeded || interrupted || observing) && <Card className="notice" size="small" title="明确确认">
      <Space direction="vertical" className="scan-status">
        <Typography.Text>请输入当前动作要求的完整确认文本。</Typography.Text>
        <Input value={confirmation} onChange={event => setConfirmation(event.target.value)} />
        <Space wrap>
          {preflightSucceeded && <Button danger type="primary" disabled={confirmation !== expectedExecute} onClick={() => void execute()}>确认迁移（输入 {expectedExecute}）</Button>}
          {interrupted && <Button type="primary" disabled={confirmation !== expectedResume} onClick={() => void resume()}>从持久化阶段继续（输入 {expectedResume}）</Button>}
          {(interrupted || observing) && <Button danger disabled={confirmation !== expectedRollback} onClick={() => void rollback()}>回退原 home（输入 {expectedRollback}）</Button>}
          {observing && <Button danger disabled={confirmation !== expectedCleanup} onClick={() => void cleanup()}>人工试验完成，清理原备份（输入 {expectedCleanup}）</Button>}
        </Space>
      </Space>
    </Card>}
  </Card>
}
