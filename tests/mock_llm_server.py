"""A controllable local LLM provider server speaking both the OpenAI chat-completions and the
Anthropic Messages wire formats, so the real LiteLLM Router → HTTP → parse → fallback path is tested."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections import defaultdict, deque
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockLLMServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.always: dict[str, dict[str, Any]] = {}  # model → behaviour applied to every request
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._app = Starlette(
            routes=[
                Route("/v1/chat/completions", self._openai, methods=["POST"]),
                Route("/v1/messages", self._anthropic, methods=["POST"]),
            ]
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # ---- control ------------------------------------------------------------
    def script(self, model: str, *behaviours: dict[str, Any]) -> None:
        """behaviour: {"content": str} | {"tool_calls": [{"name","args"}]} | {"status": int} | {"delay": s} | {"raw": body}"""
        for b in behaviours:
            self.scripts[model].append(b)

    def requests_for(self, model: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["body"].get("model") == model]

    def reset(self) -> None:
        self.requests.clear()
        self.scripts.clear()
        self.always.clear()

    def _behaviour(self, model: str) -> dict[str, Any]:
        if model in self.always:
            return dict(self.always[model])
        if self.scripts[model]:
            return dict(self.scripts[model].popleft())
        return {}

    # ---- lifecycle ------------------------------------------------------------
    def start(self) -> MockLLMServer:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port, log_level="warning", loop="asyncio")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("mock LLM server failed to start")
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    # ---- handlers -----------------------------------------------------------
    async def _common(self, request: Request) -> tuple[dict[str, Any], dict[str, Any], JSONResponse | None]:
        body = await request.json()
        self.requests.append({"path": request.url.path, "body": body, "headers": dict(request.headers)})
        beh = self._behaviour(body.get("model", ""))
        if beh.get("delay"):
            await asyncio.sleep(beh["delay"])
        if beh.get("status"):
            err = JSONResponse(
                {"error": {"type": "server_error", "message": f"mock failure {beh['status']}"}},
                status_code=beh["status"],
            )
            return body, beh, err
        if "raw" in beh:
            return body, beh, JSONResponse(beh["raw"])
        return body, beh, None

    @staticmethod
    def _last_user_text(messages: list[dict[str, Any]]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                return str(c)
        return ""

    async def _openai(self, request: Request):
        body, beh, err = await self._common(request)
        if err:
            return err
        content = beh.get("content", f"OK: {self._last_user_text(body.get('messages', []))[:60]}")
        tool_calls = beh.get("tool_calls")
        message: dict[str, Any] = {"role": "assistant", "content": None if tool_calls else content}
        if tool_calls:
            message["tool_calls"] = [
                {"id": f"call_{i}", "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc.get("args", {}))}}
                for i, tc in enumerate(tool_calls)
            ]
        return JSONResponse(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        )

    async def _anthropic(self, request: Request):
        body, beh, err = await self._common(request)
        if err:
            return err
        content = beh.get("content", f"OK: {self._last_user_text(body.get('messages', []))[:60]}")
        blocks: list[dict[str, Any]] = []
        if content and not beh.get("tool_calls"):
            blocks.append({"type": "text", "text": content})
        for i, tc in enumerate(beh.get("tool_calls") or []):
            blocks.append({"type": "tool_use", "id": f"toolu_{i}", "name": tc["name"], "input": tc.get("args", {})})
        return JSONResponse(
            {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": body.get("model"),
                "content": blocks,
                "stop_reason": "tool_use" if beh.get("tool_calls") else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 7},
            }
        )
