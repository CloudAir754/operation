import { CloudServerOutlined, DatabaseOutlined, DesktopOutlined } from '@ant-design/icons'
import { Alert, Card, Col, Descriptions, Input, Layout, Progress, Row, Space, Spin, Table, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { loadOverview, loadResources, loadUsers, readSessionToken } from './api'
import type { MountPoint, Overview, UserAccount } from './types'
import { filterUsersByUsername } from './userFilters'
import './styles.css'

const { Header, Content } = Layout

function bytes(value: number | null): string {
  if (value === null) return 'N/A'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [users, setUsers] = useState<UserAccount[]>([])
  const [usernameQuery, setUsernameQuery] = useState('')
  const [error, setError] = useState('')
  const [resourceWarning, setResourceWarning] = useState('')
  const [resourceUpdatedAt, setResourceUpdatedAt] = useState<Date | null>(null)

  useEffect(() => {
    const token = readSessionToken()
    if (!token) {
      setError('缺少启动会话令牌，请使用终端输出的完整地址访问。')
      return
    }
    let active = true
    Promise.all([loadOverview(token), loadUsers(token)])
      .then(([nextOverview, nextUsers]) => {
        if (!active) return
        setOverview(nextOverview)
        setUsers(nextUsers)
        setResourceUpdatedAt(new Date())
      })
      .catch((reason: unknown) => {
        if (!active) return
        setError(reason instanceof Error ? reason.message : '读取本机状态失败。')
      })
    const timer = window.setInterval(() => {
      loadResources(token)
        .then(resources => {
          if (!active) return
          setOverview(current => current ? { ...current, resources } : current)
          setResourceUpdatedAt(new Date())
          setResourceWarning('')
        })
        .catch((reason: unknown) => {
          if (!active) return
          setResourceWarning(reason instanceof Error ? reason.message : '刷新资源状态失败。')
        })
    }, 2000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  if (error) return <main className="center"><Alert type="error" showIcon message={error} /></main>
  if (!overview) return <main className="center"><Spin size="large" tip="正在读取本机状态" /></main>

  const { system, resources, gpu, mounts } = overview
  const filteredUsers = filterUsersByUsername(users, usernameQuery)
  const columns = [
    { title: '挂载点', dataIndex: 'mountpoint', key: 'mountpoint' },
    { title: '文件系统', dataIndex: 'filesystem', key: 'filesystem' },
    { title: '设备', dataIndex: 'device', key: 'device', ellipsis: true },
    { title: '已用', key: 'used', render: (_: unknown, row: MountPoint) => bytes(row.used) },
    { title: '可用', key: 'free', render: (_: unknown, row: MountPoint) => bytes(row.free) },
    { title: '状态', key: 'state', render: (_: unknown, row: MountPoint) => <Tag color={row.read_only ? 'orange' : 'green'}>{row.read_only ? '只读' : '可写'}</Tag> },
  ]

  return (
    <Layout className="page">
      <Header className="header"><Space><DesktopOutlined /><Typography.Title level={3}>LabOpsPanel</Typography.Title><Tag color="blue">只读原型</Tag></Space></Header>
      <Content className="content">
        <Alert className="notice" type="info" showIcon message="当前版本仅采集本机只读信息，不执行账号、磁盘或文件写操作。" />
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}><Card title={<Space><CloudServerOutlined />本机信息</Space>}>
            <Descriptions column={1} size="small" items={[
              { key: 'host', label: '主机名', children: system.hostname },
              { key: 'os', label: '系统', children: system.operating_system },
              { key: 'kernel', label: '内核', children: system.kernel },
              { key: 'arch', label: '架构', children: <Space>{system.architecture}<Tag color={system.supported_architecture ? 'green' : 'red'}>{system.supported_architecture ? '受支持' : '不受支持'}</Tag></Space> },
              { key: 'ip', label: '地址', children: system.addresses.join('、') || 'N/A' },
            ]} />
          </Card></Col>
          <Col xs={24} sm={12} lg={6}><Card title="CPU" extra={<Tag color="green">每 2 秒刷新</Tag>}>
            <Progress type="dashboard" percent={Math.round(resources.cpu.total_percent)} />
            <p>{resources.cpu.physical_cores ?? 'N/A'} 物理核 / {resources.cpu.logical_cores} 逻辑核</p>
          </Card></Col>
          <Col xs={24} sm={12} lg={6}><Card title="内存" extra={resourceUpdatedAt ? `更新于 ${resourceUpdatedAt.toLocaleTimeString()}` : null}>
            <Progress type="dashboard" percent={Math.round(resources.memory.percent)} />
            <p>{bytes(resources.memory.used)} / {bytes(resources.memory.total)}</p>
          </Card></Col>
          {resourceWarning && <Col span={24}><Alert type="warning" showIcon message={resourceWarning} /></Col>}
          <Col span={24}><Card title="NVIDIA GPU">
            {gpu.available ? <Space wrap>{gpu.devices.map(device => <Card size="small" key={device.uuid} title={`${device.index}: ${device.name}`}>利用率 {device.gpu_percent}% · 显存 {bytes(device.memory_used)} / {bytes(device.memory_total)}</Card>)}</Space> : <Alert type="warning" showIcon message="N/A" description={`未检测到可用的 NVIDIA GPU 或 NVML 驱动（${gpu.reason ?? '未知原因'}），其他功能不受影响。`} />}
          </Card></Col>
          <Col span={24}><Card title={<Space><DatabaseOutlined />挂载点</Space>}><Table rowKey={row => `${row.device}:${row.mountpoint}`} columns={columns} dataSource={mounts} pagination={{ pageSize: 8 }} scroll={{ x: 720 }} /></Card></Col>
          <Col span={24}><Card title="本机账号" extra={<Input.Search allowClear placeholder="搜索用户名" onChange={event => setUsernameQuery(event.target.value)} value={usernameQuery} className="user-search" />}>
            <Table<UserAccount>
              rowKey="uid"
              dataSource={filteredUsers}
              pagination={{ pageSize: 10 }}
              scroll={{ x: 900 }}
              columns={[
                { title: '用户名', dataIndex: 'username', key: 'username' },
                { title: 'UID / GID', key: 'identity', render: (_, row) => `${row.uid} / ${row.gid}` },
                { title: '显示名称', dataIndex: 'display_name', key: 'display_name', render: value => value || 'N/A' },
                { title: '逻辑 home', dataIndex: 'home', key: 'home' },
                { title: '物理 home', dataIndex: 'resolved_home', key: 'resolved_home' },
                { title: 'Shell', dataIndex: 'shell', key: 'shell' },
                { title: 'sudo 来源', key: 'sudo', render: (_, row) => row.sudo_sources.length ? row.sudo_sources.map(source => <Tag key={source} color="gold">{source}</Tag>) : '无' },
              ]}
            />
          </Card></Col>
        </Row>
      </Content>
    </Layout>
  )
}
