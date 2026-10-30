import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type {
  AccountBatch,
  GeneratorInfo,
  MigrationPreflight,
  PasswordPolicy,
  PolicyDiff,
  StorageSettings,
} from "../api/accounts";
import { AccountsTab } from "../components/host/accounts/AccountsTab";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import type { MockResponse } from "../test/fetchMock";

const RULES = {
  length: 20,
  minUpper: 1,
  minLower: 1,
  minDigits: 1,
  minSymbols: 1,
  symbols: "!#%+-_=.:?@^~*",
  excludeAmbiguous: true,
  excludeChars: "",
};

function generator(overrides: Partial<GeneratorInfo> = {}): GeneratorInfo {
  return {
    configured: false,
    rules: RULES,
    effectiveLength: 20,
    adjustments: [],
    example: "Xy••••••••••••••••••",
    checks: [
      { code: "length_sum", label: "各类最少字符数之和不超过总长度", ok: true, message: "4 / 20", blocking: true },
      { code: "generation", label: "按当前规则可生成合规密码", ok: true, message: "", blocking: true },
    ],
    floor: {
      minLength: 12,
      minUpper: 1,
      minLower: 0,
      minDigits: 1,
      minSymbols: 0,
      minClasses: 0,
      maxRepeat: 0,
      maxSequence: 0,
      maxClassRepeat: 0,
      rejectUsername: false,
      source: "conf",
      notes: [],
    },
    serverId: 1,
    updatedAt: null,
    ...overrides,
  };
}

function policy(): PasswordPolicy {
  return {
    serverId: 1,
    collectedAt: "2026-10-30T12:00:00Z",
    status: "ok",
    error: null,
    os: { id: "ubuntu", versionId: "24.04" },
    fields: [
      {
        key: "minlen",
        label: "最小长度",
        value: 8,
        origin: "conf",
        writable: true,
        min: 8,
        max: 128,
        reason: null,
      },
      {
        key: "retry",
        label: "认证重试次数",
        value: 3,
        origin: "pam",
        writable: true,
        min: 1,
        max: 10,
        reason: null,
      },
    ],
    capabilities: {
      pwqualityConf: true,
      pwqualityModule: true,
      pwqualityModuleEditable: true,
      pamRetryEditable: true,
      pwscore: true,
      chage: true,
      useradd: true,
      rsync: true,
      dropInDir: false,
      writableFields: ["minlen", "retry"],
      readOnly: false,
      reasons: [],
    },
    managedLines: ["password requisite pam_pwquality.so retry=3"],
    unknownKeys: ["enforcing = 1"],
    loginDefs: { PASS_MIN_LEN: "8" },
    notes: [],
    floor: generator().floor,
    generator: generator(),
  };
}

function settings(): StorageSettings {
  return {
    serverId: 1,
    dataRoot: null,
    migrationRoot: null,
    defaultDataRoot: "/data2",
    defaultMigrationRoot: "/data2/users",
  };
}

function diff(): PolicyDiff {
  return {
    serverId: 1,
    fields: { minlen: { before: 8, after: 14 } },
    diff: "--- /etc/security/pwquality.conf\n-minlen = 8\n+minlen = 14\n",
    blockers: [],
    warnings: [],
    applicable: true,
  };
}

function batch(overrides: Partial<AccountBatch> = {}): AccountBatch {
  return {
    id: "batch-1",
    serverId: 1,
    serverAlias: "lab-01",
    status: "succeeded",
    stage: "done",
    progress: 100,
    results: [
      {
        login: "alice",
        status: "succeeded",
        error: null,
        uid: 1005,
        home: "/home/alice",
        passwordGenerated: true,
      },
    ],
    error: null,
    hasGeneratedPasswords: true,
    createdAt: "2026-10-30T12:00:00Z",
    startedAt: "2026-10-30T12:00:01Z",
    finishedAt: "2026-10-30T12:00:03Z",
    ...overrides,
  };
}

function preflight(overrides: Partial<MigrationPreflight> = {}): MigrationPreflight {
  return {
    serverId: 1,
    login: "zhangsan",
    paths: {
      source: "/home/zhangsan",
      targetRoot: "/data2/users",
      target: "/data2/users/zhangsan",
      staging: "/data2/users/zhangsan.labops-staging-abcd1234",
      backup: "/home/zhangsan.labops-backup-20261030-120000-abcd1234",
    },
    sourceInfo: {},
    targetInfo: {},
    counts: { files: 12, dirs: 4 },
    space: { allocatedBytes: 1024, requiredBytes: 2048, freeBytes: 4096 },
    checks: [
      {
        code: "source_filesystem",
        label: "源文件系统为本地 ext4",
        ok: true,
        message: "",
        blocking: true,
      },
    ],
    allowed: true,
    blockers: [],
    requiresSudo: true,
    collectedAt: "2026-10-30T12:00:00Z",
    ...overrides,
  };
}

interface MockOptions {
  generatorInfo?: GeneratorInfo;
  policyInfo?: PasswordPolicy;
  preflightInfo?: MigrationPreflight;
  batchResult?: AccountBatch;
}

function installMock(options: MockOptions = {}) {
  const result = installFetchMock((request) => {
    const path = requestPath(request.url);
    const method = request.method;
    const respond = (body: unknown, status = 200): MockResponse => ({ status, body });

    if (path === "/api/accounts/generator" && method === "GET") {
      return respond(options.generatorInfo ?? generator());
    }
    if (path === "/api/accounts/generator" && method === "PUT") {
      const body = request.body as { rules: typeof RULES } | undefined;
      return respond(generator({ configured: true, rules: body?.rules ?? RULES }));
    }
    if (path === "/api/accounts/generator/preview") {
      return respond(options.generatorInfo ?? generator());
    }
    if (path === "/api/servers/1/accounts/policy" && method === "GET") {
      return respond(options.policyInfo ?? policy());
    }
    if (path === "/api/servers/1/accounts/policy/preview") {
      return respond(diff());
    }
    if (path === "/api/servers/1/accounts/policy" && method === "PUT") {
      return respond({
        id: "change-1",
        serverId: 1,
        status: "applied",
        targets: { minlen: 14 },
        diff: diff().diff,
        backupId: "backup-1",
        validation: { ok: true, behavior: "ok" },
        error: null,
        createdAt: "2026-10-30T12:00:00Z",
        restoredAt: null,
      });
    }
    if (path === "/api/servers/1/accounts/policy/changes") {
      return respond({ changes: [], count: 0 });
    }
    if (path === "/api/servers/1/storage-settings") {
      return respond(settings());
    }
    if (path === "/api/servers/1/account-batches" && method === "GET") {
      return respond({ batches: [], count: 0 });
    }
    if (path === "/api/servers/1/account-batches" && method === "POST") {
      return respond(options.batchResult ?? batch(), 201);
    }
    if (path === "/api/account-batches/batch-1/reveal") {
      return respond({
        batchId: "batch-1",
        passwords: { alice: "Gen3rated!Pass" },
        note: "只显示一次",
      });
    }
    if (path === "/api/servers/1/home-migrations") {
      return respond({ migrations: [], count: 0 });
    }
    if (path === "/api/servers/1/home-migrations/preflight") {
      return respond(options.preflightInfo ?? preflight());
    }
    if (path === "/api/servers/1/datasets") {
      return respond({ datasets: [], count: 0 });
    }
    if (path === "/api/servers/1/secret" && method === "PUT") {
      return respond({ serverId: 1, hasPassword: true, updatedAt: "2026-10-30T12:00:00Z" });
    }
    return notFoundResponse(method, path);
  });
  return { requests: result.requests };
}

function renderTab(hasSudoPassword = false) {
  return render(
    <AccountsTab serverId={1} hasSudoPassword={hasSudoPassword} onSudoChanged={() => undefined} />,
  );
}

describe("账户页签", () => {
  it("清晰区分两层密码并标注需要 sudo 的操作", async () => {
    installMock();
    renderTab();

    expect(await screen.findByTestId("accounts-tab")).toBeInTheDocument();
    const sudoCard = screen.getByTestId("sudo-card");
    expect(within(sudoCard).getAllByText(/运维 sudo 密码/).length).toBeGreaterThan(0);
    expect(within(sudoCard).getByText("未保存")).toBeInTheDocument();
    expect(screen.getByText(/目标账户登录密码/)).toBeInTheDocument();

    // 所有提权入口都必须带「需要 sudo」标注。
    expect(screen.getAllByText("需要 sudo").length).toBeGreaterThanOrEqual(4);
  });

  it("展示脱敏示例并在规则改动后重新预览", async () => {
    const user = userEvent.setup();
    const { requests } = installMock({ generatorInfo: generator({ example: "Xy••••••••" }) });
    renderTab();

    const example = await screen.findByTestId("password-example");
    expect(example).toHaveTextContent("Xy••••••••");

    const length = screen.getByLabelText("密码长度");
    await user.clear(length);
    await user.type(length, "24");

    await waitFor(() => {
      const previews = requests.filter((item) => requestPath(item.url).endsWith("/generator/preview"));
      expect(previews.length).toBeGreaterThan(0);
      expect((previews.at(-1)?.body as { rules: { length: number } }).rules.length).toBe(24);
    });
    expect(screen.getByTestId("generator-checks")).toBeInTheDocument();
  });

  it("先看差异再确认写入系统密码策略", async () => {
    const user = userEvent.setup();
    installMock();
    renderTab();

    const minlen = await screen.findByLabelText("最小长度 修改为");
    await user.clear(minlen);
    await user.type(minlen, "14");
    await user.click(screen.getByRole("button", { name: "检查差异" }));

    const diffBlock = await screen.findByTestId("policy-diff");
    expect(diffBlock).toHaveTextContent("+minlen = 14");

    await user.click(screen.getByRole("button", { name: /应用修改/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/会影响这台主机上的所有本地用户/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认写入" }));

    expect(await screen.findByText(/策略已写入/)).toBeInTheDocument();
  });

  it("批量新建用户后一次性展示生成的密码", async () => {
    const user = userEvent.setup();
    installMock();
    renderTab();

    await screen.findByTestId("users-card");
    const login = screen.getAllByLabelText("登录名")[0];
    await user.type(login, "alice");
    await user.click(screen.getByRole("button", { name: /创建 1 个账户/ }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "确认创建" }));

    const progress = await screen.findByTestId("batch-progress");
    expect(within(progress).getByText("成功")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看生成的密码（仅一次）" }));
    const modal = await screen.findByTestId("revealed-passwords");
    expect(modal).toHaveTextContent("Gen3rated!Pass");
  });

  it("预检未通过时禁止开始迁移并列出原因", async () => {
    const user = userEvent.setup();
    installMock({
      preflightInfo: preflight({
        allowed: false,
        blockers: ["源文件系统为 xfs，首版只允许 ext4"],
        checks: [
          {
            code: "source_filesystem",
            label: "源文件系统为本地 ext4",
            ok: false,
            message: "源文件系统为 xfs，首版只允许 ext4",
            blocking: true,
          },
        ],
      }),
    });
    renderTab();

    await screen.findByTestId("migration-card");
    await user.type(screen.getByLabelText("要迁移的账户"), "zhangsan");
    await user.click(screen.getByRole("button", { name: "只读预检" }));

    const result = await screen.findByTestId("migration-preflight");
    expect(within(result).getByText("预检未通过")).toBeInTheDocument();
    expect(result).toHaveTextContent("源文件系统为 xfs");
    expect(screen.getByRole("button", { name: /开始迁移/ })).toBeDisabled();
  });
});
