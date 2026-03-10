from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from util.msg_sender import send_by_tg_bot

from .asst.asst import Asst
from .asst.utils import InstanceOptionType, Message

logger = logging.getLogger(__name__)
maa_output_logger = logging.getLogger("MAA.output")

PROJECT_ROOT = Path(__file__).resolve().parents[4]
REPO_CONFIG_ROOT = Path(__file__).resolve().parent / "cli_config" / "maa"
MAA_PYTHON_RUNTIME_ROOT = Path(
    os.environ.get("AH2_MAA_PYTHON_DIR") or PROJECT_ROOT / "var" / "maa-python"
)
MAA_OTA_SUMMARY_URL = "https://ota.maa.plus/MaaAssistantArknights/api/version/summary.json"
MAA_GITHUB_RELEASES_URLS = [
    "https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases",
    "https://api.github.com/repos/MaaAssistantArknights/MaaRelease/releases",
]
DEFAULT_MAA_CLI_LINUX_PATH = Path("/root/redroid/maa")
DEFAULT_MAA_CHANNEL = "beta"
DEFAULT_DEVICE = "127.0.0.1:5555"
DEFAULT_PROFILE_NAME = "default"
DEFAULT_TASK_FILE_NAME = "my_tasks"
DEFAULT_TIMEOUT = 3600

_active_session: "MaaSession | None" = None


@dataclass(frozen=True)
class MaaPaths:
    runtime_root: Path
    bundle_root: Path
    library_dir: Path
    resource_dir: Path
    config_dir: Path
    cache_dir: Path | None = None
    hot_update_dir: Path | None = None
    log_dir: Path | None = None
    download_dir: Path | None = None


@dataclass
class TaskRunResult:
    ok: bool = True
    started_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    stopped_tasks: list[str] = field(default_factory=list)
    task_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    connection_events: list[str] = field(default_factory=list)
    fight_result: dict[str, Any] = field(
        default_factory=lambda: {
            "stage_code": "",
            "times": 0,
            "total_drops": [],
            "error": False,
        }
    )

    def has_errors(self) -> bool:
        return not self.ok or bool(self.task_errors)

    def summary_text(self) -> str:
        lines: list[str] = []
        if self.completed_tasks:
            lines.append("Completed: " + ", ".join(self.completed_tasks))
        if self.stopped_tasks:
            lines.append("Stopped: " + ", ".join(self.stopped_tasks))
        if self.fight_result["stage_code"]:
            drops = ", ".join(
                f'{drop["name"]} x{drop["count"]}' for drop in self.fight_result["total_drops"]
            )
            lines.append(
                f'Fight {self.fight_result["stage_code"]} {self.fight_result["times"]} times'
                + (f", drops: {drops}" if drops else "")
            )
        if self.task_errors:
            lines.append("Errors: " + "; ".join(self.task_errors))
        if self.warnings:
            lines.append("Warnings: " + "; ".join(self.warnings))
        return "\n".join(lines) if lines else "MAA Python run finished."

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started_tasks": self.started_tasks,
            "completed_tasks": self.completed_tasks,
            "stopped_tasks": self.stopped_tasks,
            "task_errors": self.task_errors,
            "warnings": self.warnings,
            "connection_events": self.connection_events,
            "fight_result": self.fight_result,
            "summary": self.summary_text(),
        }


def _library_filename() -> str:
    if os.name == "nt":
        return "MaaCore.dll"
    if os.name == "posix" and "darwin" in os.uname().sysname.lower():
        return "libMaaCore.dylib"
    return "libMaaCore.so"


def _run_subprocess(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def _runtime_channel() -> str:
    channel = str(os.environ.get("AH2_MAA_PYTHON_CHANNEL") or DEFAULT_MAA_CHANNEL).strip().lower()
    channel_map = {
        "alpha": "alpha",
        "nightly": "alpha",
        "beta": "beta",
        "stable": "stable",
        "release": "stable",
    }
    try:
        return channel_map[channel]
    except KeyError as exc:
        raise ValueError(f"unsupported MAA channel: {channel}") from exc


def _requests_verify() -> bool | str:
    if str(os.environ.get("AH2_MAA_PYTHON_INSECURE") or "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("AH2_MAA_PYTHON_INSECURE is enabled, TLS verification is disabled for MAA runtime downloads")
        return False

    for env_name in ("AH2_MAA_PYTHON_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value

    return True


def _runtime_layout(runtime_root: Path) -> dict[str, Path]:
    bundle_root = runtime_root / "bundle"
    return {
        "runtime_root": runtime_root,
        "bundle_root": bundle_root,
        "library_dir": bundle_root,
        "resource_dir": bundle_root / "resource",
        "config_dir": runtime_root / "config",
        "cache_dir": runtime_root / "cache",
        "hot_update_dir": runtime_root / "cache" / "resource",
        "log_dir": runtime_root / "debug",
        "download_dir": runtime_root / "downloads",
    }


def _maa_cli_path() -> Path | None:
    env_value = os.environ.get("AH2_MAA_CLI_PATH") or os.environ.get("MAA_CLI_PATH")
    if env_value:
        candidate = Path(env_value).expanduser()
        return candidate if candidate.exists() else None

    if os.name == "nt":
        return None
    if DEFAULT_MAA_CLI_LINUX_PATH.exists():
        return DEFAULT_MAA_CLI_LINUX_PATH
    return None


def _query_maa_cli_dir(maa_cli_path: Path, name: str) -> Path:
    completed = _run_subprocess([str(maa_cli_path), "dir", name])
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip())
    raise RuntimeError(completed.stderr.strip() or f"failed to query maa-cli dir {name}")


def _discover_runtime_bundle(bundle_root: Path) -> tuple[Path, Path]:
    library_name = _library_filename()
    library_candidates = sorted(bundle_root.rglob(library_name))
    resource_candidates = sorted(path for path in bundle_root.rglob("resource") if path.is_dir())
    if not library_candidates or not resource_candidates:
        raise FileNotFoundError("maa runtime bundle is incomplete")

    for library_path in library_candidates:
        library_dir = library_path.parent
        for resource_dir in resource_candidates:
            if (
                resource_dir.parent == library_dir
                or resource_dir.parent in library_dir.parents
                or library_dir in resource_dir.parents
            ):
                return library_dir, resource_dir

    return library_candidates[0].parent, resource_candidates[0]


def _ensure_runtime_dirs(paths: MaaPaths) -> None:
    required_dirs = [
        paths.runtime_root,
        paths.bundle_root,
        paths.config_dir,
        paths.cache_dir,
        paths.hot_update_dir,
        paths.log_dir,
        paths.download_dir,
    ]
    for directory in required_dirs:
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)


def _current_runtime_state(paths: MaaPaths) -> dict[str, str]:
    library_file = paths.library_dir / _library_filename()
    return {
        "runtime_root": str(paths.runtime_root),
        "bundle_root": str(paths.bundle_root),
        "library_dir": str(paths.library_dir),
        "resource_dir": str(paths.resource_dir),
        "config_dir": str(paths.config_dir),
        "cache_dir": str(paths.cache_dir) if paths.cache_dir else "",
        "hot_update_dir": str(paths.hot_update_dir) if paths.hot_update_dir else "",
        "log_dir": str(paths.log_dir) if paths.log_dir else "",
        "download_dir": str(paths.download_dir) if paths.download_dir else "",
        "library_file": str(library_file),
        "library_exists": str(library_file.exists()).lower(),
        "resource_exists": str(paths.resource_dir.exists()).lower(),
        "channel": _runtime_channel(),
    }


def _runtime_metadata_path(paths: MaaPaths) -> Path:
    return paths.runtime_root / "runtime.json"


def _load_runtime_metadata(paths: MaaPaths) -> dict[str, Any]:
    metadata_path = _runtime_metadata_path(paths)
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read MAA runtime metadata from %s: %s", metadata_path, exc)
        return {}


def _write_runtime_metadata(paths: MaaPaths, runtime_state: dict[str, Any]) -> None:
    metadata_path = _runtime_metadata_path(paths)
    metadata_path.write_text(
        json.dumps(runtime_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _maa_release_target() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        if machine in {"aarch64", "arm64"}:
            return "linux-aarch64"
        return "linux-x86_64"
    if system == "Windows":
        if machine in {"amd64", "x86_64"}:
            return "win-x64"
        if machine in {"arm64", "aarch64"}:
            return "win-arm64"
    raise RuntimeError(f"unsupported platform for MAA Python runtime: {system}/{machine}")


def _select_runtime_asset(detail: dict[str, Any]) -> tuple[str, list[str]]:
    details = detail.get("details") or {}
    assets = details.get("assets") or detail.get("assets") or []
    target = _maa_release_target()
    for asset in assets:
        asset_name = str(asset.get("name") or "")
        if asset_name.endswith("AppImage"):
            continue
        if not asset_name.startswith("MAA-") or target not in asset_name:
            continue
        urls = [str(asset["browser_download_url"])]
        urls.extend(str(url) for url in asset.get("mirrors") or [])
        return asset_name, urls
    raise RuntimeError(f"failed to find MAA runtime asset for {target}")


def _fetch_ota_release_info(channel: str, verify: bool | str) -> tuple[str, dict[str, Any]]:
    response = requests.get(MAA_OTA_SUMMARY_URL, timeout=30, verify=verify)
    response.raise_for_status()
    summary = response.json()
    detail_url = summary[channel]["detail"]
    version = summary[channel]["version"]

    detail_response = requests.get(detail_url, timeout=30, verify=verify)
    detail_response.raise_for_status()
    return version, detail_response.json()


def _select_github_release(releases: list[dict[str, Any]], channel: str) -> dict[str, Any]:
    if channel == "stable":
        for release in releases:
            if not bool(release.get("prerelease")):
                return release
    elif channel == "beta":
        for release in releases:
            if "beta" in str(release.get("tag_name") or "").lower():
                return release
    else:
        for release in releases:
            tag_name = str(release.get("tag_name") or "").lower()
            if "alpha" in tag_name or "nightly" in tag_name:
                return release
    if releases:
        return releases[0]
    raise RuntimeError("no GitHub releases found for MAA runtime")


def _fetch_github_release_info(channel: str, verify: bool | str) -> tuple[str, dict[str, Any]]:
    headers = {"Accept": "application/vnd.github+json"}
    errors: list[str] = []
    for releases_url in MAA_GITHUB_RELEASES_URLS:
        try:
            response = requests.get(releases_url, timeout=30, verify=verify, headers=headers)
            response.raise_for_status()
            releases = response.json()
            release = _select_github_release(releases, channel)
            return str(release["tag_name"]), release
        except Exception as exc:
            errors.append(f"{releases_url}: {exc}")
    raise RuntimeError("failed to query GitHub MAA releases: " + "; ".join(errors))


def _fetch_runtime_release_info(verify: bool | str) -> tuple[str, dict[str, Any]]:
    channel = _runtime_channel()
    try:
        return _fetch_ota_release_info(channel, verify)
    except Exception as exc:
        logger.warning("Failed to query MAA OTA release metadata: %s", exc)
        return _fetch_github_release_info(channel, verify)


def _copy_runtime_from_cli(paths: MaaPaths) -> MaaPaths:
    maa_cli_path = _maa_cli_path()
    if maa_cli_path is None:
        raise RuntimeError("maa-cli fallback is unavailable because no local maa-cli executable was found")

    source_library_dir = _query_maa_cli_dir(maa_cli_path, "library")
    source_resource_dir = _query_maa_cli_dir(maa_cli_path, "resource")
    if not (source_library_dir / _library_filename()).exists() or not source_resource_dir.exists():
        logger.info("maa-cli runtime looks incomplete, running `maa install` before copying fallback assets")
        completed = _run_subprocess([str(maa_cli_path), "install"])
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "maa install failed")
        source_library_dir = _query_maa_cli_dir(maa_cli_path, "library")
        source_resource_dir = _query_maa_cli_dir(maa_cli_path, "resource")

    target_root = paths.bundle_root / "cli-fallback"
    target_library_dir = target_root / "lib"
    target_resource_dir = target_root / "resource"
    shutil.rmtree(target_root, ignore_errors=True)
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_library_dir, target_library_dir, dirs_exist_ok=True)
    shutil.copytree(source_resource_dir, target_resource_dir, dirs_exist_ok=True)

    resolved_paths = get_maa_paths()
    runtime_state = _current_runtime_state(resolved_paths)
    runtime_state.update(
        {
            "version": "local-cli-fallback",
            "asset_name": "copied-from-maa-cli",
            "source_url": str(maa_cli_path),
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_runtime_metadata(resolved_paths, runtime_state)
    return resolved_paths


def _download_file(urls: list[str], destination: Path) -> str:
    errors: list[str] = []
    verify = _requests_verify()
    for url in urls:
        try:
            with requests.get(url, stream=True, timeout=(10, 300), verify=verify) as response:
                response.raise_for_status()
                with open(destination, "wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            file.write(chunk)
            return url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            destination.unlink(missing_ok=True)
    raise RuntimeError("failed to download MAA runtime asset: " + "; ".join(errors))


def _extract_archive(archive_path: Path, destination: Path) -> None:
    temp_dir = destination.parent / ".bundle.tmp"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        if archive_path.name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as archive:
                archive.extractall(temp_dir)
        elif archive_path.name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(temp_dir)
        else:
            raise RuntimeError(f"unsupported MAA runtime archive: {archive_path.name}")

        shutil.rmtree(destination, ignore_errors=True)
        temp_dir.rename(destination)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def download_maa_python_runtime(force: bool = False, fallback_to_cli: bool = True) -> MaaPaths:
    paths = get_maa_paths()
    _ensure_runtime_dirs(paths)

    bundle_ready = (paths.library_dir / _library_filename()).exists() and paths.resource_dir.exists()
    if bundle_ready and not force:
        return paths

    logger.info("Preparing MAA Python runtime under %s", paths.runtime_root)
    verify = _requests_verify()
    try:
        version, release_detail = _fetch_runtime_release_info(verify)
        asset_name, download_urls = _select_runtime_asset(release_detail)
        archive_path = paths.download_dir / asset_name
        source_url = _download_file(download_urls, archive_path)
        logger.info("Downloaded MAA runtime %s from %s", asset_name, source_url)

        try:
            _extract_archive(archive_path, paths.bundle_root)
        finally:
            archive_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Failed to download official MAA runtime bundle: %s", exc)
        if fallback_to_cli:
            return _copy_runtime_from_cli(paths)
        raise

    resolved_paths = get_maa_paths()
    library_ready = (resolved_paths.library_dir / _library_filename()).exists()
    if not library_ready or not resolved_paths.resource_dir.exists():
        raise RuntimeError(
            "downloaded MAA runtime is incomplete: "
            f"library_dir={resolved_paths.library_dir}, resource_dir={resolved_paths.resource_dir}"
        )
    runtime_state = _current_runtime_state(resolved_paths)
    runtime_state.update(
        {
            "version": version,
            "asset_name": asset_name,
            "source_url": source_url,
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_runtime_metadata(resolved_paths, runtime_state)
    return resolved_paths


def describe_maa_python_runtime(paths: MaaPaths | None = None) -> dict[str, str]:
    if paths is None:
        paths = get_maa_paths()
    return _current_runtime_state(paths)


def get_maa_paths() -> MaaPaths:
    layout = _runtime_layout(MAA_PYTHON_RUNTIME_ROOT)
    library_dir = layout["library_dir"]
    resource_dir = layout["resource_dir"]
    bundle_root = layout["bundle_root"]
    if bundle_root.exists():
        try:
            library_dir, resource_dir = _discover_runtime_bundle(bundle_root)
        except FileNotFoundError:
            pass

    return MaaPaths(
        runtime_root=layout["runtime_root"],
        bundle_root=bundle_root,
        library_dir=library_dir,
        resource_dir=resource_dir,
        config_dir=layout["config_dir"],
        cache_dir=layout["cache_dir"],
        hot_update_dir=layout["hot_update_dir"],
        log_dir=layout["log_dir"],
        download_dir=layout["download_dir"],
    )


def _resource_load_root(resource_dir: Path) -> Path:
    return resource_dir.parent if resource_dir.name == "resource" else resource_dir


def _ensure_maa_core(paths: MaaPaths, force_download: bool = False) -> MaaPaths:
    _ensure_runtime_dirs(paths)
    library_file = paths.library_dir / _library_filename()
    if not force_download and library_file.exists() and paths.resource_dir.exists():
        return paths

    logger.info("MAA Python runtime missing or refresh requested, downloading official runtime bundle")
    return download_maa_python_runtime(force=force_download)


def _installed_runtime_version(paths: MaaPaths) -> str | None:
    runtime_state = _load_runtime_metadata(paths)
    version = str(runtime_state.get("version") or "").strip()
    return version or None


def _check_and_update_runtime(paths: MaaPaths) -> MaaPaths:
    current_version = _installed_runtime_version(paths)
    verify = _requests_verify()
    latest_version, _release_detail = _fetch_runtime_release_info(verify)

    if current_version == latest_version:
        logger.info("MAA Python runtime is up to date: %s", latest_version)
        return paths

    if current_version:
        logger.info("MAA Python runtime update available: %s -> %s", current_version, latest_version)
    else:
        logger.info("MAA Python runtime version is unknown, refreshing to %s", latest_version)

    updated_paths = download_maa_python_runtime(force=True, fallback_to_cli=False)
    logger.info("MAA Python runtime updated to %s", latest_version)
    return updated_paths


def sync_maa_config(paths: MaaPaths | None = None) -> Path:
    if paths is None:
        paths = get_maa_paths()
    _ensure_runtime_dirs(paths)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_CONFIG_ROOT, paths.config_dir, dirs_exist_ok=True)
    return paths.config_dir


def ensure_maa_python_runtime(force_download: bool = False, check_update: bool = False) -> MaaPaths:
    paths = get_maa_paths()
    sync_maa_config(paths)
    had_runtime = (paths.library_dir / _library_filename()).exists() and paths.resource_dir.exists()
    paths = _ensure_maa_core(paths, force_download=force_download)
    if check_update and had_runtime and not force_download:
        try:
            paths = _check_and_update_runtime(paths)
        except Exception as exc:
            logger.warning("Failed to check MAA Python runtime updates: %s", exc)
    return paths


def _parse_time_string(value: str) -> datetime_time:
    return datetime.strptime(value, "%H:%M:%S").time()


def _match_condition(condition: dict[str, Any], now: datetime) -> bool:
    if not condition:
        return True

    condition_type = condition.get("type")
    if condition_type == "Time":
        current = now.time()
        start = _parse_time_string(condition["start"]) if condition.get("start") else None
        end = _parse_time_string(condition["end"]) if condition.get("end") else None
        if start and end:
            if start <= end:
                return start <= current <= end
            return current >= start or current <= end
        if start:
            return current >= start
        if end:
            return current <= end
        return True
    if condition_type == "DateTime":
        start = datetime.fromisoformat(condition["start"]) if condition.get("start") else None
        end = datetime.fromisoformat(condition["end"]) if condition.get("end") else None
        if start and now < start:
            return False
        if end and now > end:
            return False
        return True
    if condition_type == "Weekday":
        weekdays = {str(day).lower()[:3] for day in condition.get("weekdays", [])}
        return now.strftime("%a").lower()[:3] in weekdays
    if condition_type == "And":
        return all(_match_condition(item, now) for item in condition.get("conditions", []))
    if condition_type == "Or":
        return any(_match_condition(item, now) for item in condition.get("conditions", []))

    logger.warning("Unsupported MAA task condition type: %s", condition_type)
    return False


def _resolve_task_params(task: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    if now is None:
        now = datetime.now()

    base_params = dict(task.get("params") or {})
    variants = task.get("variants") or []
    if not variants:
        return base_params

    matched_variants = [variant for variant in variants if _match_condition(variant.get("condition") or {}, now)]
    if not matched_variants:
        return None

    strategy = str(task.get("strategy") or "first").lower()
    if strategy == "merge":
        merged = dict(base_params)
        for variant in matched_variants:
            merged.update(variant.get("params") or {})
        return merged

    selected = matched_variants[0]
    merged = dict(base_params)
    merged.update(selected.get("params") or {})
    return merged


def load_maa_profile(
    profile_name: str = DEFAULT_PROFILE_NAME,
    paths: MaaPaths | None = None,
) -> dict[str, Any]:
    if paths is None:
        paths = get_maa_paths()
    sync_maa_config(paths)
    profile_path = paths.config_dir / "profiles" / f"{profile_name}.toml"
    with open(profile_path, "rb") as file:
        return tomllib.load(file)


def load_task_batch_from_config(
    task_file_name: str = DEFAULT_TASK_FILE_NAME,
    profile_name: str = DEFAULT_PROFILE_NAME,
    paths: MaaPaths | None = None,
) -> list[dict[str, Any]]:
    _ = profile_name
    if paths is None:
        paths = get_maa_paths()
    sync_maa_config(paths)
    task_path = paths.config_dir / "tasks" / f"{task_file_name}.toml"
    with open(task_path, "rb") as file:
        task_config = tomllib.load(file)

    task_batch: list[dict[str, Any]] = []
    now = datetime.now()
    for task in task_config.get("tasks", []):
        params = _resolve_task_params(task, now)
        if params is None:
            continue
        task_batch.append(
            {
                "name": task.get("name") or task.get("type"),
                "type": task["type"],
                "params": params,
            }
        )
    return task_batch


def _discover_devices(adb_path: str) -> list[str]:
    completed = _run_subprocess([adb_path, "devices"])
    if completed.returncode != 0:
        logger.warning("failed to enumerate adb devices: %s", completed.stderr.strip())
        return []
    devices: list[str] = []
    for line in completed.stdout.splitlines():
        if "\tdevice" not in line:
            continue
        serial, _, _status = line.partition("\t")
        serial = serial.strip()
        if serial:
            devices.append(serial)
    return devices


def _resolve_connection(profile: dict[str, Any]) -> tuple[str, str, str]:
    connection = dict(profile.get("connection") or {})
    adb_path = str(connection.get("adb_path") or "adb")
    configured_device = os.environ.get("MAA_DEVICE") or connection.get("device")
    config_name = str(connection.get("config") or "General")

    if configured_device:
        return adb_path, str(configured_device), config_name

    devices = _discover_devices(adb_path)
    if DEFAULT_DEVICE in devices:
        return adb_path, DEFAULT_DEVICE, config_name
    if len(devices) == 1:
        return adb_path, devices[0], config_name
    if devices:
        logger.warning("multiple adb devices detected, defaulting to %s", devices[0])
        return adb_path, devices[0], config_name

    logger.warning("no adb devices reported, falling back to %s", DEFAULT_DEVICE)
    return adb_path, DEFAULT_DEVICE, config_name


def _bool_to_option(value: bool) -> str:
    return "1" if value else "0"


class MaaCallbackBridge:
    def __init__(self) -> None:
        self.result = TaskRunResult()

        @Asst.CallBackType
        def _callback(msg: int, details: bytes | None, _arg: object) -> None:
            self.handle_callback(msg, details)

        self.callback = _callback

    def handle_callback(self, msg: int, payload: bytes | None) -> None:
        try:
            message = Message(msg)
        except ValueError:
            maa_output_logger.debug("[MAA] unhandled callback code=%s", msg)
            return

        details: dict[str, Any] = {}
        if payload:
            try:
                details = json.loads(payload.decode("utf-8"))
            except Exception:
                maa_output_logger.debug("[MAA] callback payload decode failed: %r", payload)

        if message == Message.ConnectionInfo:
            self._handle_connection_info(details)
        elif message == Message.TaskChainStart:
            task = str(details.get("taskchain") or "")
            if task:
                self.result.started_tasks.append(task)
                maa_output_logger.info("[MAA] %s started", task)
        elif message == Message.TaskChainCompleted:
            task = str(details.get("taskchain") or "")
            if task:
                self.result.completed_tasks.append(task)
                maa_output_logger.info("[MAA] %s completed", task)
        elif message == Message.TaskChainStopped:
            task = str(details.get("taskchain") or "")
            if task:
                self.result.stopped_tasks.append(task)
                maa_output_logger.warning("[MAA] %s stopped", task)
        elif message == Message.TaskChainError:
            task = str(details.get("taskchain") or "unknown")
            error_text = f"{task}: task chain error"
            self.result.ok = False
            self.result.task_errors.append(error_text)
            maa_output_logger.error("[MAA] %s", error_text)
        elif message in {Message.InternalError, Message.InitFailed}:
            error_text = json.dumps(details, ensure_ascii=False) if details else "internal error"
            self.result.ok = False
            self.result.task_errors.append(error_text)
            maa_output_logger.error("[MAA] %s", error_text)
        elif message == Message.SubTaskError:
            subtask = str(details.get("subtask") or "unknown")
            warning_text = f"{subtask}: subtask error"
            self.result.warnings.append(warning_text)
            maa_output_logger.warning("[MAA] %s", warning_text)
        elif message == Message.SubTaskExtraInfo:
            self._handle_subtask_extra(details)

    def _handle_connection_info(self, details: dict[str, Any]) -> None:
        what = str(details.get("what") or "")
        why = str(details.get("why") or "")
        event = f"{what}: {why}" if why else what
        if event:
            self.result.connection_events.append(event)
        if what in {"ConnectFailed", "Disconnect", "ScreencapFailed"}:
            self.result.warnings.append(event or "connection warning")
            maa_output_logger.warning("[MAA] %s", event)
        elif event:
            maa_output_logger.info("[MAA] %s", event)

    def _handle_subtask_extra(self, details: dict[str, Any]) -> None:
        what = str(details.get("what") or "")
        extra = details.get("details") or {}
        if what == "StageDrops":
            stage_info = extra.get("stage") or {}
            stats = extra.get("stats") or []
            total_drops = []
            for item in stats:
                name = item.get("itemName")
                quantity = item.get("quantity")
                if not name or quantity is None:
                    continue
                total_drops.append({"name": str(name), "count": int(quantity)})
            stage_code = str(stage_info.get("stageCode") or self.result.fight_result["stage_code"])
            current_times = int(extra.get("cur_times") or self.result.fight_result["times"] or 0)
            self.result.fight_result = {
                "stage_code": stage_code,
                "times": current_times,
                "total_drops": total_drops,
                "error": self.result.fight_result["error"],
            }
            drop_summary = ", ".join(f'{drop["name"]} x{drop["count"]}' for drop in total_drops) or "NoDrop"
            maa_output_logger.info("[MAA] %s total drops: %s", stage_code, drop_summary)
        elif what == "RecruitResult":
            recruit_details = extra
            if recruit_details.get("level") == 6:
                tags_choose = recruit_details.get("result", [{}])[0].get("tags", [])
                send_by_tg_bot("公招出 6 星了!", f"选择标签: {tags_choose}")


class MaaSession:
    def __init__(self, profile_name: str = DEFAULT_PROFILE_NAME) -> None:
        self.paths = get_maa_paths()
        sync_maa_config(self.paths)
        self.paths = _ensure_maa_core(self.paths)
        self.profile = load_maa_profile(profile_name, paths=self.paths)
        self.callback_bridge = MaaCallbackBridge()
        incremental_path = self.paths.hot_update_dir if self.paths.hot_update_dir and self.paths.hot_update_dir.exists() else None
        Asst.load(
            path=self.paths.library_dir,
            user_dir=self.paths.runtime_root,
            resource_path=_resource_load_root(self.paths.resource_dir),
            incremental_path=incremental_path,
        )
        self.asst = Asst(callback=self.callback_bridge.callback)
        self._configure_instance_options()
        self._connect()
        maa_output_logger.info("[MAA] connected to %s", self.device)

    def _configure_instance_options(self) -> None:
        instance_options = dict(self.profile.get("instance_options") or {})
        touch_mode = str(instance_options.get("touch_mode") or "MAATouch").lower()
        self.asst.set_instance_option(InstanceOptionType.touch_type, touch_mode)
        self.asst.set_instance_option(
            InstanceOptionType.deployment_with_pause,
            _bool_to_option(bool(instance_options.get("deployment_with_pause", False))),
        )
        self.asst.set_instance_option(
            InstanceOptionType.adblite_enabled,
            _bool_to_option(bool(instance_options.get("adb_lite_enabled", False))),
        )
        self.asst.set_instance_option(
            InstanceOptionType.kill_on_adb_exit,
            _bool_to_option(bool(instance_options.get("kill_adb_on_exit", False))),
        )

    def _connect(self) -> None:
        self.adb_path, self.device, self.connection_config = _resolve_connection(self.profile)
        if not self.asst.connect(self.adb_path, self.device, self.connection_config):
            raise RuntimeError(f"failed to connect MAA to device {self.device}")

    def append_task(self, task_type: str, params: dict[str, Any] | None = None) -> int:
        params = params or {}
        task_id = self.asst.append_task(task_type, params)
        if task_id <= 0:
            raise RuntimeError(f"failed to append MAA task {task_type}")
        return task_id

    def start(self) -> None:
        if not self.asst.start():
            raise RuntimeError("failed to start MAA task queue")

    def wait(self, timeout: int | None = DEFAULT_TIMEOUT) -> TaskRunResult:
        start_time = time.time()
        while self.asst.running():
            if timeout is not None and time.time() - start_time > timeout:
                self.callback_bridge.result.ok = False
                self.callback_bridge.result.task_errors.append(f"timeout after {timeout} seconds")
                raise TimeoutError(f"MAA task timed out after {timeout} seconds")
            time.sleep(0.5)
        return self.callback_bridge.result

    def close(self) -> None:
        if getattr(self, "asst", None) is None:
            return
        try:
            self.asst.stop()
        except Exception:
            logger.debug("failed to stop MAA session cleanly", exc_info=True)
        finally:
            asst = self.asst
            self.asst = None
            del asst


def run_task_batch(task_batch: list[dict[str, Any]], timeout: int = DEFAULT_TIMEOUT) -> TaskRunResult:
    if not task_batch:
        return TaskRunResult()

    session = MaaSession()
    try:
        for task in task_batch:
            session.append_task(task["type"], dict(task.get("params") or {}))
        session.start()
        result = session.wait(timeout)
        if result.has_errors():
            raise RuntimeError(result.summary_text())
        return result
    finally:
        session.close()


def run_all_tasks_result(timeout: int = DEFAULT_TIMEOUT) -> TaskRunResult:
    paths = ensure_maa_python_runtime(check_update=True)
    return run_task_batch(load_task_batch_from_config(paths=paths), timeout=timeout)


def run_all_tasks() -> str:
    return run_all_tasks_result(timeout=DEFAULT_TIMEOUT).summary_text()


def init_maa() -> Asst:
    global _active_session
    shutdown_maa()
    _active_session = MaaSession()
    return _active_session.asst


def wait_maa_task_finish(timeout_seconds: int = 1200) -> TaskRunResult:
    global _active_session
    if _active_session is None:
        raise RuntimeError("maa session not started")
    try:
        result = _active_session.wait(timeout_seconds if timeout_seconds > 0 else None)
        if result.has_errors():
            raise RuntimeError(result.summary_text())
        return result
    finally:
        shutdown_maa()


def shutdown_maa() -> None:
    global _active_session
    if _active_session is None:
        return
    _active_session.close()
    _active_session = None


def _normalize_stage_code(stage_code: str | None) -> str | None:
    if not stage_code:
        return stage_code
    stage_code = stage_code.upper()
    if not all(char.isalnum() or char == "-" for char in stage_code):
        raise ValueError("Invalid stage code")
    if stage_code.startswith("SS-") and stage_code[3:].isdigit():
        return "AveMujica-" + stage_code[3:]
    return stage_code


def maa_fight(stage_code: str | None, times: int | None = None, expiring_medicine: int | None = None, timeout: int = 1800) -> dict[str, Any]:
    params: dict[str, Any] = {}
    normalized_stage = _normalize_stage_code(stage_code)
    if normalized_stage:
        params["stage"] = normalized_stage

    if times is not None:
        params["times"] = times
        if times >= 1:
            params["series"] = 0
    else:
        params["series"] = 0

    if expiring_medicine is None:
        now = datetime.now().astimezone(tz=timezone(timedelta(hours=4)))
        if now.weekday() in {5, 6}:
            expiring_medicine = 100
    if expiring_medicine:
        params["expiring_medicine"] = expiring_medicine

    result = run_task_batch([{"type": "Fight", "params": params}], timeout=timeout)
    fight_result = dict(result.fight_result)
    if not fight_result["stage_code"] and normalized_stage:
        fight_result["stage_code"] = normalized_stage
    if times == 0 and fight_result["times"] == 0:
        time.sleep(1)
    return fight_result


def maa_startup(timeout: int = 120, client_type: str = "Official", **kwargs: Any) -> str:
    params: dict[str, Any] = {
        "client_type": client_type,
        "start_game_enabled": True,
    }
    params.update(kwargs)
    result = run_task_batch([{"type": "StartUp", "params": params}], timeout=timeout)
    return result.summary_text()


def maa_infrast(asst: Asst) -> int:
    logger.info("add maa infrast task...")
    return asst.append_task(
        "Infrast",
        {
            "facility": ["Training", "Trade", "Reception", "Mfg", "Control", "Power", "Office", "Dorm"],
            "drones": "Money",
            "replenish": True,
            "continue_training": True,
        },
    )


def maa_award(asst: Asst) -> int:
    logger.info("add maa award task...")
    return asst.append_task(
        "Award",
        {
            "award": True,
            "mail": True,
            "recruit": True,
            "orundum": True,
            "mining": True,
            "specialaccess": True,
        },
    )


def maa_mall(asst: Asst) -> int:
    logger.info("add maa mall task...")
    return asst.append_task(
        "Mall",
        {
            "shopping": True,
            "buy_first": ["招聘许可", "龙门币"],
            "blacklist": ["加急许可", "家具零件"],
        },
    )


def maa_recruit(asst: Asst) -> int:
    logger.info("add maa recruit task...")
    return asst.append_task(
        "Recruit",
        {
            "refresh": True,
            "select": [5, 4, 1],
            "confirm": [5, 4, 3, 1],
            "times": 4,
        },
    )


def maa_rouge_like(theme: str) -> None:
    global _active_session
    shutdown_maa()
    _active_session = MaaSession()
    _active_session.append_task("Roguelike", {"theme": theme})
    _active_session.start()


atexit.register(shutdown_maa)
