# python_a2a.py —— 轻量 A2A 客户端（兼容多种桥返回格式）
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import requests
import threading

class MessageRole:
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    AGENT = "agent"

@dataclass
class TextContent:
    text: str

@dataclass
class Message:
    role: str
    content: TextContent
    conversation_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    parent_message_id: Optional[str] = None
    message_id: Optional[str] = None

@dataclass
class _A2AResponse:
    content: TextContent
    conversation_id: Optional[str] = None

def _extract_text_and_conv(data: Dict[str, Any], fallback_conv: Optional[str]) -> _A2AResponse:
    """
    兼容多种桥返回格式：
      1) {"content":{"text":"..."}}
      2) {"response":"..."} / {"text":"..."} / {"message":"..."}
      3) {"parts":[{"text":"...","type":"text"}], "metadata":{"conversation_id":"..."}}
      4) 纯字符串
      5) 常见错误字段 {"error": "..."} / {"detail": "..."} / {"errors":[{"message":"..."}]}
    同时尽量从顶层或 metadata 里补齐 conversation_id。
    """
    # 会话ID：顶层 -> metadata.conversation_id -> fallback
    conv = data.get("conversation_id")
    if not conv:
        md = data.get("metadata")
        if isinstance(md, dict):
            conv = md.get("conversation_id")
    if not conv:
        conv = fallback_conv

    # 1) 标准 content.text
    c = data.get("content")
    if isinstance(c, dict) and isinstance(c.get("text"), str):
        return _A2AResponse(content=TextContent(text=c["text"]), conversation_id=conv)
    if isinstance(c, str):
        return _A2AResponse(content=TextContent(text=c), conversation_id=conv)

    # 2) 顶层 response/text/message
    for k in ("response", "text", "message"):
        v = data.get(k)
        if isinstance(v, str):
            return _A2AResponse(content=TextContent(text=v), conversation_id=conv)

    # 3) parts[0].text（很多桥/SDK会返回这种）
    parts = data.get("parts")
    if isinstance(parts, list) and parts:
        first = parts[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return _A2AResponse(content=TextContent(text=first["text"]), conversation_id=conv)

    # 4) 常见错误字段
    for key in ("error", "detail", "description"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return _A2AResponse(content=TextContent(text=f"[error] {v}"), conversation_id=conv)
        if isinstance(v, dict):
            msg = v.get("message") or v.get("text")
            if isinstance(msg, str) and msg.strip():
                return _A2AResponse(content=TextContent(text=f"[error] {msg}"), conversation_id=conv)

    errs = data.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        if isinstance(first, dict):
            msg = first.get("message") or first.get("detail") or first.get("title")
            if isinstance(msg, str) and msg.strip():
                return _A2AResponse(content=TextContent(text=f"[error] {msg}"), conversation_id=conv)
        if isinstance(first, str) and first.strip():
            return _A2AResponse(content=TextContent(text=f"[error] {first}"), conversation_id=conv)

    # 5) 纯字符串
    if isinstance(data, str):
        return _A2AResponse(content=TextContent(text=data), conversation_id=conv)

    # 6) 兜底：带上状态码（若有）
    status = data.get("_http_status")
    if status:
        return _A2AResponse(
            content=TextContent(text=f"(A2A) HTTP {status}, body: {data}"),
            conversation_id=conv
        )

    # 都不匹配，回显原始
    return _A2AResponse(
        content=TextContent(text=f"(A2A) unexpected response: {data}"),
        conversation_id=conv
    )

class A2AClient:
    def __init__(self, base_url: str, timeout: int = 60):
        # 传入的通常形如 http://localhost:6000/a2a 或 http://host:6000
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    def _try_post(self, json_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        兼容不同桥实现的常见 POST 路径。
        无论 2xx/4xx/5xx 都解析响应体：
          - Content-Type 含 json -> r.json()
          - 否则 -> {"response": r.text}
        并附加 _http_status 方便上层兜底显示。
        """
        candidates: List[str] = []
        if not self.base_url.endswith("/a2a"):
            candidates.append(self.base_url + "/a2a")
        candidates.extend([
            self.base_url,
            self.base_url + "/send",
            self.base_url + "/message",
            self.base_url + "/messages",
        ])

        headers = {"Content-Type": "application/json"}
        last_err = None
        for url in candidates:
            try:
                r = requests.post(url, json=json_payload, timeout=self.timeout, headers=headers)
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype:
                    try:
                        data = r.json()
                    except Exception:
                        data = {"response": r.text}
                else:
                    data = {"response": r.text}
                if isinstance(data, dict):
                    data.setdefault("_http_status", r.status_code)
                return data
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return None

    def send_message(self, message: Message) -> _A2AResponse:
        """
        主方法：把 Message 转成桥能理解的 JSON，POST 到桥；把返回转成 _A2AResponse
        """
        payload = {
            "role": message.role,
            "content": {"type": "text", "text": message.content.text},
            "conversation_id": message.conversation_id,
            "metadata": message.metadata or {},
            "parent_message_id": message.parent_message_id,
            "message_id": message.message_id,
        }
        try:
            data = self._try_post(payload)
            return _extract_text_and_conv(data, message.conversation_id)
        except requests.Timeout:
            return _A2AResponse(content=TextContent(text="[error] request timeout"), conversation_id=message.conversation_id)
        except Exception as e:
            return _A2AResponse(content=TextContent(text=f"[error] {type(e).__name__}: {e}"), conversation_id=message.conversation_id)

    # 异步快捷（开线程）
    def send_message_async(self, message: Message):
        t = threading.Thread(target=self.send_message, args=(message,))
        t.daemon = True
        t.start()
        return t

# 保留占位，兼容 import（server 用你已有的 agent_bridge.py 跑）
class A2AServer:
    pass

def run_server(*args, **kwargs):
    raise NotImplementedError("This minimal python_a2a does not implement server; use your agent_bridge.py instead.")
