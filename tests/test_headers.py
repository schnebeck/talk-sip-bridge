# talk-sip-bridge - tests/test_headers.py
# Every file says who owns it, who wrote it and under what licence.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is
#   in LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every file says who owns it, who wrote it and under what licence.

A header naming the file it sits in goes stale the first time that file
is renamed, and a description copied from a docstring goes stale the
first time the docstring is rewritten. Both are worth having anyway -
they are only worth having if something checks them.

What cannot carry a header (JSON, and the recordings the tests replay
byte for byte) is named in REUSE.toml instead, and that list has to stay
the exact complement of this one.
"""
import ast
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _reuse():
    """Beside the interpreter running the tests before anywhere else, the
    same way test_static.py finds ruff."""
    beside = pathlib.Path(sys.executable).parent / "reuse"
    return str(beside) if beside.is_file() else shutil.which("reuse")

HOLDER = "Thorsten Schnebeck <thorsten.schnebeck@gmx.net>"
WRITER = "Anthropic Claude Opus 5"
PROJECT = "talk-sip-bridge"

# The one part of this project that has no free choice of licence: a
# Nextcloud app builds on OCP, and Nextcloud is AGPL-3.0-or-later.
AGPL_PREFIX = "nextcloud-app/"

LICENCE_FILE = {"GPL-3.0-or-later": "LICENSES/GPL-3.0-or-later.txt",
                "AGPL-3.0-or-later": "LICENSES/AGPL-3.0-or-later.txt"}


def tracked():
    """Every file the repository tracks, or None where there is no
    repository to ask - an installed copy of these tests, which is a
    directory of files next to a deployment and has no git, no
    REUSE.toml and no LICENSES/. There is nothing here for it to check,
    and erroring out would fail the suite on the one machine where
    running it proves the most."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return sorted(out.stdout.split())


def header_of(path: str) -> list:
    """The header's lines, stripped of whatever comment markers the file
    uses, or [] if there is none."""
    text = (ROOT / path).read_text(errors="replace")
    if "SPDX-License-Identifier" not in text:
        return []
    lines = []
    for raw in text.splitlines():
        # What has to stay on line 1 of the file comes before the header.
        if not lines and (raw.startswith("#!") or raw.startswith("<?")):
            continue
        if raw.strip() in ("<!--", "/*"):
            continue
        line = re.sub(r"^\s*(#|\*|//)\s?", "", raw).rstrip()
        lines.append(line)
        if "SPDX-License-Identifier" in line:
            break
    return lines


def field(lines, name: str):
    for line in lines:
        if line.strip().startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return None


def expected_licence(path: str) -> str:
    return ("AGPL-3.0-or-later" if path.startswith(AGPL_PREFIX)
            else "GPL-3.0-or-later")


def first_sentence(docstring: str) -> str:
    paragraph = " ".join(docstring.strip().split("\n\n")[0].split())
    match = re.match(r"^(.+?[.!?])(\s|$)", paragraph)
    return match.group(1) if match else paragraph


def described(lines) -> str:
    """The description: everything between the file's name and the blank
    line that follows it, as one string."""
    description = []
    for line in lines[1:]:
        if not line.strip():
            break
        description.append(line.strip())
    return " ".join(description)


class HeaderTest(unittest.TestCase):
    """One assertion per property, over every tracked file at once, so a
    failure names every file that has it wrong rather than the first."""

    @classmethod
    def setUpClass(cls):
        cls.files = tracked()
        if cls.files is None:
            raise unittest.SkipTest("not a checkout - nothing to check here")
        cls.covered = set(re.findall(r'"([^"]+)"',
                                     (ROOT / "REUSE.toml").read_text()))
        # LICENSES/ holds the licence texts themselves. They are what the
        # headers point at, they are quoted verbatim, and the
        # specification reserves the directory for exactly that - so
        # neither a header nor a REUSE.toml entry belongs on them.
        cls.headers = {p: header_of(p) for p in cls.files
                       if p not in cls.covered and p != "REUSE.toml"
                       and not p.startswith("LICENSES/")}

    def test_every_file_is_covered(self):
        """Either a header or an entry in REUSE.toml - never neither."""
        missing = [p for p, lines in self.headers.items() if not lines]
        self.assertEqual(missing, [], "no licence header and not in "
                                      "REUSE.toml:\n" + "\n".join(missing))

    def test_reuse_toml_lists_nothing_that_has_a_header(self):
        """The two lists are complements. A file that gained a header and
        stayed in REUSE.toml would have its licence stated twice, and
        nothing would notice when the two disagreed."""
        both = [p for p in self.files
                if p in self.covered and "SPDX-License-Identifier"
                in (ROOT / p).read_text(errors="replace")]
        self.assertEqual(both, [], "\n".join(both))

    def test_the_header_names_the_file_it_is_in(self):
        wrong = [f"{p}: {lines[0]!r}" for p, lines in self.headers.items()
                 if lines and lines[0] != f"{PROJECT} - {p}"]
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_copyright_is_the_same_everywhere(self):
        wrong = [f"{p}: {field(lines, 'SPDX-FileCopyrightText')!r}"
                 for p, lines in self.headers.items() if lines
                 and field(lines, "SPDX-FileCopyrightText") != f"(C) 2026 {HOLDER}"]
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_writer_is_named_everywhere(self):
        wrong = [p for p, lines in self.headers.items() if lines and
                 field(lines, "SPDX-FileContributor")
                 != f"{WRITER} (AI generated content)"]
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_the_licence_matches_the_part_of_the_project(self):
        """AGPL inside the Nextcloud app, GPL everywhere else."""
        wrong = [f"{p}: {field(lines, 'SPDX-License-Identifier')} "
                 f"(expected {expected_licence(p)})"
                 for p, lines in self.headers.items() if lines
                 and field(lines, "SPDX-License-Identifier") != expected_licence(p)]
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_header_points_at_a_licence_that_is_there(self):
        for path, lines in self.headers.items():
            if not lines:
                continue
            with self.subTest(path=path):
                wanted = LICENCE_FILE[expected_licence(path)]
                self.assertIn(wanted, " ".join(lines))
                self.assertTrue((ROOT / wanted).is_file())

    def test_a_python_header_says_what_the_docstring_says(self):
        """The description is the module docstring's first sentence. If
        one is rewritten the other has to follow, which is the whole
        reason for repeating it up there."""
        wrong = []
        for path, lines in self.headers.items():
            if not path.endswith(".py") or not lines:
                continue
            doc = ast.get_docstring(ast.parse((ROOT / path).read_text()))
            if doc and described(lines) != first_sentence(doc):
                wrong.append(f"{path}:\n  header: {described(lines)}\n"
                             f"  docstring: {first_sentence(doc)}")
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    @unittest.skipIf(_reuse() is None,
                     "the reuse tool is not installed (see tests/requirements.txt)")
    def test_the_project_is_reuse_compliant(self):
        """The specification's own tool, on top of the checks above: it
        validates REUSE.toml itself, which nothing here parses properly,
        and rejects a licence identifier that is not a real one."""
        result = subprocess.run([_reuse(), "lint", "--quiet"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         subprocess.run([_reuse(), "lint"], cwd=ROOT,
                                        capture_output=True, text=True,
                                        timeout=120).stdout[-2000:])

    def test_the_licence_texts_are_the_originals(self):
        """Not a paraphrase and not an excerpt: the GPL's own terms for
        conveying it say so."""
        for spdx, name in LICENCE_FILE.items():
            with self.subTest(licence=spdx):
                text = (ROOT / name).read_text()
                self.assertIn("TERMS AND CONDITIONS", text)
                self.assertIn("END OF TERMS AND CONDITIONS", text)
                self.assertGreater(len(text.splitlines()), 600)


if __name__ == "__main__":
    unittest.main()
