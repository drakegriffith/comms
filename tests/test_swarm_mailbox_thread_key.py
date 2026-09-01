#!/usr/bin/env python3
"""thread_key's INPUT CONTRACT: the two ways a key got fabricated in the field.

This file is the positive control for panel 2026-08-31-4514-board-integrity
(seats K2 and F1). Both defects it pins were observed in live board rows in
/tmp/comms-machine-ops, not imagined:

  DEF-3 (K2)  thread_key had no cwd argument, so os.path.realpath resolved a
              RELATIVE path against whatever directory the hook process
              happened to be in. A repo-root-relative path handed to a hook
              running from a subdirectory of that repo produced a DOUBLED key:
                doc:.claude/<prefix>/<prefix>/briefs/COMMON.md
              Both keys for one document coexisted in one mailbox file, so two
              seats editing that file landed in two threads and never saw each
              other -- the board failing silently at the exact collision case
              it exists to prevent.

  DEF-4 (F1)  os.path.realpath does not require existence, and the marker walk
              still finds a real .git above a phantom path, so thread_key
              minted a key for a file that does not exist. That contradicts
              thread_key's own docstring, which argues "a made-up key is an
              invisible mis-grouping" -- and then made one.

THE CONTRACT THESE TESTS FIX IN PLACE:
  * a relative path is a HARD ERROR (ValueError). thread_key cannot know what
    a relative path is relative to; the caller can, and must say so. Guessing
    via os.getcwd() is what produced the doubled key.
  * an absolute path that does not exist is None, not a key. None is a
    VISIBLE non-grouping (the row takes the unthreaded path); a fabricated key
    is an invisible mis-grouping, which is strictly worse.

EVERY negative here is paired with a positive control in
TestThreadKeyContractPositiveControls: a guard that turned every input into
None/ValueError would satisfy the negatives alone and inspect zero real
subjects.
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_HERE), "lib")
_spec = importlib.util.spec_from_file_location(
    "swarm_mailbox", os.path.join(_LIB, "swarm_mailbox.py")
)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


class _ThreadKeyCase(unittest.TestCase):
    """Shared fixture: real directories with real `.git` entries, same shape
    the existing TestThreadKey suite uses (no repo_root= override exists, and
    none is added here)."""

    def setUp(self):
        # conftest's autouse fixture points COMMS_ROOT at a fresh per-test dir.
        self.tmp = os.environ["COMMS_ROOT"]

    def _repo(self, name):
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, ".git"), exist_ok=True)
        return root

    def _touch(self, root, relpath):
        path = os.path.join(root, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x\n")
        return path

    def _chdir(self, where):
        """chdir with an unconditional restore -- a leaked cwd would poison
        every later test in the process."""
        back = os.getcwd()
        self.addCleanup(os.chdir, back)
        os.chdir(where)


class TestRelativePathIsAHardError(_ThreadKeyCase):
    """DEF-3. Reproduces K2's doubling, then pins the contract that kills it."""

    def test_K2s_reproducer_no_longer_silently_doubles_the_key(self):
        """K2's three-line reproducer, verbatim in shape: chdir into a
        subdirectory of a repo, hand thread_key the REPO-ROOT-RELATIVE spelling
        of a file in that subdirectory. The observed field output was

          doc:<repo>/<prefix>/<prefix>/COMMON.md

        i.e. the prefix appearing twice. THIS TEST IS THE POSITIVE CONTROL FOR
        THE WHOLE FIX: before the fix it fails by returning that doubled key."""
        root = self._repo("harness")
        prefix = "docs/panels/briefs"
        rel_from_repo_root = prefix + "/COMMON.md"
        self._touch(root, rel_from_repo_root)
        self._chdir(os.path.join(root, *prefix.split("/")))

        doubled = "doc:harness/%s/%s" % (prefix, rel_from_repo_root)

        with self.assertRaises(ValueError) as caught:
            mb.thread_key(rel_from_repo_root)
        # Name the offending input, so the adapter's one-line stderr guard
        # ("doc-enrol leg failed: %s") is actionable rather than mysterious.
        self.assertIn(rel_from_repo_root, str(caught.exception))
        # And state the thing that was actually wrong, for the record: the old
        # behaviour was not an error, it was a plausible-looking wrong answer.
        self.assertNotEqual(doubled, "")  # doubled key is documented, not returned

    def test_a_bare_relative_filename_is_a_hard_error(self):
        """The simplest form of the same ambiguity. Pre-fix this returned
        "doc:comms/a.md" -- correct ONLY by the accident of the process cwd."""
        root = self._repo("comms")
        self._touch(root, "a.md")
        self._chdir(root)
        with self.assertRaises(ValueError):
            mb.thread_key("a.md")

    def test_dot_slash_and_dotdot_spellings_are_also_rejected(self):
        root = self._repo("comms")
        self._touch(root, "docs/a.md")
        self._chdir(os.path.join(root, "docs"))
        for spelling in ("./a.md", "../docs/a.md", "."):
            with self.subTest(spelling=spelling):
                with self.assertRaises(ValueError):
                    mb.thread_key(spelling)

    def test_the_empty_string_is_a_hard_error_not_the_cwds_key(self):
        """os.path.realpath("") is os.getcwd(): the empty string used to key
        the CURRENT DIRECTORY, which is the ambiguity at its purest."""
        root = self._repo("comms")
        self._chdir(root)
        with self.assertRaises(ValueError):
            mb.thread_key("")

    def test_None_is_a_hard_error_not_a_crash_deep_inside_posixpath(self):
        with self.assertRaises(ValueError):
            mb.thread_key(None)


class TestNonexistentPathIsNoKey(_ThreadKeyCase):
    """DEF-4. thread_key used to mint keys for paths that do not exist."""

    def test_an_absolute_path_that_does_not_exist_is_None(self):
        root = self._repo("comms")
        self._touch(root, "real.md")  # the repo and its .git are real
        ghost = os.path.join(root, "docs", "never-written.md")
        self.assertFalse(os.path.exists(ghost))
        self.assertIsNone(mb.thread_key(ghost))

    def test_the_field_shaped_doubled_path_is_None(self):
        """F1 D4's join bug, reproduced as thread_key sees it: git porcelain
        emits REPO-ROOT-relative paths even from a subdirectory, and the
        adapter joined one onto the payload cwd. The result is ABSOLUTE (so
        the DEF-3 guard does not see it) and nonexistent (so this one does)."""
        root = self._repo("harness")
        prefix = "docs/panels/wave2"
        porcelain_says = prefix + "/briefs/COMMON.md"
        self._touch(root, porcelain_says)
        payload_cwd = os.path.join(root, *prefix.split("/"))
        joined = os.path.join(payload_cwd, *porcelain_says.split("/"))
        self.assertFalse(os.path.exists(joined))
        self.assertIsNone(mb.thread_key(joined))

    def test_the_plausible_phantom_is_None_too(self):
        """F1's nastier variant: a root-relative path joined onto a
        subdirectory cwd that does NOT visibly double. It reads as a real path
        and cannot be spotted by eye, so only existence catches it."""
        root = self._repo("harness")
        self._touch(root, "hooks/decisions/run.sh")
        phantom = os.path.join(root, "docs", "panels", "hooks", "decisions", "run.sh")
        self.assertFalse(os.path.exists(phantom))
        self.assertIsNone(mb.thread_key(phantom))

    def test_a_deleted_file_stops_keying_while_its_live_sibling_still_keys(self):
        """The honest cost of the existence rule, stated: a file removed
        between the write and the beat loses its thread. That is the trade the
        docstring already argues for -- a visible non-grouping over an
        invisible mis-grouping -- and the sibling proves the rule is
        path-specific, not a blanket None."""
        root = self._repo("comms")
        gone = self._touch(root, "gone.md")
        stays = self._touch(root, "stays.md")
        self.assertEqual(mb.thread_key(gone), "doc:comms/gone.md")
        os.remove(gone)
        self.assertIsNone(mb.thread_key(gone))
        self.assertEqual(mb.thread_key(stays), "doc:comms/stays.md")

    def test_a_dangling_symlink_is_None_not_a_key_on_its_missing_target(self):
        root = self._repo("comms")
        link = os.path.join(root, "dangling.md")
        os.symlink(os.path.join(root, "no-such-target.md"), link)
        self.assertTrue(os.path.lexists(link))
        self.assertIsNone(mb.thread_key(link))


class TestThreadKeyContractPositiveControls(_ThreadKeyCase):
    """WITHOUT THESE, the tests above are satisfiable by `return None`.

    Each one asserts that a real, existing, absolute subject still produces the
    key it produced before this change -- i.e. the new guards inspect the
    inputs they claim to and nothing else."""

    def test_an_existing_absolute_file_still_keys(self):
        root = self._repo("comms")
        path = self._touch(root, "docs/reference/llm-ops.md")
        self.assertEqual(
            mb.thread_key(path), "doc:comms/docs/reference/llm-ops.md"
        )

    def test_an_existing_directory_still_keys(self):
        root = self._repo("comms")
        os.makedirs(os.path.join(root, "docs", "runbooks"), exist_ok=True)
        self.assertEqual(
            mb.thread_key(os.path.join(root, "docs", "runbooks")),
            "doc:comms/docs/runbooks",
        )

    def test_the_repo_root_itself_still_keys_on_the_repo_alone(self):
        root = self._repo("comms")
        self.assertEqual(mb.thread_key(root), "doc:comms")

    def test_a_live_symlink_still_resolves_to_its_targets_repo(self):
        root = self._repo("comms")
        target = self._touch(root, "docs/plan.md")
        link = os.path.join(self.tmp, "plan-link.md")
        os.symlink(target, link)
        self.assertEqual(mb.thread_key(link), "doc:comms/docs/plan.md")

    def test_outside_any_repo_is_still_None(self):
        path = os.path.join(self.tmp, "loose.md")
        with open(path, "w") as fh:
            fh.write("x\n")
        self.assertIsNone(mb.thread_key(path))

    def test_the_cwd_no_longer_changes_the_answer_for_an_absolute_path(self):
        """The invariant DEF-3 violated, stated directly: one document, one
        key, from anywhere. Same path, three cwds, one answer."""
        root = self._repo("comms")
        path = self._touch(root, "docs/a.md")
        other = self._repo("elsewhere")
        answers = set()
        for where in (root, os.path.join(root, "docs"), other, self.tmp):
            self._chdir(where)
            answers.add(mb.thread_key(path))
        self.assertEqual(answers, {"doc:comms/docs/a.md"})

    def test_a_NUL_in_the_path_still_raises_and_does_not_return_a_key(self):
        """tests/test_swarm_heartbeat.sh's (s) case drives an embedded NUL
        through the live hook and asserts the beat survives a RAISING
        thread_key. Keep the raise: silently returning None here would make
        that test's positive control assert nothing."""
        with self.assertRaises(ValueError):
            mb.thread_key("/tmp/nul\x00path.md")


if __name__ == "__main__":
    unittest.main()
