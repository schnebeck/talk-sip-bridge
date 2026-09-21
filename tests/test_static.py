"""Names that are used but never defined, and imports that are never
used.

This exists because the rest of the suite cannot see them. A name that
is missing inside a function is a perfectly importable module and a
perfectly passing test run - right up to the moment that line is
reached, which for this bridge is in the middle of a call. Splitting
one module into several introduced exactly that twice in one evening:
`json.dumps` left behind in a file that no longer imported `json`, on
the two paths that ask another participant for their audio.

Imports that are nothing but leftovers are the milder half of the same
check, and they are what makes a module's dependencies readable at all.
A deliberate one says so with `# noqa`.
"""
import pathlib
import re
import unittest

from tests import CODE_DIR

try:
    from pyflakes.api import check
except ImportError:  # pyflakes is a convenience, not a dependency
    check = None

TESTS = pathlib.Path(__file__).resolve().parent
# Reported by pyflakes but intended here: a test asserting on a name is
# not "using" it in a way pyflakes recognises.
IGNORE = ("f-string is missing placeholders",)


class Collector:
    """Gathers what pyflakes says, instead of printing it."""

    def __init__(self):
        self.found = []

    def unexpectedError(self, filename, message):
        self.found.append(f"{filename}: {message}")

    def syntaxError(self, filename, message, lineno, offset, text):
        self.found.append(f"{filename}:{lineno}: {message}")

    def flake(self, message):
        self.found.append(str(message))


def findings(*paths):
    collector = Collector()
    for path in paths:
        for source in sorted(pathlib.Path(path).glob("*.py")):
            check(source.read_text(), str(source), collector)
    keep = []
    for finding in collector.found:
        if any(reason in finding for reason in IGNORE):
            continue
        where = re.match(r"(.*?):(\d+):", finding)
        if where:
            try:
                line = pathlib.Path(where.group(1)).read_text().split("\n")[int(where.group(2)) - 1]
            except (IndexError, OSError):
                line = ""
            if "noqa" in line:
                continue
        keep.append(finding)
    return keep


@unittest.skipIf(check is None, "pyflakes is not installed")
class StaticCheckTest(unittest.TestCase):
    def test_the_bridge_has_no_undefined_or_unused_names(self):
        """The code that runs in production. Nothing here may rely on a
        name it does not have."""
        found = findings(CODE_DIR)
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_the_tests_do_not_either(self):
        found = findings(TESTS, TESTS / "hardware")
        self.assertEqual(found, [], "\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
