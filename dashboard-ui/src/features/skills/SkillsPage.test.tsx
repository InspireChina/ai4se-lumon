import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { dashboardApi } from "../../app/api";
import { I18nProvider } from "../../shared/i18n";
import type { SkillInventory } from "../../shared/types";
import { SkillsPage } from "./SkillsPage";

const inventory: SkillInventory = { error_count: 0, skills: [
  { id: "one", name: "jira", description: "Work with Jira", path: "/user/jira/SKILL.md", source: "user", enabled: true, codex_enabled: true },
  { id: "two", name: "jira", description: "Project guidance", path: "/project/jira/SKILL.md", source: "repo", enabled: false, codex_enabled: false },
] };

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.setItem("lumon.locale", "en");
  vi.spyOn(dashboardApi, "listSkills").mockResolvedValue(inventory);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

it("shows discovered skills and saves an individual switch immediately", async () => {
  const save = vi.spyOn(dashboardApi, "setSkillEnabled").mockResolvedValue();
  vi.mocked(dashboardApi.listSkills).mockResolvedValueOnce(inventory).mockResolvedValue({
    ...inventory, skills: [{ ...inventory.skills[0], enabled: false }, inventory.skills[1]],
  });
  const element = document.createElement("div");
  const root = createRoot(element);
  try {
    await act(async () => root.render(<I18nProvider><SkillsPage workspaceId="one" /></I18nProvider>));
    expect(element.querySelectorAll(".skills-row")).toHaveLength(2);
    expect(element.textContent).toContain("Work with Jira");
    const switches = element.querySelectorAll<HTMLInputElement>('input[role="switch"]');
    expect(switches[1].disabled).toBe(true);
    await act(async () => switches[0].click());
    expect(save).toHaveBeenCalledWith("one", "one", false);
    expect(element.querySelector<HTMLInputElement>('input[role="switch"]')?.checked).toBe(false);
  } finally { await act(async () => root.unmount()); }
});

it("filters by description and shows partial discovery errors", async () => {
  vi.mocked(dashboardApi.listSkills).mockResolvedValue({ ...inventory, error_count: 1 });
  const element = document.createElement("div");
  const root = createRoot(element);
  try {
    await act(async () => root.render(<I18nProvider><SkillsPage workspaceId="one" /></I18nProvider>));
    expect(element.textContent).toContain("Some Skills could not be loaded (1)");
    const input = element.querySelector<HTMLInputElement>('input[aria-label="Search Skills"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "project guidance");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(element.querySelectorAll(".skills-row")).toHaveLength(1);
    expect(element.textContent).toContain("Project guidance");
  } finally { await act(async () => root.unmount()); }
});

it("shows discovery and save failures without reporting success", async () => {
  vi.mocked(dashboardApi.listSkills).mockRejectedValueOnce(new Error("unavailable"));
  const save = vi.spyOn(dashboardApi, "setSkillEnabled").mockRejectedValue(new Error("failed"));
  const element = document.createElement("div");
  const root = createRoot(element);
  try {
    await act(async () => root.render(<I18nProvider><SkillsPage workspaceId="one" /></I18nProvider>));
    expect(element.querySelector('[role="alert"]')?.textContent).toContain("Could not load Skills");
    expect(element.textContent).not.toContain("No Skills found");
    await act(async () => element.querySelector<HTMLButtonElement>(".skills-heading > button")!.click());
    await act(async () => element.querySelector<HTMLInputElement>('input[role="switch"]')!.click());
    expect(save).toHaveBeenCalled();
    expect(element.querySelector('[role="alert"]')?.textContent).toContain("Could not save");
    expect(element.querySelector<HTMLInputElement>('input[role="switch"]')?.checked).toBe(true);
  } finally { await act(async () => root.unmount()); }
});
