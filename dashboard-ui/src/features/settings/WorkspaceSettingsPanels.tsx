import { useEffect, useRef, useState } from "react";
import { ApiError, dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { AutoTestSettings, SettingsUpdate, WorkspaceSettings } from "../../shared/types";
import { AutoDeliverySettingsPanel } from "./AutoDeliverySettingsPanel";
import { AutoScanSettingsPanel } from "./AutoScanSettingsPanel";
import { AutoTestSettingsPanel } from "./AutoTestSettingsPanel";
import { JiraSettingsPanel } from "./JiraSettingsPanel";
import { SettingsPage } from "./SettingsPage";

export function WorkspaceSettingsPanels({ settings, onSave, onSaveAutoTest, onTest, onDirtyChange, onError }: {
  settings: WorkspaceSettings;
  onSave: (update: SettingsUpdate) => Promise<boolean>;
  onSaveAutoTest: (update: Partial<AutoTestSettings>) => Promise<AutoTestSettings | undefined>;
  onTest: (url?: string) => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
  onError: (error: string | null) => void;
}): React.JSX.Element {
  const { t } = useI18n();
  const [tests, setTests] = useState<AutoTestSettings>();
  const [webhookDirty, setWebhookDirty] = useState(false);
  const [jiraDirty, setJiraDirty] = useState(false);
  const [deliveryDirty, setDeliveryDirty] = useState(false);
  const [scanDirty, setScanDirty] = useState(false);
  const [testDirty, setTestDirty] = useState(false);
  const pendingSave = useRef(Promise.resolve(true));
  const dirty = webhookDirty || jiraDirty || deliveryDirty || scanDirty || testDirty;

  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    let current = true;
    void dashboardApi.autoTestSettings(settings.workspace_id).then((next) => {
      if (current) setTests(next);
    }).catch((error: unknown) => {
      if (current) onError(error instanceof ApiError ? error.message : t("app.dashboardRequestFailed"));
    });
    return () => { current = false; };
  }, [settings.workspace_id, onError, t]);

  function saveInOrder(write: () => Promise<boolean>): Promise<boolean> {
    // Both settings endpoints write the same Workspace profile.
    const next = pendingSave.current.then(write, write);
    pendingSave.current = next;
    return next;
  }

  function saveSettings(update: SettingsUpdate): Promise<boolean> {
    return saveInOrder(() => onSave(update));
  }

  function saveAutoTest(update: Partial<AutoTestSettings>): Promise<boolean> {
    return saveInOrder(async () => {
      const next = await onSaveAutoTest(update);
      if (!next) return false;
      setTests(next);
      return true;
    });
  }

  return <>
    <SettingsPage settings={settings} onSave={saveSettings} onTest={onTest} onDirtyChange={setWebhookDirty} />
    <JiraSettingsPanel workspaceId={settings.workspace_id} settings={settings.jira} onSave={saveSettings}
      onDirtyChange={setJiraDirty} />
    <AutoDeliverySettingsPanel settings={settings} onSave={saveSettings} onDirtyChange={setDeliveryDirty} />
    <AutoScanSettingsPanel settings={settings} onSave={saveSettings} onDirtyChange={setScanDirty} />
    <AutoTestSettingsPanel settings={tests} onSave={saveAutoTest} onDirtyChange={setTestDirty} />
  </>;
}
