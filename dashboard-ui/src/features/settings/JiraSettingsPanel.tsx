import { LoaderCircle, Plug, Save, Ticket } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { JiraSettings, SettingsUpdate } from "../../shared/types";

export function JiraSettingsPanel({ workspaceId, settings, onSave, onDirtyChange }: {
  workspaceId: string;
  settings?: JiraSettings & { configured: boolean };
  onSave: (update: SettingsUpdate) => Promise<boolean>;
  onDirtyChange: (dirty: boolean) => void;
}): React.JSX.Element {
  const { t } = useI18n();
  const { site = "", project = "", configured = false } = settings ?? {};
  const [draft, setDraft] = useState<JiraSettings>({ site, project });
  const [pending, setPending] = useState<"test" | "save">();
  const [result, setResult] = useState<{ success: boolean; detail: string }>();
  const dirty = draft.site !== site || draft.project !== project;
  const connection = { site: draft.site.trim().toLowerCase(), project: draft.project.trim() };
  const ready = Boolean(connection.site && connection.project);

  useEffect(() => {
    setDraft({ site, project });
    setResult(undefined);
  }, [site, project]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  function edit(field: keyof JiraSettings, value: string): void {
    setDraft((current) => ({ ...current, [field]: value }));
    setResult(undefined);
  }

  async function test(): Promise<void> {
    if (!ready || pending) return;
    setPending("test");
    setResult(undefined);
    try {
      setResult(await dashboardApi.testJira(workspaceId, connection));
    } catch (error) {
      setResult({ success: false, detail: error instanceof ApiError ? error.message : t("app.dashboardRequestFailed") });
    } finally {
      setPending(undefined);
    }
  }

  async function save(): Promise<void> {
    if (!ready || pending || (configured && !dirty)) return;
    setPending("save");
    try {
      if (await onSave({ jira: connection })) setDraft(connection);
    } finally {
      setPending(undefined);
    }
  }

  return <section className="panel settings-panel jira-settings-panel" aria-labelledby="jira-settings-title">
    <div className="panel-heading">
      <div className="settings-title">
        <span className="workspace-glyph glyph-blue"><Ticket size={18} aria-hidden="true" /></span>
        <h2 id="jira-settings-title">Jira</h2>
      </div>
      {dirty && <span className="unsaved-label">{t("settings.unsaved")}</span>}
    </div>
    <form className="jira-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset className="form-grid jira-settings-fields" disabled={pending !== undefined}>
        <label>
          <span className="field-label">{t("settings.jiraSite")}</span>
          <input id="jira-site" className="text-input" value={draft.site} required
            maxLength={253} placeholder="team.atlassian.net" autoCapitalize="none" autoCorrect="off"
            spellCheck={false} onChange={(event) => edit("site", event.target.value)} />
        </label>
        <label>
          <span className="field-label">{t("settings.jiraProject")}</span>
          <input id="jira-project" className="text-input mono" value={draft.project} required
            maxLength={64} placeholder="TEAM" autoCapitalize="characters" spellCheck={false}
            onChange={(event) => edit("project", event.target.value)} />
        </label>
      </fieldset>
      <div className="settings-actions">
        {result && <p className={`jira-test-result ${result.success ? "" : "is-error"}`}
          role={result.success ? "status" : "alert"}>{result.detail}</p>}
        <button type="button" className="button button-secondary" disabled={!ready || pending !== undefined}
          onClick={() => void test()}>
          {pending === "test" ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Plug size={16} aria-hidden="true" />}
          {t("settings.jiraTestConnection")}
        </button>
        <button type="submit" className="button button-primary"
          disabled={!ready || pending !== undefined || (configured && !dirty)}
          aria-label={`${t("settings.webhookSave")} Jira`}>
          {pending === "save" ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Save size={16} aria-hidden="true" />}
          {t("settings.webhookSave")}
        </button>
      </div>
    </form>
  </section>;
}
