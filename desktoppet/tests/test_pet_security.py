import importlib.util
import json
import os
import stat
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PET_PATH = Path(__file__).resolve().parents[1] / "pet.py"
SPEC = importlib.util.spec_from_file_location("owaua_test_module", PET_PATH)
pet = importlib.util.module_from_spec(typing.cast(typing.Any, SPEC))
typing.cast(typing.Any, typing.cast(typing.Any, SPEC).loader).exec_module(pet)


class SafeMathTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(pet.safe_calculate("12 * 8"), 96)
        self.assertEqual(pet.safe_calculate("2 ** 10"), 1024)
        self.assertEqual(pet.safe_calculate("(7 + 5) / 3"), 4)
        self.assertEqual(pet.safe_calculate("11 % 4"), 3)

    def test_code_and_unsafe_numbers_are_rejected(self):
        rejected = (
            "__import__('os').system('id')",
            "(1, 2)",
            "2 ** 100",
            "1000000000000000000 + 1",
            "9 ** 9 ** 9",
            "0 ** -1",
            "1 / 0",
        )
        for expression in rejected:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                pet.safe_calculate(expression)


class EndpointTests(unittest.TestCase):
    def test_public_https_endpoint_is_normalized(self):
        self.assertEqual(
            pet.validate_api_base_url("https://api.groq.com/openai/v1/"),
            "https://api.groq.com/openai/v1",
        )

    def test_unsafe_endpoints_are_rejected(self):
        rejected = (
            "http://api.example.com/v1",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?token=secret",
            "https://127.0.0.1/v1",
            "https://169.254.169.254/latest/meta-data",
            "https://service.internal/v1",
            "https://2130706433/v1",
            "https://0177.0.0.1/v1",
            "https://0x7f.0x0.0x0.0x1/v1",
            "https://single-label/v1",
            "https://-bad.example/v1",
        )
        for endpoint in rejected:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                pet.validate_api_base_url(endpoint)

    def test_local_development_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            pet.validate_api_base_url("http://localhost:8080/v1")
        self.assertEqual(
            pet.validate_api_base_url("http://localhost:8080/v1", allow_insecure_local=True),
            "http://localhost:8080/v1",
        )
        with self.assertRaises(ValueError):
            pet.validate_api_base_url(
                "http://169.254.169.254/latest/meta-data",
                allow_insecure_local=True,
            )
        with self.assertRaises(ValueError):
            pet.validate_api_base_url(
                "http://192.168.1.10/v1",
                allow_insecure_local=True,
            )

    def test_provider_values_cannot_be_mixed(self):
        brain = pet.Brain(
            {"OWAUA_AI_KEY": "secret", "GROQ_API_KEY": "other"},
            dict(pet.DEFAULT_SETTINGS),
        )
        self.assertEqual(brain.provider, "offline")
        self.assertTrue(brain.configuration_error)

        brain = pet.Brain(
            {"GROQ_API_KEY": "secret", "DEEPSEEK_BASE_URL": "https://wrong.example/v1"},
            dict(pet.DEFAULT_SETTINGS),
        )
        self.assertEqual(brain.provider, "groq")
        self.assertEqual(brain.base_url, "https://api.groq.com/openai/v1")


class ConfigurationTests(unittest.TestCase):
    def test_settings_are_schema_validated_and_bounded(self):
        settings, mood = pet.validate_settings(
            {
                "name": "\x00  A" + "b" * 80,
                "pace": "Ludicrous",
                "tts": "yes",
                "ai": False,
                "unknown": "discard me",
            },
            {"hunger": 500, "happiness": -2, "energy": float("nan")},
        )
        self.assertLessEqual(len(settings["name"]), 32)
        self.assertNotIn("\x00", settings["name"])
        self.assertEqual(settings["pace"], "Normal")
        self.assertIs(settings["tts"], True)
        self.assertIs(settings["ai"], False)
        self.assertNotIn("unknown", settings)
        self.assertEqual(mood["hunger"], 100)
        self.assertEqual(mood["happiness"], 0)
        self.assertEqual(mood["energy"], 80)

    def test_save_is_atomic_private_and_excludes_internal_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / ".owaua"
            config_file = config_dir / "config.json"
            with (
                mock.patch.object(pet, "CONFIG_DIR", config_dir),
                mock.patch.object(pet, "CONFIG_FILE", config_file),
            ):
                pet.save_settings(
                    {**pet.DEFAULT_SETTINGS, "_hunger": 99, "unknown": "secret"},
                    pet.DEFAULT_MOOD,
                )

            payload = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], pet.CONFIG_VERSION)
            self.assertNotIn("_hunger", payload["settings"])
            self.assertNotIn("unknown", payload["settings"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(list(config_dir.glob("*.tmp")), [])

    def test_env_parser_uses_allowlist_and_hardens_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "GROQ_API_KEY=secret\nUNRELATED_SECRET=do-not-load\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                env_file.chmod(0o644)
            values = pet.load_env_file(env_file)
            self.assertEqual(values, {"GROQ_API_KEY": "secret"})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_pre_rename_private_state_and_key_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_dir = root / ("." + "sef" + "pet")
            canonical_dir = root / ".owaua"
            legacy_dir.mkdir()
            legacy_key = "SEF" + "PET_AI_KEY"
            (legacy_dir / ".env").write_text(f"{legacy_key}=secret\n", encoding="utf-8")
            fake_keyring = mock.Mock()
            fake_keyring.get_password.side_effect = [None, "keyring-secret"]
            with (
                mock.patch.object(pet.Path, "home", return_value=root),
                mock.patch.object(pet, "CONFIG_DIR", canonical_dir),
                mock.patch.dict(sys.modules, {"keyring": fake_keyring}),
            ):
                pet._migrate_legacy_user_state()
            self.assertFalse(legacy_dir.exists())
            self.assertEqual(
                (canonical_dir / ".env").read_text(encoding="utf-8"),
                "OWAUA_AI_KEY=secret\n",
            )
            fake_keyring.set_password.assert_called_once_with(
                pet.KEYRING_SERVICE, "OWAUA_AI_KEY", "keyring-secret"
            )
            fake_keyring.delete_password.assert_called_once()

    def test_corrupt_settings_are_preserved_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / ".owaua"
            config_dir.mkdir()
            config_file = config_dir / "config.json"
            config_file.write_text("{not-json", encoding="utf-8")
            with (
                mock.patch.object(pet, "CONFIG_DIR", config_dir),
                mock.patch.object(pet, "CONFIG_FILE", config_file),
            ):
                settings, mood = pet.load_settings()
            self.assertEqual(settings, pet.DEFAULT_SETTINGS)
            self.assertEqual(mood, {key: float(value) for key, value in pet.DEFAULT_MOOD.items()})
            self.assertFalse(config_file.exists())
            self.assertEqual(
                (config_dir / "config.corrupt.json").read_text(encoding="utf-8"),
                "{not-json",
            )

    def test_symlinked_private_files_are_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "actual.env"
            target.write_text("GROQ_API_KEY=secret\n", encoding="utf-8")
            link = root / ".env"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            self.assertEqual(pet.load_env_file(link), {})


class ResourceAndProcessTests(unittest.TestCase):
    def test_working_directory_cannot_override_sprite(self):
        with tempfile.TemporaryDirectory() as directory:
            attacker_dir = Path(directory) / "attacker"
            custom_dir = Path(directory) / "approved"
            attacker_dir.mkdir()
            custom_dir.mkdir()
            (attacker_dir / "pet.png").write_bytes(b"not really an image")
            with (
                mock.patch.object(pet, "CUSTOM_SPRITE_DIR", custom_dir),
                mock.patch("os.getcwd", return_value=str(attacker_dir)),
                mock.patch.object(Path, "cwd", return_value=attacker_dir),
            ):
                self.assertIsNone(pet.resource_path("pet.png"))
                self.assertIsNone(pet.resource_path("../pet.png"))

    def test_tts_text_is_not_interpolated_into_powershell(self):
        hostile = "'); Start-Process calc; #"
        self.assertNotIn(hostile, pet.WINDOWS_TTS_SCRIPT)
        self.assertEqual(pet._clean_spoken_text(hostile), hostile)

    def test_child_environment_excludes_provider_secrets(self):
        with mock.patch.dict(
            os.environ,
            {"PATH": "/bin", "GROQ_API_KEY": "secret", "OWAUA_AI_KEY": "secret2"},
            clear=True,
        ):
            child_env = pet._subprocess_environment()
        self.assertEqual(child_env, {"PATH": "/bin"})

    def test_brain_reads_live_mood(self):
        settings = {**pet.DEFAULT_SETTINGS, "ai": False}
        mood = {**pet.DEFAULT_MOOD, "hunger": 90}
        brain = pet.Brain({}, settings, mood)
        self.assertIn("hungry", brain._offline("how are you").lower())


class HeadlessWindowTests(unittest.TestCase):
    def test_window_starts_tracks_a_screen_and_shuts_down(self):
        app = pet.QApplication.instance() or pet.QApplication([])
        settings, mood = pet.validate_settings()
        with mock.patch.object(pet, "save_settings", return_value=True):
            window = pet.PetWindow(settings, mood, {})
            app.processEvents()
            self.assertIsNotNone(window.screen)
            self.assertFalse(window.tray_available)
            window._quit()
            app.processEvents()
            self.assertTrue(window._quitting)
        window.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
