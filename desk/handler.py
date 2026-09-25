"""Forwards owterminal/<model> to the OpenWeights pool. One try. No second job."""

import os
import sys

import httpx
from litellm import CustomLLM, ModelResponse
from litellm.llms.custom_llm import CustomLLMError

sys.path.insert(0, os.path.dirname(__file__))
from pricing import cost  # noqa: E402


def _name(model: str) -> str:
    prefix = "owterminal/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def _fill(model_response: ModelResponse, model: str, body: dict) -> ModelResponse:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = body.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    model_response.model = model
    model_response.choices[0].message.content = message.get("content") or ""
    model_response.choices[0].finish_reason = "stop"
    model_response.usage.prompt_tokens = prompt
    model_response.usage.completion_tokens = completion
    model_response.usage.total_tokens = prompt + completion
    model_response._hidden_params["response_cost"] = cost(model, prompt, completion)
    return model_response


def _post(model: str, messages: list, api_base: str | None, api_key: str | None, timeout) -> dict:
    if not api_base or not api_key:
        raise CustomLLMError(status_code=500, message="OWT_API_BASE and OWT_API_KEY are required")
    root = api_base.rstrip("/")
    url = root if root.endswith("/chat/completions") else f"{root}/chat/completions"
    try:
        res = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": _name(model), "messages": messages, "stream": False},
            timeout=timeout or 25,
        )
    except httpx.TimeoutException as err:
        raise CustomLLMError(status_code=504, message="The pool did not answer in time") from err
    if res.status_code >= 400:
        raise CustomLLMError(status_code=res.status_code, message=res.text[:300] or "pool refused")
    return res.json()


class OpenWeights(CustomLLM):
    def completion(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose, encoding, api_key, logging_obj, optional_params, **kwargs):
        body = _post(model, messages, api_base, api_key, kwargs.get("timeout"))
        return _fill(model_response, _name(model), body)

    async def acompletion(self, model, messages, api_base, custom_prompt_dict, model_response, print_verbose, encoding, api_key, logging_obj, optional_params, **kwargs):
        body = _post(model, messages, api_base, api_key, kwargs.get("timeout"))
        return _fill(model_response, _name(model), body)


owterminal = OpenWeights()
