import unittest
from unittest.mock import patch

try:
    import agent
except ModuleNotFoundError as exc:  # Allows source checks in an offline bootstrap environment.
    agent = None
    IMPORT_ERROR = str(exc)
else:
    IMPORT_ERROR = ""

@unittest.skipIf(agent is None, f"runtime dependencies unavailable: {IMPORT_ERROR}")
class AgentSafetyTests(unittest.TestCase):
    def test_duration_format(self):
        self.assertEqual(agent.fmt_duration(90061), "1d 1h 1m")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            agent.do_command({"action": "shell", "payload": {"cmd": "whoami"}})

    def test_non_allowlisted_app_is_rejected(self):
        with self.assertRaises(ValueError):
            agent.do_command({"action": "launch_app", "payload": {"name": "terminal"}})

    @patch("agent.time.time", return_value=100)
    def test_signature_binds_timestamp_and_body(self, _):
        headers = agent.signed_headers("key", b'{"x":1}')
        self.assertEqual(headers["X-Agent-Timestamp"], "100")
        self.assertNotEqual(headers["X-Agent-Signature"], agent.signed_headers("key", b'{}')["X-Agent-Signature"])

if __name__ == "__main__":
    unittest.main()
