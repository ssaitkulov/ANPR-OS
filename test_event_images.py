import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import io
import json
import unittest
from unittest.mock import patch
from PIL import Image
import anpr_telegram as bot
from event_images import prepare_event_images


def jpeg(size, color):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, 'JPEG')
    return output.getvalue()


class ImageTests(unittest.TestCase):
    def test_two_jpegs_in_one_payload(self):
        scene, plate = jpeg((800, 600), 'blue'), jpeg((200, 60), 'white')
        ev = {'Object.ObjectType': 'Plate', 'SceneImage.Offset': '0', 'SceneImage.Length': str(len(scene)),
              'Object.Image.Offset': str(len(scene)), 'Object.Image.Length': str(len(plate))}
        self.assertEqual(prepare_event_images(ev, [scene + plate]), [scene, plate])

    def test_crop_uses_normalized_coordinates(self):
        event = {'TrafficCar.BoundingBox': '[2048,2048,6144,4096]'}
        result = prepare_event_images(event, [jpeg((800, 600), 'blue')])
        self.assertEqual(Image.open(io.BytesIO(result[1])).size, (400, 150))

    def test_invalid_metadata_does_not_guess_crop(self):
        scene = jpeg((800, 600), 'blue')
        event = {'Object.ObjectType': 'Plate', 'Object.Image.Offset': '-1', 'Object.Image.Length': '20'}
        self.assertEqual(prepare_event_images(event, [scene]), [scene])

    def test_album_attachment_names_match_references(self):
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', 'example'), patch.object(bot, 'get_recipients', return_value=['1']), patch.object(bot.requests, 'post') as post:
            post.return_value.status_code = 200
            self.assertTrue(bot.telegram_send_media_group([(b'a', 'caption'), (b'b', '')]))
            args = post.call_args.kwargs
            media = json.loads(args['data']['media'])
            self.assertEqual([m['media'] for m in media], ['attach://0', 'attach://1'])
            self.assertEqual([name for name, value in args['files']], ['0', '1'])


if __name__ == '__main__':
    unittest.main()
