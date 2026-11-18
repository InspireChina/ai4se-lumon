import {
  AlertTriangle,
  FilePlus2,
  LoaderCircle,
  Power,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { MarkdownPreview } from "../flows/MarkdownPreview";

export interface MarkdownDocumentSummary {
  name: string;
  enabled: boolean;
  brief: string;
  path: string;
  valid: boolean;
  error: string | null;
}

export interface MarkdownDocumentContent {
  content: string;
}

export interface MarkdownDocumentApi<
  TSummary extends MarkdownDocumentSummary,
  TDocument extends TSummary & MarkdownDocumentContent,
> {
  list: (workspaceId: string) => Promise<TSummary[]>;
  get: (workspaceId: string, id: string) => Promise<TDocument>;
  create: (workspaceId: string, content: string) => Promise<TDocument>;
  update: (workspaceId: string, id: string, content: string) => Promise<TDocument>;
  delete: (workspaceId: string, id: string) => Promise<void>;
}

export interface MarkdownDocumentLabels {
  title: string;
  subtitle: string;
  unsaved: string;
  select: string;
  refresh: string;
  new: string;
  empty: string;
  emptyHelp: string;
  search: string;
  status: string;
  all: string;
  noMatches: string;
  enabled: string;
  disabled: string;
  invalid: string;
  content: string;
  viewMode: string;
  preview: string;
  edit: string;
  metadata: string;
  idHelp: string;
  enable: string;
  disable: string;
  save: string;
  delete: string;
  saved: string;
  created: string;
  deleted: string;
  confirmDelete: string;
  loadFailed: string;
  unsavedConfirm: string;
}

interface MarkdownDocumentsPageProps<
  TSummary extends MarkdownDocumentSummary,
  TDocument extends TSummary & MarkdownDocumentContent,
> {
  workspaceId: string;
  initialDocumentId?: string;
  onDirtyChange: (dirty: boolean) => void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
  labels: MarkdownDocumentLabels;
  starterContent: string;
  api: MarkdownDocumentApi<TSummary, TDocument>;
  getId: (document: TSummary) => string;
  details?: {
    label: string;
    render: (
      document: TDocument,
      onDirtyChange: (dirty: boolean) => void,
      executionBlocked: boolean,
      headerActions: HTMLElement | null,
    ) => ReactNode;
  };
}

type DocumentViewMode = "preview" | "edit" | "details";

export function MarkdownDocumentsPage<
  TSummary extends MarkdownDocumentSummary,
  TDocument extends TSummary & MarkdownDocumentContent,
>({
  workspaceId,
  initialDocumentId,
  onDirtyChange,
  onNotice,
  onError,
  labels,
  starterContent,
  api,
  getId,
  details,
}: MarkdownDocumentsPageProps<TSummary, TDocument>): React.JSX.Element {
  const [documents, setDocuments] = useState<TSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [document, setDocument] = useState<TDocument | null>(null);
  const [draft, setDraft] = useState(false);
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingDocument, setLoadingDocument] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [viewMode, setViewMode] = useState<DocumentViewMode>("preview");
  const [detailsDirty, setDetailsDirty] = useState(false);
  const [detailsRevision, setDetailsRevision] = useState(0);
  const [headerActions, setHeaderActions] = useState<HTMLDivElement | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const selection = useRef(0);
  const openDetails = Boolean(initialDocumentId && details);

  useEffect(() => {
    let cancelled = false;
    const request = ++selection.current;
    setLoading(true);
    setDocuments([]);
    setLoadingDocument(false);
    setDocument(null);
    setDraft(false);
    setContent("");
    setSelectedId(null);
    setViewMode("preview");
    setDetailsDirty(false);
    setDetailsRevision((revision) => revision + 1);
    setSearch("");
    setFilter("all");
    onDirtyChange(false);
    void api.list(workspaceId)
      .then(async (nextDocuments) => {
        if (cancelled) return;
        setDocuments(nextDocuments);
        const first = nextDocuments[0];
        const nextId = initialDocumentId ?? (first ? getId(first) : null);
        if (!nextId || request !== selection.current) return;
        setSelectedId(nextId);
        setLoadingDocument(true);
        setLoading(false);
        const selectedDocument = await api.get(workspaceId, nextId);
        if (cancelled || request !== selection.current) return;
        setDocument(selectedDocument);
        setContent(selectedDocument.content);
        if (openDetails && selectedDocument.valid) setViewMode("details");
      })
      .catch((reason: unknown) => {
        if (!cancelled && request === selection.current) {
          setSelectedId(null);
          onError(messageFor(reason, labels.loadFailed));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          if (request === selection.current) setLoadingDocument(false);
        }
      });
    return () => {
      cancelled = true;
      selection.current++;
    };
  }, [api, getId, initialDocumentId, labels.loadFailed, onDirtyChange, onError, openDetails, workspaceId]);

  async function selectDocument(summary: TSummary): Promise<void> {
    const nextId = getId(summary);
    if (nextId === selectedId && (document || loadingDocument)) return;
    if (!confirmDiscard()) return;
    const request = ++selection.current;
    setSelectedId(nextId);
    setDocument(null);
    setDraft(false);
    setContent("");
    setViewMode("preview");
    setDetailsDirty(false);
    setDetailsRevision((revision) => revision + 1);
    onDirtyChange(false);
    setLoadingDocument(true);
    try {
      const nextDocument = await api.get(workspaceId, nextId);
      if (request !== selection.current) return;
      setDocument(nextDocument);
      setContent(nextDocument.content);
    } catch (reason) {
      if (request === selection.current) {
        setSelectedId(null);
        onError(messageFor(reason, labels.loadFailed));
      }
    } finally {
      if (request === selection.current) setLoadingDocument(false);
    }
  }

  async function createDocument(): Promise<void> {
    if (!confirmDiscard()) return;
    selection.current++;
    setLoadingDocument(false);
    setSelectedId(null);
    setDocument(null);
    setDraft(true);
    setContent(starterContent);
    setViewMode("edit");
    setDetailsDirty(false);
    setDetailsRevision((revision) => revision + 1);
    onDirtyChange(true);
  }

  async function saveDocument(): Promise<void> {
    if (!draft && (!document || !selectedId)) return;
    setSaving(true);
    try {
      let saved: TDocument;
      if (draft) {
        saved = await api.create(workspaceId, content);
      } else {
        if (!document || !selectedId) return;
        saved = await api.update(workspaceId, selectedId, content);
      }
      setDocument(saved);
      setDraft(false);
      setContent(saved.content);
      if (!saved.valid && viewMode === "details") setViewMode("edit");
      setSelectedId(getId(saved));
      onDirtyChange(detailsDirty);
      await reloadDocuments(getId(saved));
      onNotice(draft ? labels.created : labels.saved);
    } catch (reason) {
      onError(messageFor(reason, labels.loadFailed));
    } finally {
      setSaving(false);
    }
  }

  async function toggleDocument(): Promise<void> {
    if (!document || !selectedId || !document.valid) return;
    if (!confirmDiscard()) return;
    discardDetailsChanges();
    const nextContent = replaceEnabledFlag(content, !document.enabled);
    if (!nextContent) {
      onError(labels.loadFailed);
      return;
    }
    setSaving(true);
    try {
      const saved = await api.update(workspaceId, selectedId, nextContent);
      setDocument(saved);
      setContent(saved.content);
      setDetailsRevision((revision) => revision + 1);
      onDirtyChange(false);
      await reloadDocuments(getId(saved));
      onNotice(labels.saved);
    } catch (reason) {
      onError(messageFor(reason, labels.loadFailed));
    } finally {
      setSaving(false);
    }
  }

  async function deleteDocument(): Promise<void> {
    if (!selectedId || !document || !window.confirm(labels.confirmDelete)) return;
    discardDetailsChanges();
    setDeleting(true);
    try {
      await api.delete(workspaceId, selectedId);
      const deletedId = selectedId;
      setSelectedId(null);
      setDocument(null);
      setDraft(false);
      setContent("");
      setViewMode("preview");
      setDetailsRevision((revision) => revision + 1);
      onDirtyChange(false);
      await reloadDocuments(null, deletedId);
      onNotice(labels.deleted);
    } catch (reason) {
      onError(messageFor(reason, labels.loadFailed));
    } finally {
      setDeleting(false);
    }
  }

  async function reloadDocuments(preferredId: string | null, removedId?: string): Promise<void> {
    const nextDocuments = await api.list(workspaceId);
    setDocuments(nextDocuments);
    if (removedId && selectedId === removedId) setSelectedId(null);
    if (preferredId && !nextDocuments.some((item) => getId(item) === preferredId)) {
      setSelectedId(null);
    }
  }

  async function refresh(): Promise<void> {
    setLoading(true);
    try {
      setDocuments(await api.list(workspaceId));
    } catch (reason) {
      onError(messageFor(reason, labels.loadFailed));
    } finally {
      setLoading(false);
    }
  }

  function confirmDiscard(): boolean {
    if (draft || detailsDirty || (document && content !== document.content)) {
      return window.confirm(labels.unsavedConfirm);
    }
    return true;
  }

  function discardDetailsChanges(): void {
    setDetailsDirty(false);
    setDetailsRevision((revision) => revision + 1);
  }

  const contentDirty = draft || (document !== null && content !== document.content);
  const dirty = contentDirty || detailsDirty;
  const query = search.trim().toLowerCase();
  const visibleDocuments = documents.filter((summary) => (
    (filter === "all" || (summary.valid && summary.enabled === (filter === "enabled")))
    && [summary.name, summary.brief, summary.path].some((entry) => entry.toLowerCase().includes(query))
  ));
  const documentDetails = document?.valid ? details : undefined;

  return (
    <div className="page-stack">
      <div className="page-heading resource-page-heading">
        <div>
          <h1>{labels.title}</h1>
          <p className="muted">{labels.subtitle}</p>
        </div>
        <div className="resource-heading-actions">
          {dirty && <span className="unsaved-label">{labels.unsaved}</span>}
          <button className="button button-primary" type="button" onClick={() => void createDocument()} disabled={saving || deleting}>
            <FilePlus2 size={15} />{labels.new}
          </button>
        </div>
      </div>

      <div className="panel flows-layout">
        <section className="flows-list-panel" aria-label={labels.title}>
          <div className="flow-catalog-tools">
            <div className="flow-search-row">
              <input type="search" className="text-input" value={search} placeholder={labels.search} aria-label={labels.search}
                onChange={(event) => setSearch(event.target.value)} />
              <button className="icon-button" type="button" onClick={() => void refresh()} disabled={loading} aria-label={labels.refresh} title={labels.refresh}>
                <RefreshCw size={15} className={loading ? "spin" : ""} />
              </button>
            </div>
            <select className="flow-status-filter" aria-label={labels.status} value={filter} onChange={(event) => setFilter(event.target.value)}>
              <option value="all">{labels.all}</option>
              <option value="enabled">{labels.enabled}</option>
              <option value="disabled">{labels.disabled}</option>
            </select>
          </div>
          {loading ? (
            <div className="loading-inline flows-loading"><LoaderCircle size={20} className="spin" />Loading…</div>
          ) : documents.length === 0 ? (
            <div className="flow-catalog-empty"><strong>{labels.empty}</strong><p>{labels.emptyHelp}</p></div>
          ) : visibleDocuments.length === 0 ? (
            <p className="flow-catalog-empty">{labels.noMatches}</p>
          ) : (
            <div className="flows-list">
              {visibleDocuments.map((summary) => {
                const id = getId(summary);
                const statusLabel = !summary.valid
                  ? labels.invalid
                  : summary.enabled
                    ? labels.enabled
                    : labels.disabled;
                return (
                  <button
                    className={"flow-row" + (id === selectedId ? " active" : "")}
                    type="button"
                    key={summary.path + ":" + id}
                    aria-current={id === selectedId ? "true" : undefined}
                    disabled={saving || deleting}
                    onClick={() => void selectDocument(summary)}
                  >
                    <span className="flow-row-copy"><strong>{summary.name || id}</strong><small>{summary.brief || summary.path}</small>{!summary.valid && <em>{summary.error ?? labels.invalid}</em>}</span>
                    <span className="flow-status-dot" data-status={!summary.valid ? "invalid" : summary.enabled ? "enabled" : "disabled"} title={statusLabel}>
                      <span className="sr-only">{statusLabel}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <section className="flow-editor-panel">
          <div className="flow-editor-heading">
            <div>
              <h2>{document?.name ?? (draft ? labels.new : labels.select)}</h2>
              {document && <div className="mono flow-editor-path">{document.path}</div>}
            </div>
            {document && (
              <div className="resource-heading-actions">
                <span className={document.valid && document.enabled ? "status-pill status-ready" : "status-pill status-neutral"}>
                  {!document.valid ? labels.invalid : document.enabled ? labels.enabled : labels.disabled}
                </span>
                {documentDetails && <div ref={setHeaderActions} />}
              </div>
            )}
          </div>
          {document && (
            <div className="flow-view-toggle" role="group" aria-label={labels.viewMode}>
              <button type="button" aria-pressed={viewMode === "preview"} onClick={() => setViewMode("preview")}>{labels.preview}</button>
              <button type="button" aria-pressed={viewMode === "edit"} onClick={() => setViewMode("edit")}>{labels.edit}</button>
              {documentDetails && <button type="button" aria-pressed={viewMode === "details"} onClick={() => setViewMode("details")}>{documentDetails.label}</button>}
            </div>
          )}
          {document && documentDetails && (
            <div key={detailsRevision} hidden={viewMode !== "details"}>
              {documentDetails.render(document, (value) => {
                setDetailsDirty(value);
                onDirtyChange(value || contentDirty);
              }, contentDirty || saving || deleting, headerActions)}
            </div>
          )}
          {loadingDocument ? (
            <div className="loading-inline"><LoaderCircle size={20} className="spin" />Loading…</div>
          ) : document || draft ? (
            <>
              {viewMode === "preview" ? (
                <MarkdownPreview content={content} metadataLabel={labels.metadata} />
              ) : viewMode === "edit" && (
                <textarea
                  className="flow-editor"
                  value={content}
                  aria-label={labels.content}
                  onChange={(event) => {
                    setContent(event.target.value);
                    onDirtyChange(
                      detailsDirty || draft || (document !== null && event.target.value !== document.content),
                    );
                  }}
                  spellCheck={false}
                />
              )}
              {document?.error && <p className="flow-editor-error"><AlertTriangle size={14} />{document.error}</p>}
              <div className="flow-editor-actions">
                {document?.valid && (
                  <button className="button button-secondary" type="button" onClick={() => void toggleDocument()} disabled={deleting || saving}>
                    {saving ? <LoaderCircle size={15} className="spin" /> : <Power size={15} />}
                    {document.enabled ? labels.disable : labels.enable}
                  </button>
                )}
                {document && (
                  <button className="button button-secondary" type="button" onClick={() => void deleteDocument()} disabled={deleting || saving}>
                    {deleting ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}{labels.delete}
                  </button>
                )}
                <button className="button button-primary" type="button" onClick={() => void saveDocument()} disabled={saving || deleting || !contentDirty}>
                  {saving ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}{labels.save}
                </button>
              </div>
            </>
          ) : (
            <div className="flow-editor-empty muted">{labels.idHelp}</div>
          )}
        </section>
      </div>
    </div>
  );
}

function replaceEnabledFlag(content: string, enabled: boolean): string | null {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(content);
  if (!frontmatter) return null;
  const enabledLine = /^(\s*enabled\s*=\s*)(true|false)(\s*)$/m;
  if (!enabledLine.test(frontmatter[1])) return null;
  const nextFrontmatter = frontmatter[1].replace(
    enabledLine,
    (_match, prefix: string, _current: string, suffix: string) =>
      prefix + String(enabled) + suffix,
  );
  return content.replace(frontmatter[1], nextFrontmatter);
}

function messageFor(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}
