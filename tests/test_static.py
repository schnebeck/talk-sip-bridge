"""What the rest of the suite cannot see.

A name that is missing inside a function is a perfectly importable
module and a perfectly passing test run - right up to the moment that
line is reached, which for this bridge is in the middle of a call.
Splitting one module into several introduced exactly that twice in one
evening: `json.dumps` left behind in a file that no longer imported
`json`, on the two paths that ask another participant for their audio.

So this runs `ruff` over everything, with the rules in `ruff.toml`.
Python has no Nextcloud standard to adopt - Nextcloud writes PHP and
JavaScript, and its published rulesets cover those - so those rules are
PEP 8 plus the checks that catch the mistakes every other check passes.

Nothing here reformats anything. A formatter would rewrite files that
are laid out the way they read best, and in this project the comments
are half the point.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _ruff():
    """Beside the interpreter running the tests before anywhere else: it
    is installed into the same venv, which is not on PATH unless
    somebody activated it."""
    beside = pathlib.Path(sys.executable).parent / "ruff"
    return str(beside) if beside.is_file() else shutil.which("ruff")


RUFF = _ruff()


def findings(*paths):
    """What ruff says about those paths, one readable line each."""
    result = subprocess.run(
        [RUFF, "check", "--output-format=json", "--no-cache", *[str(p) for p in paths]],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 1):
        raise AssertionError(f"ruff could not run: {result.stderr.strip()[:400]}")
    return [f"{pathlib.Path(f['filename']).relative_to(ROOT)}:{f['location']['row']}: "
            f"{f['code']} {f['message']}"
            for f in json.loads(result.stdout or "[]")]


@unittest.skipIf(RUFF is None, "ruff is not installed (see tests/requirements.txt)")
class StaticCheckTest(unittest.TestCase):
    def test_the_bridge_is_clean(self):
        """The code that runs in production. Nothing here may rely on a
        name it does not have."""
        found = findings(ROOT / "bridge")
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_the_tests_are_too(self):
        """Including the hardware scripts, which the suite never runs -
        so this is the only thing that notices when a refactor leaves
        one of them calling a method that moved."""
        found = findings(ROOT / "tests")
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_the_rules_are_the_ones_in_the_file(self):
        """A config ruff cannot read would leave both tests above
        passing against its defaults, which is not what they claim."""
        self.assertTrue((ROOT / "ruff.toml").is_file())
        result = subprocess.run([RUFF, "check", "--show-settings", "--no-cache", "bridge"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertIn("ruff.toml", result.stdout, result.stderr[:400])


if __name__ == "__main__":
    unittest.main()
