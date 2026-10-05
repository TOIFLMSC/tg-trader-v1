import unittest
from bootstrap import owner_candidate, require, SetupError


class OwnerBindingTests(unittest.TestCase):
    def update(self, **changes):
        message = {'text': '/start secret', 'chat': {'id': 123, 'type': 'private'},
                   'from': {'id': 123, 'is_bot': False}}
        message.update(changes)
        return {'message': message}

    def test_matching_private_sender(self):
        self.assertEqual(owner_candidate(self.update(), '/start secret')['id'], 123)

    def test_wrong_nonce(self):
        self.assertIsNone(owner_candidate(self.update(), '/start other'))

    def test_group_and_bot_rejected(self):
        self.assertIsNone(owner_candidate(self.update(chat={'id': 123, 'type': 'group'}), '/start secret'))
        self.assertIsNone(owner_candidate(self.update(**{'from': {'id': 123, 'is_bot': True}}), '/start secret'))

    def test_mismatched_sender_rejected(self):
        self.assertIsNone(owner_candidate(self.update(**{'from': {'id': 456, 'is_bot': False}}), '/start secret'))

    def test_missing_value_does_not_disclose_other_secret(self):
        with self.assertRaises(SetupError) as error:
            require({'KEY': 'private-value'}, 'TOKEN')
        self.assertNotIn('private-value', str(error.exception))


if __name__ == '__main__':
    unittest.main()
