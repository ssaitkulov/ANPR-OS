"""Отчёт в XLSX: структура файла и доставка в Telegram.

Генератор без внешних зависимостей: проверяем, что это настоящий zip с одной
вкладкой «Проезды», что данные и экранирование корректны, и что при отказе
отправки файла отчёт не теряется — уходит текстом через существующую очередь.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция от боевых данных

import io
import unittest
import zipfile
from unittest.mock import Mock, patch

import anpr_telegram as bot
from app.core.report_xlsx import build_report_xlsx, report_summary

EVENTS = [
    {'time': '08:15:03', 'plate': '01A123BC', 'color': 'White', 'type': 'SaloonCar',
     'speed': '60', 'direction': 'Obverse', 'lane': '1', 'backfill': False},
    {'time': '09:20:11', 'plate': '02B456DE', 'color': 'Black', 'type': 'SUV',
     'speed': '40', 'direction': 'Reverse', 'lane': '2', 'backfill': True},
]


def _xlsx(events=None):
    """Файл с теми же форматтерами, что использует боевой отправщик."""
    return build_report_xlsx('2026-11-22',
                             EVENTS if events is None else events,
                             bot.display_plate, bot._report_direction)


def _parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name).decode('utf-8')
                for name in archive.namelist()}


class XlsxStructureTests(unittest.TestCase):
    def test_single_sheet_named_proezdy(self):
        parts = _parts(_xlsx())
        self.assertIn('xl/worksheets/sheet1.xml', parts)
        self.assertNotIn('xl/worksheets/sheet2.xml', parts)
        self.assertIn('name="Проезды"', parts['xl/workbook.xml'])
        self.assertNotIn('Итоги', parts['xl/workbook.xml'])
        self.assertTrue(all(name in parts for name in (
            '[Content_Types].xml', '_rels/.rels', 'xl/styles.xml')))

    def test_detail_sheet_has_header_and_each_event(self):
        parts = _parts(_xlsx())
        sheet = parts['xl/worksheets/sheet1.xml']
        self.assertIn('Номер', sheet)
        self.assertIn('Источник', sheet)
        self.assertNotIn('Скорость', sheet)
        self.assertNotIn('Полоса', sheet)
        self.assertIn('<c r="A1" t="inlineStr" s="1"><is><t xml:space="preserve">№</t></is></c>', sheet)
        self.assertIn('01 A 123 BC', sheet)  # display_plate
        self.assertIn('02 B 456 DE', sheet)
        self.assertIn(f'<row r="{len(EVENTS) + 1}">', sheet)

    def test_rows_are_numbered(self):
        sheet = _parts(_xlsx())['xl/worksheets/sheet1.xml']
        self.assertIn('<c r="A2"><v>1</v></c>', sheet)
        self.assertIn('<c r="A3"><v>2</v></c>', sheet)

    def test_direction_source_color_and_type_are_localized(self):
        sheet = _parts(_xlsx())['xl/worksheets/sheet1.xml']
        self.assertIn('Entry', sheet)          # Obverse
        self.assertIn('Exit', sheet)           # Reverse
        self.assertIn('Камера', sheet)
        self.assertIn('Архив', sheet)
        self.assertIn('Белый', sheet)
        self.assertIn('Чёрный', sheet)
        self.assertIn('Легковой', sheet)
        self.assertIn('Внедорожник', sheet)

    def test_unknown_color_and_type_are_marked(self):
        events = [dict(EVENTS[0], color='Unknown', type='')]
        sheet = _parts(_xlsx(events))['xl/worksheets/sheet1.xml']
        self.assertIn('Не определён', sheet)

    def test_special_characters_are_escaped(self):
        events = [dict(EVENTS[0], plate='A&B <C>')]
        parts = _parts(build_report_xlsx('2026-11-22', events))
        sheet = parts['xl/worksheets/sheet1.xml']
        self.assertIn('A&amp;B &lt;C&gt;', sheet)
        self.assertNotIn('<C>', sheet)

    def test_summary_numbers(self):
        total, unique, avg = report_summary(EVENTS)
        self.assertEqual((total, unique), (2, 2))
        self.assertEqual(avg, '50 км/ч')

    def test_empty_report_still_valid(self):
        parts = _parts(build_report_xlsx('2026-11-22', []))
        self.assertIn('проездов не зафиксировано', parts['xl/worksheets/sheet1.xml'])
        self.assertEqual(report_summary([])[0], 0)


class ReportDeliveryTests(anpr_testenv.IsolatedTestCase):
    def _state(self):
        return {'days': {}, 'seen': [], 'reported_days': [], 'last_report_date': ''}

    def test_document_is_sent_and_day_marked(self):
        state = self._state()
        with patch.object(bot, 'STORE') as store, \
             patch.object(bot, 'build_report_xlsx', return_value=b'XLSX') as gen, \
             patch.object(bot, 'telegram_send_document', return_value=True) as send, \
             patch.object(bot, 'mark_reported') as mark, \
             patch.object(bot, 'save_state') as save:
            store.day_passages.return_value = []
            bot.send_report_for_day(state, '2026-11-22')
        self.assertTrue(gen.called)
        self.assertTrue(send.called)
        self.assertTrue(mark.called)
        self.assertTrue(save.called)

    def test_falls_back_to_text_when_document_fails(self):
        state = self._state()
        with patch.object(bot, 'STORE') as store, \
             patch.object(bot, 'build_report_xlsx', return_value=b'XLSX'), \
             patch.object(bot, 'telegram_send_document', return_value=False), \
             patch.object(bot, 'telegram_send', return_value=True) as text_send, \
             patch.object(bot, 'mark_reported'), \
             patch.object(bot, 'save_state'):
            store.day_passages.return_value = []
            bot.send_report_for_day(state, '2026-11-22')
        self.assertTrue(text_send.called)
        self.assertEqual(text_send.call_args.kwargs.get('kind'), 'report')

    def test_post_document_builds_multipart_request(self):
        response = Mock()
        with patch.object(bot, '_telegram_call', return_value=response) as call:
            ok = bot._telegram_post_document('42', 'otchet.xlsx', b'DATA', 'caption')
        self.assertTrue(ok)
        self.assertEqual(call.call_args.args[0], 'post')
        self.assertIn('/sendDocument', call.call_args.args[1])
        document = call.call_args.kwargs['files']['document']
        self.assertEqual(document, ('otchet.xlsx', b'DATA'))
        self.assertEqual(call.call_args.kwargs['data']['chat_id'], '42')

    def test_document_sender_reports_delivery(self):
        with patch.object(bot, 'get_recipients', return_value=['42', '43']), \
             patch.object(bot, '_telegram_post_document', side_effect=[True, False]), \
             patch.object(bot, '_set_delivery') as delivery:
            ok = bot.telegram_send_document('a.xlsx', b'X', 'caption')
        self.assertTrue(ok)
        self.assertEqual(delivery.call_args.args, (['42'], ['43']))

    def test_document_sender_returns_false_without_recipients(self):
        with patch.object(bot, 'get_recipients', return_value=[]), \
             patch.object(bot, 'queue_missed_text') as queue:
            ok = bot.telegram_send_document('otchet.xlsx', b'XLSX', 'caption')
        self.assertFalse(ok)
        self.assertTrue(queue.called)


if __name__ == '__main__':
    unittest.main()
