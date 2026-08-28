import { Alert, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Steps, Table, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { loadTask, lookupPort, terminateProcess } from './api'
import type { PersistentTask, PortUsageSnapshot } from './types'

export default function ProcessPanel({ token, privileged }: { token: string, privileged: boolean }) {
  const [result, setResult] = useState<PortUsageSnapshot | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [task, setTask] = useState<PersistentTask | null>(null)

  useEffect(() => {
    if (!task || ['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(task.state)) return
    const timer = window.setInterval(() => {
      loadTask(token, task.id).then(setTask).catch(reason => {
        setError(reason instanceof Error ? reason.message : '刷新进程任务失败。')
      })
    }, 750)
    return () => window.clearInterval(timer)
  }, [task, token])

  const search = async (values: { port: number, protocol: 'tcp' | 'udp' | 'any' }) => {
    setLoading(true)
    setError('')
    try {
      setResult(await lookupPort(token, values.port, values.protocol))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '端口查询失败。')
    } finally {
      setLoading(false)
    }
  }

  const confirmTermination = (row: PortUsageSnapshot['connections'][number]) => {
    if (row.pid === null || row.uid === null || row.create_time === null) return
    const expected = `结束进程:${row.pid}`
    let confirmation = ''
    Modal.confirm({
      title: `结束 PID ${row.pid}？`,
      content: <Space direction="vertical" className="full-width"><Typography.Text>将先发送 TERM，5 秒后仍存活才发送 KILL。请输入：<Typography.Text code>{expected}</Typography.Text></Typography.Text><Input onChange={event => { confirmation = event.target.value }} /></Space>,
      okText: '确认结束进程',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        if (confirmation !== expected) throw new Error('确认文本不匹配')
        try {
          setTask(await terminateProcess(token, { pid: row.pid!, uid: row.uid!, create_time: row.create_time! }, confirmation))
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : '终止进程失败。')
          throw reason
        }
      },
    })
  }

  return <Card title="端口占用与进程控制" extra={<Tag color={privileged ? 'green' : 'orange'}>{privileged ? '完整视图与终止已启用' : '查询可能不完整'}</Tag>}>
    <Form layout="inline" initialValues={{ protocol: 'any' }} onFinish={values => void search(values as { port: number, protocol: 'tcp' | 'udp' | 'any' })}>
      <Form.Item label="端口" name="port" rules={[{ required: true }]}><InputNumber min={1} max={65535} /></Form.Item>
      <Form.Item label="协议" name="protocol"><Select className="protocol-select" options={[{ value: 'any', label: 'TCP + UDP' }, { value: 'tcp', label: 'TCP' }, { value: 'udp', label: 'UDP' }]} /></Form.Item>
      <Form.Item><Button type="primary" htmlType="submit" loading={loading}>查询</Button></Form.Item>
    </Form>
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {result && <><Alert className="notice" type={result.complete ? 'success' : 'warning'} showIcon message={result.complete ? '已使用 root 只读视图完成查询' : result.reason} /><Table rowKey={(row, index) => `${row.protocol}:${row.pid ?? 'unknown'}:${index}`} size="small" dataSource={result.connections} pagination={false} scroll={{ x: 1100 }} locale={{ emptyText: '未发现该本地端口的连接或监听进程' }} columns={[
      { title: '协议', dataIndex: 'protocol', key: 'protocol' },
      { title: '地址', key: 'address', render: (_, row) => `${row.local_address ?? '*'}:${row.local_port}` },
      { title: '状态', dataIndex: 'status', key: 'status', render: value => value ?? 'N/A' },
      { title: 'PID', dataIndex: 'pid', key: 'pid', render: value => value ?? '权限受限' },
      { title: '用户', key: 'user', render: (_, row) => row.username ? `${row.username} (${row.uid})` : 'N/A' },
      { title: '命令', dataIndex: 'command', key: 'command', ellipsis: true, render: value => value ?? 'N/A' },
      { title: '操作', key: 'action', fixed: 'right', render: (_, row) => <Button danger size="small" disabled={!privileged || row.pid === null || row.uid === null || row.create_time === null} onClick={() => confirmTermination(row)}>结束进程</Button> },
    ]} /></>}
    {task && <Card className="notice" size="small" title={`进程任务 ${task.id}`} extra={<Tag color={task.state === 'succeeded' ? 'green' : task.state === 'failed' ? 'red' : 'blue'}>{task.state}</Tag>}><Steps size="small" items={task.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />{task.error && <Alert type="error" showIcon message={task.error} />}</Card>}
  </Card>
}
