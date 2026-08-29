export interface SystemSnapshot {
  hostname: string
  operating_system: string
  kernel: string
  architecture: string
  supported_architecture: boolean
  addresses: string[]
  capabilities: {
    proc: boolean
    sys: boolean
    systemd: boolean
    commands: Record<string, boolean>
    directory_account_sources: string[]
  }
}

export interface ResourceSnapshot {
  sample_interval_seconds: number | null
  cpu: {
    model: string
    logical_cores: number
    physical_cores: number | null
    load_average: number[]
    total_percent: number
    per_core_percent: number[]
  }
  memory: {
    total: number
    available: number
    used: number
    cached: number | null
    percent: number
    swap_total: number
    swap_used: number
  }
  disks: Array<{
    name: string
    read_bytes_per_second: number | null
    write_bytes_per_second: number | null
    read_iops: number | null
    write_iops: number | null
    busy_percent: number | null
  }>
  networks: Array<{
    name: string
    is_up: boolean | null
    speed_mbps: number | null
    addresses: string[]
    sent_bytes_per_second: number | null
    received_bytes_per_second: number | null
    errors_in: number
    errors_out: number
    drops_in: number
    drops_out: number
  }>
}

export interface GpuSnapshot {
  available: boolean
  reason: string | null
  devices: Array<{
    index: number
    uuid: string
    pci_bus_id: string
    name: string
    driver_version: string
    memory_total: number
    memory_used: number
    gpu_percent: number
    memory_activity_percent: number
    temperature_celsius: number | null
    fan_percent: number | null
    power_watts: number | null
    graphics_clock_mhz: number | null
    sm_clock_mhz: number | null
    memory_clock_mhz: number | null
    pcie_rx_bytes_per_second: number | null
    pcie_tx_bytes_per_second: number | null
    dram_bandwidth_percent: number | null
    processes: GpuProcess[]
  }>
  processes: GpuProcess[]
}

export interface GpuProcess {
  gpu_index: number
  gpu_uuid: string
  pid: number
  types: string[]
  memory_used: number | null
  memory_percent: number | null
  sm_percent: number | null
  memory_activity_percent: number | null
  encoder_percent: number | null
  decoder_percent: number | null
  bandwidth: null
  sample_period_seconds: number | null
  username: string | null
  command: string | null
}

export interface MountPoint {
  device: string
  mountpoint: string
  filesystem: string
  total: number | null
  used: number | null
  free: number | null
  percent: number | null
  inodes_total: number | null
  inodes_free: number | null
  read_only: boolean
}

export interface Overview {
  system: SystemSnapshot
  resources: ResourceSnapshot
  gpu: GpuSnapshot
  mounts: MountPoint[]
}

export interface UserAccount {
  username: string
  uid: number
  gid: number
  display_name: string
  home: string
  resolved_home: string
  shell: string
  account_source: string
  account_status: string
  home_storage: null | {
    device: string
    filesystem: string
    mountpoint: string
    total: number | null
    free: number | null
    read_only: boolean
  }
  sudo_sources: string[]
  sudo_sources_complete: boolean
}

export interface StorageSnapshot {
  available: boolean
  error: string | null
  devices: Array<{
    name: string
    path: string
    type: string
    size: number
    filesystem: string | null
    mountpoints: string[]
    model: string | null
    serial: string | null
    wwn: string | null
    read_only: boolean
    removable: boolean
    transport: string | null
    uuid: string | null
    parent: string | null
    by_id: string[]
    stable_identity: string | null
  }>
}

export interface ScanNode {
  name: string
  path: string
  type: string
  logical_size: number
  allocated_size: number
  file_count: number
  owner_uid: number | null
  owner: string | null
  group_gid: number | null
  group: string | null
  mode: number | null
  world_writable: boolean
  unreadable: boolean
  cross_filesystem: boolean
  hardlink_duplicate: boolean
  changed_during_scan: boolean
  children: ScanNode[]
  error?: string
}

export interface HomeScanTask {
  id: string
  username: string
  root: string
  max_depth: number
  max_nodes: number
  target_uid: number | null
  include_hidden: boolean
  cross_filesystems: boolean
  include_unreadable: boolean
  throttle_milliseconds: number
  state: 'queued' | 'running' | 'completed' | 'cancelled' | 'failed'
  nodes_scanned: number
  started_at: string | null
  finished_at: string | null
  error: string | null
  snapshot_expired: boolean
  result: null | {
    scanned_at: string
    expires_at: string
    scope: string
    target_uid: number | null
    cross_filesystems: boolean
    hardlink_policy: string
    nodes_scanned: number
    unreadable_count: number
    changed_count: number
    cross_filesystem_count: number
    largest_paths: Array<{
      path: string
      logical_size: number
      allocated_size: number
      owner_uid: number
    }>
    tree: ScanNode
  }
}

export type UserStorageScanTask = HomeScanTask

export interface UserImportPreview {
  header_detected: boolean
  counts: { will_create: number, exists: number, invalid: number }
  rows: Array<{
    source_row: number
    username: string
    display_name: string
    status: 'will_create' | 'exists' | 'invalid'
    errors: string[]
  }>
}

export interface PasswordPolicyValues {
  length: number
  min_uppercase: number
  min_lowercase: number
  min_digits: number
  min_symbols: number
  symbols: string
  exclude_ambiguous: boolean
}

export interface PasswordPolicySummary {
  masked_example: string
  estimated_entropy_bits: number
  policy: PasswordPolicyValues
}

export interface PamPolicySnapshot {
  common_password: string
  password_stack: Array<{
    line: number
    control: string
    module: string
    options: string[]
  }>
  pwquality_module_installed: boolean
  pwquality_enabled: boolean
  settings: Record<string, number | boolean>
  module_settings: Record<string, number | boolean>
  config_sources: string[]
  generation_minimums: {
    length: number
    min_digits: number
    min_uppercase: number
    min_lowercase: number
    min_symbols: number
    min_classes: number
  }
  warnings: string[]
  modification_supported: boolean
  modification_reason: string | null
  password_aging_managed: false
}

export interface PamPolicyChangeValues {
  minlen?: number
  dcredit?: number
  ucredit?: number
  lcredit?: number
  ocredit?: number
  minclass?: number
  maxrepeat?: number
  maxsequence?: number
  maxclassrepeat?: number
  usercheck?: boolean
  retry?: number
  difok?: number
}

export interface PamPolicyChangePreview {
  supported: boolean
  reason: string | null
  config_path: string
  current: Record<string, number | boolean>
  requested: PamPolicyChangeValues
  effective_after: Record<string, number | boolean>
  module_conflicts: string[]
  confirmation_text: string
}

export interface PrivilegedCapability {
  available: boolean
  reason: string | null
  path: string
}

export interface PersistentTask {
  id: string
  kind: string
  state: 'queued' | 'prechecking' | 'waiting_confirmation' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted' | 'rollback_pending' | 'rolled_back'
  target: string
  parameters: Record<string, unknown>
  error: string | null
  created_at: string
  updated_at: string
  steps: Array<{
    sequence: number
    name: string
    state: string
    detail: Record<string, unknown> | null
  }>
}

export interface AppSettingsSnapshot {
  values: {
    scan_throttle_milliseconds: number
    scan_snapshot_ttl_seconds: number
  }
  database_path: string
  runtime: {
    version: string
    python: string
    pid: number
    effective_uid: number
    started_at: string
    host: string
  }
}

export interface HomeMigrationStatus {
  migration_id: string
  phase: string
  username: string
  uid: number
  source: string
  backup: string
  target_parent: string
  destination: string
  required_bytes?: number
  safety_margin_bytes?: number
  observation_started_at?: string
}

export interface DiskInitializationPlan {
  stable_identity: string
  device_path: string
  command_device: string
  model: string | null
  serial: string
  wwn: string | null
  size: number
  label: string
  mountpoint: string
  mount_options: string
  commands: string[][]
  fstab_preview: string
  destructive: true
}

export interface DiskInitializationStatus {
  operation_id: string
  phase: string
  stable_identity: string
  serial: string
  model: string | null
  size: number
  label: string
  mountpoint: string
  mount_options: string
  partition: string | null
  filesystem_uuid: string | null
  failed_phase: string | null
  failure: string | null
  fstab_restored: boolean | null
}

export interface PortUsageSnapshot {
  port: number
  protocol: 'tcp' | 'udp' | 'any'
  complete: boolean
  reason: string | null
  connections: Array<{
    protocol: string
    family: string
    local_address: string | null
    local_port: number
    remote_address: string | null
    remote_port: number | null
    status: string | null
    pid: number | null
    uid: number | null
    username: string | null
    create_time: number | null
    command: string | null
  }>
}
