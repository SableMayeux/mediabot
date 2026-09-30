import errno
import importlib.util
import io
import os
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location("private_ha_check", Path(__file__).resolve().parents[1] / "scripts/check_private_ha.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PrivateHAAuditTests(unittest.TestCase):
    def test_no_target_or_private_target_is_rejected_before_network_access(self):
        for targets in ("[]", '["10.0.0.55"]', '["127.0.0.1"]', '["example.com"]'):
            with patch.dict(os.environ, {"PRIVATE_HA_AUDIT_TARGETS": targets}), \
                    patch.object(audit.socket, "create_connection") as connect:
                with self.assertRaises(ValueError):
                    audit.main()
                connect.assert_not_called()

    def test_reachable_listener_fails_audit_and_wan_address_is_not_published(self):
        with patch.dict(os.environ, {"PRIVATE_HA_AUDIT_TARGETS": '["1.1.1.1"]'}), \
                patch.object(audit.socket, "create_connection", return_value=MagicMock()), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(audit.main(), 1)
        self.assertNotIn("1.1.1.1", output.getvalue())

    def test_refused_listeners_pass_but_unavailable_network_family_is_inconclusive(self):
        for error, expected in ((ConnectionRefusedError(errno.ECONNREFUSED, "refused"), 0),
                                (OSError(errno.ENETUNREACH, "no route"), 1)):
            with patch.dict(os.environ, {"PRIVATE_HA_AUDIT_TARGETS": '["1.1.1.1"]'}), \
                    patch.object(audit.socket, "create_connection", side_effect=error) as connect, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(), expected)
            self.assertEqual(connect.call_count, 3)
