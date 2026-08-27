import { CloudServerOutlined, DatabaseOutlined, DesktopOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Descriptions, Input, Layout, Progress, Row, Space, Spin, Table, Tag, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { cancelHomeScan, loadGpu, loadOverview, loadResources, loadStorage, loadUsers, readSessionToken, startHomeScan, watchHomeScan } from './api'
import type { HomeScanTask, MountPoint, Overview, StorageSnapshot, UserAccount } from './types'
import { filterUsersByUsername } from './userFilters'
import './styles.css'

const { Header, Content } = Layout
const TreemapView = lazy(() => import('./TreemapView'))
const UserImportPanel = lazy(() => import('./UserImportPanel'))

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

function rate(value: number | null): string {
  return value === null ? '采样中' : `${bytes(value)}/s`
}

function Sparkline({ values, color }: { values: number[], color: string }) {
  if (values.length < 2) return <div className="sparkline-placeholder">等待趋势样本</div>
  const width = 180
  const height = 42
  const points = values.map((value, index) => {
    const x = index * width / (values.length - 1)
    const y = height - Math.max(0, Math.min(100, value)) * height / 100
    return `${x},${y}`
  }).join(' ')
  return <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="最近一分钟利用率趋势"><polyline fill="none" stroke={color} strokeWidth="2" points={points} /></svg>
}

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [users, setUsers] = useState<UserAccount[]>([])
  const [storage, setStorage] = useState<StorageSnapshot | null>(null)
  const [usernameQuery, setUsernameQuery] = useState('')
  const [error, setError] = useState('')
  const [resourceWarning, setResourceWarning] = useState('')
  const [resourceUpdatedAt, setResourceUpdatedAt] = useState<Date | null>(null)
  const [resourceHistory, setResourceHistory] = useState<Array<{ cpu: number, memory: number }>>([])
  const [homeScan, setHomeScan] = useState<HomeScanTask | null>(null)
  const [homeScanError, setHomeScanError] = useState('')
  const sessionToken = useRef('')
  const scanController = useRef<AbortController | null>(null)

  useEffect(() => {
    const token = readSessionToken()
    sessionToken.current = token
    if (!token) {
      setError('缺少启动会话令牌，请使用终端输出的完整地址访问。')
      return
    }
    let active = true
    Promise.all([loadOverview(token), loadUsers(token), loadStorage(token)])
      .then(([nextOverview, nextUsers, nextStorage]) => {
        if (!active) return
        setOverview(nextOverview)
        setUsers(nextUsers)
        setStorage(nextStorage)
        setResourceUpdatedAt(new Date())
        setResourceHistory([{ cpu: nextOverview.resources.cpu.total_percent, memory: nextOverview.resources.memory.percent }])
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
          setResourceHistory(current => [...current, { cpu: resources.cpu.total_percent, memory: resources.memory.percent }].slice(-30))
          setResourceWarning('')
        })
        .catch((reason: unknown) => {
          if (!active) return
          setResourceWarning(reason instanceof Error ? reason.message : '刷新资源状态失败。')
        })
      loadGpu(token)
        .then(gpu => {
          if (!active) return
          setOverview(current => current ? { ...current, gpu } : current)
        })
        .catch(() => undefined)
    }, 2000)
    return () => {
      active = false
      window.clearInterval(timer)
      scanController.current?.abort()
    }
  }, [])

  const scanHome = async (username: string) => {
    setHomeScanError('')
    scanController.current?.abort()
    try {
      const task = await startHomeScan(sessionToken.current, username)
      setHomeScan(task)
      const controller = new AbortController()
      scanController.current = controller
      await watchHomeScan(sessionToken.current, task.id, setHomeScan, controller.signal)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setHomeScanError(reason instanceof Error ? reason.message : '扫描 home 失败。')
    }
  }

  const stopHomeScan = async () => {
    if (!homeScan) return
    try {
      await cancelHomeScan(sessionToken.current, homeScan.id)
    } catch (reason) {
      setHomeScanError(reason instanceof Error ? reason.message : '取消扫描失败。')
    }
  }

  if (error) return <main className="center"><Alert type="error" showIcon message={error} /></main>
  if (!overview) return <main className="center"><Spin size="large" tip="正在读取本机状态" /></main>

  const { system, resources, gpu, mounts } = overview
  const filteredUsers = filterUsersByUsername(users, usernameQuery)
  const gpuUsers = [...new Set(gpu.processes.flatMap(item => item.username ? [item.username] : []))]
  const columns = [
    { title: '挂载点', dataIndex: 'mountpoint', key: 'mountpoint' },
    { title: '文件系统', dataIndex: 'filesystem', key: 'filesystem' },
    { title: '设备', dataIndex: 'device', key: 'device', ellipsis: true },
    { title: '已用', key: 'used', render: (_: unknown, row: MountPoint) => bytes(row.used) },
    { title: '可用', key: 'free', render: (_: unknown, row: MountPoint) => bytes(row.free) },
    { title: '可用 inode', key: 'inodes', render: (_: unknown, row: MountPoint) => row.inodes_free === null || row.inodes_total === null ? 'N/A' : `${row.inodes_free.toLocaleString()} / ${row.inodes_total.toLocaleString()}` },
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
              { key: 'directory', label: '目录账号来源', children: system.capabilities.directory_account_sources.join('、') || '未检测到' },
              { key: 'commands', label: '系统命令', children: `${Object.values(system.capabilities.commands).filter(Boolean).length} / ${Object.keys(system.capabilities.commands).length} 可用` },
            ]} />
          </Card></Col>
          <Col xs={24} sm={12} lg={6}><Card title="CPU" extra={<Tag color="green">每 2 秒刷新</Tag>}>
            <Progress type="dashboard" percent={Math.round(resources.cpu.total_percent)} />
            <p>{resources.cpu.physical_cores ?? 'N/A'} 物理核 / {resources.cpu.logical_cores} 逻辑核</p>
            <Typography.Text type="secondary">{resources.cpu.model}</Typography.Text>
            <Sparkline values={resourceHistory.map(item => item.cpu)} color="#1677ff" />
          </Card></Col>
          <Col xs={24} sm={12} lg={6}><Card title="内存" extra={resourceUpdatedAt ? `更新于 ${resourceUpdatedAt.toLocaleTimeString()}` : null}>
            <Progress type="dashboard" percent={Math.round(resources.memory.percent)} />
            <p>{bytes(resources.memory.used)} / {bytes(resources.memory.total)}</p>
            <Typography.Text type="secondary">可用 {bytes(resources.memory.available)} · Swap {bytes(resources.memory.swap_used)} / {bytes(resources.memory.swap_total)}</Typography.Text>
            <Sparkline values={resourceHistory.map(item => item.memory)} color="#13a8a8" />
          </Card></Col>
          <Col span={24}><Card title="按核 CPU 利用率"><Space wrap>{resources.cpu.per_core_percent.map((value, index) => <Tag key={index} color={value >= 90 ? 'red' : value >= 70 ? 'orange' : 'blue'}>CPU {index}: {Math.round(value)}%</Tag>)}</Space></Card></Col>
          {resourceWarning && <Col span={24}><Alert type="warning" showIcon message={resourceWarning} /></Col>}
          <Col span={24}><Card title="NVIDIA GPU">
            {gpu.available ? <Space wrap>{gpu.devices.map(device => <Card size="small" key={device.uuid} title={`${device.index}: ${device.name}`}>
              <p>GPU {device.gpu_percent}% · 显存活跃 {device.memory_activity_percent}%</p>
              <p>显存 {bytes(device.memory_used)} / {bytes(device.memory_total)}</p>
              <p>温度 {device.temperature_celsius ?? 'N/A'} °C · 风扇 {device.fan_percent ?? 'N/A'}% · 功耗 {device.power_watts?.toFixed(1) ?? 'N/A'} W</p>
              <p>PCIe RX {rate(device.pcie_rx_bytes_per_second)} · TX {rate(device.pcie_tx_bytes_per_second)}</p>
              <p>DRAM 带宽 {device.dram_bandwidth_percent === null ? 'N/A' : `${device.dram_bandwidth_percent}%`}</p>
              <Typography.Text type="secondary">{device.uuid} · {device.pci_bus_id}</Typography.Text>
            </Card>)}</Space> : <Alert type="warning" showIcon message="N/A" description={`未检测到可用的 NVIDIA GPU 或 NVML 驱动（${gpu.reason ?? '未知原因'}），其他功能不受影响。`} />}
          </Card></Col>
          {gpu.available && <Col span={24}><Card title="GPU 进程"><Table rowKey={row => `${row.gpu_uuid}:${row.pid}`} size="small" dataSource={gpu.processes} pagination={{ pageSize: 10 }} scroll={{ x: 1100 }} columns={[
            { title: 'GPU', dataIndex: 'gpu_index', key: 'gpu_index', sorter: (left, right) => left.gpu_index - right.gpu_index },
            { title: 'PID', dataIndex: 'pid', key: 'pid' },
            { title: '用户', dataIndex: 'username', key: 'username', filters: gpuUsers.map(value => ({ text: value, value })), onFilter: (value, row) => row.username === value },
            { title: '类型', key: 'types', render: (_, row) => row.types.join(' + ') },
            { title: '显存', key: 'memory', render: (_, row) => row.memory_used === null ? 'N/A' : `${bytes(row.memory_used)} (${row.memory_percent?.toFixed(1)}%)`, sorter: (left, right) => (left.memory_used ?? -1) - (right.memory_used ?? -1) },
            { title: 'SM', key: 'sm', render: (_, row) => row.sm_percent === null ? 'N/A' : `${row.sm_percent}%` },
            { title: '显存活跃', key: 'memory_activity', render: (_, row) => row.memory_activity_percent === null ? 'N/A' : `${row.memory_activity_percent}%` },
            { title: '采样窗口', key: 'sample_period', render: (_, row) => row.sample_period_seconds === null ? 'N/A' : `${row.sample_period_seconds} 秒` },
            { title: '进程带宽', dataIndex: 'bandwidth', key: 'bandwidth', render: () => 'N/A' },
            { title: '命令', dataIndex: 'command', key: 'command', ellipsis: true, render: value => value ?? '进程已退出' },
          ]} /></Card></Col>}
          <Col span={24}><Card title={<Space><DatabaseOutlined />挂载点</Space>}><Table rowKey={row => `${row.device}:${row.mountpoint}`} columns={columns} dataSource={mounts} pagination={{ pageSize: 8 }} scroll={{ x: 720 }} /></Card></Col>
          <Col span={24}><Card title="块设备与稳定身份">
            {storage?.available ? <Table rowKey={row => `${row.path}:${row.type}`} size="small" dataSource={storage.devices} pagination={{ pageSize: 10 }} scroll={{ x: 1100 }} columns={[
              { title: '设备', dataIndex: 'path', key: 'path' },
              { title: '类型', dataIndex: 'type', key: 'type' },
              { title: '型号', dataIndex: 'model', key: 'model', render: value => value ?? 'N/A' },
              { title: '容量', key: 'size', render: (_, row) => bytes(row.size) },
              { title: '文件系统', dataIndex: 'filesystem', key: 'filesystem', render: value => value ?? 'N/A' },
              { title: '挂载点', key: 'mountpoints', render: (_, row) => row.mountpoints.join('、') || '未挂载' },
              { title: '稳定身份', dataIndex: 'stable_identity', key: 'stable_identity', ellipsis: true, render: value => value ?? <Tag color="red">缺失</Tag> },
              { title: '只读', key: 'read_only', render: (_, row) => row.read_only ? '是' : '否' },
            ]} /> : <Alert type="warning" showIcon message="块设备信息不可用" description={storage?.error ?? '尚未读取'} />}
          </Card></Col>
          <Col span={24}><Card title="磁盘 I/O"><Table rowKey="name" size="small" dataSource={resources.disks} pagination={{ pageSize: 8 }} columns={[
            { title: '设备', dataIndex: 'name', key: 'name' },
            { title: '读取', key: 'read', render: (_, row) => rate(row.read_bytes_per_second) },
            { title: '写入', key: 'write', render: (_, row) => rate(row.write_bytes_per_second) },
            { title: '读取 IOPS', key: 'read_iops', render: (_, row) => row.read_iops?.toFixed(1) ?? '采样中' },
            { title: '写入 IOPS', key: 'write_iops', render: (_, row) => row.write_iops?.toFixed(1) ?? '采样中' },
            { title: '繁忙度', key: 'busy', render: (_, row) => row.busy_percent === null ? 'N/A' : `${row.busy_percent.toFixed(1)}%` },
          ]} /></Card></Col>
          <Col span={24}><Card title="网络"><Table rowKey="name" size="small" dataSource={resources.networks} pagination={{ pageSize: 8 }} scroll={{ x: 900 }} columns={[
            { title: '接口', dataIndex: 'name', key: 'name' },
            { title: '状态', key: 'state', render: (_, row) => <Tag color={row.is_up ? 'green' : 'default'}>{row.is_up ? '已连接' : '未连接'}</Tag> },
            { title: '地址', key: 'addresses', render: (_, row) => row.addresses.join('、') || 'N/A' },
            { title: '接收', key: 'received', render: (_, row) => rate(row.received_bytes_per_second) },
            { title: '发送', key: 'sent', render: (_, row) => rate(row.sent_bytes_per_second) },
            { title: '错误 / 丢包', key: 'errors', render: (_, row) => `${row.errors_in + row.errors_out} / ${row.drops_in + row.drops_out}` },
          ]} /></Card></Col>
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
                { title: '账号来源', dataIndex: 'account_source', key: 'account_source' },
                { title: 'home 文件系统', key: 'home_storage', render: (_, row) => row.home_storage ? <Space wrap><Tag>{row.home_storage.filesystem}</Tag><span>{row.home_storage.mountpoint}</span><span>可用 {bytes(row.home_storage.free)}</span>{row.home_storage.read_only && <Tag color="orange">只读</Tag>}</Space> : 'N/A' },
                { title: 'sudo 来源', key: 'sudo', render: (_, row) => <Space wrap>{row.sudo_sources.length ? row.sudo_sources.map(source => <Tag key={source} color="gold">{source}</Tag>) : '无'}{!row.sudo_sources_complete && <Tag color="orange">读取受限</Tag>}</Space> },
                { title: '空间扫描', key: 'scan', fixed: 'right', render: (_, row) => <Button size="small" onClick={() => void scanHome(row.username)} disabled={homeScan?.state === 'queued' || homeScan?.state === 'running'}>扫描 home</Button> },
              ]}
            />
          </Card></Col>
          {(homeScan || homeScanError) && <Col span={24}><Card title="home 空间扫描" extra={(homeScan?.state === 'queued' || homeScan?.state === 'running') && <Button danger onClick={() => void stopHomeScan()}>取消</Button>}>
            {homeScanError && <Alert type="error" showIcon message={homeScanError} />}
            {homeScan && <Space direction="vertical" className="scan-status">
              <Typography.Text>用户 {homeScan.username} · 状态 {homeScan.state} · 已扫描 {homeScan.nodes_scanned.toLocaleString()} 个节点</Typography.Text>
              <Typography.Text type="secondary">范围 {homeScan.root} · 不跨文件系统 · 不跟随符号链接 · 节点上限 {homeScan.max_nodes.toLocaleString()}</Typography.Text>
              {(homeScan.state === 'queued' || homeScan.state === 'running') && <Spin tip="限速扫描中" />}
              {homeScan.error && <Alert type="error" showIcon message={homeScan.error} />}
            </Space>}
            {homeScan?.result && <>
              <Alert className="notice" type="info" message={`快照时间 ${new Date(homeScan.result.scanned_at).toLocaleString()}；${homeScan.result.hardlink_policy}`} />
              <Suspense fallback={<Spin tip="正在加载 treemap" />}><TreemapView root={homeScan.result.tree} /></Suspense>
            </>}
          </Card></Col>}
          <Col span={24}><Suspense fallback={<Spin tip="正在加载账号导入工具" />}><UserImportPanel token={sessionToken.current} /></Suspense></Col>
        </Row>
      </Content>
    </Layout>
  )
}
