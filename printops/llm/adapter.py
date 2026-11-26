"""Compatibility facade for the PrintOps LLM adapter package (stdlib only).

Internal responsibilities live in focused modules under ``printops/llm/``:
- ``config_store.py``: restrictive (mode 600) local config persistence
- ``transport.py``: SSRF guard, redirect blocking, bounded HTTP JSON transport
- ``budget.py``: context clipping, bounded tool ledger, request byte budget
- ``protocol.py``: native tool_calls + JSON envelope parsing & prompt version anchor
- ``probe.py``: connectivity pre-probe and concurrent capability probing
- ``plan_validator.py``: strict allowlist validation of model plans
- ``planner.py``: OpenAICompatiblePlanner facade
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from printops.runtime.deadline import deadline_sleep
from printops.llm.config_store import (
    ConfigWriteError,
    _REPLACE_RETRY_DELAYS,
    _clear_readonly,
    _key_file_accounts,
    _replace_with_retry,
    _restrict_key_file,
    read_saved_config,
    write_saved_config,
)
from printops.llm.planner import OpenAICompatiblePlanner
from printops.llm.protocol import PLANNER_PROMPT_VERSION
from printops.llm.transport import (
    _NoRedirect,
    _close_error_response,
    _reject_private_host,
    _system_proxy_hint,
    normalize_base_url,
)

__all__ = [
    "ConfigWriteError",
    "OpenAICompatiblePlanner",
    "PLANNER_PROMPT_VERSION",
    "_NoRedirect",
    "deadline_sleep",
    "normalize_base_url",
    "read_saved_config",
    "write_saved_config",
]
