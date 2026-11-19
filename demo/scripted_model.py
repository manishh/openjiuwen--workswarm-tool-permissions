#!/usr/bin/env python3
"""A scripted stand-in for an LLM, served on an OpenAI-compatible endpoint.

It plays the worst case: an agent whose context has been hijacked and that
always asks for the dangerous tool call. Each user message carries a tag such
as ``[step:delete_build]``. On the first model call of that turn the server
answers with the tool call scripted for the tag. Once a tool result comes
back it answers in plain text and quotes that result, so the demo can see what
the runtime did with the call. Messages without a tag (titles, summaries and
other housekeeping calls WorkSwarm makes) get a short plain answer.

Nothing here decides whether a call is allowed. That is the point: whatever
this "model" asks for, the permission layer in WorkSwarm makes the decision.

Run it on its own with:  python demo/scripted_model.py --port 18765
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_NAME = "scripted-agent"
TAG = re.compile(r"\[step:([a-z0-9_]+)\]")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


class Script:
    """Maps a step tag to the tool call the scripted model makes for it."""

    def __init__(self, steps: dict[str, tuple[str, dict[str, Any]]], log_path: Path | None = None):
        self.steps = steps
        self.log_path = log_path
        self.lock = threading.Lock()

    def log(self, record: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        with self.lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reply(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return {"content": str} or {"tool_call": (name, args)} for one model call."""
        messages = body.get("messages") or []
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        tag = TAG.search(_text(messages[last_user].get("content"))) if last_user >= 0 else None
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        if tag is None:
            self.log({"kind": "housekeeping", "tools": len(tools)})
            return {"content": "OK."}
        step = tag.group(1)
        after = messages[last_user + 1:]
        results = [m for m in after if m.get("role") == "tool"]
        if step == "list_tools":
            self.log({"kind": "tools", "step": step, "tools": body.get("tools") or []})
            return {"content": "Tools: " + ", ".join(sorted(n for n in tools if n))}
        if step not in self.steps:
            return {"content": f"No script for step {step}."}
        if not results:
            name, args = self.steps[step]
            self.log({"kind": "tool_call", "step": step, "tool": name, "args": args,
                      "offered": name in tools})
            return {"tool_call": (name, args)}
        result = _text(results[-1].get("content"))
        self.log({"kind": "tool_result", "step": step, "result": result[:2000]})
        return {"content": f"RESULT[{step}]: {result[:600]}"}


def _completion(reply: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.get("content")}
    finish = "stop"
    if "tool_call" in reply:
        name, args = reply["tool_call"]
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}
        finish = "tool_calls"
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion",
            "created": int(time.time()), "model": MODEL_NAME,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _chunks(reply: dict[str, Any]) -> list[dict[str, Any]]:
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_NAME,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    if "tool_call" in reply:
        name, args = reply["tool_call"]
        call = {"index": 0, "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        out = [chunk({"role": "assistant", "content": None, "tool_calls": [call]}), chunk({}, "tool_calls")]
    else:
        out = [chunk({"role": "assistant", "content": reply.get("content") or ""}), chunk({}, "stop")]
    usage = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_NAME,
             "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return out + [usage]


def make_handler(script: Script) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # keep stdout quiet
            pass

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": {"message": "not found"}})
                return
            reply = script.reply(body)
            if not body.get("stream"):
                self._json(200, _completion(reply))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for c in _chunks(reply):
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def serve(script: Script, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(script))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--log", default="scripted_model.log.jsonl")
    args = parser.parse_args()
    srv = serve(Script({}, Path(args.log)), args.port)
    print(f"scripted model on http://127.0.0.1:{args.port}/v1 (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
