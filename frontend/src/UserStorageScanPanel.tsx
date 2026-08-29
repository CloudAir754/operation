import { Alert, AutoComplete, Button, Card, Col, Descriptions, Form, InputNumber, Row, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { cancelUserStorageScan, loadAppSettings, startUserStorageScan, watchUserStorageScan } from './api'
import type { MountPoint, UserAccount, UserStorageScanTask } from './types'

const TreemapView = lazy(() => import('./TreemapView'))

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

interface Values {
  identity: string
  mountpoint: string
  max_depth: number
  include_hidden: boolean
  cross_filesystems: boolean
  include_unreadable: boolean
  throttle_milliseconds: number
}

export default function UserStorageScanPanel({ token, users, mounts }: { token: string, users: UserAccount[], mounts: MountPoint[] }) {
  const [form] = Form.useForm<Values>()
  const [task, setTask] = useState<UserStorageScanTask | null>(null)
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)

  useEffect(() => {
    void loadAppSettings(token).then(settings => {
      form.setFieldValue('throttle_milliseconds', settings.values.scan_throttle_milliseconds)
    }).catch(() => undefined)
    return () => controller.current?.abort()
  }, [form, token])

  const start = async (values: Values) => {
    setError('')
    controller.current?.abort()
    try {
      const created = await startUserStorageScan(token, values)
      setTask(created)
      const nextController = new AbortController()
      controller.current = nextController
      await watchUserStorageScan(token, created.id, setTask, nextController.signal)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : '用户占用扫描失败。')
    }
  }

  const cancel = async () => {
    if (!task) return
    try {
      setTask(await cancelUserStorageScan(token, task.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消扫描失败。')
    }
  }

  const running = task?.state === 'queued' || task?.state === 'running'
  const expired = task?.result ? Date.now() >= Date.parse(task.result.expires_at) : false

  return <Card title="按用户搜索磁盘占用" extra={running && <Button danger onClick={() => void cancel()}>取消扫描</Button>}>
    <Form<Values>
      form={form}
      layout="vertical"
      initialValues={{ max_depth: 8, include_hidden: true, cross_filesystems: false, include_unreadable: true, mountpoint: mounts[0]?.mountpoint }}
      onFinish={values => void start(values)}
    >
      <Row gutter={12}>
        <Col xs={24} md={6}><Form.Item label="用户名或 UID" name="identity" rules={[{ required: true }]}><AutoComplete placeholder="输入用户名或十进制 UID" filterOption={(input, option) => String(option?.label ?? '').toLowerCase().includes(input.toLowerCase())} options={users.map(user => ({ value: user.account_source === 'local' ? user.username : String(user.uid), label: `${user.username} · UID ${user.uid} · ${user.account_source}` }))} /></Form.Item></Col>
        <Col xs={24} md={8}><Form.Item label="完整挂载点" name="mountpoint" rules={[{ required: true }]}><Select options={mounts.map(mount => ({ value: mount.mountpoint, label: `${mount.mountpoint} · ${mount.filesystem}` }))} /></Form.Item></Col>
        <Col xs={12} md={3}><Form.Item label="扫描深度" name="max_depth"><InputNumber min={1} max={32} /></Form.Item></Col>
        <Col xs={12} md={3}><Form.Item label="每节点限速 (ms)" name="throttle_milliseconds" rules={[{ required: true }]}><InputNumber min={0} max={100} /></Form.Item></Col>
        <Col xs={24} md={4}><Form.Item label="操作"><Button type="primary" htmlType="submit" loading={running} disabled={!mounts.length}>开始扫描</Button></Form.Item></Col>
      </Row>
      <Space wrap>
        <Form.Item label="包含隐藏文件" name="include_hidden" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item label="跨文件系统" name="cross_filesystems" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item label="保留无权限目录标记" name="include_unreadable" valuePropName="checked"><Switch /></Form.Item>
      </Space>
    </Form>
    <Alert type="info" showIcon message="按文件稳定 UID 统计；目录仅作为层级容器。扫描不跟随符号链接，结果默认 15 分钟过期。当前服务权限不足时会明确标记盲区。" />
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    {task && <Space direction="vertical" className="scan-status">
      <Typography.Text>用户 {task.username}（UID {task.target_uid}）· 状态 {task.state} · 已检查 {task.nodes_scanned.toLocaleString()} 个节点</Typography.Text>
      <Typography.Text type="secondary">范围 {task.root} · 最大深度 {task.max_depth} · 每节点等待 {task.throttle_milliseconds} ms</Typography.Text>
      {running && <Spin tip="限速扫描中，可随时取消" />}
      {task.error && <Alert type="error" showIcon message={task.error} />}
    </Space>}
    {task?.result && <>
      <Alert className="notice" type={expired ? 'warning' : 'success'} showIcon message={expired ? '扫描快照已过期，不再作为实时结果展示' : `快照有效至 ${new Date(task.result.expires_at).toLocaleString()}`} />
      <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 5 }} items={[
        { key: 'logical', label: '逻辑总量', children: bytes(task.result.tree.logical_size) },
        { key: 'allocated', label: '实际占用', children: bytes(task.result.tree.allocated_size) },
        { key: 'files', label: '文件数', children: task.result.tree.file_count.toLocaleString() },
        { key: 'unreadable', label: '权限盲区', children: task.result.unreadable_count },
        { key: 'changed', label: '扫描中变化', children: task.result.changed_count },
      ]} />
      <Typography.Title level={5}>占用最大的文件</Typography.Title>
      <Table rowKey="path" size="small" pagination={{ pageSize: 5 }} dataSource={task.result.largest_paths} columns={[
        { title: '路径', dataIndex: 'path', key: 'path', ellipsis: true },
        { title: '逻辑大小', dataIndex: 'logical_size', key: 'logical_size', render: (value: number) => bytes(value) },
        { title: '实际占用', dataIndex: 'allocated_size', key: 'allocated_size', render: (value: number) => bytes(value) },
        { title: 'UID', dataIndex: 'owner_uid', key: 'owner_uid', render: (value: number) => <Tag>{value}</Tag> },
      ]} />
      {!expired && <Suspense fallback={<Spin tip="正在加载 treemap" />}><TreemapView root={task.result.tree} scannedAt={task.result.scanned_at} expiresAt={task.result.expires_at} /></Suspense>}
    </>}
  </Card>
}
