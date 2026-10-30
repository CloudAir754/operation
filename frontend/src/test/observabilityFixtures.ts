/**
 * Factories for stage-3 observability payloads. Every fixture mirrors the
 * camelCase wire contract in `docs/requirements/api.md` §8 so component tests
 * assert against realistic data instead of ad-hoc objects.
 */

import type {
  CpuMetricData,
  DiskDeviceMetric,
  DiskMetricData,
  DiskMountMetric,
  GpuDeviceDetail,
  GpuMetricData,
  GpuMonitor,
  GpuProcess,
  GpuSampleMetric,
  HomeStorage,
  MemoryMetricData,
  MetricPoint,
  Metrics,
  MetricsCurrent,
  MountInfo,
  MountsResponse,
  NetworkInterfaceMetric,
  NetworkMetricData,
  ObservableSection,
  StorageNode,
  StorageScan,
  StorageUidSummary,
  SudoState,
  UserInfo,
  UsersResponse,
} from "../api/types";

const BASE_TIME = Date.parse("2026-10-30T12:00:00Z");

/** ISO timestamp `offsetSeconds` away from the fixture epoch. */
export function isoAt(offsetSeconds: number): string {
  return new Date(BASE_TIME + offsetSeconds * 1000).toISOString();
}

export function makeCpuMetricData(overrides: Partial<CpuMetricData> = {}): CpuMetricData {
  return {
    totalPercent: 12.5,
    userPercent: 8,
    systemPercent: 3,
    iowaitPercent: 1,
    stealPercent: 0,
    idlePercent: 87.5,
    logicalCpus: 32,
    load1: 0.42,
    load5: 0.55,
    load15: 0.61,
    ...overrides,
  };
}

export function makeMemoryMetricData(
  overrides: Partial<MemoryMetricData> = {},
): MemoryMetricData {
  return {
    totalBytes: 68719476736,
    availableBytes: 34359738368,
    usedBytes: 34359738368,
    usedPercent: 50,
    buffersBytes: 1073741824,
    cachedBytes: 8589934592,
    commitBytes: 12884901888,
    swapTotalBytes: 8589934592,
    swapUsedBytes: 429496729,
    swapFreeBytes: 8160437862,
    swapInPerSecond: null,
    swapOutPerSecond: 0,
    ...overrides,
  };
}

export function makeDiskDevice(
  overrides: Partial<DiskDeviceMetric> & { name: string },
): DiskDeviceMetric {
  return {
    kind: "disk",
    parent: null,
    model: "Samsung SSD 990 PRO",
    sizeBytes: 2000398934016,
    hidden: false,
    readBytesPerSecond: 1048576,
    writeBytesPerSecond: 2097152,
    readIops: 12.5,
    writeIops: 8,
    awaitMs: 0.42,
    utilPercent: 12.3,
    reset: false,
    ...overrides,
  };
}

export function makeDiskMountMetric(
  overrides: Partial<DiskMountMetric> & { mount: string },
): DiskMountMetric {
  return {
    device: "/dev/nvme0n1p2",
    filesystem: "ext4",
    deviceName: "nvme0n1p2",
    readBytesPerSecond: null,
    writeBytesPerSecond: null,
    ...overrides,
  };
}

export function makeDiskMetricData(overrides: Partial<DiskMetricData> = {}): DiskMetricData {
  return {
    devices: [
      makeDiskDevice({ name: "nvme0n1" }),
      makeDiskDevice({
        name: "nvme0n1p2",
        kind: "partition",
        parent: "nvme0n1",
        readBytesPerSecond: null,
        writeBytesPerSecond: null,
      }),
      makeDiskDevice({ name: "loop0", kind: "loop", hidden: true, model: null, sizeBytes: null }),
    ],
    mounts: [
      makeDiskMountMetric({ mount: "/" }),
      makeDiskMountMetric({
        mount: "/data",
        device: "/dev/nvme1n1",
        deviceName: "nvme1n1",
        readBytesPerSecond: 4096,
        writeBytesPerSecond: 2048,
      }),
    ],
    ...overrides,
  };
}

export function makeNetworkInterfaceMetric(
  overrides: Partial<NetworkInterfaceMetric> & { name: string },
): NetworkInterfaceMetric {
  return {
    kind: "physical",
    state: "up",
    speedMbps: 10000,
    addresses: ["10.0.0.11/24"],
    rxBytesPerSecond: 1024,
    txBytesPerSecond: 2048,
    rxPacketsPerSecond: 12,
    txPacketsPerSecond: 8,
    rxErrors: 0,
    txErrors: 0,
    rxDrops: 0,
    txDrops: 0,
    reset: false,
    ...overrides,
  };
}

export function makeNetworkMetricData(
  overrides: Partial<NetworkMetricData> = {},
): NetworkMetricData {
  return {
    defaultInterface: "eth0",
    interfaces: [
      makeNetworkInterfaceMetric({ name: "eth0" }),
      makeNetworkInterfaceMetric({
        name: "docker0",
        kind: "bridge",
        state: "down",
        speedMbps: null,
        addresses: [],
        rxBytesPerSecond: null,
        txBytesPerSecond: null,
      }),
    ],
    ...overrides,
  };
}

export function makeGpuSample(overrides: Partial<GpuSampleMetric> = {}): GpuSampleMetric {
  return {
    index: 0,
    uuid: "GPU-00000000-0000-0000-0000-000000000000",
    name: "NVIDIA GeForce RTX 4090",
    utilizationPercent: 37,
    memoryUsedMb: 8192,
    memoryTotalMb: 24564,
    encoderPercent: null,
    decoderPercent: null,
    temperatureC: 52,
    powerDrawW: 210.5,
    ...overrides,
  };
}

export function makeGpuMetricData(overrides: Partial<GpuMetricData> = {}): GpuMetricData {
  return { devices: [makeGpuSample()], ...overrides };
}

export function observableSection<T>(
  data: T | null,
  overrides: Partial<ObservableSection<T>> = {},
): ObservableSection<T> {
  return {
    status: data === null ? "unavailable" : "ok",
    error: null,
    requiresSudo: false,
    collectedAt: isoAt(0),
    data,
    ...overrides,
  };
}

export function makeMetricsCurrent(overrides: Partial<MetricsCurrent> = {}): MetricsCurrent {
  return {
    cpu: observableSection(makeCpuMetricData()),
    memory: observableSection(makeMemoryMetricData()),
    disk: observableSection(makeDiskMetricData()),
    network: observableSection(makeNetworkMetricData()),
    gpu: observableSection(makeGpuMetricData()),
    ...overrides,
  };
}

export function makeMetricPoint(overrides: Partial<MetricPoint> = {}): MetricPoint {
  return {
    collectedAt: isoAt(0),
    receivedAt: isoAt(0),
    sampleIntervalMs: 2000,
    reset: false,
    resetReason: null,
    cpuPercent: 12.5,
    userPercent: 8,
    systemPercent: 3,
    iowaitPercent: 1,
    memoryUsedPercent: 50,
    swapUsedPercent: 5,
    disk: {
      nvme0n1: {
        readBytesPerSecond: 1048576,
        writeBytesPerSecond: 2097152,
        readIops: 12.5,
        writeIops: 8,
        reset: false,
      },
      loop0: {
        readBytesPerSecond: null,
        writeBytesPerSecond: null,
        readIops: null,
        writeIops: null,
        reset: true,
      },
    },
    network: {
      eth0: {
        rxBytesPerSecond: 1024,
        txBytesPerSecond: 2048,
        rxPacketsPerSecond: 12,
        txPacketsPerSecond: 8,
        reset: false,
      },
    },
    gpu: [makeGpuSample()],
    ...overrides,
  };
}

export function makeMetrics(overrides: Partial<Metrics> = {}): Metrics {
  const points =
    overrides.points ??
    [
      makeMetricPoint({ collectedAt: isoAt(0), receivedAt: isoAt(0), cpuPercent: 10, memoryUsedPercent: 40, swapUsedPercent: 4 }),
      makeMetricPoint({ collectedAt: isoAt(2), receivedAt: isoAt(2), cpuPercent: 18, memoryUsedPercent: 44, swapUsedPercent: 5 }),
      makeMetricPoint({ collectedAt: isoAt(4), receivedAt: isoAt(4), cpuPercent: 26, memoryUsedPercent: 48, swapUsedPercent: 5 }),
      makeMetricPoint({ collectedAt: isoAt(6), receivedAt: isoAt(6), cpuPercent: 22, memoryUsedPercent: 52, swapUsedPercent: 6 }),
    ];
  return {
    serverId: 1,
    collectedAt: isoAt(6),
    windowSeconds: 300,
    retentionSeconds: 3600,
    sampleIntervalMs: 2000,
    stale: false,
    status: "ok",
    error: null,
    requiresSudo: false,
    current: makeMetricsCurrent(),
    points,
    raw: {},
    ...overrides,
  };
}

export function makeGpuDevice(
  overrides: Partial<GpuDeviceDetail> = {},
): GpuDeviceDetail {
  return {
    index: 0,
    uuid: "GPU-00000000-0000-0000-0000-000000000000",
    pciBusId: "0000:01:00.0",
    name: "NVIDIA GeForce RTX 4090",
    driverVersion: "550.107.02",
    memoryTotalMb: 24564,
    memoryUsedMb: 8192,
    utilizationGpuPercent: 37,
    utilizationMemoryPercent: 33.3,
    encoderPercent: null,
    decoderPercent: null,
    temperatureC: 52,
    powerDrawW: 210.5,
    powerLimitW: 450,
    clockGraphicsMhz: 2520,
    clockMemoryMhz: 10501,
    migMode: "Disabled",
    computeMode: "Default",
    migDevices: [],
    ...overrides,
  };
}

export function makeGpuProcess(overrides: Partial<GpuProcess> & { pid: number }): GpuProcess {
  return {
    user: "labuser",
    name: "python",
    command: "python train.py --config a.yaml",
    memoryUsedMb: 8192,
    utilizationPercent: 37.5,
    gpuUuid: "GPU-00000000-0000-0000-0000-000000000000",
    gpuIndex: 0,
    programPath: "/usr/bin/python3.12",
    cwd: "/home/labuser/work",
    startedAt: isoAt(-14400),
    stale: false,
    error: null,
    ...overrides,
  };
}

export function makeGpuMonitor(overrides: Partial<GpuMonitor> = {}): GpuMonitor {
  const processes =
    overrides.processes ??
    [
      makeGpuProcess({ pid: 4242 }),
      makeGpuProcess({
        pid: 5151,
        user: "alice",
        name: "python",
        command: "python eval.py",
        memoryUsedMb: 1024,
        utilizationPercent: null,
      }),
      makeGpuProcess({
        pid: 6161,
        name: "jupyter",
        command: "jupyter-lab",
        memoryUsedMb: 4096,
        utilizationPercent: 8,
        stale: true,
        error: "采样后进程已退出",
      }),
    ];
  return {
    serverId: 1,
    collectedAt: isoAt(0),
    status: "ok",
    error: null,
    requiresSudo: false,
    capabilities: { nvml: true, processes: "ok", mig: true, perProcessUtilization: false },
    devices: [makeGpuDevice()],
    processes,
    processesStatus: "ok",
    processesError: null,
    notes: [],
    ...overrides,
  };
}

export function makeSudoState(overrides: Partial<SudoState> = {}): SudoState {
  return {
    groupMember: true,
    policyPresent: true,
    effective: "true",
    checkedAt: isoAt(0),
    error: null,
    ...overrides,
  };
}

export function makeHomeStorage(overrides: Partial<HomeStorage> = {}): HomeStorage {
  return {
    allocatedBytes: 10737418240,
    apparentBytes: 12884901888,
    fileCount: 12000,
    directoryCount: 900,
    depth: 2,
    truncated: false,
    incomplete: false,
    error: null,
    collectedAt: isoAt(0),
    ...overrides,
  };
}

export function makeUser(overrides: Partial<UserInfo> & { login: string; uid: number }): UserInfo {
  return {
    gid: overrides.uid,
    primaryGroup: overrides.login,
    supplementaryGroups: [],
    home: `/home/${overrides.login}`,
    shell: "/bin/bash",
    system: false,
    recentLogin: null,
    sudo: makeSudoState(),
    homeStorage: makeHomeStorage(),
    ...overrides,
  };
}

export function makeUsersResponse(overrides: Partial<UsersResponse> = {}): UsersResponse {
  const includeSystem = overrides.includeSystem ?? false;
  const users: UserInfo[] = [
    makeUser({
      login: "labuser",
      uid: 1001,
      supplementaryGroups: ["sudo", "docker"],
      sudo: makeSudoState({ effective: "true" }),
    }),
    makeUser({
      login: "alice",
      uid: 1002,
      supplementaryGroups: ["docker"],
      sudo: makeSudoState({ groupMember: false, policyPresent: true, effective: "false" }),
      homeStorage: null,
    }),
    makeUser({
      login: "bob",
      uid: 1003,
      sudo: makeSudoState({
        groupMember: null,
        policyPresent: null,
        effective: "unknown",
        checkedAt: null,
      }),
      homeStorage: makeHomeStorage({ allocatedBytes: null, fileCount: null, truncated: true, incomplete: true }),
    }),
    makeUser({
      login: "svc",
      uid: 2001,
      system: true,
      shell: "/usr/sbin/nologin",
      sudo: makeSudoState({ groupMember: false, policyPresent: null, effective: "unknown" }),
      homeStorage: null,
    }),
  ];
  return {
    serverId: 1,
    collectedAt: isoAt(0),
    includeSystem,
    status: "ok",
    error: null,
    requiresSudo: false,
    users: includeSystem ? users : users.filter((user) => !user.system),
    notes: ["sudo 有效权限仅对当前 SSH 用户执行非交互验证，其他用户为 unknown"],
    ...overrides,
  };
}

export function makeMount(overrides: Partial<MountInfo> & { mount: string }): MountInfo {
  return {
    device: "/dev/nvme0n1p2",
    filesystem: "ext4",
    options: "rw,relatime",
    uuid: "0f8fad5b-0000-0000-0000-000000000000",
    majorMinor: "259:2",
    physicalDevice: "nvme0n1p2",
    readOnly: false,
    ...overrides,
  };
}

export function makeMountsResponse(overrides: Partial<MountsResponse> = {}): MountsResponse {
  return {
    serverId: 1,
    collectedAt: isoAt(0),
    status: "ok",
    error: null,
    requiresSudo: false,
    mounts: [makeMount({ mount: "/" }), makeMount({ mount: "/data", device: "/dev/nvme1n1" })],
    ...overrides,
  };
}

function parentOf(path: string): string | null {
  if (path === "/" || path === "") return null;
  const segments = path.split("/").filter(Boolean);
  if (segments.length === 1) return "/";
  return `/${segments.slice(0, -1).join("/")}`;
}

export function makeStorageNode(
  overrides: Partial<StorageNode> & { path: string },
): StorageNode {
  const path = overrides.path;
  const name = overrides.name ?? (path === "/" ? "/" : path.split("/").filter(Boolean).pop() ?? path);
  return {
    id: `node-${path}`,
    parentPath: parentOf(path),
    name,
    kind: "dir",
    allocatedBytes: 0,
    apparentBytes: 0,
    fileCount: 0,
    directoryCount: 0,
    uid: 0,
    gid: 0,
    inode: 1,
    hardlinkDuplicateOf: null,
    deletedOpen: false,
    unscanned: false,
    incomplete: false,
    error: null,
    modifiedAt: isoAt(-3600),
    ...overrides,
  };
}

export function makeStorageNodes(): StorageNode[] {
  return [
    makeStorageNode({
      path: "/",
      name: "/",
      allocatedBytes: 1_000_000_000,
      apparentBytes: 1_100_000_000,
      fileCount: 1200,
      directoryCount: 80,
    }),
    makeStorageNode({ path: "/data", allocatedBytes: 600_000_000, fileCount: 400, directoryCount: 20, uid: 1001 }),
    makeStorageNode({ path: "/home", allocatedBytes: 300_000_000, fileCount: 700, directoryCount: 50, uid: 1001 }),
    makeStorageNode({ path: "/tmp", allocatedBytes: 80_000_000, fileCount: 90, directoryCount: 8 }),
    makeStorageNode({ path: "/etc", allocatedBytes: 20_000_000, fileCount: 10, directoryCount: 2 }),
    makeStorageNode({
      path: "/data/logs",
      allocatedBytes: 400_000_000,
      fileCount: 300,
      directoryCount: 6,
      uid: 1001,
    }),
    makeStorageNode({
      path: "/data/app.log",
      kind: "file",
      allocatedBytes: 200_000_000,
      apparentBytes: 210_000_000,
      fileCount: 1,
      uid: 1001,
      modifiedAt: isoAt(-7200),
    }),
  ];
}

export function makeStorageUidSummary(): StorageUidSummary[] {
  return [
    {
      uid: 1001,
      allocatedBytes: 900_000_000,
      apparentBytes: 950_000_000,
      fileCount: 900,
      directoryCount: 40,
      incomplete: false,
    },
    {
      uid: 0,
      allocatedBytes: 100_000_000,
      apparentBytes: 110_000_000,
      fileCount: 300,
      directoryCount: 40,
      incomplete: true,
    },
  ];
}

export function makeStorageLargestFiles(): StorageNode[] {
  return [
    makeStorageNode({
      path: "/data/app.log",
      name: "app.log",
      kind: "file",
      allocatedBytes: 200_000_000,
      apparentBytes: 210_000_000,
      uid: 1001,
      modifiedAt: isoAt(-7200),
    }),
    makeStorageNode({
      path: "/tmp/core.dump",
      name: "core.dump",
      kind: "file",
      allocatedBytes: 80_000_000,
      apparentBytes: 80_000_000,
      uid: 0,
      modifiedAt: isoAt(-3600),
    }),
  ];
}

export function makeStorageScan(overrides: Partial<StorageScan> = {}): StorageScan {
  return {
    scanId: "scan-1",
    serverId: 1,
    serverAlias: "lab-gpu-01",
    mount: "/",
    mountInfo: { device: "/dev/nvme0n1p2", mount: "/", filesystem: "ext4" },
    status: "succeeded",
    stage: "done",
    progress: 100,
    oneFileSystem: true,
    followSymlinks: false,
    maxDepth: 3,
    userFilter: null,
    minFileSize: 0,
    extension: null,
    nodeCount: 7,
    truncated: true,
    incomplete: true,
    crossDeviceCount: 2,
    deletedOpenBytes: 1048576,
    deletedOpenCount: 2,
    nodes: makeStorageNodes(),
    largestFiles: makeStorageLargestFiles(),
    summaryByUid: makeStorageUidSummary(),
    error: null,
    createdAt: isoAt(-60),
    startedAt: isoAt(-60),
    finishedAt: isoAt(0),
    ...overrides,
  };
}
