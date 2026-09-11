"""Protect the destructive boundary of the disposable Docker test harness."""

import unittest

from scripts.test_fresh_install import (
    LABEL, PROJECT_LABEL, SmokeFailure, assert_owned_resource,
)


class CleanupOwnershipTests(unittest.TestCase):
    project = "mediabot-fresh-0123456789abcdef"

    def test_cleanup_requires_both_labels_for_each_resource_kind(self):
        labels = {LABEL: self.project, PROJECT_LABEL: self.project}
        for kind in ("container", "network", "volume", "image"):
            with self.subTest(kind=kind):
                payload = {"Config": {"Labels": labels}} if kind in {"container", "image"} else {"Labels": labels}
                assert_owned_resource(payload, self.project, kind)
                for omitted in labels:
                    incomplete = {key: value for key, value in labels.items() if key != omitted}
                    payload = {"Config": {"Labels": incomplete}} if kind in {"container", "image"} else {"Labels": incomplete}
                    with self.assertRaises(SmokeFailure):
                        assert_owned_resource(payload, self.project, kind)

    def test_cleanup_rejects_a_different_project_and_unscoped_names(self):
        for target in ("mediabot", "mediabot-fresh-ffffffffffffffff"):
            with self.subTest(target=target):
                payload = {"Labels": {LABEL: self.project, PROJECT_LABEL: self.project}}
                with self.assertRaises(SmokeFailure):
                    assert_owned_resource(payload, target, "volume")

    def test_cleanup_rejects_unlabeled_or_malformed_metadata(self):
        for payload in ({}, {"Labels": None}, {"Labels": []}):
            with self.assertRaises(SmokeFailure):
                assert_owned_resource(payload, self.project, "volume")


if __name__ == "__main__":
    unittest.main()
