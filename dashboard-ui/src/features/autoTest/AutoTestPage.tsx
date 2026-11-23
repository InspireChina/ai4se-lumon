import {
  ArrowLeft, ArrowUpRight, ChevronRight, Download, FileSpreadsheet, FlaskConical, Image, LoaderCircle,
  Play, RotateCw, Settings, Square, Video,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { AutoTestHistory, AutoTestRun } from "../../shared/types";
import { EvidencePreview, type EvidenceFile } from "./EvidencePreview";
import "./autoTest.css";

type Props = {
  workspaceId: string;
  onSettings: () => void;
  onError: (error: string | null) => void;
};

const messages = {
  en: {
    run: "Run now", runs: "Test runs", settings: "Settings",
    issue: "Jira issue", status: "Status", results: "Results", started: "Started",
    stop: "Stop", stopping: "Stopping", retry: "Retest", publish: "Retry Jira publication",
    files: "Evidence", noRuns: "No test runs yet", next: "Run a test to see its results here.",
    configure: "Set the Jira site and project in Settings → Jira, then add a trigger query in Auto Test settings.", waiting: "Checking Jira…",
    loading: "Loading tests…", workbook: "Excel test cases", recording: "Recording",
    screenshot: "Screenshot", download: "Download", previewLoading: "Loading preview…",
    previewFailed: "Preview unavailable. Try downloading the file.",
    running: "Running", completed: "Completed", blocked: "Blocked",
    stopped: "Stopped", interrupted: "Interrupted", planning: "Planning cases", building: "Building",
    testing: "Testing", passed: "Passed", failed: "Failed", review: "Review",
    not_run: "Not run", pending: "Pending", published: "Published", partial: "Partially published",
    idle: "No eligible Jira issues.", publication: "Jira publication",
  },
  "zh-TW": {
    run: "立即執行", runs: "測試記錄", settings: "設定",
    issue: "Jira 卡片", status: "狀態", results: "結果", started: "開始時間",
    stop: "停止", stopping: "正在停止", retry: "重測", publish: "重試 Jira 發佈",
    files: "測試證據", noRuns: "目前沒有測試記錄", next: "執行測試後，結果會顯示在這裡。",
    configure: "請在「設定 → Jira」填寫站點和專案鍵，再到 Auto Test 設定中填寫觸發條件。", waiting: "正在查詢 Jira…",
    loading: "正在載入測試…", workbook: "Excel 用例", recording: "錄屏",
    screenshot: "截圖", download: "下載", previewLoading: "正在載入預覽…",
    previewFailed: "無法載入預覽。請下載檔案後查看。",
    running: "執行中", completed: "已完成", blocked: "阻塞",
    stopped: "已停止", interrupted: "已中斷", planning: "生成用例", building: "構建中",
    testing: "測試中", passed: "通過", failed: "失敗", review: "待複核",
    not_run: "未執行", pending: "待發佈", published: "已發佈", partial: "部分發佈",
    idle: "沒有符合條件的 Jira 卡片。", publication: "Jira 發佈",
  },
  "zh-CN": {
    run: "立即执行", runs: "测试记录", settings: "配置",
    issue: "Jira 卡片", status: "状态", results: "结果", started: "开始时间",
    stop: "停止", stopping: "正在停止", retry: "重测", publish: "重试 Jira 发布",
    files: "测试证据", noRuns: "暂无测试记录", next: "执行测试后，结果会显示在这里。",
    configure: "请在「配置 → Jira」填写站点和项目键，再到 Auto Test 配置中填写触发条件。", waiting: "正在查询 Jira…",
    loading: "正在加载测试…", workbook: "Excel 用例", recording: "录屏",
    screenshot: "截图", download: "下载", previewLoading: "正在加载预览…",
    previewFailed: "无法加载预览。请下载文件后查看。",
    running: "执行中", completed: "已完成", blocked: "阻塞",
    stopped: "已停止", interrupted: "已中断", planning: "生成用例", building: "构建中",
    testing: "测试中", passed: "通过", failed: "失败", review: "待复核",
    not_run: "未执行", pending: "待发布", published: "已发布", partial: "部分发布",
    idle: "没有符合条件的 Jira 卡片。", publication: "Jira 发布",
  },
};
type Copy = (typeof messages)[keyof typeof messages];

const outcomes = ["passed", "failed", "review", "blocked", "not_run"] as const;

function RunStatus({ run, copy }: { run: AutoTestRun; copy: Copy }): React.JSX.Element {
  const style = run.status === "completed" ? "ready"
    : run.status === "blocked" ? "warning" : run.status === "running" ? "running" : "neutral";
  return <span className={`status-pill status-${style}`}>
    <span className={`auto-test-dot is-${run.status}`} aria-hidden="true" />
    {run.status === "running" && run.stopping ? copy.stopping : copy[run.status]}
  </span>;
}

function phaseLabel(run: AutoTestRun, copy: Copy): string {
  if (run.status !== "running") return "";
  const phase = run.phase;
  return phase === "planning" || phase === "building" || phase === "testing" ? copy[phase] : "";
}

export function AutoTestPage({ workspaceId, onSettings, onError }: Props): React.JSX.Element {
  const { locale, t } = useI18n();
  const copy = messages[locale];
  const [history, setHistory] = useState<AutoTestHistory>();
  const [currentId, setCurrentId] = useState<string>();
  const returnTo = useRef<string | undefined>(undefined);
  const [configured, setConfigured] = useState<boolean>();
  const [pending, setPending] = useState(false);
  const reportError = useCallback((error: unknown) => {
    onError(error instanceof ApiError ? error.message : t("app.dashboardRequestFailed"));
  }, [onError, t]);

  useEffect(() => {
    let current = true;
    void dashboardApi.autoTestSettings(workspaceId).then((settings) => {
      if (current) setConfigured(Boolean(settings.trigger_jql && settings.jira_site && settings.jira_project));
    }).catch((error: unknown) => { if (current) reportError(error); });
    return () => { current = false; };
  }, [workspaceId, reportError]);

  useEffect(() => {
    let current = true;
    let refreshing = false;
    async function refresh(): Promise<void> {
      if (refreshing) return;
      refreshing = true;
      try {
        const next = await dashboardApi.autoTestHistory(workspaceId);
        if (current) setHistory(next);
      } catch (error: unknown) {
        if (current) reportError(error);
      } finally {
        refreshing = false;
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => { current = false; window.clearInterval(timer); };
  }, [workspaceId, reportError]);

  async function perform(request: () => Promise<unknown>): Promise<boolean> {
    setPending(true);
    onError(null);
    try {
      await request();
      setHistory(await dashboardApi.autoTestHistory(workspaceId));
      return true;
    } catch (error: unknown) {
      reportError(error);
      return false;
    } finally {
      setPending(false);
    }
  }

  const runs = history?.runs ?? [];
  const active = runs.find((run) => run.status === "running");
  const selected = runs.find((run) => run.run_id === currentId);
  const selectedId = selected?.run_id;
  const busy = Boolean(history?.starting || active);
  const status = history?.poll;
  const checking = Boolean(history?.starting && !active);
  const pollMessage = checking ? copy.waiting : status?.detail || (status?.status === "idle" ? copy.idle : "");
  const date = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });

  useEffect(() => {
    if (selectedId) {
      document.getElementById("auto-test-run-title")?.focus();
    } else if (returnTo.current) {
      document.getElementById(`auto-test-open-${returnTo.current}`)?.focus();
    }
  }, [selectedId]); // Polling updates a run in place without moving focus.

  if (selected) {
    return <div className="auto-test">
      <RunDetail run={selected} workspaceId={workspaceId} copy={copy} started={date.format(new Date(selected.started_at))}
        pending={pending} busy={busy} perform={perform} onBack={() => setCurrentId(undefined)}
        onRetest={async () => {
          if (await perform(() => dashboardApi.startAutoTest(workspaceId, selected.run_id))) {
            setCurrentId((current) => current === selected.run_id ? undefined : current);
          }
        }} />
    </div>;
  }

  return (
    <div className="page-stack auto-test">
      <div className="page-heading auto-test-heading">
        <h1>Auto Test</h1>
        <div className="auto-test-toolbar">
          <button className="button button-secondary" type="button" onClick={onSettings}>
            <Settings size={15} aria-hidden="true" />{copy.settings}
          </button>
          <button className="button button-primary" type="button" disabled={pending || busy || !configured || !history}
            onClick={() => void perform(() => dashboardApi.startAutoTest(workspaceId))}>
            {checking ? <LoaderCircle size={15} className="spin" aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
            {copy.run}
          </button>
        </div>
      </div>
      {pollMessage && <p className="auto-test-poll" role="status">{pollMessage}</p>}
      {runs.length ? (
        <RunTable runs={runs} copy={copy} date={date} onOpen={(runId) => {
          returnTo.current = runId;
          setCurrentId(runId);
        }} />
      ) : (
        <section className="auto-test-empty" aria-live="polite">
          <span className="auto-test-empty-icon"><FlaskConical size={22} aria-hidden="true" /></span>
          <h2>{history ? copy.noRuns : copy.loading}</h2>
          {history && <p>{configured === false ? copy.configure : copy.next}</p>}
        </section>
      )}
    </div>
  );
}

function RunTable({ runs, copy, date, onOpen }: {
  runs: AutoTestRun[]; copy: Copy; date: Intl.DateTimeFormat; onOpen: (runId: string) => void;
}): React.JSX.Element {
  return (
    <table className="auto-test-history" aria-label={copy.runs}>
      <thead>
        <tr>
          <th scope="col">{copy.issue}</th>
          <th scope="col">{copy.status}</th>
          <th scope="col" className="auto-test-table-results">{copy.results}</th>
          <th scope="col" className="auto-test-table-date">{copy.started}</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const hasResults = outcomes.some((name) => run.counts[name]);
          const phase = phaseLabel(run, copy);
          return <tr key={run.run_id}>
            <th scope="row">
              <button id={`auto-test-open-${run.run_id}`} type="button" className="auto-test-open"
                onClick={() => onOpen(run.run_id)}>
                <span className="auto-test-open-name">
                  <span className="auto-test-issue-key">{run.issue_key}</span>
                  <strong>{run.issue_title}</strong>
                </span>
                <ChevronRight size={15} aria-hidden="true" />
              </button>
            </th>
            <td>
              <RunStatus run={run} copy={copy} />
              {phase && <span className="auto-test-table-phase">{phase}</span>}
            </td>
            <td className="auto-test-table-results">
              {hasResults ? <ul className="auto-test-result-summary">
                {outcomes.filter((name) => run.counts[name]).map((name) => (
                  <li key={name} className={`is-${name}`}>
                    <strong>{run.counts[name]}</strong> {copy[name]}
                  </li>
                ))}
              </ul> : "—"}
            </td>
            <td className="auto-test-table-date">
              <time dateTime={run.started_at}>{date.format(new Date(run.started_at))}</time>
            </td>
          </tr>;
        })}
      </tbody>
    </table>
  );
}

function RunDetail({ run, workspaceId, copy, started, pending, busy, perform, onBack, onRetest }: {
  run: AutoTestRun; workspaceId: string; copy: Copy; pending: boolean; busy: boolean;
  started: string; perform: (request: () => Promise<unknown>) => Promise<boolean>;
  onBack: () => void; onRetest: () => Promise<void>;
}): React.JSX.Element {
  const [preview, setPreview] = useState<EvidenceFile>();
  const issue = `https://${run.jira_site}/browse/${run.issue_key}`;
  const fileBase = `/api/workspaces/${workspaceId}/auto-test/runs/${run.run_id}/artifacts/`;
  const phase = phaseLabel(run, copy);

  return (
    <article className="auto-test-detail" aria-labelledby="auto-test-run-title">
      <button className="auto-test-back" type="button" onClick={onBack}>
        <ArrowLeft size={16} aria-hidden="true" />{copy.runs}
      </button>
      <header className="page-heading auto-test-detail-header">
        <div className="auto-test-detail-title">
          <a className="auto-test-issue-link auto-test-issue-key" href={issue} target="_blank" rel="noreferrer">
            {run.issue_key}<ArrowUpRight size={13} aria-hidden="true" />
          </a>
          <h1 id="auto-test-run-title" tabIndex={-1}>{run.issue_title}</h1>
          <div className="auto-test-detail-meta">
            <RunStatus run={run} copy={copy} />
            <time dateTime={run.started_at}>{started}</time>
            {phase && <span role="status">{phase}</span>}
          </div>
        </div>
        {run.status === "running" ? (
          <button className="button button-secondary" type="button" disabled={pending || run.stopping}
            onClick={() => void perform(() => dashboardApi.stopAutoTest(workspaceId, run.run_id))}>
            <Square size={14} aria-hidden="true" />{run.stopping ? copy.stopping : copy.stop}
          </button>
        ) : (
          <button className="button button-secondary" type="button" disabled={pending || busy} onClick={() => void onRetest()}>
            <RotateCw size={14} aria-hidden="true" />{copy.retry}
          </button>
        )}
      </header>

      <dl className="auto-test-outcomes">
        {outcomes.filter((name) => name !== "not_run" || run.counts.not_run).map((name) => (
          <div key={name} className={run.counts[name] ? `has-count is-${name}` : undefined}>
            <dt>{copy[name]}</dt><dd>{run.counts[name] ?? 0}</dd>
          </div>
        ))}
      </dl>
      {run.detail && <p className="auto-test-notes">{run.detail}</p>}

      {run.artifacts.length > 0 && (
        <section className="auto-test-evidence" aria-labelledby="auto-test-evidence-title">
          <h2 id="auto-test-evidence-title">{copy.files}</h2>
          <ul>
            {run.artifacts.map((file) => {
              const Icon = file.kind === "workbook" ? FileSpreadsheet : file.kind === "screenshot" ? Image : Video;
              const typeLabel = file.kind === "screenshot" ? copy.screenshot : copy.recording;
              const label = file.kind === "workbook" ? copy.workbook
                : [typeLabel, file.case_id, file.platform === "ios" ? "iOS" : file.platform === "web" ? "Web" : "", file.attempt && file.attempt > 1 ? `#${file.attempt}` : ""].filter(Boolean).join(" · ");
              const url = fileBase + encodeURIComponent(file.filename);
              return (
                <li key={file.filename}>
                  <button className="auto-test-file-preview" type="button" title={file.filename} aria-haspopup="dialog"
                    onClick={() => setPreview({ kind: file.kind, url, label })}>
                    <Icon size={17} className={`auto-test-file-icon is-${file.kind}`} aria-hidden="true" />
                    <strong className="auto-test-file-name">{label}</strong>
                  </button>
                  <a className="auto-test-file-action" href={url} download
                    aria-label={`${copy.download} ${label}`} title={copy.download}>
                    <Download size={15} aria-hidden="true" />
                  </a>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {preview && <EvidencePreview file={preview} copy={copy} onClose={() => setPreview(undefined)} />}

      {run.publishable && (
        <section className="auto-test-publication" aria-label={copy.publication}>
          <div>
            <div className="auto-test-publication-status">
              <h2>{copy.publication}</h2>
              <span className={`status-pill status-${run.jira.status === "published" ? "ready" : run.jira.status === "failed" ? "warning" : "neutral"}`}>{copy[run.jira.status]}</span>
            </div>
            {run.jira.detail && <p className="auto-test-notes">{run.jira.detail}</p>}
          </div>
          {run.jira.status !== "published" && (
            <button className="button button-secondary" type="button" disabled={pending || busy}
              onClick={() => void perform(() => dashboardApi.publishAutoTest(workspaceId, run.run_id))}>
              {copy.publish}
            </button>
          )}
        </section>
      )}
    </article>
  );
}
