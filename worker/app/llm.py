"""The single inference function shared by the Standard, Priority and Flex workers.

The ONLY difference between Standard, Priority and Flex processing is the value passed in
``service_tier`` ("default", "priority" or "flex"). Everything else – client, deployment, prompt,
retry policy, timeout – is identical, which is exactly what this demo proves.
"""
from __future__ import annotations

import random
import time

import openai

from .common import iso, log, utcnow

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
INTERESTING_HEADERS = (
    "x-ms-region",
    "apim-request-id",
    "x-request-id",
    "x-ms-deployment-name",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "retry-after",
    "retry-after-ms",
)


def _headers(response) -> dict:
    if response is None:
        return {}
    return {h: response.headers.get(h) for h in INTERESTING_HEADERS if response.headers.get(h) is not None}


def _retry_after_seconds(headers: dict) -> float | None:
    try:
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000.0
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except ValueError:
        return None
    return None


def complete(
    client: openai.OpenAI,
    deployment: str,
    prompt: str,
    service_tier: str,
    *,
    reasoning_effort: str = "low",
    max_completion_tokens: int = 2000,
    max_attempts: int = 6,
    max_total_seconds: float = 1800.0,
) -> dict:
    """Run one chat completion with bounded exponential backoff + jitter.

    Returns a measurement record (never raises for API errors).
    """
    attempts: list[dict] = []
    started_at = utcnow()
    t0 = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        a_start = utcnow()
        m0 = time.monotonic()
        status, error, headers = None, None, {}
        try:
            # Streaming lets us measure time to first token (TTFT) and time to last token (TTLT).
            raw = client.chat.completions.with_raw_response.create(
                model=deployment,
                messages=[{"role": "user", "content": prompt}],
                service_tier=service_tier,  # "default" (Standard), "priority" or "flex" – the only difference
                reasoning_effort=reasoning_effort,
                max_completion_tokens=max_completion_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            headers_s = time.monotonic() - m0
            headers = _headers(raw.http_response)
            status = raw.http_response.status_code
            ttft = None
            parts: list[str] = []
            usage = finish_reason = model = tier = None
            chunks = 0
            for chunk in raw.parse():
                chunks += 1
                model = chunk.model or model
                tier = getattr(chunk, "service_tier", None) or tier
                if chunk.usage:
                    usage = chunk.usage
                for choice in chunk.choices or []:
                    if choice.delta and choice.delta.content:
                        if ttft is None:
                            ttft = time.monotonic() - m0
                        parts.append(choice.delta.content)
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
            duration = time.monotonic() - m0
            content = "".join(parts)
            attempts.append({"attempt": attempt, "started_at": iso(a_start), "duration_s": duration,
                             "status": status, "headers": headers})
            completion_tokens = usage.completion_tokens if usage else None
            gen_s = duration - ttft if ttft is not None else None
            return {
                "success": True,
                "started_at": iso(started_at),
                "finished_at": iso(utcnow()),
                "latency_s": duration,  # latency of the successful call (= time to last token)
                "ttft_s": ttft,  # request sent -> first content token
                "ttlt_s": duration,  # request sent -> stream finished
                "headers_s": headers_s,  # request sent -> HTTP response headers
                "generation_s": gen_s,  # first -> last token
                # Decode speed: tokens generated after the first one / time spent streaming them.
                "output_tps": (completion_tokens - 1) / gen_s if completion_tokens and gen_s else None,
                # End-to-end throughput including queueing/prefill.
                "e2e_tps": completion_tokens / duration if completion_tokens and duration else None,
                "stream_chunks": chunks,
                "total_s": time.monotonic() - t0,  # what the application experienced incl. retries/backoff
                "attempt_count": attempt,
                "attempts": attempts,
                "requested_service_tier": service_tier,
                "service_tier": tier,
                "model": model,
                "finish_reason": finish_reason,
                "output_chars": len(content),
                "output_preview": content[:200],
                "usage": {
                    "prompt_tokens": usage.prompt_tokens if usage else None,
                    "completion_tokens": completion_tokens,
                    "reasoning_tokens": getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
                    "cached_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None),
                },
                "headers": headers,
            }
        except openai.APIStatusError as e:
            status = e.status_code
            headers = _headers(e.response)
            error = {"type": type(e).__name__, "code": getattr(e, "code", None), "message": str(e)[:500]}
        except openai.APITimeoutError as e:
            status = 408
            error = {"type": type(e).__name__, "message": str(e)[:500]}
        except openai.APIConnectionError as e:
            status = None
            error = {"type": type(e).__name__, "message": str(e)[:500]}

        duration = time.monotonic() - m0
        attempts.append({"attempt": attempt, "started_at": iso(a_start), "duration_s": duration,
                         "status": status, "error": error, "headers": headers})
        log.warning("%s tier=%s attempt=%d failed status=%s %s", deployment, service_tier, attempt, status,
                    (error or {}).get("message", "")[:160])

        retryable = status is None or status in RETRYABLE_STATUS
        if not retryable or attempt == max_attempts:
            break
        delay = _retry_after_seconds(headers)
        if delay is None:
            delay = min(60.0, 2.0 ** attempt) * random.uniform(0.5, 1.5)
        if (time.monotonic() - t0) + delay > max_total_seconds:
            break
        time.sleep(delay)

    return {
        "success": False,
        "started_at": iso(started_at),
        "finished_at": iso(utcnow()),
        "latency_s": None,
        "total_s": time.monotonic() - t0,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "requested_service_tier": service_tier,
        "service_tier": None,
        "final_status": attempts[-1]["status"] if attempts else None,
        "final_error": attempts[-1].get("error") if attempts else None,
    }
