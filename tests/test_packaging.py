from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = (ROOT / "manifest.json").is_file()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fatfish  # noqa: E402
from fatfish.db import SCHEMA_PATH  # noqa: E402
from fatfish.lexicon.service import SEED_PATH  # noqa: E402


def _load_make_app():
    spec = importlib.util.spec_from_file_location(
        "fatfish_make_app", ROOT / "scripts" / "make_app.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/make_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_app = _load_make_app()

REQUIRED_FILES = (
    "fatfish/web/server.py",
    "web/index.html",
    "web/app.js",
    "web/style.css",
    "config/low.toml",
    "scripts/start_all.py",
    "启动大肥鱼.bat",
    "start.bat",
    "start.sh",
    "manifest.json",
    "VERSION",
    "LICENSE",
    "README.md",
    "docs/README.md",
    "docs/branches/ops.md",
    "data/stickers/.gitkeep",
)

# Runtime voice helpers are allowed even though voice experiment scripts are not.
RUNTIME_VOICE_FILES = {"scripts/cosyvoice_soft_server.py"}

# Cloud/home servers and the ops watchdog: they belong to the full bundle only
# (the one-click zip stays slim, see CLEAN_SCRIPT_FILES).  These were once added
# to scripts/ but not to SCRIPT_FILES, so ``build`` skipped them *silently* and
# the cloud kept running a stale hand-scp'd copy.
OPERATIONAL_SCRIPTS = (
    "napcat_supervisor.py",
    "napcat_watch.py",
    "sing_server.py",
    "photo_server.py",
    "memory_bundle.py",
)


class ScriptWhitelistTests(unittest.TestCase):
    def test_operational_scripts_are_whitelisted(self) -> None:
        for name in OPERATIONAL_SCRIPTS:
            self.assertIn(name, make_app.SCRIPT_FILES, f"not in SCRIPT_FILES: {name}")
            self.assertNotIn(
                name,
                make_app.CLEAN_SCRIPT_FILES,
                f"the one-click bundle must stay slim: {name}",
            )

    def test_whitelisted_scripts_exist_on_disk(self) -> None:
        # A typo here is invisible to ``build`` (it skips missing names), so the
        # list itself has to be checked against the working tree.
        for name in make_app.SCRIPT_FILES:
            self.assertTrue(
                (ROOT / "scripts" / name).is_file(), f"whitelisted but missing: {name}"
            )


@unittest.skipIf(PACKAGED, "running inside a packaged build")
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="fatfish-package-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.dest = cls.tmp / "fatfish-app"
        cls.result = make_app.build(cls.dest, zip_path=None)
        cls.files = sorted(
            path.relative_to(cls.dest).as_posix()
            for path in cls.dest.rglob("*")
            if path.is_file()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_required_files_exist(self) -> None:
        for relative in REQUIRED_FILES:
            self.assertTrue(
                (self.dest / relative).is_file(), f"missing packaged file: {relative}"
            )
        for name in ("stickers", "audio", "archive", "backups", "eval"):
            self.assertTrue(
                (self.dest / "data" / name / ".gitkeep").is_file(),
                f"missing data/{name}/.gitkeep",
            )
        self.assertIsNone(self.result["zip"])
        self.assertEqual(self.result["version"], make_app.read_version())

    def test_operational_scripts_are_packaged(self) -> None:
        for name in OPERATIONAL_SCRIPTS:
            relative = f"scripts/{name}"
            self.assertTrue(
                (self.dest / relative).is_file(), f"missing packaged file: {relative}"
            )

    def test_forbidden_content_absent(self) -> None:
        for relative in self.files:
            parts = relative.split("/")
            lowered = relative.lower()
            self.assertNotIn("__pycache__", parts, relative)
            self.assertNotIn(".pytest_cache", parts, relative)
            self.assertNotIn(".venv", parts, relative)
            for forbidden_dir in ("repos", "models", "downloads", "notes", "dist"):
                self.assertNotIn(forbidden_dir, parts, relative)
            self.assertNotEqual(parts[-1], ".env", relative)
            self.assertFalse(relative.endswith((".pyc", ".pyo")), relative)
            self.assertFalse(
                relative.endswith((".db", ".db-wal", ".db-shm")), relative
            )
            for prefix in ("voice_matrix", "ming_", "mimo_"):
                self.assertNotIn(prefix, lowered, relative)
            if lowered.startswith("scripts/cosyvoice_"):
                self.assertIn(relative, RUNTIME_VOICE_FILES, relative)
        data_files = [rel for rel in self.files if rel.startswith("data/")]
        self.assertTrue(data_files)
        for relative in data_files:
            self.assertTrue(relative.endswith("/.gitkeep"), relative)

    def test_manifest_hashes_and_flags(self) -> None:
        manifest = json.loads(
            (self.dest / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual("fatfish-app", manifest["name"])
        self.assertEqual(self.result["version"], manifest["version"])
        self.assertFalse(manifest["encrypted"])
        self.assertTrue(manifest["source_readable"])
        self.assertEqual(">=3.11", manifest["python"])
        self.assertEqual("low", manifest["profile_default"])
        self.assertEqual("fatfish/__main__.py", manifest["entry"])
        self.assertEqual("web/index.html", manifest["web_entry"])
        self.assertEqual(
            {"windows": "start.bat", "posix": "start.sh"}, manifest["start"]
        )
        self.assertEqual("启动大肥鱼.bat", manifest["one_click"]["windows"])
        self.assertEqual("scripts/start_all.py", manifest["one_click"]["script"])

        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(paths))
        self.assertGreater(len(paths), 40)
        actual = {
            relative
            for relative in self.files
            if relative != "manifest.json" and not relative.endswith(".zip")
        }
        self.assertEqual(set(paths), actual, "manifest must list every packaged file")
        for entry in entries:
            target = self.dest / entry["path"]
            self.assertTrue(target.is_file(), entry["path"])
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(digest, entry["sha256"], entry["path"])
            self.assertEqual(target.stat().st_size, entry["size"], entry["path"])

    def test_launch_scripts(self) -> None:
        bat = (self.dest / "start.bat").read_text(encoding="utf-8")
        self.assertIn("python -m fatfish serve --open", bat)
        self.assertIn('cd /d "%~dp0"', bat)
        self.assertIn("@echo off", bat)
        self.assertTrue((self.dest / "start_web.bat").is_file())

        sh = (self.dest / "start.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m fatfish serve", sh)
        self.assertIn('cd "$(dirname "$0")"', sh)
        self.assertIn("exec python3", sh)

        one_click = (self.dest / "启动大肥鱼.bat").read_text(encoding="utf-8")
        self.assertIn("@echo off", one_click)
        self.assertIn("chcp 65001", one_click)
        self.assertIn('cd /d "%~dp0"', one_click)
        self.assertIn("scripts\\start_all.py", one_click)
        self.assertIn("3.11", one_click)
        self.assertNotIn("runas", one_click.lower())

        version = (self.dest / "VERSION").read_text(encoding="utf-8")
        self.assertEqual(self.result["version"] + "\n", version)

    def test_zip_has_top_level_folder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fatfish-zip-test-") as tmp:
            zip_path = Path(tmp) / "fatfish-app.zip"
            result = make_app.build(
                Path(tmp) / "fatfish-app", zip_path=zip_path, clean=True
            )
            self.assertEqual(str(zip_path.resolve()), result["zip"])
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
            self.assertTrue(names)
            self.assertTrue(all(name.startswith("fatfish-app/") for name in names), names[:5])
            self.assertIn("fatfish-app/manifest.json", names)
            self.assertIn("fatfish-app/web/index.html", names)
            self.assertIn("fatfish-app/start.bat", names)
            self.assertFalse(any("__pycache__" in name for name in names))

    def test_verify_package_isolated(self) -> None:
        summary = make_app.verify_package(self.dest, run_tests=False)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(self.result["version"], summary["version"])
        self.assertEqual(
            {"doctor", "init", "server", "health", "static", "chat", "tick"},
            set(summary["checks"]),
        )
        for name, value in summary["checks"].items():
            self.assertIs(value, True, f"{name} failed")

    def test_clean_bundle_is_minimal_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fatfish-clean-test-") as tmp:
            dest = Path(tmp) / "fatfish-oneclick"
            result = make_app.build(dest, zip_path=None, clean_bundle=True)
            self.assertEqual("fatfish-oneclick", result["bundle"])

            for relative in (
                "fatfish/web/server.py",
                "web/index.html",
                "config/low.toml",
                "docs/README.md",
                "docs/branches/ops.md",
                "scripts/start_all.py",
                "启动大肥鱼.bat",
                "start.bat",
                "B站登录.bat",
                "QQ登录.bat",
                ".env.example",
                "requirements.txt",
                "README.md",
                "LICENSE",
                "manifest.json",
                "VERSION",
                "data/stickers/.gitkeep",
                "data/logs/.gitkeep",
                "data/run/.gitkeep",
            ):
                self.assertTrue((dest / relative).is_file(), f"missing {relative}")

            for absent in (
                "tests",
                "skills",
                "vendor",
                "models",
                "repos",
                "notes",
                "downloads",
                ".env",
                "scripts/make_app.py",
                "scripts/cosyvoice_matrix.py",
                "scripts/napcat_watch.py",
                "scripts/sing_server.py",
                "scripts/photo_server.py",
                "scripts/memory_bundle.py",
                "start.sh",
                "start_web.bat",
            ):
                self.assertFalse((dest / absent).exists(), f"should be absent: {absent}")

            manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("fatfish-oneclick", manifest["name"])
            self.assertEqual(
                {"windows": "start.bat", "posix": "scripts/start_all.py"},
                manifest["start"],
            )
            self.assertEqual("启动大肥鱼.bat", manifest["one_click"]["windows"])
            listed = {entry["path"] for entry in manifest["files"]}
            self.assertEqual(
                listed,
                {
                    path.relative_to(dest).as_posix()
                    for path in dest.rglob("*")
                    if path.is_file() and path.name != "manifest.json"
                },
            )

            summary = make_app.verify_package(dest, run_tests=True)
            self.assertTrue(summary["ok"], summary)
            self.assertNotIn("tests", summary["checks"])


# ---------------------------------------------------------------------------
# Python distribution (wheel/sdist) packaging: the files the *installed*
# package reads at runtime.  Nothing here builds or installs anything --
# ``scripts/build_pkg.py --check --smoke`` is the end-to-end proof.
# ---------------------------------------------------------------------------

PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class BuildSystemTests(unittest.TestCase):
    def test_build_system_declared(self) -> None:
        build = _pyproject().get("build-system")
        self.assertIsNotNone(build, "pyproject is missing [build-system]")
        self.assertEqual(build["build-backend"], "setuptools.build_meta")
        requires = [req.split(";")[0].strip() for req in build["requires"]]
        self.assertTrue(
            any(req.startswith("setuptools") for req in requires),
            f"setuptools missing from build requires: {requires}",
        )


class WheelVersionTests(unittest.TestCase):
    def test_single_source_matches_dunder_version(self) -> None:
        project = _pyproject()["project"]
        if "version" in project:
            # A static version is fine only while it still agrees.
            self.assertEqual(project["version"], fatfish.__version__)
            return
        self.assertIn(
            "version",
            project.get("dynamic", []),
            'project declares neither a static version nor dynamic = ["version"]',
        )
        attr = _pyproject()["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "fatfish.__version__")


class WheelPackageDataTests(unittest.TestCase):
    def setUp(self) -> None:
        declared = _pyproject()["tool"]["setuptools"]["package-data"]
        self.package_data = {
            str(package): list(patterns) for package, patterns in declared.items()
        }

    def _covered(self, package: str, filename: str) -> bool:
        return any(
            fnmatch.fnmatch(filename, pattern)
            for pattern in self.package_data.get(package, [])
        )

    def test_schema_sql_is_packaged(self) -> None:
        self.assertTrue(
            self._covered("fatfish", "schema.sql"),
            f"schema.sql not matched by package-data: {self.package_data}",
        )

    def test_lexicon_seed_is_packaged(self) -> None:
        self.assertTrue(
            self._covered("fatfish.lexicon", "seed.json"),
            f"lexicon/seed.json not matched by package-data: {self.package_data}",
        )

    def test_every_pattern_matches_a_real_file(self) -> None:
        package_dirs = {
            "fatfish": ROOT / "fatfish",
            "fatfish.lexicon": ROOT / "fatfish" / "lexicon",
        }
        for package, patterns in self.package_data.items():
            directory = package_dirs.get(package)
            self.assertIsNotNone(directory, f"package-data for unknown package {package}")
            names = [entry.name for entry in directory.iterdir() if entry.is_file()]
            for pattern in patterns:
                self.assertTrue(
                    any(fnmatch.fnmatch(name, pattern) for name in names),
                    f"{package}: pattern {pattern!r} matches nothing in {directory}",
                )


class WheelRuntimePathTests(unittest.TestCase):
    def test_schema_path_exists_and_is_not_empty(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), f"missing {SCHEMA_PATH}")
        self.assertTrue(SCHEMA_PATH.read_text(encoding="utf-8").strip())

    def test_seed_path_exists(self) -> None:
        self.assertTrue(SEED_PATH.is_file(), f"missing {SEED_PATH}")

    def test_runtime_paths_live_inside_the_package(self) -> None:
        package_dir = Path(fatfish.__file__).resolve().parent
        for path in (SCHEMA_PATH, SEED_PATH):
            self.assertTrue(path.resolve().is_relative_to(package_dir), path)


if __name__ == "__main__":
    unittest.main()
