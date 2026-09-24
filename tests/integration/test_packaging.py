"""Built wheel imports and runs outside the source checkout.

Opt-in (``LGW_PACKAGING=1``): it builds a wheel and installs its dependencies
into a fresh virtual environment, which needs a package index or cache.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("LGW_PACKAGING") != "1",
                                reason="set LGW_PACKAGING=1 to build and install the wheel")


def run(*args, cwd=None):
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    return done.stdout


def test_wheel_installs_and_runs_demo_outside_checkout(tmp_path):
    run(sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path / "dist"), str(ROOT))
    wheel = next((tmp_path / "dist").glob("langgraph_workflow-*.whl"))
    venv.create(tmp_path / "venv", with_pip=True)
    python = tmp_path / "venv" / "bin" / "python"
    run(str(python), "-m", "pip", "install", "-q", str(wheel))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    location = run(str(python), "-c", "import langgraph_workflow, sys; "
                   "print(langgraph_workflow.__file__)", cwd=elsewhere)
    assert str(ROOT) not in location and "site-packages" in location
    out = run(str(python), str(ROOT / "examples" / "demo.py"), "--profile",
              str(tmp_path / "demo-profile"), cwd=elsewhere)
    assert "process 5 resume: verified" in out
    assert "process 2 decide prefer:0: verified" in out
    assert "(never repeated)" in out
