"""Local LLM configuration storage with restrictive file permissions (mode 600)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any

def read_saved_config(path: str | Path) -> dict[str, str]:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    if os.name != "nt":
        # dsh credentials-local fails closed on world-readable secrets.
        # Self-heal the mode first (a hand-edited file may carry the
        # creator's umask); only refuse to load when it stays exposed.
        try:
            if config_path.stat().st_mode & 0o077:
                os.chmod(config_path, 0o600)
                if config_path.stat().st_mode & 0o077:
                    return {}
        except OSError:
            return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, RecursionError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key, "")) for key in ("url", "model", "key") if data.get(key) is not None}

def _restrict_key_file(path: Path) -> None:
    """Best-effort owner-only permissions for the plaintext key file.

    mkstemp already creates 0600 on POSIX; Windows chmod is a no-op, so drop
    inherited ACL entries and grant the current user Full Control via icacls.
    Full Control matters: the grant also carries DELETE, and ``os.replace``
    cannot rename a file whose ACL allows only read/write (WinError 5).  The
    file stays private to this account, but remains replaceable by it.
    Failures are never fatal: the inherited user ACL still works.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    for account in _key_file_accounts():
        try:
            proc = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(F)"],
                capture_output=True, timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return
        if proc.returncode == 0:
            return

def _key_file_accounts() -> list[str]:
    """Candidate icacls principals: domain-qualified first, plain user second."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    accounts = [f"{domain}\\{user}" if domain and user else "", user]
    return [account for index, account in enumerate(accounts)
            if account and account not in accounts[:index]]

def _clear_readonly(path: Path) -> None:
    """Drop the Windows read-only attribute that blocks os.replace."""
    if os.name != "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & stat.S_IWRITE:
        return
    try:
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass

_REPLACE_RETRY_DELAYS = (0.05, 0.12, 0.25)

def _replace_with_retry(source: Path, target: Path) -> None:
    """Atomic replace with short retries for Windows sharing violations.

    Antivirus, the search indexer and cloud-sync filters open files briefly,
    and os.replace then reports WinError 5/32/33.  A restrictive ACL is not
    transient, so the last attempt surfaces the stage-tagged error instead of
    retrying forever; the caller's cleanup still unlinks the temp file.
    """
    healed = False
    for attempt, delay in enumerate(_REPLACE_RETRY_DELAYS):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not healed:
                # A file written by an older build may carry an (R,W)-only
                # ACL; grant this account Full Control once before retrying.
                healed = True
                _clear_readonly(target)
                if os.name == "nt" and target.exists():
                    _restrict_key_file(target)
            if attempt == len(_REPLACE_RETRY_DELAYS) - 1:
                raise ConfigWriteError("替换配置文件", target, error) from error
            time.sleep(delay)
        except OSError as error:
            raise ConfigWriteError("替换配置文件", target, error) from error

class ConfigWriteError(OSError):
    """A settings-file write failed at a named stage.

    The stage and the OS error code are what let the settings panel say
    "replace failed: Windows error 5" instead of a generic write failure; the
    original exception stays chained for the server log.
    """

    def __init__(self, stage: str, path: str | Path, cause: BaseException) -> None:
        winerror = getattr(cause, "winerror", None)
        errno_value = getattr(cause, "errno", None)
        detail = f"{stage}失败"
        if winerror is not None:
            detail += f"（Windows 错误 {winerror}）"
        elif errno_value is not None:
            detail += f"（错误 {errno_value}）"
        super().__init__(f"{detail}：{cause}")
        # OSError.__init__ clears winerror, so the copied codes go on after it.
        self.winerror = winerror
        self.errno_value = errno_value
        self.stage = stage
        self.path = str(path)
        self.cause = cause

def write_saved_config(path: str | Path, config: dict[str, str]) -> None:
    # dsh atomic-write pattern: a crash can never leave a half-written
    # config behind, and the key is only ever visible at its final path
    # with owner-only permissions already applied.
    config_path = Path(path)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigWriteError("创建配置目录", config_path.parent, error) from error
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".llm-config-", dir=str(config_path.parent))
    except OSError as error:
        raise ConfigWriteError("创建临时文件", config_path.parent, error) from error
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise ConfigWriteError("写入临时文件", tmp_path, error) from error
        _restrict_key_file(tmp_path)
        _clear_readonly(config_path)
        _replace_with_retry(tmp_path, config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
