"""PlatformIO middleware for the TestBoard_ADC124 capacitance-test firmware.

1. Applies the production USB core patch by executing the unmodified production
   pre-script (the production usb_core_patch module is imported first so the
   production script finds it although PROJECT_DIR differs).
2. Embeds the git revision in the firmware so capstatus can report it.
"""

from pathlib import Path
import subprocess
import sys

Import("env")

project_dir = Path(env.subst("$PROJECT_DIR"))
production_scripts = (project_dir / ".." / "TestBoard_ADC124" / "scripts").resolve()

sys.path.insert(0, str(production_scripts))
import usb_core_patch  # noqa: F401,E402  (cached in sys.modules for the production script)

env.SConscript(str(production_scripts / "pre_usb_core_patch.py"), exports="env")


def git_revision():
    try:
        rev = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=project_dir,
            stderr=subprocess.DEVNULL, text=True).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "HEAD", "--", str(project_dir)],
            cwd=project_dir, stderr=subprocess.DEVNULL) != 0
        return rev + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


env.Append(CPPDEFINES=[("CAPTEST_GIT_HASH", env.StringifyMacro(git_revision()))])
