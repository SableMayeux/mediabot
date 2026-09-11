import unittest
from scripts.release_notes import release_notes

class ReleaseNotesTests(unittest.TestCase):
    def test_only_requested_stable_tag_section_is_published(self):
        text='## [Unreleased]\nSecret future work\n## [2.5.0] - 2026-09-10\n\n### Fixed\n\n- Current fix\n\n## [2.4.0] - 2026-09-10\nOld work\n'
        result=release_notes('v2.5.0',text)
        self.assertIn('Current fix',result)
        self.assertNotIn('future work',result);self.assertNotIn('Old work',result)
        for tag in ('v2.6.0','v2.5.0-rc1','v2.5.0; echo bad'):
            with self.assertRaises(ValueError):release_notes(tag,text)
