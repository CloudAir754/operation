import type {
  Host,
  SectionStatus,
  Settings,
  Snapshot,
  SnapshotSection,
  SnapshotSections,
  SshConfig,
  Task,
} from "../api/types";

export function makeHost(overrides: Partial<Host> = {}): Host {
  return {
    id: 1,
    alias: "lab-gpu-01",
    hostname: "10.0.0.11",
    user: "labadmin",
    port: 22,
    identityFile: "~/.ssh/id_ed25519",
    proxyJump: null,
    proxyCommand: null,
    connectTimeout: 10,
    source: "ssh_config",
    enabled: true,
    notes: "GPU 训练节点，维护窗口周日 02:00",
    hasSudoPassword: true,
    lastConnect: { status: "ok", error: null, at: "2026-10-25T12:00:00Z" },
    ...overrides,
  };
}

export function section<T>(
  data: T,
  status: SectionStatus = "ok",
  error: string | null = null,
): SnapshotSection<T> {
  return { status, error, data };
}

const BASE_SNAPSHOT: Snapshot = {
  serverId: 1,
  collectedAt: "2026-10-25T12:00:00Z",
  ageSeconds: 4,
  stale: false,
  status: "ok",
  error: null,
  data: {
    system: section({
      hostname: "lab-gpu-01",
      os: "Ubuntu 24.04.1 LTS",
      kernel: "6.8.0-45-generic",
      arch: "x86_64",
      uptimeSeconds: 864000,
      bootTime: "2026-10-15T12:00:00Z",
    }),
    cpu: section({
      model: "AMD EPYC 7543",
      cores: 32,
      usagePercent: 12.5,
      load1: 0.4,
      load5: 0.5,
      load15: 0.6,
    }),
    memory: section({
      totalBytes: 68719476736,
      availableBytes: 34359738368,
      usedPercent: 50,
      swapTotalBytes: 0,
      swapUsedBytes: 0,
    }),
    disks: section({
      mounts: [
        {
          mount: "/",
          filesystem: "/dev/nvme0n1p2",
          totalBytes: 1000000000000,
          usedBytes: 400000000000,
          freeBytes: 600000000000,
          usedPercent: 40,
        },
      ],
    }),
    network: section({
      defaultInterface: "eth0",
      gateway: "10.0.0.1",
      interfaces: [
        {
          name: "eth0",
          state: "up",
          speedMbps: 10000,
          rxBytes: 123456,
          txBytes: 654321,
          rxErrors: 0,
          txErrors: 0,
          addresses: ["10.0.0.11/24"],
        },
      ],
    }),
    gpu: { status: "unavailable", error: "nvidia-smi not found", data: null },
    services: section({
      runningCount: 42,
      failedCount: 1,
      failedUnits: ["broken.service"],
      units: [
        {
          name: "nginx.service",
          load: "loaded",
          active: "active",
          sub: "running",
          description: "A high performance web server",
          protected: false,
        },
        {
          name: "ssh.service",
          load: "loaded",
          active: "active",
          sub: "running",
          description: "OpenBSD Secure Shell server",
          protected: true,
        },
      ],
    }),
  },
};

export function makeSnapshot(
  options: { data?: Partial<SnapshotSections> } & Omit<Partial<Snapshot>, "data"> = {},
): Snapshot {
  const { data, ...rest } = options;
  return {
    ...BASE_SNAPSHOT,
    ...rest,
    data: { ...BASE_SNAPSHOT.data, ...(data ?? {}) },
  };
}

const BASE_TASK: Task = {
  id: "task-running",
  serverId: 1,
  serverAlias: "lab-gpu-01",
  action: "service_restart",
  actionLabel: "重启服务",
  risk: "medium",
  params: { unit: "nginx.service" },
  status: "running",
  progress: 40,
  exitCode: null,
  error: null,
  outputTail: "restarting nginx.service...",
  idempotencyKey: "6c1d0000-0000-4000-8000-000000000000",
  createdAt: "2026-10-25T12:00:00Z",
  startedAt: "2026-10-25T12:00:01Z",
  finishedAt: null,
  steps: [
    {
      seq: 1,
      name: "预检",
      status: "ok",
      exitCode: 0,
      outputTail: "",
      startedAt: "2026-10-25T12:00:01Z",
      finishedAt: "2026-10-25T12:00:02Z",
    },
    {
      seq: 2,
      name: "重启服务",
      status: "running",
      exitCode: null,
      outputTail: "",
      startedAt: "2026-10-25T12:00:02Z",
      finishedAt: null,
    },
  ],
};

export function makeTask(overrides: Partial<Task> = {}): Task {
  return { ...BASE_TASK, ...overrides };
}

export function makeSshConfig(overrides: Partial<SshConfig> = {}): SshConfig {
  return {
    path: "C:\\Users\\me\\.ssh\\config",
    exists: true,
    hosts: [
      {
        alias: "lab-gpu-01",
        hostname: "10.0.0.11",
        user: "labadmin",
        port: 22,
        identityFile: "~/.ssh/id_ed25519",
        proxyJump: null,
        proxyCommand: null,
        connectTimeout: 10,
        imported: true,
        lineNumber: 3,
      },
      {
        alias: "lab-cpu-02",
        hostname: "10.0.0.12",
        user: "labadmin",
        port: 22,
        identityFile: null,
        proxyJump: "bastion",
        proxyCommand: null,
        connectTimeout: 10,
        imported: false,
        lineNumber: 12,
      },
    ],
    includes: [],
    errors: [],
    ...overrides,
  };
}

export function makeSettings(overrides: Partial<Settings> = {}): Settings {
  return {
    allowArbitraryCommands: false,
    snapshotTtlSeconds: 20,
    maxOutputBytes: 262144,
    sshConfigPath: "C:\\Users\\me\\.ssh\\config",
    appDataDir: "C:\\Users\\me\\AppData\\Local\\LabOpsPanel",
    bindHost: "127.0.0.1",
    bindPort: 8765,
    transport: "openssh",
    ...overrides,
  };
}

// Stage-3 observability payload factories live in their own module and are
// re-exported here so tests can import every fixture from one place.
export * from "./observabilityFixtures";
