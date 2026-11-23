import { FlaskConical, LoaderCircle, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useI18n } from "../../shared/i18n";
import type { AutoTestSettings } from "../../shared/types";

const labels = {
  en: {
    schedule: "Schedule tests", cron: "Schedule (cron)", model: "OpenAI model",
  },
  "zh-TW": {
    schedule: "啟用定時測試", cron: "排程（cron）", model: "OpenAI 模型",
  },
  "zh-CN": {
    schedule: "启用定时测试", cron: "计划（cron）", model: "OpenAI 模型",
  },
};

type TestControls = Pick<AutoTestSettings, "enabled" | "schedule_expression" | "model" | "trigger_jql">;

export function AutoTestSettingsPanel({ settings, onDirtyChange, onSave }: {
  settings?: AutoTestSettings;
  onDirtyChange: (dirty: boolean) => void;
  onSave: (settings: TestControls) => Promise<boolean>;
}): React.JSX.Element {
  const { locale, t } = useI18n();
  const copy = labels[locale];
  const { enabled = false, schedule_expression = "", model = "", trigger_jql = "" } = settings ?? {};
  const [draft, setDraft] = useState<TestControls>({ enabled, schedule_expression, model, trigger_jql });
  const [saving, setSaving] = useState(false);
  const dirty = draft.enabled !== enabled || draft.schedule_expression !== schedule_expression
    || draft.model !== model || draft.trigger_jql !== trigger_jql;

  useEffect(() => {
    setDraft({ enabled, schedule_expression, model, trigger_jql });
  }, [enabled, schedule_expression, model, trigger_jql]);

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
              <span>{copy.schedule}</span>
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
            {([["schedule_expression", copy.cron], ["model", copy.model]] as const).map(([key, label]) => (
              <label key={key}>
                <span className="field-label">{label}</span>
                <input name={key} className="text-input" value={draft[key]} onChange={(event) => edit(key, event.target.value)} />
              </label>
            ))}
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
