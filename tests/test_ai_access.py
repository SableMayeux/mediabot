import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from mediabot.core import database
from mediabot.services.ai_access import AIAccessService


class AIAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        replacement = patch.object(database, "DB_PATH", str(Path(temporary.name) / "access.db"))
        replacement.start()
        self.addCleanup(replacement.stop)
        self.service = AIAccessService()

    def test_member_defaults_and_owner_immunity(self):
        access = self.service.access(42)
        self.assertEqual({key: value["allowed"] for key, value in access.items()},
                         {"server": True, "desktop": False, "web": True})
        self.service.set_access(42, "server", False, actor_id=1)
        owner = self.service.access(42, owner=True)
        self.assertTrue(all(value == {"allowed": True, "source": "owner"} for value in owner.values()))
        with self.assertRaisesRegex(ValueError, "owner always retains"):
            self.service.set_access(42, "desktop", False, actor_id=1, owner=True)

    def test_overrides_persist_across_service_restart_and_are_independent(self):
        self.service.set_access(42, "desktop", True, actor_id=1)
        self.service.set_access(42, "server", False, actor_id=1)
        self.service.set_access(42, "web", False, actor_id=1)
        current = AIAccessService().access(42)
        self.assertEqual({key: value["allowed"] for key, value in current.items()},
                         {"server": False, "desktop": True, "web": False})
        self.assertTrue(self.service.access(99)["server"]["allowed"])
        reset = self.service.set_access(42, "server", None, actor_id=1)
        self.assertEqual(reset["server"], {"allowed": True, "source": "default"})
        self.assertEqual(reset["desktop"], {"allowed": True, "source": "override"})

    def test_validation_and_additive_schema_preserve_unrelated_data(self):
        with database.connection() as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('Keep this')")
        for user, capability, value in ((True, "web", True), (-1, "web", True),
                (42, "shell", True), (42, "web", 1)):
            with self.assertRaises(ValueError):
                self.service.set_access(user, capability, value, actor_id=1)
        self.service.initialize()
        self.service.initialize()
        with database.connection() as conn:
            self.assertEqual(conn.execute("SELECT value FROM unrelated").fetchone()[0], "Keep this")


if __name__ == "__main__":
    unittest.main()
