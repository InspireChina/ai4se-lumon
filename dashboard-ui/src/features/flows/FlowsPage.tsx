import { LoaderCircle, Play, Save } from "lucide-react";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { FlowDocument, FlowSummary } from "../../shared/types";
import {
  MarkdownDocumentsPage,
  type MarkdownDocumentApi,
  type MarkdownDocumentLabels,
} from "../shared/MarkdownDocumentsPage";

interface FlowsPageProps {
  workspaceId: string;
  initialDocumentId?: string;
  onDirtyChange: (dirty: boolean) => void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}

const starterContent = [
  "---",
  'id = "my-flow"',
  'name = "My workflow"',
  "enabled = true",
  'brief = "Describe the user request this workflow handles."',
  "---",
  "",
  "# My workflow",
  "",
  "## When to use",
  "",
  "Describe the request or business situation this workflow handles.",
  "",
  "## Inputs and evidence",
  "",
  "List the files, links, user-provided context, and Workspace evidence to inspect.",
  "",
  "## Process",
  "",
  "1. Describe the first step and the decision it makes.",
  "2. Describe the next step, including tools or commands when needed.",
  "3. Describe validation, safety checks, and what to do when evidence is missing.",
  "",
  "## Output",
  "",
  "Describe the files, reply, or other result to produce and how to verify it.",
  "",
  "## Boundaries",
  "",
  "State what this workflow does not cover and when another Agent or workflow should be used.",
  "",
].join("\n");

const flowApi: MarkdownDocumentApi<FlowSummary, FlowDocument> = {
  list: dashboardApi.listFlows,
  get: dashboardApi.getFlow,
  create: dashboardApi.createFlow,
  update: dashboardApi.updateFlow,
  delete: dashboardApi.deleteFlow,
};

const getFlowId = (flow: FlowSummary) => flow.flow_id;

export function FlowsPage(props: FlowsPageProps): React.JSX.Element {
  const { t } = useI18n();
  const labels: MarkdownDocumentLabels = {
    title: t("flows.title"),
    subtitle: t("flows.subtitle"),
    unsaved: t("flows.unsaved"),
    select: t("flows.select"),
    loading: t("resources.loading"),
    refresh: t("flows.refresh"),
    new: t("flows.new"),
    empty: t("flows.empty"),
    emptyHelp: t("flows.emptyHelp"),
    search: t("resources.search"),
    status: t("resources.status"),
    all: t("resources.all"),
    noMatches: t("resources.noMatches"),
    enabled: t("flows.enabled"),
    disabled: t("flows.disabled"),
    invalid: t("flows.invalid"),
    content: t("flows.content"),
    viewMode: t("flows.viewMode"),
    preview: t("flows.preview"),
    edit: t("flows.edit"),
    metadata: t("flows.metadata"),
    idHelp: t("flows.idHelp"),
    enable: t("flows.enable"),
    disable: t("flows.disable"),
    save: t("flows.save"),
    delete: t("flows.delete"),
    saved: t("flows.saved"),
    created: t("flows.created"),
    deleted: t("flows.deleted"),
    confirmDelete: t("flows.confirmDelete"),
    loadFailed: t("flows.loadFailed"),
    unsavedConfirm: t("app.unsavedViewConfirm"),
  };

  return (
    <MarkdownDocumentsPage
      {...props}
      labels={labels}
      starterContent={starterContent}
      api={flowApi}
      getId={getFlowId}
      details={{
        label: t("resources.schedule"),
        render: (document, markDirty, executionBlocked, headerActions) => (
          <FlowSchedulePanel
            key={`${props.workspaceId}:${document.flow_id}:${document.enabled}:${document.schedule_enabled}:${document.schedule_expression}`}
            workspaceId={props.workspaceId}
            flow={document}
            onDirtyChange={markDirty}
            onNotice={props.onNotice}
            onError={props.onError}
            executionBlocked={executionBlocked}
            headerActions={headerActions}
          />
        ),
      }}
    />
  );
}

interface FlowSchedulePanelProps {
  workspaceId: string;
  flow: FlowDocument;
  onDirtyChange: (dirty: boolean) => void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
  executionBlocked: boolean;
  headerActions: HTMLElement | null;
}

function FlowSchedulePanel({
  workspaceId,
  flow: selectedFlow,
  onDirtyChange,
  onNotice,
  onError,
  executionBlocked,
  headerActions,
}: FlowSchedulePanelProps): React.JSX.Element {
  const { t } = useI18n();
  const [enabled, setEnabled] = useState(selectedFlow.schedule_enabled);
  const [scheduleExpression, setScheduleExpression] = useState(selectedFlow.schedule_expression);
  const [savedEnabled, setSavedEnabled] = useState(selectedFlow.schedule_enabled);
  const [savedExpression, setSavedExpression] = useState(selectedFlow.schedule_expression);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const runPending = useRef(false);

  const changed = enabled !== savedEnabled || scheduleExpression !== savedExpression;

  async function runNow(): Promise<void> {
    if (runPending.current || executionBlocked || !selectedFlow.enabled) return;
    runPending.current = true;
    setRunning(true);
    try {
      await dashboardApi.runFlow(workspaceId, selectedFlow.flow_id);
      onNotice(t("flows.runCompleted"));
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : t("flows.runFailed"));
    } finally {
      runPending.current = false;
      setRunning(false);
    }
  }

  async function saveSchedule(): Promise<void> {
    setSaving(true);
    try {
      const saved = await dashboardApi.updateFlowSchedule(
        workspaceId,
        selectedFlow.flow_id,
        enabled,
        scheduleExpression.trim(),
      );
      setEnabled(saved.schedule_enabled);
      setSavedEnabled(saved.schedule_enabled);
      setScheduleExpression(saved.schedule_expression);
      setSavedExpression(saved.schedule_expression);
      onDirtyChange(false);
      onNotice(t("flows.scheduleSaved"));
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : t("flows.loadFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="flow-schedule-panel" aria-label={t("flows.scheduleTitle")}>
      {headerActions && createPortal(
        <button
          className="button button-secondary"
          type="button"
          onClick={() => void runNow()}
          disabled={running || saving || executionBlocked || !selectedFlow.enabled}
        >
          {running ? <LoaderCircle size={16} className="spin" /> : <Play size={16} />}
          {t(running ? "flows.running" : "flows.runNow")}
        </button>,
        headerActions,
      )}
      <div className="flow-schedule-heading">
        <label className={`settings-toggle ${enabled ? "is-enabled" : ""}`}>
          <span>{t("flows.scheduleToggle")}</span>
          <input
            type="checkbox"
            role="switch"
            checked={enabled}
            disabled={!selectedFlow.enabled || running}
            aria-label={t("flows.scheduleToggle")}
            onChange={(event) => {
              const nextEnabled = event.target.checked;
              setEnabled(nextEnabled);
              onDirtyChange(
                nextEnabled !== savedEnabled || scheduleExpression !== savedExpression,
              );
            }}
          />
          <span className="settings-switch" aria-hidden="true"><span className="settings-switch-thumb" /></span>
        </label>
      </div>
      <div className="flow-schedule-form">
        <div className="flow-schedule-fields">
          <label className="field-label" htmlFor="flow-schedule-expression">
            {t("flows.scheduleExpression")}
          </label>
          <input
            id="flow-schedule-expression"
            className="text-input mono"
            value={scheduleExpression}
            maxLength={128}
            placeholder="0 8 * * 1-5"
            onChange={(event) => {
              const nextExpression = event.target.value;
              setScheduleExpression(nextExpression);
              onDirtyChange(enabled !== savedEnabled || nextExpression !== savedExpression);
            }}
          />
        </div>
        <div className="settings-actions">
          <button
            className="button button-primary"
            type="button"
            onClick={() => void saveSchedule()}
            disabled={saving || running || !changed || (enabled && !selectedFlow.enabled)}
          >
            {saving ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}
            {t("flows.scheduleSave")}
          </button>
        </div>
      </div>
      {!selectedFlow.enabled && <p className="field-help">{t("flows.scheduleFlowDisabled")}</p>}
    </section>
  );
}
