"""Этап 4: полировка перед боевым режимом.

Регрессионные тесты на то, что проверяет приёмка:

* `BACKFILL_SEND_PHOTOS` — не мёртвая настройка: добор архива умеет
  приложить фотографии, которые уже есть в фотоархиве, и честно пишет,
  когда снимков нет (сама камера снимки из архива не отдаёт);
* поиск фото проезда терпим к пробелам и регистру номера и к секундному
  расхождению времени между живым событием и записью архива;
* фотоархив и база согласованы: путь к снимку лежит в базе, файл — на диске;
* `SAVE_PHOTOS` действительно пишет снимок на диск и регистрирует его путь.

Каждый тест работает с настоящим кодом сервиса; база, состояние и файлы
подписчиков — во временном каталоге (см. `anpr_testenv`).
"""
import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import anpr_telegram as bot
from app.storage.photo_archive import save_photo

TZ = bot.TZ


def _jpeg(width=64, height=48) -> bytes:
    """Настоящий JPEG: сервис проверяет снимки через PIL."""
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (30, 60, 90)).save(buffer, format='JPEG')
    return buffer.getvalue()


JPEG = _jpeg()


def _archive_record(rec_no=1, plate='01ABC123', hour=10, day='2026-11-17'):
    """Запись архива камеры в том виде, в каком её отдаёт recordFinder."""
    wall = datetime.fromisoformat(f'{day}T{hour:02d}:00:00+00:00')
    return {'RecNo': rec_no, 'Time': int(wall.timestamp()), 'PlateNumber': plate,
            'PlateColor': 'White', 'VehicleColor': 'Black', 'VehicleSign': 'Unknown',
            'Category': 'SaloonCar', 'Lane': '0', 'JunctionDirection': '0',
            'VehicleDirection': 'Unknown', 'DetailedAddress': 'Area-Z',
            'SnapSource': 'med.Force', 'Event': '34'}


class FindPhotoPathTests(anpr_testenv.IsolatedTestCase):
    """Поиск снимка проезда: сначала точное совпадение, потом близкое."""

    def test_exact_match_returns_the_archived_path(self):
        self.store.set_photo_path('2026-11-17T10:00:00+05:00', '01ABC123', r'D:\photos\a.jpg')
        self.assertEqual(
            self.store.find_photo_path('2026-11-17T10:00:00+05:00', '01ABC123'),
            r'D:\photos\a.jpg')

    def test_plate_formatting_and_a_second_of_drift_do_not_hide_the_photo(self):
        """Номер из архива приходит с пробелами, время — с секундой разницы."""
        self.store.set_photo_path('2026-11-17T10:00:07+05:00', '01 ABC 123', r'D:\photos\b.jpg')
        found = self.store.find_photo_path('2026-11-17T10:00:00+05:00', '01ABC123')
        self.assertEqual(found, r'D:\photos\b.jpg')

    def test_another_plate_is_not_matched(self):
        self.store.set_photo_path('2026-11-17T10:00:00+05:00', '99XXX999', r'D:\photos\c.jpg')
        self.assertIsNone(self.store.find_photo_path('2026-11-17T10:00:00+05:00', '01ABC123'))

    def test_missing_photo_is_not_an_error(self):
        self.assertIsNone(self.store.find_photo_path('2026-11-17T10:00:00+05:00', '01ABC123'))


class BackfillPhotosTests(anpr_testenv.IsolatedTestCase):
    """BACKFILL_SEND_PHOTOS — рабочая настройка, а не пустое место в .env."""

    def _photo_on_disk(self, iso, plate):
        folder = self.image_dir
        path = save_photo(folder, iso, plate, JPEG)
        self.store.set_photo_path(iso, plate, path)
        return path

    def test_collect_finds_the_photo_of_an_archived_passage(self):
        iso = '2026-11-17T10:00:00+05:00'
        self._photo_on_disk(iso, '01ABC123')
        photos = bot.collect_backfill_photos([{'iso': iso, 'plate': '01ABC123', 'time': '10:00:00'}])
        self.assertEqual(list(photos), [(iso, '01ABC123')])
        self.assertEqual(photos[(iso, '01ABC123')], JPEG)

    def test_collect_without_photo_is_empty(self):
        photos = bot.collect_backfill_photos([{'iso': '2026-11-17T10:00:00+05:00',
                                              'plate': '01ABC123', 'time': '10:00:00'}])
        self.assertEqual(photos, {})

    def test_text_marks_passages_with_a_photo(self):
        records = [{'iso': '2026-11-17T10:00:00+05:00', 'plate': '01ABC123', 'time': '10:00:00',
                    'color': '', 'type': '', 'direction': '0'},
                   {'iso': '2026-11-17T11:00:00+05:00', 'plate': '02XYZ789', 'time': '11:00:00',
                    'color': '', 'type': '', 'direction': '0'}]
        photos = {('2026-11-17T10:00:00+05:00', '01ABC123'): JPEG}
        text = bot.build_backfill_text(records, datetime(2026, 11, 17, 10), datetime(2026, 11, 17, 12),
                                       photos)
        self.assertIn('📷', text)
        self.assertIn('Фото приложены отдельно: 1 из 2', text)
        self.assertNotIn('Фотографии этих проездов недоступны', text)

    def test_text_without_photos_says_so(self):
        records = [{'iso': '2026-11-17T11:00:00+05:00', 'plate': '02XYZ789', 'time': '11:00:00',
                    'color': '', 'type': '', 'direction': '0'}]
        text = bot.build_backfill_text(records, datetime(2026, 11, 17, 10), datetime(2026, 11, 17, 12))
        self.assertNotIn('📷', text)
        self.assertIn('Фотографии этих проездов недоступны', text)

    def test_backfill_sends_the_album_when_the_flag_is_on(self):
        """С флагом добор отправляет список и альбом с уже сохранённым фото."""
        iso = '2026-11-17T10:00:00+05:00'
        self._photo_on_disk(iso, '01ABC123')
        calls = {'text': 0, 'album': 0}
        start = datetime(2026, 11, 17, 9, tzinfo=TZ)
        end = datetime(2026, 11, 17, 12, tzinfo=TZ)
        with patch.object(bot, '_backfill_range',
                          return_value=(start, end)), \
             patch.object(bot, '_fetch_archive_records', return_value=[_archive_record()]), \
             patch.object(bot, 'BACKFILL_SEND_PHOTOS', True), \
             patch.object(bot, 'telegram_send', side_effect=lambda *a, **k: calls.__setitem__('text', calls['text'] + 1) or True), \
             patch.object(bot, 'telegram_send_media_group',
                          side_effect=lambda *a, **k: calls.__setitem__('album', calls['album'] + 1) or True):
            added = bot.backfill_once(object(), self.state)
        self.assertEqual(added, 1)
        self.assertEqual(calls['text'], 1)
        self.assertEqual(calls['album'], 1)

    def test_backfill_without_the_flag_stays_text_only(self):
        iso = '2026-11-17T10:00:00+05:00'
        self._photo_on_disk(iso, '01ABC123')
        calls = {'album': 0}
        start = datetime(2026, 11, 17, 9, tzinfo=TZ)
        end = datetime(2026, 11, 17, 12, tzinfo=TZ)
        with patch.object(bot, '_backfill_range',
                          return_value=(start, end)), \
             patch.object(bot, '_fetch_archive_records', return_value=[_archive_record()]), \
             patch.object(bot, 'BACKFILL_SEND_PHOTOS', False), \
             patch.object(bot, 'telegram_send', return_value=True), \
             patch.object(bot, 'telegram_send_media_group',
                          side_effect=lambda *a, **k: calls.__setitem__('album', calls['album'] + 1) or True):
            added = bot.backfill_once(object(), self.state)
        self.assertEqual(added, 1)
        self.assertEqual(calls['album'], 0)


class SavePhotosTests(anpr_testenv.IsolatedTestCase):
    """SAVE_PHOTOS — снимок действительно попадает на диск, путь — в базу."""

    def _live_event(self, plate='01ABC123'):
        """Событие камеры в том виде, в каком его отдаёт snapManager."""
        return {
            'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '100', 'IndexInGroup': '0',
            'TrafficCar.PlateNumber': plate, 'Lane': '0', 'Direction': 'Approach',
        }

    def test_photo_is_written_and_registered(self):
        with patch.object(bot, 'SAVE_PHOTOS', True), patch.object(bot, 'send_entry_notification'):
            with patch.object(bot, 'PHOTOS') as photos:
                photos.put.return_value = None
                bot.flush_pending_events(self.state, {'k': self._live_event()}, [JPEG])
        files = list(self.image_dir.rglob('*.jpg'))
        self.assertEqual(len(files), 1)
        rows = self.store.conn.execute('SELECT captured_at, plate, path FROM passage_photo_paths').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(Path(rows[0][2]).exists())

    def test_nothing_is_written_when_the_flag_is_off(self):
        with patch.object(bot, 'SAVE_PHOTOS', False), patch.object(bot, 'send_entry_notification'):
            with patch.object(bot, 'PHOTOS') as photos:
                photos.put.return_value = None
                bot.flush_pending_events(self.state, {'k': self._live_event()}, [JPEG])
        self.assertEqual(list(self.image_dir.rglob('*.jpg')), [])
        self.assertEqual(
            self.store.conn.execute('SELECT COUNT(*) FROM passage_photo_paths').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
