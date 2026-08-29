import { Alert, Button, Card, Descriptions, Form, InputNumber, Space, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { loadAppSettings, updateAppSettings } from './api'
import type { AppSettingsSnapshot } from './types'

export default function SettingsPanel({ token }: { token: string }) {
  const [form] = Form.useForm<AppSettingsSnapshot['values']>()
  const [snapshot, setSnapshot] = useState<AppSettingsSnapshot | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void loadAppSettings(token).then(next => {
      setSnapshot(next)
      form.setFieldsValue(next.values)
    }).catch(reason => setError(reason instanceof Error ? reason.message : '读取本机设置失败。'))
  }, [form, token])

  const save = async (values: AppSettingsSnapshot['values']) => {
    setSaving(true)
    setError('')
    try {
      const updated = await updateAppSettings(token, values)
      setSnapshot(current => current ? { ...current, values: updated.values } : current)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '更新本机设置失败。')
    } finally {
      setSaving(false)
    }
  }

  return <Card id="settings-panel" title="设置">
    {error && <Alert className="notice" type="error" showIcon message={error} />}
    <Form form={form} layout="inline" onFinish={values => void save(values)}>
      <Form.Item label="默认扫描限速" name="scan_throttle_milliseconds" rules={[{ required: true }]}><InputNumber min={0} max={100} addonAfter="ms / 节点" /></Form.Item>
      <Form.Item label="快照保留时间" name="scan_snapshot_ttl_seconds" rules={[{ required: true }]}><InputNumber min={60} max={86400} addonAfter="秒" /></Form.Item>
      <Form.Item><Button type="primary" htmlType="submit" loading={saving}>保存设置</Button></Form.Item>
    </Form>
    <Typography.Paragraph className="notice" type="secondary">新建扫描使用这些默认值；用户占用扫描仍可在任务表单中临时覆盖限速。快照到期后自动清理。</Typography.Paragraph>
    {snapshot && <Descriptions size="small" column={{ xs: 1, md: 2, lg: 4 }} items={[
      { key: 'database', label: '本地数据库', children: snapshot.database_path },
      { key: 'version', label: '应用 / Python', children: `${snapshot.runtime.version} / ${snapshot.runtime.python}` },
      { key: 'process', label: '进程', children: `PID ${snapshot.runtime.pid} · EUID ${snapshot.runtime.effective_uid}` },
      { key: 'runtime', label: '监听 / 启动时间', children: <Space direction="vertical"><span>{snapshot.runtime.host}</span><span>{new Date(snapshot.runtime.started_at).toLocaleString()}</span></Space> },
    ]} />}
  </Card>
}
