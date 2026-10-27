"""Seam test for the LLM transport adapter.

Spins up two fake backends on localhost and asserts what actually goes over
the wire. The OpenAI-compatible fake 404s any Ollama-native path, exactly like
OmniRoute/9router does — so if the adapter ever regresses to POSTing
/api/chat at 9router, this test fails the way production did.

    ./venv/bin/python tests/test_llm_transport.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OLLAMA_MODELS = ["qwen2.5vl:3b", "qwen2.5:14b", "gemma3:27b"]
ROUTER_MODELS = ["Free-Tier", "Vision-Tier"]
EXPECTED_KEY = "test-key-123"

RECORD = {}


class FakeBackend(BaseHTTPRequestHandler):
    """Serves either Ollama's native API or an OpenAI-compatible one."""

    mode = "ollama"

    def log_message(self, *_args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._json({"error": {"type": "not_found", "message": "no such route"}}, 404)

    def do_GET(self):
        RECORD.clear()
        RECORD.update(path=self.path, method="GET",
                      auth=self.headers.get("Authorization"))
        if self.mode == "ollama" and self.path == "/api/tags":
            return self._json({"models": [{"name": m} for m in OLLAMA_MODELS]})
        if self.mode == "openai" and self.path == "/v1/models":
            if self.headers.get("Authorization") != f"Bearer {EXPECTED_KEY}":
                return self._json({"error": {"type": "unauthorized"}}, 401)
            return self._json({"data": [{"id": m} for m in ROUTER_MODELS]})
        return self._not_found()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        RECORD.clear()
        RECORD.update(path=self.path, method="POST", body=payload,
                      auth=self.headers.get("Authorization"))
        if self.mode == "openai":
            if self.headers.get("Authorization") != f"Bearer {EXPECTED_KEY}":
                return self._json({"error": {"type": "unauthorized"}}, 401)
            if self.path == "/v1/chat/completions":
                return self._json({"choices": [{"message": {
                    "role": "assistant", "content": "  He steps forward, quietly.  "}}]})
            return self._not_found()
        if self.path == "/api/chat":
            return self._json({"message": {"role": "assistant",
                                           "content": "He steps forward, quietly."}})
        if self.path == "/api/generate":
            return self._json({"response": '[{"name": "Jin-Woo"}]'})
        return self._not_found()


def serve(mode):
    handler = type(f"Fake_{mode}", (FakeBackend,), {"mode": mode})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


PASSED = FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


def main():
    import config
    from pipeline import llm_transport

    ollama_srv, ollama_url = serve("ollama")
    router_srv, router_url = serve("openai")

    print("\n== transport selection ==")
    config.USE_NINE_ROUTER = False
    t = llm_transport.build_transport(ollama_url)
    check("default arm is Ollama", t.kind == "ollama", t.kind)
    config.USE_NINE_ROUTER = True
    config.NINE_ROUTER_HOST = router_url
    config.NINE_ROUTER_API_KEY = EXPECTED_KEY
    t9 = llm_transport.build_transport()
    check("USE_NINE_ROUTER selects OpenAI-compatible arm",
          t9.kind == "openai", t9.kind)
    check("api key reaches the transport", t9.api_key == EXPECTED_KEY, t9.api_key)
    check("as_transport passes a transport through unchanged",
          llm_transport.as_transport(t9) is t9)
    coerced = llm_transport.as_transport(ollama_url)
    check("as_transport coerces a legacy host string",
          coerced.kind == "ollama" and coerced.host == ollama_url)

    print("\n== Ollama arm wire format (must be unchanged) ==")
    out = t.chat("qwen2.5:14b", "tell me", ["QUJD"], temperature=0.78,
                 top_p=0.90, max_tokens=90, repeat_penalty=1.20, top_k=40)
    body = RECORD["body"]
    check("posts to /api/chat", RECORD["path"] == "/api/chat", RECORD["path"])
    check("strips the reply", out == "He steps forward, quietly.", repr(out))
    check("num_predict mapped", body["options"]["num_predict"] == 90, body["options"])
    check("repeat_penalty mapped", body["options"]["repeat_penalty"] == 1.20)
    check("top_k mapped", body["options"]["top_k"] == 40)
    check("images stay bare base64",
          body["messages"][-1]["images"] == ["QUJD"], body["messages"][-1])
    check("no Authorization sent when no key", RECORD["auth"] is None, RECORD["auth"])
    check("system message omitted when absent",
          [m["role"] for m in body["messages"]] == ["user"])
    t.chat("gemma3:27b", "scene text", system="You narrate.",
           top_k=40, max_tokens=200, repeat_penalty=1.18)
    sbody = RECORD["body"]
    check("Ollama scene call puts system first",
          [m["role"] for m in sbody["messages"]] == ["system", "user"],
          sbody["messages"])
    check("Ollama scene call keeps top_k and num_predict",
          sbody["options"]["top_k"] == 40 and sbody["options"]["num_predict"] == 200,
          sbody["options"])
    gen = t.generate("qwen2.5vl:3b", "list them", ["QUJD"], max_tokens=500)
    check("generate uses /api/generate", RECORD["path"] == "/api/generate")
    check("generate parses 'response'", gen == '[{"name": "Jin-Woo"}]', gen)
    check("keep_alive only sent when asked",
          "keep_alive" not in RECORD["body"])
    check("list_models returns Ollama names", t.list_models() == OLLAMA_MODELS)
    t.chat("m", "p", keep_alive="30m")
    check("keep_alive forwarded when requested",
          RECORD["body"].get("keep_alive") == "30m")

    print("\n== 9router arm wire format (was 404ing) ==")
    out9 = t9.chat("Free-Tier", "tell me", ["QUJD"], temperature=0.78, top_p=0.90,
                   max_tokens=90, repeat_penalty=1.20, top_k=40, keep_alive="30m",
                   system="You narrate.")
    body9 = RECORD["body"]
    check("posts to /v1/chat/completions, NOT /api/chat",
          RECORD["path"] == "/v1/chat/completions", RECORD["path"])
    check("parses choices[0].message.content",
          out9 == "He steps forward, quietly.", repr(out9))
    check("max_tokens used instead of num_predict",
          body9.get("max_tokens") == 90 and "num_predict" not in json.dumps(body9))
    check("no Ollama options block", "options" not in body9, list(body9))
    check("repeat_penalty approximated as frequency_penalty",
          body9.get("frequency_penalty") == 0.4, body9.get("frequency_penalty"))
    check("top_k dropped", "top_k" not in body9)
    check("keep_alive dropped", "keep_alive" not in body9)
    check("system message first", body9["messages"][0]["role"] == "system")
    parts = body9["messages"][1]["content"]
    check("text becomes a content part",
          parts[0] == {"type": "text", "text": "tell me"}, parts[0])
    check("image becomes an image_url data URL",
          parts[1] == {"type": "image_url",
                       "image_url": {"url": "data:image/jpeg;base64,QUJD"}}, parts[1])
    check("Bearer key sent on inference calls",
          RECORD["auth"] == f"Bearer {EXPECTED_KEY}", RECORD["auth"])
    txt = t9.chat("Free-Tier", "no images here")
    check("text-only prompt stays a plain string",
          txt and RECORD["body"]["messages"][-1]["content"] == "no images here")
    t9.generate("Free-Tier", "single turn", ["QUJD"])
    check("9router generate maps to chat completions, not /api/generate",
          RECORD["path"] == "/v1/chat/completions", RECORD["path"])
    check("list_models returns router tier ids", t9.list_models() == ROUTER_MODELS)

    print("\n== health ==")
    check("Ollama describe running", t.describe()["running"] is True)
    check("9router describe running", t9.describe()["running"] is True)
    dead = llm_transport.OllamaTransport("http://127.0.0.1:1")
    check("dead backend reports not running", dead.describe()["running"] is False)
    t9bad = llm_transport.OpenAICompatTransport(router_url, "wrong-key")
    check("wrong key is not silently healthy", t9bad.describe()["running"] is False)

    print("\n== narrator integration (the original bug) ==")
    config.USE_NINE_ROUTER = True
    from pipeline.narrator import Narrator
    n = Narrator()
    check("narrator picked the 9router arm",
          n.transport.kind == "openai", n.transport.kind)
    line = n._chat_with_retries("narrate this panel", None)
    check("narration reached /v1/chat/completions",
          RECORD["path"] == "/v1/chat/completions", RECORD["path"])
    check("narration returned real text", line == "He steps forward, quietly.", repr(line))
    health = n.check_ollama()
    check("health reports the router's models",
          health["models"] == ROUTER_MODELS, health["models"])
    check("an Ollama model name is honestly reported as absent on 9router",
          health["model_available"] is False, health)
    n.writer_model = "Free-Tier"
    check("model_available goes true for a listed tier",
          n.check_ollama()["model_available"] is True)
    n.writer_model = "not-installed"
    check("model_available goes false for an absent model",
          n.check_ollama()["model_available"] is False)
    check("9router arm never guesses a replacement model",
          n._resolve_model("some-typo-tier") == "some-typo-tier")
    n.transport = t
    check("Ollama arm still does family matching",
          n._resolve_model("qwen2.5") == "qwen2.5:14b", n._resolve_model("qwen2.5"))

    config.USE_NINE_ROUTER = False
    ollama_srv.shutdown()
    router_srv.shutdown()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        print("\nSUITE CRASHED — a backend call raised outside a check()")
        sys.exit(1)
