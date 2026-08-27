export interface SystemSnapshot {
  hostname: string
  operating_system: string
  kernel: string
  architecture: string
  supported_architecture: boolean
  addresses: string[]
  capabilities: Record<string, boolean>
}

export interface ResourceSnapshot {
  cpu: {
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
}

export interface GpuSnapshot {
  available: boolean
  reason: string | null
  devices: Array<{
    index: number
    uuid: string
    name: string
    memory_total: number
    memory_used: number
    gpu_percent: number
  }>
}

export interface MountPoint {
  device: string
  mountpoint: string
  filesystem: string
  total: number | null
  used: number | null
  free: number | null
  percent: number | null
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
  sudo_sources: string[]
}
