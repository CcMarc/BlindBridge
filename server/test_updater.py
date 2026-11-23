"""Tests for updater.py. Run with: python3 -m unittest test_updater"""

from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import updater  # noqa: E402

GOOD = {
    "blindbridge/VERSION": "9.9.9\n",
    "blindbridge/server/server.py": "x = 1\n",
    "blindbridge/bridge/bridge.py": "y = 2\n",
}


def make(files: dict, links: tuple = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            info = zipfile.ZipInfo(name)
            if name in links:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, text)
    return buffer.getvalue()


class ReadTests(unittest.TestCase):
    def refused(self, files: dict, words: str, **kwargs) -> None:
        with self.assertRaises(updater.UpdateError) as caught:
            updater.read_update(make(files, **kwargs))
        self.assertIn(words, str(caught.exception))

    def test_a_plain_release_is_read(self):
        self.assertEqual(sorted(updater.read_update(make(GOOD))), ["VERSION", "bridge/bridge.py", "server/server.py"])

    def test_the_top_folder_may_have_any_name_but_there_must_be_one(self):
        renamed = {name.replace("blindbridge/", "BlindBridge-1.2.3/"): text for name, text in GOOD.items()}
        self.assertEqual(sorted(updater.read_update(make(renamed))), ["VERSION", "bridge/bridge.py", "server/server.py"])
        self.refused(dict(GOOD, **{"other/server/server.py": "z = 1\n"}), "single top folder")
        self.refused({"VERSION": "1\n", "server.py": "x = 1\n"}, "single top folder")

    def test_repository_files_are_installed_and_anything_else_is_ignored(self):
        extra = dict(GOOD, **{
            "blindbridge/README.md": "r", "blindbridge/CHANGELOG.md": "c", "blindbridge/LICENSE.md": "l",
            "blindbridge/SECURITY.md": "s", "blindbridge/.gitattributes": "a", "blindbridge/.gitignore": "i",
            "blindbridge/images/page.png": "png", "blindbridge/docs/PROTOCOL.md": "p",
            "blindbridge/notes.txt": "stray", "blindbridge/.github/FUNDING.yml": "f",
            "blindbridge/venv/bin/python": "env", "blindbridge/config.json": "top level, not a known file",
            "blindbridge/server/__pycache__/server.cpython-313.pyc": "cache",
        })
        files = updater.read_update(make(extra))
        self.assertEqual(sorted(files), [".gitattributes", ".gitignore", "CHANGELOG.md", "LICENSE.md", "README.md",
                                         "SECURITY.md", "VERSION", "bridge/bridge.py", "docs/PROTOCOL.md",
                                         "images/page.png", "server/server.py"])

    def test_paths_that_could_leave_the_install_folder_are_refused(self):
        for name in ("blindbridge/../evil.py", "blindbridge/server/../../evil.py", "/blindbridge/server/x.py",
                     "blindbridge\\server\\x.py", "blindbridge/./server/x.py"):
            self.refused(dict(GOOD, **{name: "x = 1\n"}), "unsafe path")

    def test_links_are_refused(self):
        self.refused(dict(GOOD, **{"blindbridge/server/link.py": "/etc/passwd"}), "contains a link",
                     links=("blindbridge/server/link.py",))

    def test_settings_keys_and_data_can_never_be_replaced(self):
        for name in ("server/config.json", "bridge/blinds.json", "bridge/bridge.json", "bridge/pending.json",
                     "bridge/stuck.txt", "bridge/wifi_off", "server/blindbridge.db", "server/blindbridge.db-wal",
                     "docs/deep/config.json"):
            self.refused(dict(GOOD, **{"blindbridge/" + name: "{}"}), "settings or data file")
        self.refused(dict(GOOD, **{"blindbridge/bridge/captures/evil.json": "{}"}), "data folder")

    def test_what_is_missing_or_broken_is_named(self):
        self.refused({k: v for k, v in GOOD.items() if not k.endswith("bridge.py")}, "missing bridge/bridge.py")
        self.refused(dict(GOOD, **{"blindbridge/server/server.py": "def broken(:\n"}), "server/server.py does not compile")
        self.refused(dict(GOOD, **{"blindbridge/tools/nul.py": "x = 1\x00\n"}), "does not compile")
        for version in ("", "1.0 beta", "v" * 21, "1.0\n2.0", "1.0/../x"):
            self.refused(dict(GOOD, **{"blindbridge/VERSION": version}), "bad VERSION")
        with self.assertRaises(updater.UpdateError):
            updater.read_update(b"this is not a zip")

    def test_size_limits(self):
        self.refused(dict(GOOD, **{"blindbridge/docs/big.md": "x" * (updater.MAX_FILE_BYTES + 1)}), "file too large")
        many = dict(GOOD, **{f"blindbridge/docs/{n}.md": "x" for n in range(updater.MAX_FILES)})
        self.refused(many, "too many files")
        chunks = dict(GOOD, **{f"blindbridge/docs/{n}.md": "x" * updater.MAX_FILE_BYTES for n in range(5)})
        self.refused(chunks, "zip is too large")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        for rel, text in {"VERSION": "1.0.0\n", "server/server.py": "old = 1\n", "server/config.json": "SECRET",
                          "bridge/bridge.py": "y = 2\n", "bridge/blinds.json": "KEYS"}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def test_only_what_changed_is_written_and_settings_stay(self):
        result = updater.apply_update(make(dict(GOOD, **{"blindbridge/docs/NEW.md": "n"})), self.root)
        self.assertEqual((result["from"], result["to"], result["changed"]),
                         ("1.0.0", "9.9.9", ["VERSION", "docs/NEW.md", "server/server.py"]))
        self.assertEqual((self.root / "server" / "server.py").read_text(), "x = 1\n")
        self.assertEqual(((self.root / "server" / "config.json").read_text(),
                          (self.root / "bridge" / "blinds.json").read_text()), ("SECRET", "KEYS"))
        self.assertEqual(updater.apply_update(make(dict(GOOD, **{"blindbridge/docs/NEW.md": "n"})), self.root)["changed"], [])
        self.assertFalse(list(self.root.rglob("*.new")))

    def test_a_refused_zip_changes_nothing(self):
        with self.assertRaises(updater.UpdateError):
            updater.apply_update(make(dict(GOOD, **{"blindbridge/server/config.json": "{}"})), self.root)
        self.assertEqual(((self.root / "VERSION").read_text(), (self.root / "server" / "server.py").read_text()),
                         ("1.0.0\n", "old = 1\n"))

    def test_nothing_is_written_through_a_link(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "docs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(updater.UpdateError) as caught:
            updater.apply_update(make(dict(GOOD, **{"blindbridge/docs/sub/NEW.md": "n"})), self.root)
        self.assertIn("through a link", str(caught.exception))
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "docs").unlink()
        victim = outside / "victim.txt"
        victim.write_text("untouched")
        (self.root / "server" / "server.py.new").symlink_to(victim)
        updater.apply_update(make(GOOD), self.root)
        self.assertEqual((victim.read_text(), (self.root / "server" / "server.py").read_text()), ("untouched", "x = 1\n"))
        (self.root / "server" / "server.py").unlink()
        (self.root / "server" / "server.py").symlink_to(victim)
        with self.assertRaises(updater.UpdateError):
            updater.apply_update(make(dict(GOOD, **{"blindbridge/server/server.py": "z = 3\n"})), self.root)
        self.assertEqual(victim.read_text(), "untouched")


class PackTests(unittest.TestCase):
    def test_an_install_packs_into_a_release_without_settings_keys_or_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wanted = {"VERSION": "1.0.0\n", "README.md": "r", "CHANGELOG.md": "c", "LICENSE.md": "l", "SECURITY.md": "s",
                      ".gitattributes": "a", ".gitignore": "i", "updater.py": "u = 1\n", "server/server.py": "x = 1\n",
                      "server/ui.html": "<html>", "bridge/bridge.py": "y = 2\n", "deploy/install.sh": "#!/bin/sh\n",
                      "docs/PROTOCOL.md": "p", "tools/make_release.py": "m = 1\n", "images/page.png": "png"}
            unwanted = {"server/config.json": "SECRET", "server/blindbridge.db": "DB", "bridge/blinds.json": "KEYS",
                        "bridge/bridge.json": "TOKEN", "bridge/pending.json": "[]", "bridge/stuck.txt": "s",
                        "bridge/wifi_off": "1", "bridge/captures/20260101-x-probe.json": "{}", "venv/bin/python": "env",
                        "server/__pycache__/server.cpython-313.pyc": "cache", "server/server.py.new": "half",
                        "notes.txt": "stray", ".git/config": "git"}
            for rel, text in {**wanted, **unwanted}.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            os.symlink(root / "server" / "config.json", root / "docs" / "link.md")
            data = updater.build_zip(root)
            names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
            self.assertEqual(names, sorted("blindbridge/" + rel for rel in wanted))
            self.assertEqual(sorted(updater.read_update(data)), sorted(wanted))


if __name__ == "__main__":
    unittest.main()
