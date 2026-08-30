"""Install and start a pinned local ClamAV engine for the Daki container."""

from __future__ import annotations

import getpass
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Final

from owaua import config

_VERSION: Final = "1.5.4"
_PACKAGE_URL: Final = (
    "https://github.com/Cisco-Talos/clamav/releases/download/"
    f"clamav-{_VERSION}/clamav-{_VERSION}.linux.x86_64.deb"
)
_PACKAGE_SHA256: Final = "28d6efc5b4423e7830c3559339552eb53870a9eac51ac4efb37d60530d329886"
_PACKAGE_BYTES: Final = 105_819_396
_DOWNLOAD_LIMIT: Final = 110 * 1024 * 1024


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _runtime_env() -> dict[str, str]:
    environment = os.environ.copy()
    libraries = [
        config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "lib",
        config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "lib64",
    ]
    values = [str(path) for path in libraries if path.is_dir()]
    if environment.get("LD_LIBRARY_PATH"):
        values.append(environment["LD_LIBRARY_PATH"])
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(values)
    environment["CVD_CERTS_DIR"] = str(
        (config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "etc" / "certs").absolute()
    )
    return environment


def _download_package(destination: Path) -> None:
    partial = destination.with_suffix(".part")
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(  # noqa: S310
        _PACKAGE_URL,
        headers={"Accept": "application/octet-stream", "User-Agent": "owaua-clamav-setup/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            if response.geturl().split(":", 1)[0].lower() != "https":
                raise RuntimeError("ClamAV package redirected away from HTTPS")
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _DOWNLOAD_LIMIT:
                        raise RuntimeError("ClamAV package exceeded its download limit")
                    digest.update(chunk)
                    handle.write(chunk)
        if received != _PACKAGE_BYTES or digest.hexdigest() != _PACKAGE_SHA256:
            raise RuntimeError("ClamAV package checksum or size did not match the pinned release")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def _install_engine() -> None:
    binary = config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "bin" / "clamscan"
    if binary.is_file() and os.access(binary, os.X_OK):
        return
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("the pinned ClamAV package requires Linux x86_64")
    extractor = shutil.which("dpkg-deb")
    if extractor is None:
        raise RuntimeError("dpkg-deb is required to extract the pinned ClamAV package")
    root = config.MALWARE_CLAMAV_ROOT.parent
    root.mkdir(parents=True, exist_ok=True)
    package = root / f"clamav-{_VERSION}.deb"
    _download_package(package)
    config.MALWARE_CLAMAV_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(  # noqa: S603
            [extractor, "--extract", str(package), str(config.MALWARE_CLAMAV_ROOT)],
            check=True,
            timeout=180,
        )
    finally:
        package.unlink(missing_ok=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("ClamAV package extraction did not produce clamscan")


def _write_configuration() -> None:
    database = config.MALWARE_DATABASE_DIR.absolute()
    database.mkdir(parents=True, exist_ok=True)
    root = config.MALWARE_CLAMAV_ROOT.parent.absolute()
    certs = (config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "etc" / "certs").absolute()
    user = getpass.getuser()
    _write_private(
        config.MALWARE_FRESHCLAM_CONFIG,
        "\n".join(
            [
                f"DatabaseDirectory {database}",
                f"CVDCertsDirectory {certs}",
                "DatabaseMirror database.clamav.net",
                f"DatabaseOwner {user}",
                f"UpdateLogFile {root / 'freshclam.log'}",
                "LogTime yes",
                "Checks 4",
                "Bytecode yes",
                "",
            ]
        ),
    )
    _write_private(
        config.MALWARE_CLAMD_CONFIG,
        "\n".join(
            [
                f"DatabaseDirectory {database}",
                f"CVDCertsDirectory {certs}",
                f"LocalSocket {root / 'clamd.sock'}",
                f"PidFile {root / 'clamd.pid'}",
                f"LogFile {root / 'clamd.log'}",
                "LogTime yes",
                "Foreground yes",
                "MaxFileSize 100M",
                "MaxScanSize 100M",
                "StreamMaxLength 100M",
                "MaxRecursion 16",
                "MaxFiles 10000",
                "ReadTimeout 120",
                "CommandReadTimeout 30",
                "Bytecode yes",
                "ScanArchive yes",
                "AlertEncryptedArchive yes",
                "",
            ]
        ),
    )


def _update_database() -> None:
    freshclam = config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "bin" / "freshclam"
    command = [
        str(freshclam),
        f"--config-file={config.MALWARE_FRESHCLAM_CONFIG.absolute()}",
        f"--datadir={config.MALWARE_DATABASE_DIR.absolute()}",
    ]
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        timeout=15 * 60,
        env=_runtime_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    databases = list(config.MALWARE_DATABASE_DIR.glob("*.cvd")) + list(
        config.MALWARE_DATABASE_DIR.glob("*.cld")
    )
    if not databases:
        detail = completed.stdout.decode("utf-8", errors="replace")[-500:].strip()
        raise RuntimeError(
            "freshclam failed before any signature database was available"
            + (f": {detail}" if detail else "")
        )
    if completed.returncode not in {0, 1}:
        print(
            "[malware] freshclam reported an update error; keeping the existing database",
            file=sys.stderr,
        )


def _daemon_healthy() -> bool:
    clamdscan = config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "bin" / "clamdscan"
    result = subprocess.run(  # noqa: S603
        [
            str(clamdscan),
            f"--config-file={config.MALWARE_CLAMD_CONFIG.absolute()}",
            "--ping",
            "1",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        env=_runtime_env(),
    )
    return result.returncode == 0


def _start_daemon() -> None:
    if _daemon_healthy():
        return
    root = config.MALWARE_CLAMAV_ROOT.parent.absolute()
    socket_path = root / "clamd.sock"
    socket_path.unlink(missing_ok=True)
    clamd = config.MALWARE_CLAMAV_ROOT / "usr" / "local" / "sbin" / "clamd"
    log_path = root / "clamd-console.log"
    log_handle = log_path.open("ab")
    subprocess.Popen(  # noqa: S603
        [str(clamd), f"--config-file={config.MALWARE_CLAMD_CONFIG.absolute()}"],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
        env=_runtime_env(),
    )
    log_handle.close()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _daemon_healthy():
            return
        time.sleep(1)
    raise RuntimeError("clamd did not become ready within 90 seconds")


def main() -> int:
    try:
        _install_engine()
        _write_configuration()
        _update_database()
        _start_daemon()
    except Exception as error:  # noqa: BLE001
        print(f"[malware] ClamAV setup failed: {error}", file=sys.stderr)
        return 1
    print(f"[malware] ClamAV {_VERSION} installed, updated, and running locally")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
