import { RefreshCw, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { SkillInventory } from "../../shared/types";
import { HelpTooltip } from "../shared/HelpTooltip";
import "./SkillsPage.css";

export function SkillsPage({ workspaceId }: { workspaceId: string }): React.JSX.Element {
  const { t } = useI18n();
  const [inventory, setInventory] = useState<SkillInventory | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestNumber = useRef(0);
  const mutating = useRef(false);

  async function refresh(): Promise<void> {
    const current = ++requestNumber.current;
    setLoading(true);
    try {
      const result = await dashboardApi.listSkills(workspaceId);
      if (current === requestNumber.current) { setInventory(result); setError(""); }
    } catch {
      if (current === requestNumber.current) setError(t("skills.failed"));
    } finally {
      if (current === requestNumber.current) setLoading(false);
    }
  }

  useEffect(() => {
    setInventory(null);
    void refresh();
    return () => { requestNumber.current += 1; };
  }, [workspaceId, t]);

  async function setEnabled(id: string, enabled: boolean): Promise<void> {
    if (!inventory || mutating.current) return;
    const previous = inventory;
    mutating.current = true;
    setBusy(true);
    setError("");
    setInventory({ ...inventory, skills: inventory.skills.map((skill) => skill.id === id ? { ...skill, enabled } : skill) });
    try {
      await dashboardApi.setSkillEnabled(workspaceId, id, enabled);
      await refresh();
    } catch {
      setInventory(previous);
      setError(t("skills.saveFailed"));
    } finally {
      mutating.current = false;
      setBusy(false);
    }
  }

  const term = query.trim().toLocaleLowerCase();
  const skills = inventory?.skills.filter((skill) =>
    `${skill.name} ${skill.description} ${skill.path}`.toLocaleLowerCase().includes(term)
  ) ?? [];

  return <div className="page-stack skills-page">
    <div className="page-heading"><h1>{t("app.skills")}</h1></div>
    <section className="panel skills-panel" aria-busy={loading || busy}>
      <div className="panel-heading skills-heading">
        <div className="field-label-heading"><h2>{t("app.skills")}</h2>
          <HelpTooltip helpId="skills-help" label={t("app.skills")} help={t("skills.help")} />
        </div>
        <button type="button" className="button button-secondary" disabled={loading || busy} onClick={() => void refresh()}><RefreshCw size={15} />{t("skills.refresh")}</button>
      </div>
      <div className="skills-search"><Search size={17} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("skills.search")} aria-label={t("skills.search")} /></div>
      {error && <p className="skills-message skills-error" role="alert">{error}</p>}
      {inventory && inventory.error_count > 0 && <p className="skills-message skills-error" role="status">{t("skills.partial", { count: inventory.error_count })}</p>}
      {loading && !inventory && <p className="skills-message" role="status">{t("skills.loading")}</p>}
      {inventory && !skills.length && <p className="skills-message">{t("skills.empty")}</p>}
      {skills.length > 0 && <ul className="skills-list">{skills.map((skill) => <li className="skills-row" key={skill.id}>
        <div className="skills-main"><div className="skills-title"><strong>{skill.name}</strong><span className="skills-source">{skill.source}</span></div>
          <p title={skill.description}>{skill.description}</p><small title={skill.path}>{skill.path}</small>
          {!skill.codex_enabled && <small>{t("skills.codexDisabled")}</small>}
        </div>
        <label className={`settings-toggle ${skill.enabled ? "is-enabled" : ""}`} title={!skill.codex_enabled ? t("skills.codexDisabled") : undefined}>
          <input type="checkbox" role="switch" aria-label={`${t("skills.toggle", { name: skill.name })} — ${skill.path}`} checked={skill.enabled} disabled={!skill.codex_enabled || loading || busy} onChange={(event) => void setEnabled(skill.id, event.target.checked)} />
          <span className="settings-switch" aria-hidden="true"><span className="settings-switch-thumb" /></span>
        </label>
      </li>)}</ul>}
    </section>
  </div>;
}
