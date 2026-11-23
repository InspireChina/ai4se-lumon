# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
"""Export the MBPass case layout with a separate evidence sheet."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from xlsxwriter import Workbook
from xlsxwriter.exceptions import XlsxWriterException

from lumon.auto_test.model import Artifact, Record, TestRun
from lumon.errors import PreflightError
from lumon.tools.safety import sanitize_output

_HEADERS = (
    "卡link",
    "卡號標題",
    "设备/功能点",
    "用例 ID",
    "Path",
    "Test Summary",
    "Pre-condition",
    "Test Data",
    "Action",
    "Expected Result",
    "测试人",
    "測試結果",
    "測試日期",
    "備註",
    "Follow up",
)
_RESULT = {
    "passed": "驗證成功",
    "failed": "驗證失敗",
    "review": "待驗證",
    "blocked": "待驗證",
    "not_run": "待驗證",
}
_SHEETS = (("Test Cases", "ABCDEFGHIJKLMNO"), ("Evidence", "ABCDEFGHIJ"))
_XML_NS = {"": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_PREVIEW_LIMIT = 16 * 1024 * 1024


class WorkbookSheet(Record):
    name: str
    rows: tuple[tuple[str, ...], ...]


def read_workbook_preview(path: Path) -> tuple[WorkbookSheet, ...]:
    try:
        if path.stat().st_size > _PREVIEW_LIMIT:
            raise ValueError("Workbook too large.")
        with ZipFile(path) as book:
            shared = _read_xml(book, "xl/sharedStrings.xml")
            strings = tuple(
                "".join(text.text or "" for text in item.findall("t", _XML_NS)) for item in shared
            )
            return tuple(
                WorkbookSheet(
                    name=name,
                    rows=_preview_rows(
                        _read_xml(book, f"xl/worksheets/sheet{index}.xml"), columns, strings
                    ),
                )
                for index, (name, columns) in enumerate(_SHEETS, 1)
            )
    except (
        BadZipFile,
        ET.ParseError,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        RuntimeError,
    ) as exc:
        raise PreflightError("Unable to preview the Auto Test workbook.") from exc


def _read_xml(book: ZipFile, name: str) -> ET.Element:
    info = book.getinfo(name)
    if info.file_size > _PREVIEW_LIMIT:
        raise ValueError("Worksheet too large.")
    xml = book.read(info).decode("utf-8")
    if "<!DOCTYPE" in xml:
        raise ValueError("Unsupported worksheet XML.")
    return ET.fromstring(xml)


def _preview_rows(
    sheet: ET.Element, columns: str, strings: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    # shortcut: reads Lumon's two generated sheets, use a full reader for other workbook formats.
    rows: list[tuple[str, ...]] = []
    for row in sheet.findall("sheetData/row", _XML_NS):
        values: dict[str, str] = {}
        for cell in row.findall("c", _XML_NS):
            column = cell.attrib["r"].rstrip("0123456789")
            value = cell.findtext("v", "", _XML_NS)
            values[column] = strings[int(value)] if cell.get("t") == "s" else value
        rows.append(tuple(values.get(column, "") for column in columns))
    return tuple(rows)


def export_workbook(run: TestRun, evidence_dir: Path) -> Artifact:
    try:
        return _write_workbook(run, evidence_dir)
    except XlsxWriterException as exc:
        raise PreflightError("Unable to write the Auto Test workbook.") from exc


def _write_workbook(run: TestRun, evidence_dir: Path) -> Artifact:
    if run.plan is None:
        raise PreflightError("Auto Test has no case plan to export.")
    filename = f"{run.issue_key}-{run.run_id}-test-cases.xlsx"
    path = evidence_dir / filename
    with Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False}) as book:
        header = book.add_format({"bold": True, "bg_color": "#F0ECE4", "valign": "top"})
        text = book.add_format({"text_wrap": True, "valign": "top"})
        cases = book.add_worksheet(_SHEETS[0][0])
        cases.set_column("A:E", 20, text)
        cases.set_column("F:J", 38, text)
        cases.set_column("K:O", 21, text)
        cases.write_row(0, 0, _HEADERS, header)
        cases.freeze_panes(1, 0)
        details = book.add_worksheet(_SHEETS[1][0])
        details.set_column("A:A", 14, text)
        details.set_column("B:E", 42, text)
        details.set_column("F:J", 30, text)
        details.write_row(
            0,
            0,
            (
                "Case ID",
                "Acceptance criteria",
                "Status",
                "Actual result",
                "Commit / build",
                "Platform",
                "Recording",
                "Attempt",
                "Time (s)",
                "Observed check",
            ),
            header,
        )
        details.freeze_panes(1, 0)
        versions = "\n".join(f"{item.repository}@{item.sha}" for item in run.revisions)
        versions += "\n" + "\n".join(f"{side}: {name}" for side, name in run.builds.items())
        criteria = {item.id: item.quote for item in run.plan.criteria}
        evidence_row = 1
        for row, (case, result) in enumerate(zip(run.plan.cases, run.results, strict=True), 1):
            platform = "App" if case.primary_platform == "ios" else "Admin Portal"
            steps = "\n".join(
                f"{index}. [{step.platform}] {step.action}"
                for index, step in enumerate(case.steps, 1)
            )
            started = run.started_at.date().isoformat() if result.status != "not_run" else ""
            cases.write_url(
                row, 0, f"https://{run.jira_site}/browse/{run.issue_key}", string=run.issue_key
            )
            values = (
                f"{run.issue_key} · {run.issue_title}",
                platform,
                result.case_id,
                case.path,
                case.title,
                case.preconditions,
                "",
                steps,
                case.expected,
                "Lumon",
                _RESULT[result.status],
                started,
                result.detail,
                "",
            )
            cases.write_row(row, 1, tuple(sanitize_output(value) for value in values), text)
            references = "\n".join(f"{name} · {criteria[name]}" for name in case.ac_refs)
            base = (result.case_id, references, result.status, result.detail, versions)
            details.write_row(evidence_row, 0, base, text)
            evidence_row += 1
            for recording in (file for file in result.artifacts if file.kind == "video"):
                details.write_row(
                    evidence_row,
                    0,
                    (
                        result.case_id,
                        "",
                        "recording",
                        "",
                        "",
                        recording.platform,
                        recording.filename,
                        recording.attempt,
                    ),
                    text,
                )
                evidence_row += 1
            for observation in result.observations:
                details.write_row(
                    evidence_row,
                    0,
                    (
                        result.case_id,
                        "",
                        "",
                        "",
                        "",
                        observation.platform,
                        observation.recording,
                        observation.attempt,
                        observation.video_seconds,
                        f"{observation.expected} → {observation.actual}",
                    ),
                    text,
                )
                evidence_row += 1
        for gap in run.plan.gaps:
            details.write_row(evidence_row, 0, ("", gap, "coverage_gap"), text)
            evidence_row += 1
        end = max(len(run.plan.cases), 1)
        cases.autofilter(0, 0, end, 14)
        cases.data_validation(
            1,
            4,
            end,
            4,
            {
                "validate": "list",
                "source": [
                    "Happy",
                    "Alternative",
                    "Sad",
                    "Sad(Edge)",
                ],
            },
        )
        cases.data_validation(
            1,
            11,
            end,
            11,
            {
                "validate": "list",
                "source": [
                    "待驗證",
                    "驗證成功",
                    "驗證失敗",
                    "忽略",
                ],
            },
        )
    path.chmod(0o600)
    return Artifact(filename=filename, kind="workbook", size=path.stat().st_size)
