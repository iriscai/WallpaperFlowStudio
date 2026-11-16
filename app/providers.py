"""Text and image calls. Empty credentials stay on the local draft path."""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.carry import FlowError

TEXT_TIMEOUT = 300
IMAGE_TIMEOUT = 600
DOWNLOAD_TIMEOUT = 300
MAX_REQUEST_ATTEMPTS = 3


class ProviderError(FlowError):
    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        detail = (code + " " + message).lower()
        quota = any(marker in detail for marker in (
            "insufficient_quota", "insufficient_user_quota", "insufficient_token_quota",
            "quota_exceeded", "余额不足", "额度不足", "配额不足", "额度已用尽",
            "insufficient balance", "insufficient credit", "insufficient funds",
            "quota is not enough", "quota has been exhausted", "exceeded your current quota",
        ))
        self.key_unavailable = quota or status in {401, 402, 403}


class KeyPool:
    """Share rejected credentials across concurrent jobs in one flow run."""

    def __init__(self, keys: list[str]):
        self.keys = tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))
        self._blocked: dict[int, ProviderError] = {}
        self._lock = threading.Lock()

    def call(self, operation, start_index: int = 0):
        if not self.keys:
            raise ProviderError("当前 API 配置还没有密钥。")
        for offset in range(len(self.keys)):
            index = (start_index + offset) % len(self.keys)
            with self._lock:
                if index in self._blocked:
                    continue
            try:
                return operation(self.keys[index])
            except ProviderError as exc:
                if not exc.key_unavailable:
                    raise
                with self._lock:
                    self._blocked[index] = exc
        with self._lock:
            last_error = str(next(reversed(self._blocked.values())))
        for key in self.keys:
            last_error = last_error.replace(key, "[已隐藏密钥]")
        raise ProviderError(
            f"已尝试当前 API 配置的全部 {len(self.keys)} 条密钥，"
            f"均因余额不足、密钥无效或无权限而不可用。请充值或更换密钥。最后错误：{last_error}"
        )

    def switch_message(self) -> str:
        with self._lock:
            count = len(self._blocked)
        return f" 已自动跳过 {count} 条余额不足、无效或无权限的密钥，切换其他密钥完成。" if count else ""


def _http_error(status: int, detail: str) -> ProviderError:
    code = ""
    try:
        data = json.loads(detail)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
    except (ValueError, TypeError):
        pass
    return ProviderError(f"模型接口返回 {status}：{detail[:500]}", status=status, code=code)


def join_api(base: str, suffix: str) -> str:
    root = (base or "").rstrip("/")
    if not root:
        raise ProviderError("模型配置里还没有接口地址")
    if root.endswith("/" + suffix) or root.endswith(suffix):
        return root
    return root + "/" + suffix


def _read_response(request: urllib.request.Request, timeout: int) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise _http_error(exc.code, detail) from exc


def _request_bytes(request: urllib.request.Request, timeout: int, stage: str) -> bytes:
    """Retry the individual request, including failures during response.read()."""
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            return _read_response(request, timeout)
        except ProviderError as exc:
            # Credential rejection belongs to KeyPool and should switch immediately.
            if exc.key_unavailable:
                raise
            if exc.status not in {429, 500, 502, 503, 504}:
                raise ProviderError(f"{stage}失败：{exc}", status=exc.status, code=exc.code) from exc
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise ProviderError(
                    f"{stage}失败，已尝试 {attempt} 次：{exc}", status=exc.status, code=exc.code,
                ) from exc
            time.sleep(8 * attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            timed_out = isinstance(reason, TimeoutError) or any(
                marker in str(reason).lower() for marker in ("timed out", "timeout")
            )
            if attempt == MAX_REQUEST_ATTEMPTS:
                if timed_out:
                    detail = f"读取超时（单次等待上限 {timeout:g} 秒，已尝试 {attempt} 次）。接口长时间未返回完整响应。"
                else:
                    detail = f"连接中断或响应不完整，已尝试 {attempt} 次：{reason}"
                raise ProviderError(stage + detail) from exc
            time.sleep(5 * attempt)


def _post_json(url: str, api_key: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    raw = _request_bytes(request, timeout, "生文接口")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError("生文接口返回的不是 JSON") from exc


def text_message_content(prompt: str, image_paths: list[str] | None = None) -> str | list[dict]:
    """Match GPTKeepOnline: text first, then each image as a data-URL image_url part."""
    paths = [str(item) for item in (image_paths or []) if str(item).strip()]
    if not paths:
        return prompt
    contents: list[dict] = [{"type": "text", "text": prompt}]
    for image_path in paths:
        file_path = Path(image_path)
        if not file_path.is_file():
            raise ProviderError(f"参考图文件不存在：{image_path}")
        mime_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
        encoded = base64.b64encode(file_path.read_bytes()).decode("utf-8")
        contents.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}})
    return contents


def chat(profile: dict, api_key: str, user_text: str, timeout: int = TEXT_TIMEOUT, system: str | None = "你是微信公众号编辑。只输出要求的成品，不要解释工作过程。", images: list[str] | None = None) -> str:
    payload = profile.get("payload") or {}
    model = (payload.get("model") or "").strip()
    if not model:
        raise ProviderError("文本模型还没有填写模型名")
    if not api_key:
        raise ProviderError("文本模型还没有密钥")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": text_message_content(user_text, images)})
    body = {
        "model": model,
        "messages": messages,
        "temperature": _temperature(payload.get("temperature")),
        "stream": False,
    }
    max_tokens = payload.get("maxTokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        body["max_tokens"] = max_tokens
    result = _post_json(join_api(payload.get("baseUrl") or "", "chat/completions"), api_key, body, timeout)
    content = (((result.get("choices") or [{}])[0].get("message") or {}).get("content"))
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        content = "\n".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("模型没有返回文本")
    return content.strip()


def image_request_body(model: str, prompt: str, width: int, height: int) -> dict:
    """画风生图的请求体：只含模型、提示词和配置尺寸，不带参考图。"""
    safe_width = max(1, int(width or 768))
    safe_height = max(1, int(height or 1024))
    return {"model": model, "prompt": prompt, "size": f"{safe_width}x{safe_height}"}


def extract_image_payload(data: dict) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    items = data.get("data")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        first = items[0]
        for key in ("url", "image_url"):
            if first.get(key):
                return "url", str(first[key])
        for key in ("b64_json", "image", "image_base64", "result"):
            if first.get(key):
                return "b64_json", str(first[key])
    for key in ("url", "image_url"):
        if data.get(key):
            return "url", str(data[key])
    for key in ("b64_json", "image", "image_base64", "result"):
        if data.get(key):
            return "b64_json", str(data[key])
    return "", ""


def generate_image(profile: dict, api_key: str, prompt: str, width: int, height: int, timeout: int = IMAGE_TIMEOUT) -> tuple[bytes, str]:
    payload = profile.get("payload") or {}
    model = (payload.get("model") or "").strip()
    if not model:
        raise ProviderError("生图模型还没有填写模型名")
    if not api_key:
        raise ProviderError("生图模型还没有密钥")
    body = image_request_body(model, prompt, width, height)
    url = join_api(payload.get("baseUrl") or "", "images/generations")
    result = _post_image_json(url, api_key, body, timeout)
    kind, value = extract_image_payload(result)
    if kind == "b64_json" and value:
        raw = value.strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        return base64.b64decode(raw), body["size"]
    if kind == "url" and value:
        return _download_image(value), body["size"]
    raise ProviderError("生图接口没有返回图片")


def _image_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def _post_image_json(url: str, api_key: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=_image_headers(api_key), method="POST",
    )
    raw = _request_bytes(request, timeout, "生图接口")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProviderError("生图接口返回的不是 JSON") from exc


def _download_image(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return _request_bytes(request, DOWNLOAD_TIMEOUT, "生成图片下载")


def _temperature(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.7
    return max(0.0, min(2.0, number))
