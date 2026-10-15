"""
大模型客户端（OpenAI 兼容接口，两条线路）
src/agent/llm_client.py

  · remote：远程 API，默认通义千问 DashScope 兼容模式（也可换 DeepSeek / 其他兼容服务，只改配置）
  · local ：本地主机上的 Ollama / vLLM / 板载 RKLLM 服务（同样是 OpenAI 兼容接口）
  · auto  ：远程 → 本地 依次尝试；都不可用时由调用方退回规则模板，保证断网也有输出

只用标准库 urllib，不引入新依赖。API Key 只从环境变量读取（或运行时在总后台临时填写，仅存内存）。
"""
import json
import os
import time
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

from src.config import cfg


class LLMError(Exception):
    pass


class Provider:
    def __init__(self, name: str):
        self.name = name   # "remote" / "local"
        self.runtime_key: Optional[str] = None

    def conf(self, key: str, default=None):
        return cfg(f"agent.{self.name}.{key}", default)

    @property
    def base_url(self) -> str:
        return str(self.conf("base_url", "") or "").rstrip("/")

    @property
    def model(self) -> str:
        return str(self.conf("model", "") or "")

    @property
    def vision_model(self) -> str:
        return str(self.conf("vision_model", "") or "")

    @property
    def api_key(self) -> str:
        if self.runtime_key:
            return self.runtime_key
        env = self.conf("api_key_env", "") or ""
        return os.environ.get(env, "") if env else ""

    @property
    def configured(self) -> bool:
        if not self.base_url or not self.model:
            return False
        # 远程服务必须有 Key；本地服务一般不需要
        return bool(self.api_key) if self.name == "remote" else True

    def label(self) -> str:
        return f"{'远程' if self.name == 'remote' else '本地'} · {self.model}"

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[list] = None,
             model: Optional[str] = None, timeout: Optional[float] = None,
             max_tokens: int = 600) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model or self.model, "messages": messages,
                                "temperature": 0.3, "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        timeout = float(timeout or cfg("agent.timeout_s", 20.0))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except Exception as e:
            raise LLMError(f"{type(e).__name__}: {e}") from None
        try:
            return data["choices"][0]["message"]
        except Exception:
            raise LLMError(f"返回格式异常: {str(data)[:200]}") from None


class LLMRouter:
    def __init__(self):
        self.providers = {"remote": Provider("remote"), "local": Provider("local")}
        self.last_provider: Optional[str] = None
        self.last_error: str = ""
        self.last_latency_ms: Optional[int] = None

    @property
    def route(self) -> str:
        r = str(cfg("agent.route", "auto") or "auto")
        return r if r in ("auto", "remote", "local", "off") else "auto"

    def order(self) -> List[Provider]:
        r = self.route
        if r == "off":
            return []
        if r in ("remote", "local"):
            return [self.providers[r]]
        return [self.providers["remote"], self.providers["local"]]

    def chat(self, messages, tools=None, image_b64: Optional[str] = None,
             max_tokens: int = 600) -> Tuple[Dict[str, Any], Provider]:
        """按线路依次尝试，返回 (message, provider)。全部失败抛 LLMError"""
        errors = []
        for p in self.order():
            if not p.configured:
                errors.append(f"{p.name}: 未配置" + ("（缺少 API Key 环境变量）" if p.name == "remote" else ""))
                continue
            msgs, model = messages, None
            allow_img = p.name == "local" or bool(cfg("agent.send_images_remote", False))
            if image_b64 and allow_img and p.vision_model:
                model = p.vision_model
                msgs = _attach_image(messages, image_b64)
            t0 = time.time()
            try:
                try:
                    msg = p.chat(msgs, tools=tools, model=model, max_tokens=max_tokens)
                except LLMError as e:
                    # 部分本地模型不支持 tools 参数：去掉工具重试一次
                    if tools and ("tool" in str(e).lower() or "HTTP 400" in str(e)):
                        msg = p.chat(msgs, tools=None, model=model, max_tokens=max_tokens)
                    else:
                        raise
                self.last_provider = p.name
                self.last_error = ""
                self.last_latency_ms = int((time.time() - t0) * 1000)
                return msg, p
            except LLMError as e:
                errors.append(f"{p.name}: {e}")
        self.last_error = "；".join(errors) if errors else "线路已关闭"
        raise LLMError(self.last_error)

    def test(self, name: str) -> Dict[str, Any]:
        p = self.providers.get(name)
        if p is None:
            return {"ok": False, "error": "未知线路"}
        if not p.configured:
            return {"ok": False, "error": "未配置（远程线路需设置 API Key 环境变量）"}
        t0 = time.time()
        try:
            msg = p.chat([{"role": "user", "content": "请只回复：在线"}], max_tokens=10, timeout=10)
            return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                    "reply": (msg.get("content") or "")[:40], "model": p.model}
        except LLMError as e:
            return {"ok": False, "error": str(e)[:200]}

    def status(self) -> Dict[str, Any]:
        out = {"route": self.route, "last_provider": self.last_provider,
               "last_error": self.last_error, "last_latency_ms": self.last_latency_ms}
        for name, p in self.providers.items():
            out[name] = {"configured": p.configured, "base_url": p.base_url, "model": p.model,
                         "vision_model": p.vision_model, "has_key": bool(p.api_key)}
        return out


def _attach_image(messages, image_b64: str):
    """把图片附加到最后一条 user 消息（OpenAI 多模态格式）"""
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] = [{"type": "text", "text": m["content"]},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]
            break
    return msgs
