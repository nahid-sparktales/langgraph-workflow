"""Rebuild the Locus plugin's vendored wheel, checksums, and dependency lock.

    python tools/build_plugin.py

Run after changing src/ or plugin/requirements.in. The lock is universal
(markers for every Python >= 3.10 and platform) with hashes, so the plugin
launcher can install it with --require-hashes wherever Locus runs.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def main() -> None:
    wheels = PLUGIN / "wheels"
    for old in wheels.glob("*.whl"):
        old.unlink()
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheels), str(ROOT)],
                   check=True)
    sums = "".join(f"{hashlib.sha256(w.read_bytes()).hexdigest()}  {w.name}\n"
                   for w in sorted(wheels.glob("*.whl")))
    (wheels / "SHA256SUMS").write_text(sums)
    subprocess.run([sys.executable, "-m", "uv", "pip", "compile", "--quiet", "--universal",
                    "--generate-hashes", "--python-version", "3.10", "--no-strip-markers",
                    "--custom-compile-command", "python tools/build_plugin.py",
                    "-o", str(PLUGIN / "requirements.lock"), str(PLUGIN / "requirements.in")],
                   check=True, cwd=ROOT)
    print(sums, end="")


if __name__ == "__main__":
    main()
