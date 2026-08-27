import { Alert, Button, Card, Col, Form, Input, InputNumber, Row, Space, Switch, Table, Tabs, Tag, Typography, Upload } from 'antd'
import { useEffect, useState } from 'react'
import { loadDefaultPasswordPolicy, previewFileImport, previewPasswordPolicy, previewTextImport } from './api'
import type { PasswordPolicySummary, PasswordPolicyValues, UserImportPreview } from './types'

export default function UserImportPanel({ token }: { token: string }) {
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileKind, setFileKind] = useState<'csv' | 'xlsx'>('csv')
  const [preview, setPreview] = useState<UserImportPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [loading, setLoading] = useState(false)
  const [password, setPassword] = useState<PasswordPolicySummary | null>(null)
  const [passwordError, setPasswordError] = useState('')

  useEffect(() => {
    loadDefaultPasswordPolicy(token).then(setPassword).catch(reason => {
      setPasswordError(reason instanceof Error ? reason.message : '读取密码规则失败。')
    })
  }, [token])

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

  return <Row gutter={[16, 16]}>
    <Col span={24}><Card title="批量账号导入预览" extra={<Tag color="blue">当前仅预览，不创建账号</Tag>}>
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
      </>}
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
  </Row>
}
