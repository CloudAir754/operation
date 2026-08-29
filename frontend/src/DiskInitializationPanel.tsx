import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, Select, Space, Steps, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { executeDiskInitialization, loadDiskInitializationStatus, loadTask, loadTasks, planDiskInitialization, resumeDiskInitialization } from './api'
import type { DiskInitializationPlan, DiskInitializationStatus, PersistentTask, StorageSnapshot } from './types'

interface Props {
  token: string
  privileged: boolean
  devices: StorageSnapshot['devices']
}

const terminalStates = new Set(['succeeded', 'failed', 'cancelled', 'interrupted', 'rolled_back'])

function bytes(value: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export default function DiskInitializationPanel({ token, privileged, devices }: Props) {
  const disks = devices.filter(device => device.type === 'disk' && device.stable_identity)
  const [stableIdentity, setStableIdentity] = useState('')
  const [label, setLabel] = useState('')
  const [mountpoint, setMountpoint] = useState('')
  const [mountOptions, setMountOptions] = useState<string[]>(['defaults', 'nofail'])
  const [plan, setPlan] = useState<DiskInitializationPlan | null>(null)
  const [task, setTask] = useState<PersistentTask | null>(null)
  const [status, setStatus] = useState<DiskInitializationStatus | null>(null)
  const [operationId, setOperationId] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!privileged) return
    void loadTasks(token).then(tasks => {
      const recent = tasks.find(item => item.kind === 'disk_initialization' && (item.state === 'interrupted' || item.state === 'succeeded' || item.state === 'failed'))
      if (!recent) return
      setTask(recent)
      setOperationId(recent.id)
      const parameters = recent.parameters
      if (typeof parameters.stable_identity === 'string') setStableIdentity(parameters.stable_identity)
      if (typeof parameters.label === 'string') setLabel(parameters.label)
      if (typeof parameters.mountpoint === 'string') setMountpoint(parameters.mountpoint)
      if (Array.isArray(parameters.mount_options)) setMountOptions(parameters.mount_options.filter((item): item is string => typeof item === 'string'))
    }).catch(() => undefined)
  }, [privileged, token])

  useEffect(() => {
    if (!task || terminalStates.has(task.state)) return
    const timer = window.setInterval(() => {
      void loadTask(token, task.id).then(setTask).catch(reason => {
        setError(reason instanceof Error ? reason.message : '刷新磁盘初始化任务失败。')
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [task, token])

  useEffect(() => {
    if (!operationId || !privileged) return
    const refresh = () => {
      void loadDiskInitializationStatus(token, operationId).then(setStatus).catch(() => undefined)
    }
    refresh()
    const timer = window.setInterval(refresh, 2000)
    return () => window.clearInterval(timer)
  }, [operationId, privileged, token])

  async function preview() {
    setError('')
    setPlan(null)
    try {
      setPlan(await planDiskInitialization(token, { stable_identity: stableIdentity, label, mountpoint, mount_options: mountOptions }))
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '磁盘计划生成失败。')
    }
  }

  async function execute() {
    if (!plan) return
    setError('')
    try {
      const next = await executeDiskInitialization(token, {
        stable_identity: plan.stable_identity,
        confirmed_serial: confirmation,
        label: plan.label,
        mountpoint: plan.mountpoint,
        mount_options: mountOptions,
      })
      setTask(next)
      setOperationId(next.id)
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '磁盘初始化失败。')
    }
  }

  async function resume() {
    if (!task) return
    setError('')
    try {
      setTask(await resumeDiskInitialization(token, task.id, confirmation))
      setConfirmation('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '磁盘初始化恢复失败。')
    }
  }

  const busy = Boolean(task && !terminalStates.has(task.state))
  const interrupted = task?.state === 'interrupted'
  const expectedSerial = plan?.serial ?? status?.serial ?? ''

  return <Card id="disk-initialization-panel" title="初始化并挂载空磁盘" extra={<Tag color="red">不可逆：单盘 GPT + ext4</Tag>}>
    <Alert className="notice" type="error" showIcon message="只选择确认无数据的磁盘。系统盘、已挂载盘、已有分区/签名及 LVM、RAID、ZFS 成员会被拒绝；格式化后的原数据无法恢复。" />
    <Form layout="vertical">
      <Form.Item label="磁盘稳定身份"><Select value={stableIdentity || undefined} onChange={value => { setStableIdentity(value); setPlan(null) }} options={disks.map(disk => ({ value: disk.stable_identity!, label: `${disk.model ?? disk.path} · ${bytes(disk.size)} · ${disk.stable_identity}` }))} /></Form.Item>
      <Space wrap align="start">
        <Form.Item label="ext4 卷标"><Input value={label} onChange={event => { setLabel(event.target.value); setPlan(null) }} placeholder="research" /></Form.Item>
        <Form.Item label="挂载目录"><Input value={mountpoint} onChange={event => { setMountpoint(event.target.value); setPlan(null) }} placeholder="/mnt/research" /></Form.Item>
      </Space>
      <Form.Item label="挂载选项"><Checkbox.Group value={mountOptions} options={['defaults', 'nofail', 'noatime', 'nodev', 'nosuid']} onChange={values => { const next = values.map(String); setMountOptions(next.includes('defaults') ? next : ['defaults', ...next]); setPlan(null) }} /></Form.Item>
      <Button type="primary" disabled={!privileged || busy || !stableIdentity || !label || !mountpoint} onClick={() => void preview()}>生成纯预览命令计划</Button>
    </Form>
    {!privileged && <Alert className="notice" type="info" showIcon message="安装或升级可信特权助手后才可初始化磁盘。" />}
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {plan && <Card className="notice" size="small" title="破坏性命令计划（尚未执行）">
      <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
        { key: 'model', label: '型号', children: plan.model ?? 'N/A' },
        { key: 'serial', label: '确认序列号', children: <Typography.Text code>{plan.serial}</Typography.Text> },
        { key: 'size', label: '容量', children: bytes(plan.size) },
        { key: 'mount', label: '挂载点', children: plan.mountpoint },
      ]} />
      <ol>{plan.commands.map((command, index) => <li key={index}><Typography.Text code>{command.join(' ')}</Typography.Text></li>)}</ol>
      <Typography.Paragraph code copyable>{plan.fstab_preview}</Typography.Paragraph>
      <Alert className="notice" type="warning" showIcon message="执行前请完整输入上方磁盘序列号。确认值只在本次工作线程使用，不写入任务参数。" />
      <Space.Compact block><Input value={confirmation} onChange={event => setConfirmation(event.target.value)} /><Button danger type="primary" disabled={confirmation !== plan.serial || busy} onClick={() => void execute()}>确认清盘、格式化并挂载</Button></Space.Compact>
    </Card>}
    {status && <Descriptions className="notice" size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: 'phase', label: 'root 日志阶段', children: <Tag color={status.phase === 'succeeded' ? 'green' : status.phase === 'failed' ? 'red' : 'orange'}>{status.phase}</Tag> },
      { key: 'partition', label: '稳定分区', children: status.partition ?? '尚未创建' },
      { key: 'uuid', label: '文件系统 UUID', children: status.filesystem_uuid ?? '尚未生成' },
      { key: 'rollback', label: '失败时 fstab 恢复', children: status.fstab_restored === null ? 'N/A' : status.fstab_restored ? '已恢复' : '需人工检查' },
    ]} />}
    {task && <Card className="notice" size="small" title={`磁盘初始化任务 ${task.id}`} extra={<Tag color={task.state === 'succeeded' ? 'green' : task.state === 'failed' ? 'red' : task.state === 'interrupted' ? 'orange' : 'blue'}>{task.state}</Tag>}>
      <Steps size="small" items={task.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : step.state === 'interrupted' ? 'error' : 'wait' }))} />
      {task.error && <Alert className="notice" type={interrupted ? 'warning' : 'error'} showIcon message={task.error} />}
      {interrupted && <><Typography.Paragraph>恢复前再次完整输入磁盘序列号：<Typography.Text code>{expectedSerial}</Typography.Text></Typography.Paragraph><Space.Compact block><Input value={confirmation} onChange={event => setConfirmation(event.target.value)} /><Button danger disabled={!expectedSerial || confirmation !== expectedSerial} onClick={() => void resume()}>从持久化阶段恢复</Button></Space.Compact></>}
    </Card>}
  </Card>
}
