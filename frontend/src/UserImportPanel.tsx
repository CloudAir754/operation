import { Alert, Button, Card, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Steps, Switch, Table, Tabs, Tag, Typography, Upload } from 'antd'
import { useEffect, useState } from 'react'
import { applyPamChange, createUsers, deleteCredentials, downloadCredentials, loadDefaultPasswordPolicy, loadPamPolicy, loadPrivilegedCapability, loadTask, previewFileImport, previewPamChange, previewPasswordPolicy, previewTextImport, restorePamBackup } from './api'
import type { PamPolicyChangePreview, PamPolicyChangeValues, PamPolicySnapshot, PasswordPolicySummary, PasswordPolicyValues, PersistentTask, PrivilegedCapability, UserImportPreview } from './types'

function taskBackupId(task: PersistentTask | null): string | null {
  for (const step of task?.steps ?? []) {
    const value = step.detail?.backup_id
    if (typeof value === 'string' && /^[0-9a-f]{32}$/.test(value)) return value
  }
  return null
}

export default function UserImportPanel({ token }: { token: string }) {
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileKind, setFileKind] = useState<'csv' | 'xlsx'>('csv')
  const [preview, setPreview] = useState<UserImportPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [loading, setLoading] = useState(false)
  const [password, setPassword] = useState<PasswordPolicySummary | null>(null)
  const [passwordError, setPasswordError] = useState('')
  const [pam, setPam] = useState<PamPolicySnapshot | null>(null)
  const [capability, setCapability] = useState<PrivilegedCapability | null>(null)
  const [task, setTask] = useState<PersistentTask | null>(null)
  const [taskError, setTaskError] = useState('')
  const [pamPreview, setPamPreview] = useState<PamPolicyChangePreview | null>(null)
  const [pamTask, setPamTask] = useState<PersistentTask | null>(null)
  const [pamError, setPamError] = useState('')

  useEffect(() => {
    loadDefaultPasswordPolicy(token).then(setPassword).catch(reason => {
      setPasswordError(reason instanceof Error ? reason.message : '读取密码规则失败。')
    })
    loadPamPolicy(token).then(setPam).catch(reason => {
      setPasswordError(reason instanceof Error ? reason.message : '读取 PAM 策略失败。')
    })
    loadPrivilegedCapability(token).then(setCapability).catch(reason => {
      setTaskError(reason instanceof Error ? reason.message : '读取特权能力失败。')
    })
  }, [token])

  useEffect(() => {
    if (!task || ['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(task.state)) return
    const timer = window.setInterval(() => {
      loadTask(token, task.id).then(setTask).catch(reason => {
        setTaskError(reason instanceof Error ? reason.message : '刷新任务失败。')
      })
    }, 750)
    return () => window.clearInterval(timer)
  }, [task, token])

  useEffect(() => {
    if (!pamTask) return
    if (['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(pamTask.state)) {
      if (pamTask.state === 'succeeded') void loadPamPolicy(token).then(setPam)
      return
    }
    const timer = window.setInterval(() => {
      loadTask(token, pamTask.id).then(setPamTask).catch(reason => {
        setPamError(reason instanceof Error ? reason.message : '刷新 PAM 任务失败。')
      })
    }, 750)
    return () => window.clearInterval(timer)
  }, [pamTask, token])

  const runPreview = async (mode: 'text' | 'file') => {
    setLoading(true)
    setPreviewError('')
    try {
      setPreview(mode === 'text'
        ? await previewTextImport(token, text)
        : file ? await previewFileImport(token, fileKind, file) : null)
      if (mode === 'file' && !file) setPreviewError('请先选择文件。')
    } catch (reason) {
      setPreviewError(reason instanceof Error ? reason.message : '预览失败。')
    } finally {
      setLoading(false)
    }
  }

  const updatePassword = async (_: unknown, values: PasswordPolicyValues) => {
    setPasswordError('')
    try {
      setPassword(await previewPasswordPolicy(token, values))
    } catch (reason) {
      setPasswordError(reason instanceof Error ? reason.message : '密码规则无效。')
    }
  }

  const startCreation = () => {
    if (!preview || !password) return
    const rows = preview.rows.filter(row => row.status === 'will_create').map(row => ({ username: row.username, display_name: row.display_name }))
    Modal.confirm({
      title: `确认创建 ${rows.length} 个本机账号？`,
      content: '将生成随机初始密码并写入系统。任务结束后请立即下载一次性凭据文件并妥善分发。',
      okText: '确认创建',
      cancelText: '取消',
      onOk: async () => {
        setTaskError('')
        try {
          setTask(await createUsers(token, rows, password.policy))
        } catch (reason) {
          setTaskError(reason instanceof Error ? reason.message : '创建账号失败。')
        }
      },
    })
  }

  const runPamPreview = async (values: PamPolicyChangeValues) => {
    setPamError('')
    try {
      setPamPreview(await previewPamChange(token, values))
    } catch (reason) {
      setPamError(reason instanceof Error ? reason.message : 'PAM 修改预览失败。')
    }
  }

  const confirmPamApply = () => {
    if (!pamPreview?.supported) return
    let confirmation = ''
    Modal.confirm({
      title: '修改全机 PAM 密码策略？',
      content: <Space direction="vertical" className="full-width"><Typography.Text>该规则会影响所有本地用户。请输入：<Typography.Text code>{pamPreview.confirmation_text}</Typography.Text></Typography.Text><Input onChange={event => { confirmation = event.target.value }} /></Space>,
      okText: '备份并修改',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        if (confirmation !== pamPreview.confirmation_text) throw new Error('确认文本不匹配')
        try {
          setPamTask(await applyPamChange(token, pamPreview.requested, confirmation))
        } catch (reason) {
          setPamError(reason instanceof Error ? reason.message : 'PAM 修改失败。')
          throw reason
        }
      },
    })
  }

  const confirmPamRestore = (backupId: string) => {
    const expected = `恢复PAM备份:${backupId}`
    let confirmation = ''
    Modal.confirm({
      title: '恢复修改前的 PAM 策略？',
      content: <Space direction="vertical" className="full-width"><Typography.Text>只有当前配置仍等于该次应用结果时才允许恢复。请输入：<Typography.Text code>{expected}</Typography.Text></Typography.Text><Input onChange={event => { confirmation = event.target.value }} /></Space>,
      okText: '验证并恢复',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        if (confirmation !== expected) throw new Error('确认文本不匹配')
        try {
          setPamTask(await restorePamBackup(token, backupId, confirmation))
        } catch (reason) {
          setPamError(reason instanceof Error ? reason.message : 'PAM 恢复失败。')
          throw reason
        }
      },
    })
  }

  return <Row gutter={[16, 16]}>
    <Col span={24}><Card id="user-import-panel" title="批量账号导入与创建" extra={<Tag color={capability?.available ? 'green' : 'orange'}>{capability?.available ? '特权助手可用' : '创建功能未启用'}</Tag>}>
      <Tabs items={[
        { key: 'text', label: '粘贴文本', children: <Space direction="vertical" className="full-width"><Input.TextArea rows={6} value={text} onChange={event => setText(event.target.value)} placeholder={'username 中文姓名\nalice 张三'} /><Button type="primary" loading={loading} onClick={() => void runPreview('text')}>解析并预览</Button></Space> },
        { key: 'csv', label: 'CSV', children: <Space><Upload maxCount={1} accept=".csv,text/csv" beforeUpload={value => { setFile(value); setFileKind('csv'); return false }} onRemove={() => { setFile(null); return true }}><Button>选择 UTF-8 CSV</Button></Upload><Button type="primary" loading={loading} onClick={() => void runPreview('file')}>解析并预览</Button></Space> },
        { key: 'xlsx', label: 'XLSX', children: <Space><Upload maxCount={1} accept=".xlsx" beforeUpload={value => { setFile(value); setFileKind('xlsx'); return false }} onRemove={() => { setFile(null); return true }}><Button>选择 XLSX</Button></Upload><Button type="primary" loading={loading} onClick={() => void runPreview('file')}>解析并预览</Button></Space> },
      ]} />
      {previewError && <Alert type="error" showIcon message={previewError} />}
      {preview && <>
        <Space className="preview-summary" wrap><Tag color="green">将创建 {preview.counts.will_create}</Tag><Tag>已存在 {preview.counts.exists}</Tag><Tag color="red">无法创建 {preview.counts.invalid}</Tag><Tag>表头：{preview.header_detected ? '已识别' : '无'}</Tag></Space>
        <Table rowKey="source_row" size="small" dataSource={preview.rows} pagination={{ pageSize: 10 }} columns={[
          { title: '源行', dataIndex: 'source_row', key: 'source_row' },
          { title: '用户名', dataIndex: 'username', key: 'username' },
          { title: '中文姓名', dataIndex: 'display_name', key: 'display_name' },
          { title: '结果', key: 'status', render: (_, row) => <Tag color={row.status === 'will_create' ? 'green' : row.status === 'invalid' ? 'red' : 'default'}>{row.status === 'will_create' ? '将创建' : row.status === 'exists' ? '已存在' : '无法创建'}</Tag> },
          { title: '原因', key: 'errors', render: (_, row) => row.errors.join('；') || '—' },
        ]} />
        <Button type="primary" danger disabled={!capability?.available || preview.counts.will_create === 0 || preview.counts.invalid > 0} onClick={startCreation}>确认并创建账号</Button>
      </>}
      {!capability?.available && capability?.reason && <Alert className="notice" type="info" showIcon message={capability.reason} description={`需由管理员安装可信助手：${capability.path}`} />}
      {taskError && <Alert className="notice" type="error" showIcon message={taskError} />}
      {task && <Card className="notice" size="small" title={`创建任务 ${task.id}`} extra={<Tag color={task.state === 'succeeded' ? 'green' : task.state === 'failed' ? 'red' : 'blue'}>{task.state}</Tag>}>
        <Steps direction="vertical" size="small" items={task.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />
        {task.error && <Alert type="error" showIcon message={task.error} />}
        {['succeeded', 'failed'].includes(task.state) && <Space><Button type="primary" onClick={() => void downloadCredentials(token, task.id).catch(reason => setTaskError(reason instanceof Error ? reason.message : '下载失败。'))}>下载一次性凭据 CSV</Button><Button danger onClick={() => void deleteCredentials(token, task.id).then(() => setTaskError('凭据文件已删除。')).catch(reason => setTaskError(reason instanceof Error ? reason.message : '删除失败。'))}>删除凭据文件</Button></Space>}
      </Card>}
    </Card></Col>
    <Col span={24}><Card title="随机初始密码规则">
      {passwordError && <Alert className="notice" type="error" showIcon message={passwordError} />}
      {password && <Form<PasswordPolicyValues> layout="inline" initialValues={password.policy} onValuesChange={(changed, values) => void updatePassword(changed, values)}>
        <Form.Item label="长度" name="length"><InputNumber min={15} max={128} /></Form.Item>
        <Form.Item label="大写" name="min_uppercase"><InputNumber min={0} max={20} /></Form.Item>
        <Form.Item label="小写" name="min_lowercase"><InputNumber min={0} max={20} /></Form.Item>
        <Form.Item label="数字" name="min_digits"><InputNumber min={0} max={20} /></Form.Item>
        <Form.Item label="符号" name="min_symbols"><InputNumber min={0} max={20} /></Form.Item>
        <Form.Item label="排除易混淆字符" name="exclude_ambiguous" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item label="允许符号" name="symbols"><Input /></Form.Item>
      </Form>}
      {password && <Space direction="vertical" className="password-summary"><Typography.Text code>{password.masked_example}</Typography.Text><Typography.Text>估算熵 {password.estimated_entropy_bits} bits；实际生成时仍须满足本机 PAM。</Typography.Text></Space>}
    </Card></Col>
    <Col span={24}><Card title="Ubuntu 实际 PAM 密码策略" extra={<Tag color={pam?.modification_supported ? 'green' : 'orange'}>{pam?.modification_supported ? '结构可安全修改' : '当前只读'}</Tag>}>
      {pam && <Space direction="vertical" className="full-width">
        {!pam.modification_supported && <Alert type="warning" showIcon message={pam.modification_reason} />}
        {pam.warnings.map(warning => <Alert key={warning} type="warning" showIcon message={warning} />)}
        <Typography.Text>pwquality 模块：{pam.pwquality_module_installed ? '已安装' : '未安装'}；PAM 堆栈：{pam.pwquality_enabled ? '已启用' : '未启用'}</Typography.Text>
        <Typography.Text>应用生成下限：长度 {pam.generation_minimums.length}，大写 {pam.generation_minimums.min_uppercase}，小写 {pam.generation_minimums.min_lowercase}，数字 {pam.generation_minimums.min_digits}，符号 {pam.generation_minimums.min_symbols}</Typography.Text>
        <Table rowKey="line" size="small" pagination={false} dataSource={pam.password_stack} columns={[
          { title: '行', dataIndex: 'line', key: 'line' },
          { title: '控制', dataIndex: 'control', key: 'control' },
          { title: '模块', dataIndex: 'module', key: 'module' },
          { title: '选项', key: 'options', render: (_, row) => row.options.join(' ') || '—' },
        ]} />
        <Typography.Text type="secondary">配置来源：{pam.config_sources.join('、') || '未检测到 pwquality 配置文件'}。首版不管理密码周期性到期。</Typography.Text>
        <Card size="small" title="修改 pwquality 主配置">
          <Alert className="notice" type="warning" showIcon message="修改会影响全机本地用户；空白字段保持当前值。模块行参数优先级更高时会阻断冲突字段。" />
          <Form<PamPolicyChangeValues> layout="inline" onFinish={values => void runPamPreview(values)}>
            <Form.Item label="最小长度" name="minlen"><InputNumber min={15} max={128} placeholder={String(pam.settings.minlen ?? 15)} /></Form.Item>
            <Form.Item label="最少类别" name="minclass"><InputNumber min={0} max={4} placeholder={String(pam.settings.minclass ?? 0)} /></Form.Item>
            <Form.Item label="大写 credit" name="ucredit"><InputNumber min={-20} max={0} placeholder={String(pam.settings.ucredit ?? 0)} /></Form.Item>
            <Form.Item label="小写 credit" name="lcredit"><InputNumber min={-20} max={0} placeholder={String(pam.settings.lcredit ?? 0)} /></Form.Item>
            <Form.Item label="数字 credit" name="dcredit"><InputNumber min={-20} max={0} placeholder={String(pam.settings.dcredit ?? 0)} /></Form.Item>
            <Form.Item label="符号 credit" name="ocredit"><InputNumber min={-20} max={0} placeholder={String(pam.settings.ocredit ?? 0)} /></Form.Item>
            <Form.Item label="最大重复" name="maxrepeat"><InputNumber min={0} max={128} placeholder={String(pam.settings.maxrepeat ?? 0)} /></Form.Item>
            <Form.Item label="最大连续" name="maxsequence"><InputNumber min={0} max={128} placeholder={String(pam.settings.maxsequence ?? 0)} /></Form.Item>
            <Form.Item label="同类连续" name="maxclassrepeat"><InputNumber min={0} max={128} placeholder={String(pam.settings.maxclassrepeat ?? 0)} /></Form.Item>
            <Form.Item label="用户名检查" name="usercheck"><Select allowClear placeholder={String(pam.settings.usercheck ?? true)} options={[{ value: true, label: '启用' }, { value: false, label: '禁用' }]} /></Form.Item>
            <Form.Item label="重试次数" name="retry"><InputNumber min={1} max={10} placeholder={String(pam.settings.retry ?? 3)} /></Form.Item>
            <Form.Item label="与旧密码差异" name="difok"><InputNumber min={0} max={128} placeholder={String(pam.settings.difok ?? 0)} /></Form.Item>
            <Form.Item><Button htmlType="submit" disabled={!pam.modification_supported}>生成修改预览</Button></Form.Item>
          </Form>
          {pamPreview && <Space direction="vertical" className="full-width"><Alert type={pamPreview.supported ? 'info' : 'error'} showIcon message={pamPreview.supported ? `将修改 ${Object.keys(pamPreview.requested).join('、')}` : pamPreview.reason} description={`目标文件：${pamPreview.config_path}`} /><Button danger type="primary" disabled={!pamPreview.supported || !capability?.available} onClick={confirmPamApply}>输入确认文本并应用</Button></Space>}
          {pamError && <Alert className="notice" type="error" showIcon message={pamError} />}
          {pamTask && <Card className="notice" size="small" title={`PAM 任务 ${pamTask.id}`} extra={<Tag color={pamTask.state === 'succeeded' ? 'green' : pamTask.state === 'failed' ? 'red' : 'blue'}>{pamTask.state}</Tag>}><Steps size="small" items={pamTask.steps.map(step => ({ title: step.name, status: step.state === 'succeeded' ? 'finish' : step.state === 'failed' ? 'error' : step.state === 'running' ? 'process' : 'wait' }))} />{pamTask.error && <Alert type="error" showIcon message={pamTask.error} />}{pamTask.state === 'succeeded' && taskBackupId(pamTask) && <Button danger onClick={() => confirmPamRestore(taskBackupId(pamTask)!)}>恢复该任务的修改前备份</Button>}</Card>}
        </Card>
      </Space>}
    </Card></Col>
  </Row>
}
