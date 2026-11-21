"""Генерация XLSX-отчёта без внешних зависимостей.

Минимальный SpreadsheetML: две вкладки («Итоги», «Проезды»), строки через
inlineStr, поэтому не нужен sharedStrings, и весь файл собирается в памяти.
Excel, LibreOffice и Telegram-превью открывают такой файл без предупреждений.

Безопасно для офлайн-поставки: не добавляет пакетов в requirements-lock.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
_NS_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'

_CONTENT_TYPES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

_ROOT_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_PKG}">
<Relationship Id="rId1" Type="{_NS_R}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

_WORKBOOK = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{_NS}" xmlns:r="{_NS_R}">
<sheets>
<sheet name="Итоги" sheetId="1" r:id="rId1"/>
<sheet name="Проезды" sheetId="2" r:id="rId2"/>
</sheets>
</workbook>'''

_WORKBOOK_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_PKG}">
<Relationship Id="rId1" Type="{_NS_R}/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="{_NS_R}/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="{_NS_R}/styles" Target="styles.xml"/>
</Relationships>'''

_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{_NS}">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="2">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def _col(index: int) -> str:
    """1 -> A, 27 -> AA."""
    name = ''
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(ref: str, value, bold: bool = False) -> str:
    style = ' s="1"' if bold else ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(str(value if value is not None else ''))
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: list[list], widths: list[int], *, header: bool = True,
           freeze: bool = False) -> str:
    count = max((len(row) for row in rows), default=1)
    dimension = f'A1:{_col(count)}{max(len(rows), 1)}'
    cols = ''.join(
        f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
        for i, width in enumerate(widths, 1)
    )
    pane = ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            if freeze else '')
    body = []
    for r, row in enumerate(rows, 1):
        cells = ''.join(_cell(f'{_col(c)}{r}', value, bold=(header and r == 1))
                        for c, value in enumerate(row, 1))
        body.append(f'<row r="{r}">{cells}</row>')
    auto_filter = f'<autoFilter ref="{dimension}"/>' if header and len(rows) > 1 else ''
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS}">'
            f'<dimension ref="{dimension}"/>'
            f'<sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/>'
            f'<cols>{cols}</cols>'
            f'<sheetData>{"".join(body)}</sheetData>'
            f'{auto_filter}</worksheet>')


def report_summary(events: list[dict]) -> tuple[int, int, str]:
    """Те же цифры, что и в текстовом отчёте: всего, уникальных, средняя скорость."""
    total = len(events)
    unique = len({e.get('plate', '—') for e in events})
    speeds = [float(e['speed']) for e in events
              if str(e.get('speed', '')).replace('.', '', 1).isdigit()]
    avg = f'{sum(speeds) / len(speeds):.0f} км/ч' if speeds else '—'
    return total, unique, avg


def build_report_xlsx(day: str, events: list[dict], plate_fmt=None) -> bytes:
    """Собрать XLSX за день. plate_fmt — display_plate из ядра (без циклического импорта)."""
    fmt = plate_fmt or (lambda value: str(value or '—'))
    total, unique, avg = report_summary(events)

    summary_rows = [
        [f'Проезды за {day}'],
        ['Всего проездов', total],
        ['Разных номеров', unique],
        ['Средняя скорость', avg],
        ['Сформирован', datetime.now().strftime('%Y-%m-%d %H:%M:%S')],
    ]
    detail_rows = [[
        'Дата', 'Время', 'Номер', 'Цвет', 'Тип',
        'Скорость, км/ч', 'Направление', 'Полоса', 'Источник',
    ]]
    for event in events:
        detail_rows.append([
            day,
            event.get('time', ''),
            fmt(event.get('plate', '—')),
            event.get('color', '') or '—',
            event.get('type', '') or '—',
            event.get('speed', '') or '—',
            event.get('direction', '') or '—',
            event.get('lane', '') or '—',
            'архив' if event.get('backfill') else 'живой',
        ])
    if not events:
        detail_rows.append([day, '—', 'проездов не зафиксировано'])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', _CONTENT_TYPES)
        archive.writestr('_rels/.rels', _ROOT_RELS)
        archive.writestr('xl/workbook.xml', _WORKBOOK)
        archive.writestr('xl/_rels/workbook.xml.rels', _WORKBOOK_RELS)
        archive.writestr('xl/styles.xml', _STYLES)
        archive.writestr('xl/worksheets/sheet1.xml',
                         _sheet(summary_rows, [26, 22], header=False))
        archive.writestr('xl/worksheets/sheet2.xml',
                         _sheet(detail_rows, [12, 10, 18, 14, 14, 16, 14, 10, 10], freeze=True))
    return buffer.getvalue()
