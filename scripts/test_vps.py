"""Tests for vps/dispatch.py, sandbox/s9-task and scripts/health-run.sh against stub GitHub,
Hermes and OpenRouter.

Run: python3 -m unittest scripts/test_vps.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))     # the test modules are in scripts/vps_tests/

from vps_tests.dispatch_run import (  # noqa: E402,F401
    TestDispatch, TestFailClosed, TestRecovery, TestPaging, TestTimeout, TestGuardrails, TestAnyAgent, TestPriority,
)
from vps_tests.dispatch_money import (  # noqa: E402,F401
    TestSpend, TestCredits, TestLedger, TestCpuUnits, TestPace, TestCash, TestLimitsExample, TestPacing, TestDayCap, TestCapacity, TestRunCost,
)
from vps_tests.dispatch_lanes import (  # noqa: E402,F401
    TestRoutines, TestLanes, TestModels, TestRosterTimeouts,
)
from vps_tests.host import (  # noqa: E402,F401
    TestS9Task, TestInstall, TestCloudflaredPatch, TestDashboard, HealthRun,
)
from vps_tests.host_sync import TestSync  # noqa: E402,F401
from vps_tests.codescene import TestCodeSceneToken, TestCsWrapper, TestCsInstall  # noqa: E402,F401
from vps_tests.hq_sca import (  # noqa: E402,F401
    TestHq, TestSca,
)
