from __future__ import annotations

import importlib.machinery
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

DEPLOY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "deploy"
LOADER = importlib.machinery.SourceFileLoader(
    "owaua_deploy_test_module", str(DEPLOY_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
if SPEC is None:  # pragma: no cover - importlib invariant
    raise RuntimeError("could not load deployment script")
deploy_script = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(deploy_script)


class DeploymentValidationTests(unittest.TestCase):
    def test_legacy_private_deployment_state_is_moved_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            canonical = root / "owaua"
            legacy.mkdir()
            (legacy / "config.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(deploy_script, "LEGACY_CONFIG_DIR", legacy),
                mock.patch.object(deploy_script, "CONFIG_DIR", canonical),
            ):
                deploy_script.migrate_legacy_config_dir()
            self.assertFalse(legacy.exists())
            self.assertEqual(
                (canonical / "config.json").read_text(encoding="utf-8"), "{}"
            )

    def test_rebrand_server_operations_are_narrowly_allowlisted(self) -> None:
        client = deploy_script.DakiClient(
            "https://portal.daki.cc", "ptlc_test", "server_123"
        )
        with mock.patch.object(client, "request") as request:
            client.update_startup_variable("SECOND_CMD", "PYTHONPATH=src python -m owaua.bot")
            client.rename_server("owaua")
        self.assertEqual(request.call_count, 2)
        with self.assertRaises(deploy_script.DeployError):
            client.update_startup_variable("TOKEN", "unsafe")
        with self.assertRaises(deploy_script.DeployError):
            client.rename_server("unexpected")

    def test_panel_must_be_https_and_cannot_contain_credentials(self) -> None:
        normalized, server_id, key = deploy_script.normalize_panel(
            "https://portal.daki.cc/server/server_123",
            deploy_script.PANEL_URL,
        )
        self.assertEqual(normalized, "https://portal.daki.cc")
        self.assertEqual(server_id, "server_123")
        self.assertIsNone(key)

        rejected = (
            "http://portal.daki.cc",
            "https://user:password@portal.daki.cc",
            "https://portal.daki.cc?token=secret",
            "https://portal.daki.cc/unknown/path",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(
                deploy_script.DeployError
            ):
                deploy_script.normalize_panel(value, deploy_script.PANEL_URL)

    def test_insecure_local_panel_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(deploy_script.DeployError):
            deploy_script.normalize_panel(
                "http://127.0.0.1:8080", deploy_script.PANEL_URL
            )
        normalized, _, _ = deploy_script.normalize_panel(
            "http://127.0.0.1:8080",
            deploy_script.PANEL_URL,
            allow_insecure_localhost=True,
        )
        self.assertEqual(normalized, "http://127.0.0.1:8080")

    def test_remote_file_allowlist_cannot_be_broadened_by_saved_state(self) -> None:
        self.assertEqual(
            deploy_script.validate_deployable_path("src/owaua/web.py"),
            "src/owaua/web.py",
        )
        self.assertEqual(
            deploy_script.validate_deployable_path("requirements.txt"),
            "requirements.txt",
        )
        self.assertEqual(
            deploy_script.validate_deployable_path("requirements.lock"),
            "requirements.lock",
        )
        for value in (
            ".env",
            "README.md",
            "src/owaua/token.txt",
            "src//owaua/web.py",
            "../outside.py",
        ):
            with self.subTest(value=value), self.assertRaises(
                deploy_script.DeployError
            ):
                deploy_script.validate_deployable_path(value)

    def test_noninteractive_mutation_requires_yes(self) -> None:
        config = {
            "server_id": "server_123",
            "server_name": "Production",
        }
        with (
            mock.patch.object(deploy_script.sys.stdin, "isatty", return_value=False),
            self.assertRaises(deploy_script.DeployError),
        ):
            deploy_script.confirm_deployment(
                config,
                ["requirements.txt"],
                [],
                restart=False,
            )
        deploy_script.confirm_deployment(
            config,
            ["requirements.txt"],
            [],
            restart=False,
            assume_yes=True,
        )

    def test_setup_without_changes_or_restart_performs_no_remote_write(self) -> None:
        class FakeClient:
            instances = []

            def __init__(self, *_args):
                self.calls = []
                self.instances.append(self)

            def __getattr__(self, name):
                def record(*_args, **_kwargs):
                    self.calls.append(name)

                return record

        args = types.SimpleNamespace(
            full=False,
            dry_run=False,
            restart=False,
            setup=True,
            skip_checks=False,
            allow_insecure_localhost=False,
            yes=False,
            no_restart=True,
        )
        manifest = {"requirements.txt": "digest"}
        state = {"server_id": "server_123", "files": manifest}
        config = {
            "panel_url": "https://portal.daki.cc",
            "api_key": "ptlc_test",
            "server_id": "server_123",
            "server_name": "Production",
        }
        with (
            mock.patch.object(deploy_script, "snapshot", return_value=manifest),
            mock.patch.object(deploy_script, "load_json", return_value=state),
            mock.patch.object(deploy_script, "load_config", return_value=config),
            mock.patch.object(deploy_script, "DakiClient", FakeClient),
        ):
            deploy_script.deploy(args)

        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].calls, [])

    def test_websites_project_is_accepted(self) -> None:
        with mock.patch.object(
            deploy_script.sys, "argv", ["deploy", "websites", "--dry-run"]
        ):
            args = deploy_script.parse_args()
        self.assertEqual(args.project, "websites")
        self.assertTrue(args.dry_run)

    def test_github_action_accepts_a_commit_message(self) -> None:
        with mock.patch.object(
            deploy_script.sys,
            "argv",
            ["deploy", "owaua", "github", "add release command"],
        ):
            args = deploy_script.parse_args()
        self.assertEqual(args.action, "github")
        self.assertEqual(args.commit_message, "add release command")

    def test_github_commit_stages_commits_and_pushes_current_branch(self) -> None:
        branch = types.SimpleNamespace(returncode=0, stdout="main\n")
        staged = types.SimpleNamespace(returncode=1)
        with mock.patch.object(
            deploy_script.subprocess,
            "run",
            side_effect=[branch, None, staged, None, None],
        ) as run, mock.patch.object(
            deploy_script.shutil, "which", return_value="/usr/bin/git"
        ) as git:
            deploy_script.github_commit("release command")
        git.assert_called_once_with("git")
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/git", "symbolic-ref", "--quiet", "--short", "HEAD"])
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/git", "add", "--all"])
        self.assertEqual(run.call_args_list[3].args[0], ["/usr/bin/git", "commit", "-m", "release command"])
        self.assertEqual(run.call_args_list[4].args[0], ["/usr/bin/git", "push", "origin", "main"])

    def test_github_commit_rejects_multiline_message(self) -> None:
        with self.assertRaises(deploy_script.DeployError):
            deploy_script.github_commit("bad\nmessage")

    def test_assemble_sites_flattens_kozzyx_pages_and_promotes_wearegays_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            website = Path(directory) / "website"
            (website / "kozzyx.org" / "pages").mkdir(parents=True)
            (website / "kozzyx.org" / "css").mkdir()
            (website / "kirmy.org").mkdir()
            (website / "wearegays.net").mkdir()
            (website / "wearegays.net" / "owaua").mkdir()
            (website / "wearedevsstatus").mkdir()
            (website / "social").mkdir()
            (website / "kozzyx.org" / "pages" / "index.html").write_text(
                "kozzyx", encoding="utf-8"
            )
            (website / "kozzyx.org" / "css" / "theme.css").write_text(
                "body{}", encoding="utf-8"
            )
            (website / "kirmy.org" / "index.html").write_text("kirmy", encoding="utf-8")
            (website / "wearegays.net" / "multi.html").write_text(
                "wag", encoding="utf-8"
            )
            (website / "wearegays.net" / "owaua" / "index.html").write_text(
                "owaua guide", encoding="utf-8"
            )
            (website / "wearegays.net" / "femsec").mkdir()
            (website / "wearegays.net" / "femsec" / "index.html").write_text(
                "files", encoding="utf-8"
            )
            (website / "wearedevsstatus" / "index.html").write_text(
                "status", encoding="utf-8"
            )
            (website / "social" / "index.html").write_text("social", encoding="utf-8")
            assembled = Path(directory) / "assembled"
            with mock.patch.object(deploy_script, "WEBSITE_ROOT", website):
                deploy_script.assemble_sites(assembled)
            self.assertEqual(
                (assembled / "kozzyx" / "index.html").read_text(encoding="utf-8"),
                "kozzyx",
            )
            self.assertTrue((assembled / "kozzyx" / "css" / "theme.css").is_file())
            self.assertEqual(
                (assembled / "wearegays" / "index.html").read_text(encoding="utf-8"),
                "owaua guide",
            )
            self.assertTrue((assembled / "wearegays" / "status" / "index.html").is_file())
            self.assertTrue((assembled / "kirmy" / "social" / "index.html").is_file())
            self.assertEqual(
                (assembled / "femsec" / "index.html").read_text(encoding="utf-8"),
                "files",
            )
            archive = Path(directory) / "sites-bundle.zip"
            deploy_script.build_sites_archive(assembled, archive)
            self.assertGreater(archive.stat().st_size, 0)
            self.assertTrue(deploy_script.website_digest(archive))


if __name__ == "__main__":
    unittest.main()
