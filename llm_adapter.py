"""Optional OpenAI-compatible planner.

The deterministic Agent remains the fallback when no model is configured or
when a provider is temporarily unavailable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def normalize_base_url(value: str) -> str:
    """Validate an OpenAI-compatible HTTP endpoint without exposing credentials."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口 URL 必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise ValueError("接口 URL 不应包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("接口 URL 不应包含查询参数或片段")
    return value


def read_saved_config(path: str | Path) -> dict[str, str]:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {key: str(data.get(key, "")) for key in ("url", "model", "key") if data.get(key) is not None}


def write_saved_config(path: str | Path, config: dict[str, str]) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


class OpenAICompatiblePlanner:
    PATCH_FIELDS = {
        "productType", "purpose", "quantity", "size", "pages", "orientation", "paper",
        "printing", "finishing", "binding", "deadline", "budget", "platform", "productSpecs",
    }

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: int = 20) -> None:
        try:
            safe_url = normalize_base_url(base_url)
            config_error = ""
        except ValueError:
            safe_url = ""
            config_error = "接口 URL 格式不正确"
        self.base_url, self.api_key, self.model, self.timeout = safe_url, (api_key or "").strip(), (model or "").strip(), timeout
        self.last_error = config_error

    @classmethod
    def from_env(cls) -> "OpenAICompatiblePlanner":
        return cls(os.getenv("PRINTOPS_LLM_URL", ""), os.getenv("PRINTOPS_LLM_KEY", ""), os.getenv("PRINTOPS_LLM_MODEL", ""))

    def configure(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = normalize_base_url(base_url)
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self.last_error = ""

    def public_config(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": self.base_url, "model": self.model,
                "keyConfigured": bool(self.api_key), "lastError": self.last_error}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def plan(self, text: str, order: dict[str, Any], tools: list[dict[str, Any]],
             history: list[dict[str, str]] | None = None,
             tool_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        prompt = {"role": "system", "content": "你是印刷订单助手，使用简洁自然的中文和非专业用户对话。每次只输出一个 JSON 对象，不要 Markdown 代码围栏：{reply:string,patch:object,tool:{name:string,arguments:object}|null}。reply 是给用户看的自然语言，必须在需要时提出下一步问题；patch 只能使用订单字段 productType,purpose,quantity,size,pages,orientation,paper,printing,finishing,binding,deadline,budget,platform,productSpecs；品类专属参数必须放在 productSpecs 对象中（例如 folding、paperParts、boxSize、boxStructure、labelMaterial、labelShape、bagSize、handle、cupVolume、displayMaterial、install、boardThickness）。工具只能从提供的白名单中选择，只有信息足够或用户明确要求时调用工具。收到 toolResult 时，优先解释工具结果并给出下一步，不要重复调用同一个工具。不要编造价格、供应商能力或已提交订单。"}
        messages = [prompt]
        for item in (history or [])[-12:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") if item.get("role") in {"user", "assistant"} else "user"
            content = str(item.get("text", "")).strip()
            if content:
                messages.append({"role": role, "content": content})
        user_payload: dict[str, Any] = {"text": text, "order": order, "tools": tools}
        if tool_result is not None:
            user_payload["toolResult"] = tool_result
        messages.append({"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)})
        payload = {"model": self.model, "messages": messages}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        self.last_error = ""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = self._response_text(result)
            plan = self._parse_plan(content)
            plan = self.validate_plan(plan, {item.get("name") for item in tools if isinstance(item, dict)})
            if plan is None:
                self.last_error = "模型返回内容无法识别"
            return plan
        except HTTPError as error:
            self.last_error = f"模型接口返回 HTTP {error.code}"
        except (URLError, TimeoutError, OSError):
            self.last_error = "模型接口连接失败"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "模型接口返回格式异常"
        return None

    def test_connection(self, base_url: str | None = None, model: str | None = None,
                        api_key: str | None = None) -> dict[str, Any]:
        """Check a provider without sending order data or exposing the key."""
        target_url = self.base_url if base_url is None else normalize_base_url(base_url)
        target_model = self.model if model is None else (model or "").strip()
        target_key = self.api_key if api_key is None else (api_key or "").strip()
        if not (target_url and target_model):
            return {"ok": False, "message": "模型接口尚未配置", "latencyMs": None}
        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 4,
        }
        request = urllib.request.Request(
            f"{target_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {target_key}"} if target_key else {})},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not self._response_text(result).strip():
                self.last_error = "模型接口返回空内容"
                return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}
            self.last_error = ""
            return {"ok": True, "message": "模型接口连接正常", "latencyMs": self._latency(started)}
        except HTTPError as error:
            self.last_error = f"模型接口返回 HTTP {error.code}"
        except (URLError, TimeoutError, OSError):
            self.last_error = "模型接口连接失败"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "模型接口返回格式异常"
        return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}

    @staticmethod
    def _latency(started: float) -> int:
        return round((time.monotonic() - started) * 1000)

    @staticmethod
    def _response_text(result: Any) -> str:
        """Read text from common Chat Completions and Responses-compatible shapes."""
        if not isinstance(result, dict):
            return ""
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content", choice.get("text", ""))
            if isinstance(content, list):
                return "".join(
                    str(part.get("text", "")) for part in content
                    if isinstance(part, dict) and part.get("type", "text") in {"text", "output_text"}
                )
            return str(content or "")
        if result.get("output_text"):
            return str(result["output_text"])
        return ""

    @staticmethod
    def _parse_plan(content: str) -> dict[str, Any] | None:
        text = (content or "").strip()
        if not text:
            return None
        # Models occasionally ignore the no-fence instruction; remove only the
        # surrounding fence and then accept the first valid JSON object.
        if text.startswith("```"):
            text = text[3:]
            if text.startswith("json"):
                text = text[4:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        # A plain-language answer is still useful. It should not discard the
        # model entirely just because it did not follow the JSON instruction.
        return {"reply": text, "patch": {}, "tool": None}

    @classmethod
    def validate_plan(cls, value: Any, tool_names: set[str] | None = None) -> dict[str, Any] | None:
        """Normalize the model contract before it can mutate order state or call a tool."""
        if not isinstance(value, dict):
            return None
        reply = value.get("reply")
        reply = str(reply).strip() if reply is not None else ""
        if len(reply) > 4000:
            reply = reply[:4000].rstrip() + "..."
        raw_patch = value.get("patch")
        patch: dict[str, Any] = {}
        if isinstance(raw_patch, dict):
            for key, item in raw_patch.items():
                if key not in cls.PATCH_FIELDS or item is None:
                    continue
                if key == "productSpecs":
                    if not isinstance(item, dict):
                        continue
                    specs = {str(name): str(spec).strip() for name, spec in item.items()
                             if spec is not None and str(spec).strip()}
                    if specs:
                        patch[key] = specs
                elif isinstance(item, (str, int, float, bool)):
                    text = str(item).strip()
                    if text:
                        patch[key] = text
        raw_tool = value.get("tool")
        tool: dict[str, Any] | None = None
        if isinstance(raw_tool, dict):
            name = raw_tool.get("name")
            arguments = raw_tool.get("arguments", {})
            if isinstance(name, str) and name in (tool_names or set()) and isinstance(arguments, dict):
                tool = {"name": name, "arguments": arguments}
        if not reply and not patch and tool is None:
            return None
        return {"reply": reply, "patch": patch, "tool": tool}
