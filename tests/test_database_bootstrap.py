import time
import unittest
from unittest.mock import patch

from pymongo.errors import ServerSelectionTimeoutError

from api.module import database


class DatabaseBootstrapTests(unittest.TestCase):
    def test_transient_primary_loss_defers_index_setup_without_blocking_requests(self):
        class Collection:
            def __init__(self):
                self.attempts = 0
                self.fail = True

            def create_index(self, *args, **kwargs):
                self.attempts += 1
                if self.fail:
                    raise ServerSelectionTimeoutError("No primary available for writes")

        class Database:
            def __init__(self):
                self.collection = Collection()

            def __getattr__(self, name):
                return self.collection

        db = Database()
        with patch.object(database, "get_database_sync", return_value=db), \
                patch.object(database, "_indexes_ready", False), \
                patch.object(database, "_indexes_retry_at", 0.0):
            self.assertIs(database.ensure_database_sync(), db)
            self.assertEqual(db.collection.attempts, 1)
            self.assertGreater(database._indexes_retry_at, time.monotonic())
            self.assertIs(database.ensure_database_sync(), db)
            self.assertEqual(db.collection.attempts, 1)

            db.collection.fail = False
            database._indexes_retry_at = 0.0
            self.assertIs(database.ensure_database_sync(), db)
            self.assertTrue(database._indexes_ready)
            self.assertGreater(db.collection.attempts, 1)


if __name__ == "__main__":
    unittest.main()
