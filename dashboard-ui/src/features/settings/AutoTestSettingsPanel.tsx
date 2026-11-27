import { FlaskConical, LoaderCircle, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useI18n } from "../../shared/i18n";
import type { AutoTestSettings } from "../../shared/types";

const scheduleLabels = {
  en: "Schedule (cron)",
  "zh-TW": "排程（cron）",
  "zh-CN": "计划（cron）",
};

type TestControls = Pick<AutoTestSettings, "enabled" | "schedule_expression" | "trigger_jql">;

export function AutoTestSettingsPanel({ settings, onDirtyChange, onSave }: {
  settings?: AutoTestSettings;
  onDirtyChange: (dirty: boolean) => void;
  onSave: (settings: TestControls) => Promise<boolean>;
}): React.JSX.Element {
  const { locale, t } = useI18n();
  const { enabled = false, schedule_expression = "", trigger_jql = "" } = settings ?? {};
  const [draft, setDraft] = useState<TestControls>({ enabled, schedule_expression, trigger_jql });
  const [saving, setSaving] = useState(false);
  const dirty = draft.enabled !== enabled || draft.schedule_expression !== schedule_expression
    || draft.trigger_jql !== trigger_jql;

  useEffect(() => {
    setDraft({ enabled, schedule_expression, trigger_jql });
  }, [enabled, schedule_expression, trigger_jql]);

  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  function edit<Key extends keyof TestControls>(key: Key, value: TestControls[Key]): void {
    setDraft((previous) => ({ ...previous, [key]: value }));
  }

  async function save(): Promise<void> {
    if (!settings || saving || !dirty) return;
    setSaving(true);
    try {
      await onSave(draft);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section id="auto-test-settings" className="panel settings-panel auto-test-settings-panel"
      aria-labelledby="auto-test-settings-title" tabIndex={-1}>
      <div className="panel-heading">
        <div className="settings-title">
          <span className="workspace-glyph glyph-violet"><FlaskConical size={18} aria-hidden="true" /></span>
          <h2 id="auto-test-settings-title">Auto Test</h2>
        </div>
        {settings && (
          <div className="settings-heading-actions">
            {dirty && <span className="unsaved-label">{t("settings.unsaved")}</span>}
            <label className={`settings-toggle ${draft.enabled ? "is-enabled" : ""}`}>
              <span>{draft.enabled ? t("settings.enabled") : t("settings.disabled")}</span>
              <input type="checkbox" role="switch" checked={draft.enabled} disabled={saving}
                onChange={(event) => edit("enabled", event.target.checked)} />
              <span className="settings-switch" aria-hidden="true"><span className="settings-switch-thumb" /></span>
            </label>
          </div>
        )}
      </div>
      {settings ? (
        <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <fieldset className="auto-test-settings-fields form-grid" disabled={saving}>
            <label>
              <span className="field-label">{scheduleLabels[locale]}</span>
              <input name="schedule_expression" className="text-input mono" value={draft.schedule_expression}
                onChange={(event) => edit("schedule_expression", event.target.value)} />
            </label>
            <label className="field-full">
              <span className="field-label">{t("settings.jiraQuery")}</span>
              <textarea id="auto-test-jql" className="text-input text-area mono" rows={5} maxLength={8000}
                spellCheck={false} value={draft.trigger_jql} onChange={(event) => edit("trigger_jql", event.target.value)} />
            </label>
          </fieldset>
          <div className="settings-actions">
            <button type="submit" className="button button-primary" disabled={saving || !dirty}
              aria-label={`${t("settings.webhookSave")} Auto Test`}>
              {saving ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Save size={16} aria-hidden="true" />}
              {t("settings.webhookSave")}
            </button>
          </div>
        </form>
      ) : (
        <div className="loading-inline"><LoaderCircle className="spin" size={17} aria-hidden="true" />
          {t("app.readingWorkspace")}
        </div>
      )}
    </section>
  );
}
