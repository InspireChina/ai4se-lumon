import { Download, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { dashboardApi } from "../../app/api";
import { useI18n } from "../../shared/i18n";
import type { AutoTestRun, AutoTestWorkbookSheet } from "../../shared/types";

export type EvidenceFile = {
  kind: AutoTestRun["artifacts"][number]["kind"];
  url: string;
  label: string;
};

export function EvidencePreview({ file, copy, onClose }: {
  file: EvidenceFile;
  copy: { download: string; previewLoading: string; previewFailed: string };
  onClose: () => void;
}): React.JSX.Element {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [sheets, setSheets] = useState<AutoTestWorkbookSheet[]>();
  const [sheetIndex, setSheetIndex] = useState(0);
  const [failed, setFailed] = useState(false);
  const sheet = sheets?.[sheetIndex];

  useEffect(() => {
    const dialog = dialogRef.current!;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      if (opener?.isConnected) opener.focus();
    };
  }, []);

  useEffect(() => {
    if (file.kind !== "workbook") return;
    const controller = new AbortController();
    void dashboardApi.autoTestWorkbookPreview(file.url, controller.signal).then((next) => {
      if (!controller.signal.aborted) setSheets(next);
    }).catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    });
    return () => controller.abort();
  }, [file.kind, file.url]);

  const src = file.url + "?inline=true";
  return <dialog ref={dialogRef} className="auto-test-preview" aria-labelledby="auto-test-preview-title"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <header className="auto-test-preview-header">
      <h2 id="auto-test-preview-title">{file.label}</h2>
      <a className="auto-test-file-action" download href={file.url} aria-label={copy.download} title={copy.download}>
        <Download size={17} aria-hidden="true" />
      </a>
      <button className="auto-test-file-action" type="button" aria-label={t("app.close")} title={t("app.close")}
        onClick={onClose} autoFocus><X size={19} aria-hidden="true" /></button>
    </header>
    {failed ? <p className="auto-test-preview-message" role="alert">{copy.previewFailed}</p>
      : file.kind === "screenshot" ? (
        <img className="auto-test-preview-media" src={src} alt={file.label} onError={() => setFailed(true)} />
      ) : file.kind === "video" ? (
        <video className="auto-test-preview-media" src={src} aria-label={file.label} controls playsInline
          preload="metadata" onError={() => setFailed(true)} />
      ) : sheet ? (
        <>
          <div className="auto-test-sheet-switch">
            {sheets?.map(({ name }, index) => <button key={name} type="button" aria-pressed={index === sheetIndex}
              onClick={() => setSheetIndex(index)}>{name}</button>)}
          </div>
          <div key={sheet.name} className="auto-test-sheet-scroll" role="region" aria-label={sheet.name} tabIndex={0}>
            <table aria-label={sheet.name}>
              <thead><tr>{sheet.rows[0]?.map((cell, index) => <th key={index} scope="col">{cell}</th>)}</tr></thead>
              <tbody>{sheet.rows.slice(1).map((row, index) => (
                <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>
              ))}</tbody>
            </table>
          </div>
        </>
      ) : <p className="auto-test-preview-message" role="status">{copy.previewLoading}</p>}
  </dialog>;
}
