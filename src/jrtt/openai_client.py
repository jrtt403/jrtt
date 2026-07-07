from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_BIGMODEL_MODEL = "glm-4.7-flash"
ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


class OpenAIConfigError(RuntimeError):
    pass


class OpenAIRequestError(RuntimeError):
    pass


def get_api_key() -> str:
    load_local_env()
    api_key = (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("BIGMODEL_API_KEY", "").strip()
    )
    if not api_key:
        raise OpenAIConfigError(
            "OPENAI_API_KEY is not set. Put it in .env or run: export OPENAI_API_KEY='your_api_key'"
        )
    return api_key


def configured_model() -> str:
    load_local_env()
    configured = os.environ.get("OPENAI_MODEL", "").strip()
    if configured:
        return configured
    if "bigmodel.cn" in configured_base_url():
        return DEFAULT_BIGMODEL_MODEL
    return DEFAULT_OPENAI_MODEL


def configured_base_url() -> str:
    load_local_env()
    return os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")


def load_local_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def generate_text(instructions: str, user_input: str, max_output_tokens: int = 2500) -> str:
    base_url = configured_base_url()
    if "api.openai.com" in base_url:
        return generate_responses_text(instructions, user_input, max_output_tokens)
    return generate_chat_text(instructions, user_input, max_output_tokens)


def generate_responses_text(
    instructions: str, user_input: str, max_output_tokens: int = 2500
) -> str:
    api_url = f"{configured_base_url()}/responses"
    payload = {
        "model": configured_model(),
        "instructions": instructions,
        "input": user_input,
        "max_output_tokens": max_output_tokens,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "jrtt-ai-creator-mvp/0.1",
        },
        method="POST",
    )
    try:
        with open_url(request, 180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OpenAIRequestError(f"OpenAI API request failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise OpenAIRequestError(f"OpenAI API request failed: {exc}") from exc
    except socket.timeout as exc:
        raise OpenAIRequestError("OpenAI API request timed out. Try a smaller --max-output-tokens value.") from exc

    text = extract_output_text(data)
    if not text:
        raise OpenAIRequestError("OpenAI API response did not include text output.")
    return text.strip()


def generate_chat_text(
    instructions: str, user_input: str, max_output_tokens: int = 2500
) -> str:
    api_url = f"{configured_base_url()}/chat/completions"
    payload = {
        "model": configured_model(),
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_input},
        ],
        "max_tokens": max_output_tokens,
        "temperature": 0.4,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "jrtt-ai-creator-mvp/0.1",
        },
        method="POST",
    )
    try:
        with open_url(request, 180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OpenAIRequestError(f"Chat API request failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise OpenAIRequestError(f"Chat API request failed: {exc}") from exc
    except socket.timeout as exc:
        raise OpenAIRequestError("Chat API request timed out. Try a smaller --max-output-tokens value.") from exc

    text = extract_chat_text(data)
    if not text:
        raise OpenAIRequestError("Chat API response did not include text output.")
    return text.strip()


def extract_output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks)


def open_url(request: urllib.request.Request, timeout: int):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context)


def extract_chat_text(data: dict) -> str:
    choices = data.get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "\n".join(chunks)
    return ""
