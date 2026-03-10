import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import requests

from .maa_python import (
    get_maa_paths,
    init_maa,
    maa_fight,
    maa_startup,
    run_all_tasks,
    run_all_tasks_result,
    shutdown_maa,
    sync_maa_config,
    wait_maa_task_finish,
)

logger = logging.getLogger(__name__)
maa_output_logger = logging.getLogger("MAA.output")
maa_path = Path(r"D:\software\maa_cli\maa.exe") if os.name == "nt" else Path("/root/redroid/maa")
inited = False


def init_maa_cli() -> None:
    global inited
    if not maa_path.exists():
        download_maa_cli()
    sync_maa_config(get_maa_paths())
    log_maa_cli_version()
    inited = True


def download_maa_cli() -> None:
    if os.name == "nt":
        raise RuntimeError("Auto download maa-cli only supports Linux for now.")

    response = requests.get("https://api.github.com/repos/MaaAssistantArknights/maa-cli/releases/latest")
    response.raise_for_status()
    data = response.json()
    logger.info("maa-cli latest release: %s", data["tag_name"])

    download_url = None
    filename = None
    for asset in data["assets"]:
        if "x86" in asset["name"] and "-linux" in asset["name"] and asset["name"].endswith("tar.gz"):
            download_url = asset["browser_download_url"]
            filename = asset["name"]
            break

    if download_url is None or filename is None:
        raise RuntimeError("Failed to find maa-cli download url")

    archive_path = maa_path.parent / filename
    maa_path.parent.mkdir(parents=True, exist_ok=True)
    download = requests.get(download_url)
    download.raise_for_status()
    archive_path.write_bytes(download.content)
    logger.info("maa-cli downloaded: %s", archive_path)

    temp_extract_path = maa_path.parent / ".maa_temp_extract"
    temp_extract_path.mkdir(exist_ok=True)
    with tarfile.open(archive_path) as tar:
        maa_member = next((member for member in tar.getmembers() if member.name.endswith("maa") and member.isfile()), None)
        if maa_member is None:
            raise RuntimeError("maa executable not found in tar file")
        tar.extract(maa_member, temp_extract_path)
        extracted_file = temp_extract_path / maa_member.name
        if maa_path.exists():
            maa_path.unlink()
        shutil.move(str(extracted_file), str(maa_path))

    shutil.rmtree(temp_extract_path, ignore_errors=True)
    archive_path.unlink(missing_ok=True)
    os.chmod(maa_path, 0o755)
    logger.info("maa-cli downloaded to %s", maa_path)
    subprocess.run([str(maa_path), "install"], check=False)
    log_maa_cli_version()


def log_maa_cli_version() -> None:
    if not maa_path.exists():
        return
    completed = subprocess.run([str(maa_path), "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    version = completed.stdout.strip().replace("\n", ", ")
    if version:
        logger.info("maa-cli version: %s", version)


def update_maa() -> None:
    if not maa_path.exists():
        download_maa_cli()
        return

    logger.info("Updating MAA core...")
    subprocess.run([str(maa_path), "self", "update", "beta"], check=False)
    completed = subprocess.run([str(maa_path), "update"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode != 0:
        logger.error("update maa-cli failed: %s", completed.stderr.strip() or completed.stdout.strip())


__all__ = [
    "download_maa_cli",
    "get_maa_paths",
    "init_maa",
    "init_maa_cli",
    "log_maa_cli_version",
    "maa_fight",
    "maa_path",
    "maa_startup",
    "run_all_tasks",
    "run_all_tasks_result",
    "shutdown_maa",
    "sync_maa_config",
    "update_maa",
    "wait_maa_task_finish",
]
