# Desk

This fork is the caller gateway for [OpenWeights Terminal](https://owterminal.com). Upstream remains [BerriAI/litellm](https://github.com/BerriAI/litellm). Our changes stay in `desk/` so the proxy can be merged forward.

LiteLLM already does the caller side: a virtual key, a hard budget, requests per minute, and a spend row in Postgres. The pool does the other half. A host does not publish a URL. The desk at `OWT_API_BASE` picks the cheapest live machine and bills that offer. This proxy does not try to be that router.

## Run

```bash
cp desk/.env.example .env
# fill LITELLM_MASTER_KEY, DATABASE_URL, OWT_API_KEY
docker compose -f desk/compose.yaml up
```

Or, with the proxy installed:

```bash
litellm --config desk/config.yaml --port 4000
```

## A key

```bash
curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_budget":10,"budget_duration":"30d","rpm_limit":60,"models":["qwen3.6-35b-a3b-abliterated"]}'
```

`max_budget` is US dollars. The key stops when the spend row crosses it. List weights are priced at $1 per million tokens. Abliterated, heretic, and uncensored weights are $4.

## A call

```bash
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-35b-a3b-abliterated","messages":[{"role":"user","content":"Say hi"}]}'
```

That call is forwarded to the OpenWeights pool as an OpenAI-compatible request. To prefer a local engine, add a second deployment of the same `model_name` with `api_base` set to Ollama and a lower `input_cost_per_token`. Cost-based routing then picks the cheaper one. Leave it out until that engine is actually up. An empty base is not a deployment.
