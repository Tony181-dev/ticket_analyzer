import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import doctor  # noqa: E402


class DoctorStartupTest(unittest.TestCase):
    def test_startup_m365_probe_runs_once_in_copilot_mode(self):
        original_cache = dict(doctor._cache)
        doctor._cache.update(m365=None, mcp_test={})
        self.addCleanup(lambda: (doctor._cache.clear(), doctor._cache.update(original_cache)))

        mcp_status = {
            "jira": {"url": "jira", "configured": True},
            "collab": {"url": "collab", "configured": True},
        }
        authenticated = {"state": "ready", "authenticated": True}
        with patch.object(doctor.settings, "load", return_value={"ai_mode": "copilot"}), \
                patch.object(doctor, "_refresh_windows_path"), \
                patch.object(doctor, "_node", return_value=(True, "v22")), \
                patch.object(doctor, "_uvx", return_value=(True, "uvx")), \
                patch.object(doctor.attachments, "axel_path", return_value=None), \
                patch.object(doctor.mcp, "status", return_value=mcp_status), \
                patch.object(doctor.importlib.util, "find_spec", return_value=object()), \
                patch.object(doctor, "_chromium", return_value=(True, "chromium")), \
                patch.object(doctor.m365_worker, "check", return_value=authenticated) as check_m365, \
                patch.object(doctor.copilot, "version", return_value=None):
            first = doctor.checks(probe_m365=True)
            login = next(check for check in first["checks"] if check["id"] == "m365_login")
            self.assertTrue(login["ok"])
            self.assertFalse(login["needed"], "M365 login is informational while Copilot CLI is selected")

            second = doctor.checks()
            cached_login = next(check for check in second["checks"] if check["id"] == "m365_login")
            self.assertTrue(cached_login["ok"])
            check_m365.assert_called_once()


if __name__ == "__main__":
    unittest.main()