"""What a caller pays the gateway. The pool still pays the host its own offer."""

LIST = 1 / 1_000_000
LIBERATED = 4 / 1_000_000
MARKS = ("abliterat", "heretic", "uncensored", "jailbreak")


def liberated(model: str) -> bool:
    name = model.lower()
    return any(mark in name for mark in MARKS)


def rate(model: str) -> float:
    return LIBERATED if liberated(model) else LIST


def cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    tokens = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
    return tokens * rate(model)
