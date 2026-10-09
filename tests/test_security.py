import unittest
from starwatch.security import redact, redact_text, visibility, export_allowed


class SecurityTests(unittest.TestCase):
    def test_complete_secret_values(self):
        for value in ('Authorization: Bearer secretfixture', 'GH_TOKEN=secretfixture',
                      'SLACK_WEBHOOK_URL=secretfixture', 'LLM_API_KEY=secretfixture',
                      'api_key=secretfixture', 'token=secretfixture', 'password=secretfixture',
                      'https://user:secretfixture@example.test/',
                      'https://example.test/?token=secretfixture&x=ok',
                      'https://hooks.slack.com/services/secretfixture', 'ghp_secretfixture'):
            with self.subTest(value=value):
                self.assertNotIn('secretfixture', redact_text(value))

    def test_json_secret_fragment_in_source_text(self):
        self.assertNotIn('secretfixture', redact_text('{"api_key": "secretfixture"}'))

    def test_nested_json_secrets(self):
        self.assertNotIn('secretfixture', str(redact({'rows': [{'api_key': 'secretfixture'}]})))

    def test_visibility_fail_closed(self):
        self.assertEqual(visibility({}), 'unknown')
        self.assertFalse(export_allowed({}))
        self.assertFalse(export_allowed({'visibility': 'private'}))
        self.assertFalse(export_allowed({'visibility': 'internal'}))
        self.assertTrue(export_allowed({'visibility': 'public'}))

    def test_conflicting_visibility_is_private(self):
        self.assertEqual(visibility({'visibility': 'public', 'private': True}), 'private')

    def test_private_destination_explicit(self):
        with self.assertRaises(ValueError):
            export_allowed({'visibility': 'private'}, include_private=True)
        self.assertTrue(export_allowed({'visibility': 'private'}, include_private=True, destination='private'))
        self.assertFalse(export_allowed({}, include_private=True, destination='private'))
