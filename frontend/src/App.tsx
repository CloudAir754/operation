import { CloudServerOutlined, DatabaseOutlined, DesktopOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Descriptions, Input, Layout, Modal, Progress, Row, Space, Spin, Steps, Table, Tag, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { cancelHomeScan, changeSudo, deleteUser, importSshKeys, loadGpu, loadOverview, loadPrivilegedCapability, loadResources, loadStorage, loadTask, loadUsers, readSessionToken, startHomeScan, startUserDeletionPreflight, terminateUserProcesses, watchHomeScan } from './api'
import type { HomeScanTask, MountPoint, Overview, PersistentTask, PrivilegedCapability, StorageSnapshot, UserAccount } from './types'
import { filterUsersByUsername } from './userFilters'
import './styles.css'

const { Header, Content } = Layout
const TreemapView = lazy(() => import('./TreemapView'))
const UserImportPanel = lazy(() => import('./UserImportPanel'))
const ProcessPanel = lazy(() => import('./ProcessPanel'))
const FileTransferPanel = lazy(() => import('./FileTransferPanel'))
const HomeMigrationPanel = lazy(() => import('./HomeMigrationPanel'))
const DiskInitializationPanel = lazy(() => import('./DiskInitializationPanel'))
const UserStorageScanPanel = lazy(() => import('./UserStorageScanPanel'))

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
  const [privileged, setPrivileged] = useState<PrivilegedCapability | null>(null)
  const [accessTask, setAccessTask] = useState<PersistentTask | null>(null)
  const [accessError, setAccessError] = useState('')
  const sessionToken = useRef('')
  const scanController = useRef<AbortController | null>(null)
  const reloadedDeletionTask = useRef('')

  useEffect(() => {
    const token = readSessionToken()
    sessionToken.current = token
    if (!token) {
      setError('缺少启动会话令牌，请使用终端输出的完整地址访问。')
      return
    }
    let active = true
    Promise.all([loadOverview(token), loadUsers(token), loadStorage(token), loadPrivilegedCapability(token)])
      .then(([nextOverview, nextUsers, nextStorage, nextPrivileged]) => {
        if (!active) return
        setOverview(nextOverview)
        setUsers(nextUsers)
        setStorage(nextStorage)
        setPrivileged(nextPrivileged)
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

  useEffect(() => {
    if (!accessTask || ['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(accessTask.state)) return
    const timer = window.setInterval(() => {
      loadTask(sessionToken.current, accessTask.id).then(setAccessTask).catch(reason => {
        setAccessError(reason instanceof Error ? reason.message : '刷新访问控制任务失败。')
      })
    }, 750)
    return () => window.clearInterval(timer)
  }, [accessTask])

  useEffect(() => {
    if (accessTask?.kind !== 'user_delete' || accessTask.state !== 'succeeded' || reloadedDeletionTask.current === accessTask.id) return
    reloadedDeletionTask.current = accessTask.id
    loadUsers(sessionToken.current).then(setUsers).catch(reason => {
      setAccessError(reason instanceof Error ? reason.message : '用户已删除，但刷新账号列表失败。')
    })
  }, [accessTask])

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

  const openSshImport = (username: string) => {
    let keys = ''
    Modal.confirm({
      title: `向 ${username} 导入 SSH 公钥`,
      content: <Input.TextArea rows={7} placeholder="每行一条 OpenSSH 公钥；不接受私钥或 authorized_keys 选项" onChange={event => { keys = event.target.value }} />,
      okText: '校验并导入',
      cancelText: '取消',
      onOk: async () => {
        if (!keys.trim()) throw new Error('请粘贴至少一条公钥')
        setAccessError('')
        setAccessTask(await importSshKeys(sessionToken.current, username, keys))
      },
    })
  }

  const confirmSudo = (username: string, grant: boolean) => {
    Modal.confirm({
      title: grant ? `授予 ${username} sudo 权限？` : `撤销 ${username} 的托管 sudo 规则？`,
      content: grant ? '将写入独立 sudoers.d 文件，并在写入前后运行 visudo。' : '只会撤销 LabOpsPanel 创建且内容未被手工修改的规则，不会改动 sudo 组或其他规则。',
      okText: grant ? '确认授权' : '确认撤销',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setAccessError('')
        setAccessTask(await changeSudo(sessionToken.current, username, grant))
      },
    })
  }

  const confirmTerminateUserProcesses = (username: string) => {
    const expected = `结束全部进程:${username}`
    let confirmation = ''
    Modal.confirm({
      title: `结束 ${username} 的全部进程？`,
      content: <Space direction="vertical" className="full-width"><Typography.Text>将按 UID 向全部进程发送 TERM，等待后按需发送 KILL，并重复扫描。请输入：<Typography.Text code>{expected}</Typography.Text></Typography.Text><Input onChange={event => { confirmation = event.target.value }} /></Space>,
      okText: '确认结束全部进程',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        if (confirmation !== expected) throw new Error('确认文本不匹配')
        try {
          setAccessTask(await terminateUserProcesses(sessionToken.current, username, confirmation))
        } catch (reason) {
          setAccessError(reason instanceof Error ? reason.message : '结束用户进程失败。')
          throw reason
        }
      },
    })
  }

  const preflightUserDeletion = async (username: string) => {
    setAccessError('')
    try {
      setAccessTask(await startUserDeletionPreflight(sessionToken.current, username))
    } catch (reason) {
      setAccessError(reason instanceof Error ? reason.message : '用户删除预检启动失败。')
    }
  }

  const confirmUserDeletion = (username: string, preflightTaskId: string, deleteHome: boolean) => {
    let confirmation = ''
    Modal.confirm({
      title: deleteHome ? `删除 ${username} 及其 home？` : `仅删除账号 ${username}？`,
      content: <Space direction="vertical" className="full-width"><Alert type="warning" showIcon message={deleteHome ? '账号删除后，home 将先在同一文件系统隔离，再清理。此操作不可恢复。' : '账号将被删除，但 home 数据会原样保留。'} /><Typography.Text>请输入完整用户名 <Typography.Text code>{username}</Typography.Text>：</Typography.Text><Input onChange={event => { confirmation = event.target.value }} /></Space>,
      okText: deleteHome ? '确认删除账号和 home' : '确认仅删除账号',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        if (confirmation !== username) throw new Error('确认用户名不匹配')
        try {
          setAccessError('')
          setAccessTask(await deleteUser(sessionToken.current, username, preflightTaskId, deleteHome, confirmation))
        } catch (reason) {
          setAccessError(reason instanceof Error ? reason.message : '删除用户任务启动失败。')
          throw reason
        }
      },
    })
  }

  if (error) return <main className="center"><Alert type="error" showIcon message={error} /></main>
  if (!overview) return <main className="center"><Spin size="large" tip="正在读取本机状态" /></main>

  const { system, resources, gpu, mounts } = overview
  const filteredUsers = filterUsersByUsername(users, usernameQuery)
  const deletionPreflightDetail = accessTask?.kind === 'user_delete_preflight' && accessTask.state === 'succeeded'
    ? accessTask.steps.find(step => step.sequence === 3)?.detail ?? null
    : null
  const deletionDependencyDetail = accessTask?.kind === 'user_delete_preflight' && accessTask.state === 'succeeded'
    ? accessTask.steps.find(step => step.sequence === 2)?.detail ?? null
    : null
  const deletionUsername = typeof accessTask?.parameters.username === 'string' ? accessTask.parameters.username : ''
  const deletionPreflightTaskId = accessTask?.kind === 'user_delete_preflight' ? accessTask.id : ''
  const deletionBlockers = Array.isArray(deletionPreflightDetail?.blockers) ? deletionPreflightDetail.blockers.filter((item): item is string => typeof item === 'string') : []
  const homeDeleteBlockers = Array.isArray(deletionPreflightDetail?.home_delete_blockers) ? deletionPreflightDetail.home_delete_blockers.filter((item): item is string => typeof item === 'string') : []
  const deletionWarnings = Array.isArray(deletionPreflightDetail?.warnings) ? deletionPreflightDetail.warnings.filter((item): item is string => typeof item === 'string') : []
  const processCount = Array.isArray(deletionDependencyDetail?.processes) ? deletionDependencyDetail.processes.length : 0
  const sessionCount = Array.isArray(deletionDependencyDetail?.sessions) ? deletionDependencyDetail.sessions.length : 0
  const diskUsage = deletionPreflightDetail?.disk_usage && typeof deletionPreflightDetail.disk_usage === 'object' ? deletionPreflightDetail.disk_usage as Record<string, unknown> : null
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
      <Header className="header"><Space><DesktopOutlined /><Typography.Title level={3}>LabOpsPanel</Typography.Title><Tag color="blue">阶段 3</Tag></Space></Header>
      <Content className="content">
        <Alert className="notice" type={privileged?.available ? 'warning' : 'info'} showIcon message={privileged?.available ? '账号、文件、ext4 home 迁移和空盘 GPT + ext4 初始化写操作已启用；所有破坏性步骤仍需预检与明确确认。' : '当前仅开放只读功能；系统写操作需要管理员另行安装可信特权助手。'} />
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
          <Col span={24}><Suspense fallback={<Spin tip="正在加载端口查询工具" />}><ProcessPanel token={sessionToken.current} privileged={Boolean(privileged?.available)} /></Suspense></Col>
          <Col span={24}><Card title="本机账号" extra={<Input.Search allowClear placeholder="搜索用户名" onChange={event => setUsernameQuery(event.target.value)} value={usernameQuery} className="user-search" />}>
            <Table<UserAccount>
              rowKey="uid"
              dataSource={filteredUsers}
              pagination={{ pageSize: 10 }}
              scroll={{ x: 1200 }}
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
                { title: '登录与权限', key: 'access', fixed: 'right', render: (_, row) => <Space wrap><Button size="small" disabled={!privileged?.available} onClick={() => openSshImport(row.username)}>导入 SSH 公钥</Button><Button size="small" danger disabled={!privileged?.available} onClick={() => confirmSudo(row.username, true)}>授予 sudo</Button><Button size="small" disabled={!privileged?.available} onClick={() => confirmSudo(row.username, false)}>撤销托管规则</Button><Button size="small" danger disabled={!privileged?.available} onClick={() => confirmTerminateUserProcesses(row.username)}>结束全部进程</Button><Button size="small" danger disabled={!privileged?.available} onClick={() => void preflightUserDeletion(row.username)}>删除预检</Button></Space> },
              ]}
            />
            {accessError && <Alert className="notice" type="error" showIcon message={accessError} />}
            {accessTask && <Card className="notice" size="small" title={`访问控制任务 ${accessTask.id}`} extra={<Tag color={accessTask.state === 'succeeded' ? 'green' : accessTask.state === 'failed' ? 'red' : 'blue'}>{accessTask.state}</Tag>}><Steps size="small" items={accessTask.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />{accessTask.error && <Alert type="error" showIcon message={accessTask.error} />}</Card>}
            {deletionPreflightDetail && deletionDependencyDetail && <Card className="notice" size="small" title={`删除预检：${deletionUsername}`}>
              <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
                { key: 'processes', label: '进程 / 会话', children: `${processCount} / ${sessionCount}` },
                { key: 'home', label: '物理 home', children: String(deletionPreflightDetail.resolved_home ?? 'N/A') },
                { key: 'usage', label: 'home 占用', children: diskUsage && typeof diskUsage.allocated_bytes === 'number' ? `${bytes(diskUsage.allocated_bytes)} / ${Number(diskUsage.inodes ?? 0).toLocaleString()} inode` : 'N/A' },
                { key: 'delete-home', label: '可自动删除 home', children: deletionPreflightDetail.delete_home_supported === true ? <Tag color="green">可以</Tag> : <Tag color="orange">不可</Tag> },
              ]} />
              {deletionWarnings.length > 0 && <Alert className="notice" type="warning" showIcon message="预检警告" description={deletionWarnings.join('；')} />}
              {deletionBlockers.length > 0 && <Alert className="notice" type="error" showIcon message="存在阻断项，不能删除账号" description={deletionBlockers.join('；')} />}
              {homeDeleteBlockers.length > 0 && <Alert className="notice" type="warning" showIcon message="home 不能自动删除，但仍可仅删除账号" description={homeDeleteBlockers.join('；')} />}
              <Space wrap><Button danger disabled={deletionBlockers.length > 0} onClick={() => confirmUserDeletion(deletionUsername, deletionPreflightTaskId, false)}>仅删除账号并保留 home</Button><Button danger type="primary" disabled={deletionBlockers.length > 0 || deletionPreflightDetail.delete_home_supported !== true} onClick={() => confirmUserDeletion(deletionUsername, deletionPreflightTaskId, true)}>删除账号及 home</Button><Typography.Text type="secondary">预检十分钟内有效；执行前会再次检查身份和阻断项。</Typography.Text></Space>
            </Card>}
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
              <Alert className="notice" type={homeScan.snapshot_expired ? 'warning' : 'info'} message={homeScan.snapshot_expired ? '扫描快照已过期，不再作为实时结果展示' : `快照时间 ${new Date(homeScan.result.scanned_at).toLocaleString()}；有效至 ${new Date(homeScan.result.expires_at).toLocaleString()}；${homeScan.result.hardlink_policy}`} />
              {!homeScan.snapshot_expired && <Suspense fallback={<Spin tip="正在加载 treemap" />}><TreemapView root={homeScan.result.tree} scannedAt={homeScan.result.scanned_at} expiresAt={homeScan.result.expires_at} /></Suspense>}
            </>}
          </Card></Col>}
          <Col span={24}><Suspense fallback={<Spin tip="正在加载用户占用扫描" />}><UserStorageScanPanel token={sessionToken.current} users={users} mounts={overview?.mounts ?? []} /></Suspense></Col>
          <Col span={24}><Suspense fallback={<Spin tip="正在加载文件迁移工具" />}><FileTransferPanel token={sessionToken.current} privileged={Boolean(privileged?.available)} /></Suspense></Col>
          <Col span={24}><Suspense fallback={<Spin tip="正在加载 home 迁移工具" />}><HomeMigrationPanel token={sessionToken.current} privileged={Boolean(privileged?.available)} users={users} /></Suspense></Col>
          <Col span={24}><Suspense fallback={<Spin tip="正在加载磁盘初始化工具" />}><DiskInitializationPanel token={sessionToken.current} privileged={Boolean(privileged?.available)} devices={storage?.devices ?? []} /></Suspense></Col>
          <Col span={24}><Suspense fallback={<Spin tip="正在加载账号导入工具" />}><UserImportPanel token={sessionToken.current} /></Suspense></Col>
        </Row>
      </Content>
    </Layout>
  )
}
