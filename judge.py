"""LLM-as-a-Judge support for evaluation match methods.

Supports Azure OpenAI and any OpenAI-compatible endpoint (OpenAI, Ollama,
LM Studio, vLLM, llama.cpp server). All providers use the same OpenAI Python
SDK with a configurable base_url.

Three judge methods:
    general_quality   — score response quality against a rubric/criteria
    text_similarity   — semantic similarity score 0-100
    compare_meaning   — whether two texts convey the same meaning (0-100)
"""

import json
from dataclasses import dataclass

from config import AgentSettings


@dataclass
class JudgeResult:
    score: float  # 0-100
    reasoning: str
    raw: str  # raw LLM output for debugging


_QUALITY_PROMPT = """You are an evaluator scoring an agent's response against criteria.

Score on a scale of 0-100 where:
- 0-30: Response fails to address the question or violates the criteria
- 31-60: Response partially addresses the question with significant gaps
- 61-80: Response addresses the question but has minor issues
- 81-100: Response fully addresses the question and meets the criteria

Question: {prompt}
Criteria: {criteria}
Response: {actual}

Return ONLY a JSON object with this exact shape:
{{"score": <number 0-100>, "reasoning": "<one-sentence explanation>"}}"""


_SIMILARITY_PROMPT = """You are evaluating semantic similarity between two texts.

Score on a scale of 0-100 where:
- 0-30: Texts are about completely different topics
- 31-60: Texts share some concepts but differ substantially
- 61-80: Texts cover the same topic with notable differences in detail or emphasis
- 81-100: Texts are highly similar in meaning and content

Text A (expected): {expected}
Text B (actual): {actual}

Return ONLY a JSON object with this exact shape:
{{"score": <number 0-100>, "reasoning": "<one-sentence explanation>"}}"""


_MEANING_PROMPT = """You are evaluating whether two texts convey the same meaning.

Score on a scale of 0-100 where:
- 0-30: Texts mean different things
- 31-60: Texts share some meaning but differ in important ways
- 61-80: Texts mostly agree with minor semantic differences
- 81-100: Texts convey the same meaning (paraphrase or equivalent)

Text A (expected): {expected}
Text B (actual): {actual}

Differences in word choice, phrasing, or formality should NOT lower the score
if the underlying meaning is the same. Focus on semantic equivalence.

Return ONLY a JSON object with this exact shape:
{{"score": <number 0-100>, "reasoning": "<one-sentence explanation>"}}"""


def _build_client(settings: AgentSettings):
    """Build an OpenAI-compatible client based on settings.judge_provider."""
    if not settings.has_judge_config:
        raise RuntimeError(
            "LLM judge match method requested but JUDGE_PROVIDER and JUDGE_MODEL "
            "are not set in .env. See README for configuration."
        )

    provider = settings.judge_provider
    if provider == "azure_openai":
        from openai import AzureOpenAI
        if not settings.judge_base_url:
            raise RuntimeError("JUDGE_BASE_URL is required for azure_openai provider")
        return AzureOpenAI(
            azure_endpoint=settings.judge_base_url,
            api_key=settings.judge_api_key,
            api_version=settings.judge_api_version,
        )
    elif provider in ("openai", "openai_compatible", "ollama"):
        from openai import OpenAI
        # Default base URLs for known providers
        base_url = settings.judge_base_url
        if not base_url and provider == "ollama":
            base_url = "http://localhost:11434/v1"
        return OpenAI(
            base_url=base_url or None,
            api_key=settings.judge_api_key or "not-needed",
        )
    else:
        raise RuntimeError(
            f"Unknown JUDGE_PROVIDER '{provider}'. "
            f"Use 'azure_openai', 'openai', 'openai_compatible', or 'ollama'."
        )


def _call_judge(settings: AgentSettings, prompt: str) -> JudgeResult:
    """Call the configured LLM and parse the JSON result."""
    client = _build_client(settings)

    response = client.chat.completions.create(
        model=settings.judge_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or ""

    try:
        data = json.loads(raw)
        score = float(data.get("score", 0))
        reasoning = str(data.get("reasoning", ""))
    except (json.JSONDecodeError, ValueError, TypeError):
        # Fallback: try to extract a number from the raw text
        import re
        match = re.search(r"\b(\d{1,3})\b", raw)
        score = float(match.group(1)) if match else 0.0
        reasoning = f"(parse failed) {raw[:200]}"

    score = max(0.0, min(100.0, score))
    return JudgeResult(score=score, reasoning=reasoning, raw=raw)


def judge_general_quality(
    settings: AgentSettings, prompt: str, criteria: str, actual: str
) -> JudgeResult:
    """Score the quality of an agent response against criteria."""
    return _call_judge(
        settings,
        _QUALITY_PROMPT.format(prompt=prompt, criteria=criteria, actual=actual),
    )


def judge_text_similarity(
    settings: AgentSettings, expected: str, actual: str
) -> JudgeResult:
    """Score semantic similarity between expected and actual text."""
    return _call_judge(
        settings,
        _SIMILARITY_PROMPT.format(expected=expected, actual=actual),
    )


def judge_compare_meaning(
    settings: AgentSettings, expected: str, actual: str
) -> JudgeResult:
    """Score whether expected and actual convey the same meaning."""
    return _call_judge(
        settings,
        _MEANING_PROMPT.format(expected=expected, actual=actual),
    )
