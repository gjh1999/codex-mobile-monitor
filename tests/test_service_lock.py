import tempfile
import unittest
from pathlib import Path
from service_lock import ServiceLock


class LockTests(unittest.TestCase):
    def test_reject_duplicate_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'service.lockfile'
            first=ServiceLock(path)
            try:
                with self.assertRaises(RuntimeError):
                    ServiceLock(path)
            finally:
                first.close()
            second=ServiceLock(path)
            second.close()
