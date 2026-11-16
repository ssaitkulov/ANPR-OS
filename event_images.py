"""Make a plate crop only from coordinates belonging to the same camera event."""
import io
import json
import math
from PIL import Image, UnidentifiedImageError


def plate_box(event):
    prefix = 'Object.BoundingBox' if event.get('Object.ObjectType') == 'Plate' else 'TrafficCar.BoundingBox'
    value = event.get(prefix)
    try:
        if value is not None:
            values = json.loads(value) if isinstance(value, str) else value
        else:
            values = [event[f'{prefix}[{i}]'] for i in range(4)]
        if len(values) != 4:
            return None
        left, top, right, bottom = map(float, values)
        if not all(math.isfinite(v) and 0 <= v <= 8192 for v in (left, top, right, bottom)):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom
    except (ValueError, TypeError, KeyError):
        return None


def prepare_event_images(event, images):
    """Return main scene and plate crop. Missing coordinates never produce a guessed crop."""
    payload = b''.join(images)

    def embedded(prefix):
        try:
            offset, length = int(event[prefix + '.Offset']), int(event[prefix + '.Length'])
            if offset < 0 or length <= 0 or offset + length > len(payload):
                return None
            data = payload[offset:offset + length]
            with Image.open(io.BytesIO(data)) as picture:
                picture.verify()
            return data
        except (KeyError, TypeError, ValueError, OSError):
            return None

    scene = embedded('SceneImage')
    plate = embedded('Object.Image') if event.get('Object.ObjectType') == 'Plate' else None
    if scene and plate:
        return [scene, plate]
    valid = []
    for data in ([scene] if scene else images):
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                valid.append((image.width * image.height, data))
        except (UnidentifiedImageError, OSError, ValueError):
            continue
    if not valid:
        return []
    valid.sort(key=lambda item: item[0], reverse=True)
    main = valid[0][1]
    box = plate_box(event)
    if box:
        with Image.open(io.BytesIO(main)) as image:
            left, top, right, bottom = box
            bounds = (int(left * image.width / 8192), int(top * image.height / 8192),
                      math.ceil(right * image.width / 8192), math.ceil(bottom * image.height / 8192))
            if bounds[2] - bounds[0] >= 2 and bounds[3] - bounds[1] >= 2:
                crop = image.crop(bounds).convert('RGB')
                output = io.BytesIO()
                crop.save(output, format='JPEG', quality=95)
                return [main, output.getvalue()]
    # Preserve separate camera JPEGs when supplied; do not substitute a later snapshot.
    return [item[1] for item in valid[:2]]
