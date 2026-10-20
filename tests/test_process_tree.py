"""A real descendant retaining stdout must not defeat the command deadline."""

import sys
import time
import unittest

from scripts.local_kind import run


class ProcessTreeTests(unittest.TestCase):
    def test_timeout_stops_owned_descendant_holding_inherited_pipes(self):
        # No external process, target, credentials or infrastructure is involved.
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "timed out"):
            run([sys.executable, "-c", script], timeout=1)
        self.assertLess(time.monotonic() - started, 20)
