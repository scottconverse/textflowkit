import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_audit_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("ui_regressions.cjs"))],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
