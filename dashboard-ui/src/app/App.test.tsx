import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { I18nProvider } from "../shared/i18n";
import type { AgentSettings, AutoTestRun, AutoTestSettings, FlowDocument, WorkspaceListItem, WorkspaceSettings } from "../shared/types";
import { ApiError, dashboardApi } from "./api";
import { App } from "./App";

const workspaces: WorkspaceListItem[] = ["workspace-one", "workspace-two"].map((workspace_id) => ({
  workspace_id, name: workspace_id, path: "/test", registered_at: "2026-09-30T04:00:00Z", health: "ready", detail: "",
}));
const agent: AgentSettings = {
  enabled: true, default_workspace_id: "workspace-one", agent_provider: "codex", agent_model: "gpt-5.6-luna",
  agent_reasoning_effort: "max", feishu_app_id: "cli_test", feishu_app_configured: true,
  feishu_app_secret_masked: "abcd…wxyz", observability: {
    enabled: true, provider: "langfuse", base_url: "https://cloud.langfuse.com", sample_rate: 1,
    public_key_configured: true, secret_key_configured: true, public_key_masked: "pk…1234", secret_key_masked: "sk…5678",
  },
};
const settings: WorkspaceSettings = {
  workspace_id: "workspace-one", feishu_webhook: { enabled: true, configured: true, masked_url: "https://example.test/…1234" },
  auto_delivery: { enabled: false, trigger_hooks: [], schedule_expression: "", jira_site: "", trigger_jql: "", publish_mode: "local", target_branch: "" },
  auto_scan: { enabled: false, lookback_days: 7, trigger_hooks: [], schedule_expression: "", workflow_description: "" },
};
const testSettings: AutoTestSettings = {
  enabled: false, schedule_expression: "*/5 * * * *", jira_site: "example.atlassian.net",
  jira_project: "TEST", trigger_jql: 'project = TEST AND status = "Ready For QA"',
};

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); window.history.replaceState(null, "", "/"); });

it.each([
  ["Unable to read source.py.  ", "Unable to read source.py"],
  ["配置保存失败。", "配置保存失败"],
  ["設定儲存失敗。", "設定儲存失敗"],
  ["Version 1.4.19 is unavailable.", "Version 1.4.19 is unavailable"],
  ["First sentence.\nLast sentence。", "First sentence.\nLast sentence"],
  ["Already clean", "Already clean"],
])("removes only trailing full stops from result toasts: %s", async (message, expected) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const error = new ApiError(500, message);
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue([]);
  vi.spyOn(dashboardApi, "getBootstrap").mockRejectedValue(error);
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    expect(container.querySelector(".notice-error > span")?.textContent).toBe(expected);
    expect(error.message).toBe(message);
  } finally { await act(async () => root.unmount()); }
});

it("merges history into Agent, moves global configuration to Settings and protects both settings drafts", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", "en");
  window.history.replaceState(null, "", "?workspace=workspace-one&view=chat-history");
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue(workspaces);
  vi.spyOn(dashboardApi, "getBootstrap").mockResolvedValue({ version: "1.4.9", workspace_count: 2, has_workspaces: true });
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  vi.spyOn(dashboardApi, "getCodexCliStatus").mockResolvedValue({
    status: "up_to_date", binary_path: "/test/codex", installed_version: "0.160.0", latest_version: "0.160.0",
  });
  vi.spyOn(dashboardApi, "listAgentModels").mockResolvedValue(["gpt-5.6-luna", "gpt-test"].map((model) => ({
    model, display_name: model, description: "", default_reasoning_effort: "max", supported_reasoning_efforts: ["max"],
  })));
  vi.spyOn(dashboardApi, "getSettings").mockImplementation(async (workspaceId) => ({ ...settings, workspace_id: workspaceId }));
  vi.spyOn(dashboardApi, "getOverview").mockImplementation(async (workspaceId) => ({
    workspace_id: workspaceId, name: workspaceId, path: "/test", created_at: "2026-09-30T04:00:00Z", lumon_version: "1.4.9", repositories: [], workflow_schedules: [],
  }));
  vi.spyOn(dashboardApi, "listConversations").mockResolvedValue({ items: [], total: 0 });
  vi.spyOn(dashboardApi, "autoTestSettings").mockResolvedValue(testSettings);
  const saveAgent = vi.spyOn(dashboardApi, "updateAgentSettings").mockImplementation(async (update) => ({
    ...agent, agent_model: update.agent_model, observability: { ...agent.observability, ...update.observability },
  }));
  const saveWebhook = vi.spyOn(dashboardApi, "updateSettings").mockResolvedValue(settings);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const container = document.createElement("div");
  const root = createRoot(container);
  function navigation(label: string): HTMLButtonElement {
    return container.querySelector<HTMLButtonElement>(`.side-nav button[aria-label="${label}"]`)!;
  }
  async function edit(selector: string, text: string): Promise<void> {
    await act(async () => {
      const field = container.querySelector<HTMLInputElement | HTMLSelectElement>(selector)!;
      if (field instanceof HTMLSelectElement) {
        field.value = text;
        field.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, text);
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
  }
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    expect(window.location.search).toContain("view=agent");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("h1")?.textContent).toBe("Agent");
    expect(container.querySelector("#agent-model")).toBeNull();
    expect(container.querySelectorAll(".side-nav button")).toHaveLength(10);
    expect(navigation("Auto Test")).not.toBeNull();
    expect(navigation("Skills")).not.toBeNull();
    expect(container.querySelector(".side-nav")?.textContent).not.toContain("Conversation history");

    await act(async () => navigation("Settings").click());
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("#feishu-webhook-url")).not.toBeNull();
    expect(container.querySelector("#agent-model")).not.toBeNull();
    expect(container.querySelector("#langfuse-base-url")).not.toBeNull();
    expect(container.querySelector(".settings-panel .status-pill")).toBeNull();
    expect(container.textContent).not.toContain("Global Agent settings");
    expect(container.textContent).not.toContain("Applies to the local Agent across all Workspaces.");
    expect(container.querySelectorAll('[role="switch"]')).toHaveLength(6);
    expect(container.querySelector("#auto-delivery-hooks")).not.toBeNull();
    expect(container.querySelector("#auto-scan-lookback")).not.toBeNull();
    expect(container.querySelector("#auto-scan-hooks")).not.toBeNull();
    expect(container.querySelector("#auto-scan-description")).toBeNull();
    expect(container.querySelector<HTMLInputElement>("#feishu-webhook-url")?.value).toBe("https://example.test/…1234");
    expect(container.querySelector<HTMLInputElement>("#feishu-app-secret")?.placeholder).toBe("abcd…wxyz");

    await edit("#agent-model", "gpt-test");
    await edit("#langfuse-base-url", "https://draft.langfuse.test");
    expect(container.querySelector(".agent-settings-panel .unsaved-label")?.textContent).toBe("Unsaved changes");
    await act(async () => navigation("Agent").click());
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(container.querySelector("h1")?.textContent).toBe("Settings");
    await act(async () => {
      const picker = container.querySelector<HTMLSelectElement>(".workspace-picker select")!;
      picker.value = "workspace-two";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(window.location.search).toContain("workspace=workspace-one");
    await act(async () => container.querySelector<HTMLButtonElement>(".agent-settings-actions button")!.click());
    expect(saveAgent).toHaveBeenCalledWith(expect.objectContaining({ agent_model: "gpt-test" }));
    expect(saveAgent.mock.calls[0][0].feishu_app_secret).toBeUndefined();
    expect(saveAgent.mock.calls[0][0].observability.secret_key).toBeUndefined();
    expect(saveAgent.mock.calls[0][0].observability.base_url).toBe(agent.observability.base_url);
    expect(saveWebhook).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLInputElement>("#langfuse-base-url")?.value).toBe("https://draft.langfuse.test");
    await act(async () => navigation("Agent").click());
    expect(confirm).toHaveBeenCalledTimes(3);
    expect(container.querySelector("h1")?.textContent).toBe("Settings");
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Save Langfuse Cloud"]')!.click());
    expect(saveAgent).toHaveBeenLastCalledWith(expect.objectContaining({
      agent_model: "gpt-test", observability: expect.objectContaining({ base_url: "https://draft.langfuse.test" }),
    }));

    await edit("#feishu-webhook-url", "https://example.test/replacement");
    await act(async () => navigation("Agent").click());
    expect(confirm).toHaveBeenCalledTimes(4);
    confirm.mockReturnValue(true);
    await act(async () => navigation("Agent").click());
    expect(container.querySelector("#agent-model")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("Agent");
  } finally {
    await act(async () => root.unmount());
  }
});

it.each(["en", "zh-CN", "zh-TW"] as const)("saves automation panels independently and keeps history pages execution-only in %s", async (locale) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", locale);
  window.history.replaceState(null, "", "?workspace=workspace-one&view=settings");
  let persisted: WorkspaceSettings = {
    ...structuredClone(settings),
    jira: { site: "example.atlassian.net", project: "TEST", configured: false },
  };
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue(workspaces);
  vi.spyOn(dashboardApi, "getBootstrap").mockResolvedValue({ version: "1.4.17", workspace_count: 2, has_workspaces: true });
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  vi.spyOn(dashboardApi, "getSettings").mockImplementation(async () => structuredClone(persisted));
  vi.spyOn(dashboardApi, "listAgentModels").mockResolvedValue([]);
  vi.spyOn(dashboardApi, "getCodexCliStatus").mockResolvedValue({ status: "up_to_date", binary_path: "/test/codex", installed_version: "0.160.0", latest_version: "0.160.0" });
  vi.spyOn(dashboardApi, "getDeliveryHistory").mockResolvedValue({ runs: [], polls: [] });
  vi.spyOn(dashboardApi, "listScans").mockResolvedValue([]);
  let savedTestSettings = testSettings;
  vi.spyOn(dashboardApi, "autoTestSettings").mockImplementation(async () => savedTestSettings);
  const saveTest = vi.spyOn(dashboardApi, "saveAutoTestSettings").mockImplementation(async (_id, update) => {
    savedTestSettings = { ...savedTestSettings, ...update };
    return savedTestSettings;
  });
  const testJira = vi.spyOn(dashboardApi, "testJira").mockResolvedValue({ success: true, detail: "Connected to Jira project QA." });
  const latestRun: AutoTestRun = {
    run_id: "current", issue_key: "TEST-2", issue_title: "Checkout draft", jira_site: "example.atlassian.net",
    started_at: "2026-11-20T04:00:00Z", status: "completed", phase: "finished", detail: "",
    stopping: false, publishable: true, counts: { passed: 1 },
    artifacts: [{ filename: "draft.xlsx", kind: "workbook", case_id: null, platform: null, attempt: null }],
    jira: { status: "published", detail: "", comment_id: "100" },
  };
  vi.spyOn(dashboardApi, "autoTestHistory").mockResolvedValue({
    starting: false, poll: null,
    runs: [latestRun, { ...latestRun, run_id: "older", issue_key: "TEST-1", issue_title: "Edit profile", artifacts: [] }],
  });
  const save = vi.spyOn(dashboardApi, "updateSettings").mockImplementation(async (_id, update) => {
    if (update.jira) {
      persisted.jira = { ...update.jira, configured: true };
      persisted.auto_delivery.jira_site = update.jira.site;
      savedTestSettings = { ...savedTestSettings, jira_site: update.jira.site, jira_project: update.jira.project };
    }
    persisted = {
      ...persisted,
      feishu_webhook: { ...persisted.feishu_webhook, enabled: update.feishu_webhook?.enabled ?? persisted.feishu_webhook.enabled },
      auto_delivery: { ...persisted.auto_delivery, ...update.auto_delivery },
      auto_scan: { ...persisted.auto_scan, ...update.auto_scan },
    };
    return structuredClone(persisted);
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const container = document.createElement("div");
  const root = createRoot(container);
  const field = (selector: string) => container.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
  async function edit(selector: string, text: string): Promise<void> {
    await act(async () => {
      const input = field(selector);
      const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  async function click(selector: string): Promise<void> {
    await act(async () => container.querySelector<HTMLButtonElement>(selector)!.click());
  }
  async function submit(selector: string): Promise<void> {
    const form = container.querySelector<HTMLFormElement>(selector)!;
    expect(form.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false);
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  }
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    await edit("#auto-delivery-hooks", "Check approved Stories.\n\nVerify before delivery.");
    await edit("#auto-scan-hooks", "Create verified Bugs.\n\nReuse duplicates.");
    await edit("#auto-scan-lookback", "21");
    await edit("#feishu-webhook-url", "https://example.test/replacement");
    await click(".auto-delivery-settings-panel .settings-actions button");
    expect(container.querySelector(".notice-success > span")?.textContent).not.toMatch(/[.。]$/u);
    expect(container.querySelector(".notice-success > span")?.textContent).toBeTruthy();
    expect(save.mock.calls[0][1].auto_scan).toBeUndefined();
    expect(save.mock.calls[0][1].feishu_webhook).toBeUndefined();
    expect(field("#auto-scan-lookback").value).toBe("21");
    expect(field("#auto-scan-hooks").value).toBe("Create verified Bugs.\n\nReuse duplicates.");
    expect(field("#feishu-webhook-url").value).toBe("https://example.test/replacement");
    await click('.side-nav button[aria-label="Auto Delivery"]');
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(window.location.search).toContain("view=settings");
    await click(".auto-scan-settings-panel .settings-actions button");
    expect(save.mock.calls[1][1].auto_scan).toEqual({ enabled: false, lookback_days: 21, schedule_expression: "", trigger_hooks: ["Create verified Bugs.\n\nReuse duplicates."] });
    expect(save.mock.calls[1][1].auto_delivery).toBeUndefined();
    expect(field("#feishu-webhook-url").value).toBe("https://example.test/replacement");
    await click('.side-nav button[aria-label="Auto Scan"]');
    expect(confirm).toHaveBeenCalledTimes(2);
    await click(".settings-panel:not(.auto-delivery-settings-panel):not(.auto-scan-settings-panel) .settings-actions .button-primary");
    expect(save.mock.calls[2][1].feishu_webhook?.url).toBe("https://example.test/replacement");
    await click('.side-nav button[aria-label="Auto Delivery"]');
    expect(window.location.search).toContain("view=auto-delivery");
    expect(container.querySelector(".settings-panel")).toBeNull();
    expect(container.querySelector(".delivery-progress-panel")).not.toBeNull();
    await click('.side-nav button[aria-label="Auto Scan"]');
    expect(container.querySelector(".settings-panel")).toBeNull();
    expect(container.querySelector(".scan-history-panel")).not.toBeNull();
    await click('.side-nav button[aria-label="Auto Test"]');
    expect(container.querySelector("#auto-test-settings")).toBeNull();
    expect(container.querySelector(".auto-test-history tbody")?.children).toHaveLength(2);
    expect(container.querySelector("#auto-test-run-title")).toBeNull();
    await click("#auto-test-open-current");
    expect(container.querySelector("#auto-test-run-title")?.textContent).toBe("Checkout draft");
    expect(container.querySelector('.auto-test-evidence a[href$="/draft.xlsx"]')).not.toBeNull();
    expect(container.querySelector(".auto-test-history")).toBeNull();
    await click(".auto-test-back");
    await click("#auto-test-open-older");
    expect(container.querySelector("#auto-test-run-title")?.textContent).toBe("Edit profile");
    await click(".auto-test-back");
    await click(".auto-test-toolbar .button-secondary");
    expect(window.location.search).toContain("view=settings");
    const jira = ".jira-settings-panel";
    expect(container.querySelector(`${jira} h2`)?.textContent).toBe("Jira");
    expect(container.querySelectorAll(`${jira} input`)).toHaveLength(2);
    expect(container.querySelector(`${jira} textarea`)).toBeNull();
    expect(container.querySelector(".auto-delivery-settings-panel #auto-delivery-jql")).not.toBeNull();
    expect(container.querySelector("#auto-test-settings #auto-test-jql")).not.toBeNull();
    expect(container.querySelectorAll("#auto-test-settings fieldset input")).toHaveLength(1);
    const deliveryToggle = ".auto-delivery-settings-panel .settings-toggle";
    const testToggle = "#auto-test-settings .settings-toggle";
    const toggleLabel = (selector: string) => container.querySelector(`${selector} > span`)?.textContent;
    expect(toggleLabel(testToggle)).toBe(toggleLabel(deliveryToggle));
    await click(`${deliveryToggle} input`);
    await click(`${testToggle} input`);
    expect(toggleLabel(testToggle)).toBe(toggleLabel(deliveryToggle));
    await click(`${deliveryToggle} input`);
    await click(`${testToggle} input`);
    expect(field("#auto-test-jql").value).toBe(testSettings.trigger_jql);
    expect(field("#jira-project").value).toBe("TEST");
    const jqlHelp = document.querySelector("#auto-delivery-jql-help")!.textContent!;
    const jqlUsage = {
      en: ["Jira Query Language (JQL)", "scheduled check", "configured Jira site", "Existing receipts", "at most one card", "no Agent run", "ORDER BY", "newer move", "status =", "Completed"],
      "zh-CN": ["Jira 查询语言（JQL）", "每次定时检查", "已配置的 Jira 站点", "交付记录", "最多启动一张", "不启动 Agent", "ORDER BY", "重新移入", "status =", "Completed"],
      "zh-TW": ["Jira 查詢語言（JQL）", "每次排程檢查", "已設定的 Jira 站點", "交付紀錄", "最多啟動一張", "不啟動 Agent", "ORDER BY", "重新移入", "status =", "Completed"],
    };
    for (const explanation of jqlUsage[locale]) expect(jqlHelp).toContain(explanation);

    await edit("#jira-site", "qa.atlassian.net");
    await edit("#jira-project", "QA");
    await edit("#auto-delivery-jql", 'status = "Ready for Dev"');
    await edit("#auto-delivery-hooks", "Verify release evidence.");
    await edit("#auto-test-jql", 'status = "Ready for Test"');
    await click('.side-nav button[aria-label="Auto Test"]');
    expect(confirm).toHaveBeenCalledTimes(3);

    await click(`${jira} button[type="button"]`);
    expect(testJira).toHaveBeenCalledWith("workspace-one", { site: "qa.atlassian.net", project: "QA" });
    expect(container.querySelector(`${jira} [role="status"]`)?.textContent).toBe("Connected to Jira project QA.");
    expect(save).toHaveBeenCalledTimes(3);
    expect(saveTest).not.toHaveBeenCalled();
    await submit(`${jira} form`);
    expect(save).toHaveBeenLastCalledWith("workspace-one", {
      jira: { site: "qa.atlassian.net", project: "QA" },
    });
    expect(field("#auto-delivery-hooks").value).toBe("Verify release evidence.");
    expect(field("#auto-delivery-jql").value).toBe('status = "Ready for Dev"');
    expect(field("#auto-test-jql").value).toBe('status = "Ready for Test"');
    expect(saveTest).not.toHaveBeenCalled();
    await submit("#auto-test-settings form");
    expect(saveTest).toHaveBeenLastCalledWith("workspace-one", {
      enabled: false, schedule_expression: testSettings.schedule_expression,
      trigger_jql: 'status = "Ready for Test"',
    });
    expect(savedTestSettings.jira_project).toBe("QA");
    await click(".auto-delivery-settings-panel .settings-actions button");
    expect(persisted.auto_delivery.trigger_jql).toBe('status = "Ready for Dev"');
    expect(persisted.auto_delivery.jira_site).toBe("qa.atlassian.net");
    await click('.side-nav button[aria-label="Auto Test"]');
    expect(container.querySelector("#auto-test-settings")).toBeNull();
  } finally {
    await act(async () => root.unmount());
  }
});

it("opens a scheduled workflow, reloads its saved schedule on return, and isolates workspaces", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", "en");
  window.history.replaceState(null, "", "?workspace=workspace-one&view=overview");
  let flow: FlowDocument = {
    flow_id: "auto-guard", name: "Auto Guard", enabled: true, brief: "Inspect production.", path: "lumon/flows/auto-guard.md",
    valid: true, error: null, content: "# Auto Guard", schedule_enabled: true, schedule_expression: "0 10 * * 1-5",
  };
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue(workspaces);
  vi.spyOn(dashboardApi, "getBootstrap").mockResolvedValue({ version: "1.4.10", workspace_count: 2, has_workspaces: true });
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  vi.spyOn(dashboardApi, "getSettings").mockImplementation(async (workspaceId) => ({ ...settings, workspace_id: workspaceId }));
  const overview = vi.spyOn(dashboardApi, "getOverview").mockImplementation(async (workspaceId) => ({
    workspace_id: workspaceId, name: workspaceId, path: "/test", created_at: "2026-09-30T04:00:00Z", lumon_version: "1.4.10", repositories: [],
    workflow_schedules: workspaceId === "workspace-one" ? [{ flow_id: flow.flow_id, name: flow.name, enabled: flow.schedule_enabled, schedule_expression: flow.schedule_expression }] : [],
  }));
  const flowRequest = vi.fn(async (path: string) => {
    if (path === "/api/workspaces/workspace-one/flows") return new Response(JSON.stringify([flow]));
    if (path === "/api/workspaces/workspace-two/flows") return new Response("[]");
    if (path === "/api/workspaces/workspace-one/flows/auto-guard") return new Response(JSON.stringify(flow));
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal("fetch", flowRequest);
  const saveSchedule = vi.spyOn(dashboardApi, "updateFlowSchedule").mockImplementation(async (_workspaceId, _flowId, enabled, scheduleExpression) => {
    flow = { ...flow, schedule_enabled: enabled, schedule_expression: scheduleExpression };
    return flow;
  });
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    const scheduled = Array.from(container.querySelectorAll<HTMLButtonElement>(".automation-summary")).find((row) => row.textContent?.includes("Auto Guard"))!;
    await act(async () => scheduled.click());
    expect(window.location.search).toContain("view=flows");
    expect(flowRequest).toHaveBeenCalledWith("/api/workspaces/workspace-one/flows/auto-guard", expect.any(Object));
    expect(container.querySelector(".flow-row.active strong")?.textContent).toBe("Auto Guard");
    expect(container.querySelector<HTMLInputElement>("#flow-schedule-expression")?.value).toBe("0 10 * * 1-5");
    expect(container.querySelector(".flow-schedule-panel")?.closest("[hidden]")).toBeNull();
    expect(saveSchedule).not.toHaveBeenCalled();
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>("#flow-schedule-expression")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "0 11 * * 1-5");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector<HTMLButtonElement>(".flow-schedule-panel .settings-actions button")!.click());
    expect(saveSchedule).toHaveBeenCalledWith("workspace-one", "auto-guard", true, "0 11 * * 1-5");
    await act(async () => container.querySelector<HTMLButtonElement>('.side-nav button[aria-label="Overview"]')!.click());
    expect(overview).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".workspace-automation")?.textContent).toContain("0 11 * * 1-5");
    await act(async () => {
      const picker = container.querySelector<HTMLSelectElement>(".workspace-picker select")!;
      picker.value = "workspace-two";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(container.querySelector(".workspace-automation")?.textContent).not.toContain("Auto Guard");
    await act(async () => container.querySelector<HTMLButtonElement>('.side-nav button[aria-label="Workflows"]')!.click());
    expect(container.querySelector("#flow-schedule-expression")).toBeNull();
    expect(flowRequest.mock.calls.filter(([path]) => path.endsWith("/auto-guard"))).toHaveLength(1);
  } finally {
    await act(async () => root.unmount());
  }
});

it("saves repository toggles immediately and protects only pending saves when navigating", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", "en");
  window.history.replaceState(null, "", "?workspace=workspace-one&view=repositories");
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue(workspaces);
  vi.spyOn(dashboardApi, "getBootstrap").mockResolvedValue({ version: "1.5.20", workspace_count: 2, has_workspaces: true });
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  vi.spyOn(dashboardApi, "getSettings").mockResolvedValue(settings);
  const repositoryOverview = {
    workspace_id: "workspace-one", name: "Example", path: "/test", created_at: "2026-10-29", lumon_version: "1.5.20", repositories_version: "version", workflow_schedules: [],
    repositories: [{ name: "app", path: "/test/repos/app", branch: "main", health: "ready" as const, detail: "", auto_scan: true, auto_delivery: true }],
  };
  const getOverview = vi.spyOn(dashboardApi, "getOverview").mockResolvedValue(repositoryOverview);
  let finishSave!: (response: { repositories_version: string }) => void;
  const save = vi.spyOn(dashboardApi, "updateRepositoryScopes").mockReturnValue(new Promise((resolve) => { finishSave = resolve; }));
  vi.spyOn(dashboardApi, "repositorySources").mockResolvedValue([]);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    expect(container.querySelector("h1")?.textContent).toBe("Repositories");
    expect(container.querySelector('.side-nav [aria-current="page"]')?.textContent).toBe("Repositories");
    expect(container.querySelector(".workspace-home")).toBeNull();
    await act(async () => container.querySelector<HTMLInputElement>('input[aria-label="app Auto Scan"]')!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('.side-nav button[aria-label="Overview"]')!.click());
    expect(confirm).toHaveBeenCalledOnce();
    expect(window.location.search).toContain("view=repositories");
    const picker = container.querySelector<HTMLSelectElement>(".workspace-picker select")!;
    await act(async () => { picker.value = "workspace-two"; picker.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(window.location.search).toContain("workspace-one");
    expect(container.querySelector<HTMLInputElement>('input[aria-label="app Auto Scan"]')!.checked).toBe(false);
    expect(save).toHaveBeenCalledExactlyOnceWith("workspace-one", "version", [{ name: "app", auto_scan: false, auto_delivery: true }]);
    getOverview.mockResolvedValue({ ...repositoryOverview, repositories_version: "next-version", repositories: [{ ...repositoryOverview.repositories[0], auto_scan: false }] });
    await act(async () => finishSave({ repositories_version: "next-version" }));
    await act(async () => container.querySelector<HTMLButtonElement>('.side-nav button[aria-label="Overview"]')!.click());
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(window.location.search).toContain("view=overview");
  } finally { await act(async () => root.unmount()); }
});

it.each([false, true])("ignores an old repository refresh after switching workspaces (return: %s)", async (returnToFirst) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", "en");
  window.history.replaceState(null, "", "?workspace=workspace-one&view=repositories");
  vi.spyOn(dashboardApi, "listWorkspaces").mockResolvedValue(workspaces);
  vi.spyOn(dashboardApi, "getBootstrap").mockResolvedValue({ version: "1.5.20", workspace_count: 2, has_workspaces: true });
  vi.spyOn(dashboardApi, "getAgentSettings").mockResolvedValue(agent);
  vi.spyOn(dashboardApi, "getSettings").mockImplementation(async (id) => ({ ...settings, workspace_id: id }));
  const data = (id: string, branch = "main") => ({
    workspace_id: id, name: id, path: "/test", created_at: "2026-10-29", lumon_version: "1.5.20", repositories_version: "version", workflow_schedules: [],
    repositories: [{ name: `${id}-repo`, path: `/test/${id}`, branch, health: "ready" as const, detail: "", auto_scan: true, auto_delivery: true }],
  });
  let readsForOne = 0;
  let finishRefresh!: (value: ReturnType<typeof data>) => void;
  vi.spyOn(dashboardApi, "getOverview").mockImplementation(async (id) => {
    if (id === "workspace-one" && ++readsForOne === 2) return new Promise((resolve) => { finishRefresh = resolve; });
    if (id === "workspace-one" && readsForOne >= 3) return data(id, "current-release");
    return data(id);
  });
  vi.spyOn(dashboardApi, "repositorySources").mockResolvedValue(["github.com"]);
  vi.spyOn(dashboardApi, "searchRepositories").mockResolvedValue([{ name: "new-repo", full_name: "org/new-repo", clone_url: "https://github.com/org/new-repo.git", branch: "main", mcp_server: "github", auto_scan: true, auto_delivery: true }]);
  vi.spyOn(dashboardApi, "addRepositories").mockResolvedValue();
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<I18nProvider><App /></I18nProvider>));
    await act(async () => container.querySelector<HTMLButtonElement>("#github-repositories-tab")!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Connect org/new-repo"]')!.click());
    const picker = container.querySelector<HTMLSelectElement>(".workspace-picker select")!;
    await act(async () => { picker.value = "workspace-two"; picker.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(container.querySelector("tbody")?.textContent).toContain("workspace-two-repo");
    if (returnToFirst) {
      await act(async () => { picker.value = "workspace-one"; picker.dispatchEvent(new Event("change", { bubbles: true })); });
      expect(container.querySelector("tbody")?.textContent).toContain("current-release");
    }
    await act(async () => finishRefresh(data("workspace-one", "old-release")));
    expect(window.location.search).toContain(returnToFirst ? "workspace-one" : "workspace-two");
    expect(container.querySelector("tbody")?.textContent).toContain(returnToFirst ? "current-release" : "workspace-two-repo");
    expect(container.querySelector("tbody")?.textContent).not.toContain("old-release");
    expect(container.textContent).not.toContain("Reading workspace");
  } finally { await act(async () => root.unmount()); }
});
