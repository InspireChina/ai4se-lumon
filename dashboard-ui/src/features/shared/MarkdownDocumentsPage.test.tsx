import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import {
  MarkdownDocumentsPage,
  type MarkdownDocumentApi,
  type MarkdownDocumentLabels,
  type MarkdownDocumentSummary,
} from "./MarkdownDocumentsPage";

interface TestSummary extends MarkdownDocumentSummary {
  id: string;
}

interface TestDocument extends TestSummary {
  content: string;
}

const labels: MarkdownDocumentLabels = {
  title: "Documents",
  subtitle: "Manage documents.",
  unsaved: "Unsaved",
  select: "Select a document",
  loading: "Loading resource…",
  refresh: "Refresh",
  new: "New document",
  empty: "No documents",
  emptyHelp: "Create one.",
  search: "Search documents",
  status: "Status",
  all: "All statuses",
  noMatches: "No matching documents",
  enabled: "Enabled",
  disabled: "Disabled",
  invalid: "Invalid",
  content: "Markdown",
  viewMode: "View mode",
  preview: "Preview",
  edit: "Edit",
  metadata: "Metadata",
  idHelp: "Create a draft.",
  enable: "Enable",
  disable: "Disable",
  save: "Save",
  delete: "Delete",
  saved: "Saved",
  created: "Created",
  deleted: "Deleted",
  confirmDelete: "Delete this document?",
  loadFailed: "Operation failed.",
  unsavedConfirm: "Discard changes?",
};

const starterContent = '---\nid = "my-capability"\n---\n\n# Draft';
const getId = (item: TestSummary) => item.id;

function createDocumentApi(): MarkdownDocumentApi<TestSummary, TestDocument> {
  const document: TestDocument = {
    id: "my-capability",
    name: "My capability",
    enabled: true,
    brief: "Test capability.",
    path: "lumon/capabilities/my-capability.md",
    valid: true,
    error: null,
    content: starterContent,
  };
  return {
    list: vi.fn().mockResolvedValue([]),
    get: vi.fn().mockResolvedValue(document),
    create: vi.fn().mockResolvedValue(document),
    update: vi.fn().mockResolvedValue(document),
    delete: vi.fn().mockResolvedValue(undefined),
  };
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll("button")).find((item) =>
    item.textContent?.includes(text),
  );
  if (!(button instanceof HTMLButtonElement)) throw new Error(`Missing button: ${text}`);
  return button;
}

describe("MarkdownDocumentsPage", () => {
  it("keeps the document list usable if the initial workflow no longer exists", async () => {
    const api = createDocumentApi();
    const savedDocument = await api.get("workspace-1", "my-capability");
    vi.mocked(api.list).mockResolvedValue([savedDocument]);
    vi.mocked(api.get).mockRejectedValueOnce(new Error("Workflow no longer exists"));
    const onError = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<MarkdownDocumentsPage
        workspaceId="workspace-1" initialDocumentId="my-capability" onDirtyChange={vi.fn()} onNotice={vi.fn()} onError={onError}
        labels={labels} starterContent={starterContent} api={api} getId={getId}
      />));
      expect(onError).toHaveBeenCalledWith("Workflow no longer exists");
      expect(container.querySelector(".flow-row.active")).toBeNull();
      await act(async () => container.querySelector<HTMLButtonElement>(".flow-row")!.click());
      expect(container.querySelector(".flow-editor-heading h2")?.textContent).toBe("My capability");
      expect(api.update).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("ignores an initial workflow load after switching to another workspace", async () => {
    const api = createDocumentApi();
    const savedDocument = await api.get("workspace-1", "my-capability");
    const otherDocument = { ...savedDocument, id: "other-capability", name: "Other capability" };
    vi.mocked(api.list).mockResolvedValueOnce([savedDocument]).mockResolvedValueOnce([otherDocument]);
    let finishLoad!: (document: TestDocument) => void;
    vi.mocked(api.get)
      .mockImplementationOnce(() => new Promise((resolve) => { finishLoad = resolve; }))
      .mockResolvedValueOnce(otherDocument);
    const container = document.createElement("div");
    const root = createRoot(container);
    const props = { onDirtyChange: vi.fn(), onNotice: vi.fn(), onError: vi.fn(), labels, starterContent, api, getId };
    try {
      await act(async () => root.render(<MarkdownDocumentsPage {...props} workspaceId="workspace-1" initialDocumentId="my-capability" />));
      expect(api.get).toHaveBeenLastCalledWith("workspace-1", "my-capability");
      await act(async () => root.render(<MarkdownDocumentsPage {...props} workspaceId="workspace-2" />));
      await act(async () => finishLoad(savedDocument));
      expect(api.get).toHaveBeenLastCalledWith("workspace-2", "other-capability");
      expect(container.querySelector(".flow-editor-heading h2")?.textContent).toBe("Other capability");
      expect(container.querySelector(".flow-row[aria-current='true'] svg")).toBeNull();
      expect(props.onError).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("keeps the current document in place until the next document loads", async () => {
    const api = createDocumentApi();
    const first = await api.get("workspace-1", "my-capability");
    const second = { ...first, id: "second", name: "Second", content: "# Second" };
    const missing = { ...first, id: "missing", name: "Missing" };
    vi.mocked(api.list).mockResolvedValue([first, second, missing]);
    let finishLoad!: (document: TestDocument) => void;
    vi.mocked(api.get).mockImplementation(async (_workspaceId, id) => {
      if (id === "missing") throw new Error("Document unavailable");
      if (id === second.id) return new Promise((resolve) => { finishLoad = resolve; });
      return first;
    });
    const onError = vi.fn();
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<MarkdownDocumentsPage
        workspaceId="workspace-1" onDirtyChange={vi.fn()} onNotice={vi.fn()} onError={onError}
        labels={labels} starterContent={starterContent} api={api} getId={getId}
      />));
      const reader = container.querySelector(".flow-editor-panel")!;
      const preview = reader.querySelector(".flow-markdown-preview");
      await act(async () => buttonWithText(container, second.name).click());
      expect(reader.getAttribute("aria-busy")).toBe("true");
      expect(reader.hasAttribute("inert")).toBe(true);
      expect(reader.querySelector(".flow-markdown-preview")).toBe(preview);
      expect(container.querySelector(".flow-row.active strong")?.textContent).toBe(first.name);

      await act(async () => buttonWithText(container, first.name).click());
      await act(async () => finishLoad(second));
      expect(reader.querySelector(".flow-markdown-preview")).toBe(preview);
      expect(reader.hasAttribute("inert")).toBe(false);

      await act(async () => buttonWithText(container, second.name).click());
      await act(async () => finishLoad(second));
      expect(reader.hasAttribute("inert")).toBe(false);
      expect(reader.querySelector(".flow-markdown-preview")?.textContent).toBe(second.name);
      expect(container.querySelector(".flow-row.active strong")?.textContent).toBe(second.name);

      await act(async () => buttonWithText(container, missing.name).click());
      expect(onError).toHaveBeenCalledWith("Document unavailable");
      expect(reader.hasAttribute("inert")).toBe(false);
      expect(reader.querySelector(".flow-markdown-preview")?.textContent).toBe(second.name);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("filters the catalog without replacing the open document", async () => {
    const api = createDocumentApi();
    const first = await api.get("workspace-1", "my-capability");
    const second = { ...first, id: "another", name: "Another", brief: "Build support", enabled: false };
    vi.mocked(api.list).mockResolvedValue([first, second]);
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<MarkdownDocumentsPage
        workspaceId="workspace-1" onDirtyChange={vi.fn()} onNotice={vi.fn()} onError={vi.fn()}
        labels={labels} starterContent={starterContent} api={api} getId={getId}
      />));
      expect(container.querySelectorAll(".flow-row")).toHaveLength(2);
      const select = container.querySelector("select")!;
      await act(async () => {
        select.value = "disabled";
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      expect(container.querySelector(".flow-row strong")?.textContent).toBe("Another");
      const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "unknown");
        search.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(container.querySelector(".flow-catalog-empty")?.textContent).toBe(labels.noMatches);
      expect(container.querySelector(".flow-editor-heading h2")?.textContent).toBe(first.name);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("keeps a new document local until the first save", async () => {
    const api = createDocumentApi();
    const container = document.createElement("div");
    const root = createRoot(container);

    try {
      await act(async () => {
        root.render(
          <MarkdownDocumentsPage
            workspaceId="workspace-1"
            onDirtyChange={vi.fn()}
            onNotice={vi.fn()}
            onError={vi.fn()}
            labels={labels}
            starterContent={starterContent}
            api={api}
            getId={getId}
          />,
        );
      });

      await act(async () => {
        buttonWithText(container, labels.new).click();
      });
      expect(api.create).not.toHaveBeenCalled();
      expect(container.querySelector("textarea")?.value).toBe(starterContent);

      await act(async () => {
        buttonWithText(container, labels.save).click();
      });
      expect(api.create).toHaveBeenCalledWith("workspace-1", starterContent);
    } finally {
      root.unmount();
    }
  });
});
