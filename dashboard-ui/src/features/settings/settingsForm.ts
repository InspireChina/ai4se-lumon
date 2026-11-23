import type { SettingsUpdate } from "../../shared/types";

export interface WebhookDraft {
  enabled: boolean;
  url: string;
}

export function buildSettingsUpdate(draft: WebhookDraft): SettingsUpdate {
  const url = draft.url.trim();
  return { feishu_webhook: { enabled: draft.enabled, ...(url ? { url } : {}) } };
}
