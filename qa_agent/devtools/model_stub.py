"""LOCAL OpenAI-compatible stub of POST /v1/chat/completions for tests and offline demos.

It performs NO reasoning. It answers from hand-authored recordings in fixtures/replay/<STORY>.json (plan_story, or the
Milestone A two-step recording composed into plan_story), and can be scripted to return failures:
  POST /_stub/queue  [{"status":429,"headers":{"Retry-After":"0"}}, {"content":"not json"}, {"finish_reason":"length"},
                      {"no_usage":true}, {"variant":"obey_injection"}, {"status":401,"echo_auth":true}, {"sleep":2}]
Every response carries system_fingerprint LOCAL-STUB-NOT-A-MODEL. Binds to 127.0.0.1 only.
Run:  python -m uvicorn qa_agent.devtools.model_stub:app --host 127.0.0.1 --port 8701
"""
import asyncio, hashlib, json, uuid, os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .. import ROOT
from ..providers.models import compose_plan_story
from ..util import load_json

app = FastAPI(title="LOCAL model stub (not a model)")
STATE = {"queue": [], "requests": []}


@app.middleware("http")
async def harness_instance(request, call_next):
    response = await call_next(request)
    if os.environ.get("QA_HARNESS_INSTANCE_ID"):
        response.headers["X-QA-Harness-Instance"] = os.environ["QA_HARNESS_INSTANCE_ID"]
    return response


@app.post("/_stub/queue")
async def queue(request: Request):
    STATE["queue"].extend(await request.json())
    return {"queued": len(STATE["queue"])}


@app.post("/_stub/reset")
def reset():
    STATE["queue"].clear(); STATE["requests"].clear()
    return {"ok": True}


@app.get("/_stub/requests")
def requests_():
    return STATE["requests"]


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    auth = request.headers.get("authorization", "")
    user = next((m["content"] for m in body.get("messages", []) if m["role"] == "user"), "{}")
    try:
        key = json.loads(user).get("story_key")
    except ValueError:
        key = None
    STATE["requests"].append({"auth_present": auth.startswith("Bearer "), "auth_sha256": hashlib.sha256(auth.encode()).hexdigest(),
                              "model": body.get("model"), "story_key": key, "n_messages": len(body.get("messages", [])),
                              "response_format_type": (body.get("response_format") or {}).get("type"),
                              "strict": ((body.get("response_format") or {}).get("json_schema") or {}).get("strict"),
                              "max_completion_tokens": body.get("max_completion_tokens"), "messages": body.get("messages")})
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "missing bearer token"}, 401)
    sc = STATE["queue"].pop(0) if STATE["queue"] else {}
    if sc.get("sleep"):
        await asyncio.sleep(sc["sleep"])
    if sc.get("status", 200) != 200:
        msg = {"error": f"stub error {sc['status']}"}
        if sc.get("echo_auth"):
            msg["error"] += f" (received Authorization: {auth})"   # simulates a provider echoing credentials
        return JSONResponse(msg, sc["status"], headers=sc.get("headers") or {})
    if "content" in sc:
        content = sc["content"]
    else:
        p = ROOT / "fixtures/replay" / f"{key}.json"
        if not p.exists():
            content = json.dumps({"error": f"stub has no recording for {key}"})
        else:
            rec = load_json(p)
            content = json.dumps(rec["stub_variants"][sc["variant"]] if sc.get("variant") else compose_plan_story(rec))
    prompt_tokens = len(json.dumps(body.get("messages"))) // 4
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(content) // 4,
             "total_tokens": prompt_tokens + len(content) // 4 + 100,
             "prompt_tokens_details": {"text_tokens": prompt_tokens, "cached_tokens": 0},
             "completion_tokens_details": {"reasoning_tokens": 100}}
    resp = {"id": "stub-" + uuid.uuid4().hex[:8], "object": "chat.completion", "model": body.get("model"),
            "system_fingerprint": "LOCAL-STUB-NOT-A-MODEL",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "refusal": None},
                         "finish_reason": sc.get("finish_reason", "stop")}]}
    if not sc.get("no_usage"):
        resp["usage"] = sc.get("usage", usage)
    return resp
