# Desk

This fork is the caller gateway for [OpenWeights Terminal](https://owterminal.com). Upstream remains [BerriAI/litellm](https://github.com/BerriAI/litellm). Our changes stay in `desk/` so the proxy can be merged forward.

LiteLLM holds the virtual key, the dollar budget, and the per-minute limit. The pool holds the machines. A host does not publish a URL. This proxy calls `OWT_API_BASE` once, with one pool key, and records what the caller owes.

A model is `owterminal/<id>`, the same id the desk board uses. A name with abliterated, heretic, uncensored, or jailbreak in it is $4 per million tokens. Anything else is $1. That price is the caller's bill. It is not the host's offer. The pool pays the host from the offer on the live machine. Retries are off, because a retry would open a second job.

## Run

```bash
cp desk/.env.example .env
# LITELLM_MASTER_KEY, DATABASE_URL, OWT_API_KEY
# OWT_API_BASE defaults to https://owterminal.com/api/v1
docker compose -f desk/compose.yaml up
```

The proxy file is `desk/proxy.yaml`. Upstream ignores any file named `config.yaml`, so that name cannot be committed.

```bash
python desk/pricing_test.py
```

## A key

```bash
curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_budget":10,"budget_duration":"30d","rpm_limit":60,"models":["owterminal/*"]}'
```

`max_budget` is US dollars. The key stops when its spend row crosses it.

## A call

```bash
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"owterminal/qwen3.6-35b-a3b-abliterated","messages":[{"role":"user","content":"Say hi"}]}'
```

The gateway waits up to 95 seconds. Callers should set a 90 second timeout. If no machine claims the job, the pool returns an error and this proxy does not try again.

A host with GPUs does not point the desk at this proxy. On the machine that runs LiteLLM, use the pool worker from [ow-terminal](https://github.com/Tarzelf/ow-terminal/blob/main/scripts/pool-worker.mjs). One process per `model_name`. It pulls the queue and calls this proxy with `LITELLM_KEY`.
