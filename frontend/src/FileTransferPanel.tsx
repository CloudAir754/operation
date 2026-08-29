import { Alert, Button, Card, Descriptions, Form, Input, Space, Steps, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { cleanupFileTransfer, copyFileTransfer, loadTask, startFileTransferPreflight } from './api'
import type { PersistentTask } from './types'

interface Props {
  token: string
  privileged: boolean
}

const terminalStates = new Set(['succeeded', 'failed', 'cancelled', 'interrupted', 'rolled_back'])

function numberDetail(value: unknown): string {
  return typeof value === 'number' ? value.toLocaleString() : 'N/A'
}

export default function FileTransferPanel({ token, privileged }: Props) {
  const [source, setSource] = useState('')
  const [targetDirectory, setTargetDirectory] = useState('')
  const [task, setTask] = useState<PersistentTask | null>(null)
  const [preflightTaskId, setPreflightTaskId] = useState('')
  const [copyTaskId, setCopyTaskId] = useState('')
  const [cleanupConfirmation, setCleanupConfirmation] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!task || terminalStates.has(task.state)) return
    const timer = window.setInterval(() => {
      void loadTask(token, task.id).then(setTask).catch(reason => {
        setError(reason instanceof Error ? reason.message : '刷新文件迁移任务失败。')
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [task, token])

  async function preflight() {
    setError('')
    try {
      const next = await startFileTransferPreflight(token, source, targetDirectory)
      setPreflightTaskId(next.id)
      setCopyTaskId('')
      setTask(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '文件迁移预检失败。')
    }
  }

  async function copy() {
    setError('')
    try {
      const next = await copyFileTransfer(token, preflightTaskId)
      setCopyTaskId(next.id)
      setTask(next)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '文件复制失败。')
    }
  }

  async function cleanup() {
    setError('')
    try {
      setTask(await cleanupFileTransfer(token, copyTaskId, cleanupConfirmation))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '源路径清理失败。')
    }
  }

  const detail = task?.steps[task.steps.length - 1]?.detail ?? null
  const sourceSummary = detail?.source_summary && typeof detail.source_summary === 'object'
    ? detail.source_summary as Record<string, unknown>
    : null
  const copySucceeded = task?.kind === 'file_transfer_copy' && task.state === 'succeeded'
  const copiedSource = copySucceeded && typeof detail?.source === 'string' ? detail.source : ''
  const preflightSucceeded = task?.kind === 'file_transfer_preflight' && task.state === 'succeeded'
  const busy = Boolean(task && !terminalStates.has(task.state))

  return <Card id="file-transfer-panel" title="受控文件迁移" extra={<Tag color="orange">先复制，单独确认后清理源</Tag>}>
    <Alert className="notice" type="warning" showIcon message="不跟随符号链接、不跨文件系统，拒绝系统关键路径和目标冲突；复制完成不会自动删除源数据。" />
    <Form layout="vertical">
      <Form.Item label="明确源路径"><Input value={source} onChange={event => setSource(event.target.value)} placeholder="/home/user/project" /></Form.Item>
      <Form.Item label="目标目录"><Input value={targetDirectory} onChange={event => setTargetDirectory(event.target.value)} placeholder="/data/archive" /></Form.Item>
      <Space wrap>
        <Button type="primary" disabled={!privileged || busy || !source || !targetDirectory} onClick={() => void preflight()}>预检路径、空间与 inode</Button>
        <Button disabled={!preflightSucceeded || busy} onClick={() => void copy()}>复制、校验并发布（保留源）</Button>
      </Space>
    </Form>
    {!privileged && <Alert className="notice" type="info" showIcon message="安装或升级可信特权助手后才可使用文件迁移。" />}
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {task && <Card className="notice" size="small" title={`文件任务 ${task.id}`} extra={<Tag color={task.state === 'succeeded' ? 'green' : task.state === 'failed' ? 'red' : 'blue'}>{task.state}</Tag>}>
      <Steps size="small" items={task.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />
      {task.error && <Alert className="notice" type="error" showIcon message={task.error} />}
      {sourceSummary && <Descriptions className="notice" size="small" column={{ xs: 1, sm: 3 }} items={[
        { key: 'files', label: '文件', children: numberDetail(sourceSummary.files) },
        { key: 'directories', label: '目录', children: numberDetail(sourceSummary.directories) },
        { key: 'bytes', label: '逻辑字节', children: numberDetail(sourceSummary.logical_bytes) },
      ]} />}
      {copySucceeded && <Space direction="vertical" className="scan-status">
        <Alert type="success" showIcon message="目标已发布，源数据仍完整保留。请先人工验证目标，再决定是否清理源路径。" />
        <Typography.Text>要清理源路径，请完整输入：<Typography.Text code>{copiedSource}</Typography.Text></Typography.Text>
        <Space.Compact block><Input value={cleanupConfirmation} onChange={event => setCleanupConfirmation(event.target.value)} /><Button danger disabled={cleanupConfirmation !== copiedSource} onClick={() => void cleanup()}>独立确认清理源</Button></Space.Compact>
      </Space>}
    </Card>}
  </Card>
}
