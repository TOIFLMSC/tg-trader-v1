import hashlib
import uuid
import unittest

from bootstrap import ROOT, SetupError
from trader.agent import content_for


class MediaInputTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'data' / 'media' / f'_agent-test-{uuid.uuid4().hex}.png'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b'test-image-bytes')

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def payload(self, digest):
        return {'messages': [{'id': 1, 'text': '', 'media': [{
            'path': str(self.path.relative_to(ROOT)),
            'mime': 'text/html',
            'sha256': digest,
        }]}], 'parents': []}

    def test_mime_comes_from_validated_extension(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        content = content_for(self.payload(digest))
        image = next(item for item in content if item['type'] == 'input_image')
        self.assertTrue(image['image_url'].startswith('data:image/png;base64,'))

    def test_hash_mismatch_is_rejected(self):
        with self.assertRaisesRegex(SetupError, 'integrity'):
            content_for(self.payload('0' * 64))
