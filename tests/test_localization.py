import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_mod
from build_release import apply_delta, digest, make_delta, release_entries, write_zip
from validate_translation import ROOT, check_translation, read_json, unique_object


class TranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = read_json(ROOT / "locales/ru.json")
        cls.contract = read_json(ROOT / "locales/format.json")

    def test_current_translation(self):
        self.assertEqual(check_translation(self.rows, self.contract), [])

    def test_dropped_percentage_suffix(self):
        rows = {**self.rows, "PF_BUILDING_PROGRESS": "Готовность: %amount%"}
        self.assertTrue(any("PF_BUILDING_PROGRESS" in problem for problem in check_translation(rows, self.contract)))

    def test_missing_key_and_character_outside_font(self):
        rows = self.rows.copy()
        del rows["MM_CONTINUE"]
        rows["MM_NEWMISSION"] += " 🌕"
        problems = check_translation(rows, self.contract)
        self.assertTrue(any("MM_CONTINUE" in problem for problem in problems))
        self.assertTrue(any("U+1F315" in problem for problem in problems))

    def test_duplicate_keys(self):
        with self.assertRaises(ValueError):
            unique_object([("KEY", "строка"), ("KEY", "дубликат")])

    def test_mod_requires_source_hash_for_translated_rows(self):
        hashes = self.contract["source_sha256"].copy()
        del hashes["MM_NEWMISSION"]
        contract = {**self.contract, "source_sha256": hashes}
        self.assertTrue(any("SHA-256" in problem for problem in check_translation(self.rows, contract)))


class DeltaTests(unittest.TestCase):
    def test_round_trip(self):
        original = b"header" + bytes(range(256)) * 4
        after = b"new header" + original[41:] + b"new footer"
        self.assertEqual(apply_delta(original, make_delta(original, after), len(after)), after)

    def test_truncated_literal_rejected(self):
        patch = make_delta(b"", b"sample")
        with self.assertRaises(ValueError):
            apply_delta(b"", patch[:-1], 6)



class ReleaseTests(unittest.TestCase):
    def test_windows_bundle_preserves_loader_and_adds_license_material(self):
        loader_files = {
            "winhttp.dll": b"loader",
            "doorstop_config.ini": b"enabled = true",
            "BepInEx/core/BepInEx.dll": b"bepinex",
            "changelog.txt": b"changes",
        }
        with tempfile.TemporaryDirectory() as out:
            loader = Path(out) / "bepinex.zip"
            source = Path(out) / "doorstop-source.zip"
            with zipfile.ZipFile(loader, "w") as archive:
                for name, data in loader_files.items():
                    archive.writestr(name, data)
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(f"{build_mod.DOORSTOP_SOURCE_ROOT}/LICENSE", b"LGPL")

            with (
                patch.object(build_mod, "BEPINEX_SHA256", digest(loader.read_bytes())),
                patch.object(build_mod, "DOORSTOP_SOURCE_SHA256", digest(source.read_bytes())),
            ):
                files = build_mod.windows_bundle(loader, source)
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    build_mod.windows_bundle(loader, loader)

        self.assertEqual(files["winhttp.dll"], b"loader")
        self.assertEqual(files["doorstop_config.ini"], b"enabled = true")
        self.assertEqual(files["BepInEx/core/BepInEx.dll"], b"bepinex")
        self.assertEqual(files["ATC-RU-Licenses/UnityDoorstop-LICENSE.txt"], b"LGPL")
        self.assertIn("ATC-RU-Licenses/UnityDoorstop-4.5.0-source.zip", files)
        self.assertIn("ATC-RU-Licenses/THIRD-PARTY.txt", files)
        self.assertNotIn("changelog.txt", files)

    def test_previous_release_without_history(self):
        base = {"steamBuild": "1", "appId": "2", "files": [
            {"id": "01", "path": "data", "original": "old", "modified": "r2", "previous": []}
        ]}
        prior = {"steamBuild": "1", "appId": "2", "files": [
            {"id": "01", "path": "data", "original": "old", "modified": "r1"}
        ]}
        with tempfile.TemporaryDirectory() as out:
            old_zip = Path(out) / "r1.zip"
            with zipfile.ZipFile(old_zip, "w") as package:
                package.writestr("manifest.json", json.dumps(prior))
            entries = release_entries(base, {"01": b"r3"}, [old_zip])
        self.assertEqual(entries[0]["previous"], ["r1", "r2"])

    def test_checksum_conflict_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as out:
            target = Path(out) / "release.zip"
            sidecar = target.with_suffix(".zip.sha256")
            sidecar.write_text("original checksum", encoding="ascii")
            with self.assertRaises(FileExistsError):
                write_zip(target, {"data": b"hi"})
            self.assertEqual(sidecar.read_text(encoding="ascii"), "original checksum")
            self.assertFalse(target.exists())

if __name__ == "__main__":
    unittest.main()
