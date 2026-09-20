"""LLM access: one narrow interface, a disk cache, and honest token counting.

The model's job here is to weigh evidence it is handed and to write prose an
analyst or a regulator will read. It does not decide routes, the SAR flag,
exposure, or which actions follow from which rule -- those are in policy.py,
because a model that guesses at them will eventually contradict itself and
roughly half the scored fields are mechanically checkable.

It does not pick the pattern label either. Given the Ring B burst, both
gemini-3.5-flash and -lite reasoned correctly ("threshold structuring, four
charges of $450-499 in 30 minutes") and then labelled it
`card_not_present_fraud` and `card_testing` respectively -- neither reached
`undocumented`. The classifier is rules; the model confirms and explains.

Caching is keyed on the full request, so re-running a case whose evidence has
not changed costs no quota. That matters more than it sounds: the free tier
is limited per day, and scoring the holdout means running the same cases
repeatedly while only the prompt wording changes.

    python src/llm.py --check      # verify the key and list usable models
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / ".llm_cache"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

#: Free tier offers Flash only -- Pro returns 429 (quota). Measured usable on
#: this account: gemini-3.5-flash, gemini-3-flash-preview, the -lite variants
#: and the -latest aliases. 2.5-* are closed to new accounts.
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_EMBED = "gemini-embedding-001"

#: The model returns 3,072 dimensions by default. 768 is requested instead:
#: the vectors are stored in TigerGraph as LIST<DOUBLE> and compared in GSQL,
#: so width costs memory on a 16 GiB workspace and time in every similarity
#: scan. 5,565 closed cases at 3,072 dims is 17M doubles; at 768 it is 4.3M.
EMBED_DIM = 768


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    """Token accounting, so `tokens` in an answer file is a count not a guess."""

    prompt: int = 0
    output: int = 0
    total: int = 0
    calls: int = 0
    cached: int = 0

    def add(self, meta: dict[str, Any], from_cache: bool = False) -> None:
        self.calls += 1
        if from_cache:
            self.cached += 1
            return
        self.prompt += int(meta.get("promptTokenCount") or 0)
        self.output += int(meta.get("candidatesTokenCount") or 0)
        self.total += int(meta.get("totalTokenCount") or 0)

    def reset(self) -> None:
        self.prompt = self.output = self.total = self.calls = self.cached = 0


@dataclass
class Gemini:
    model: str = DEFAULT_MODEL
    embed_model: str = DEFAULT_EMBED
    api_key: str = ""
    cache: bool = True
    usage: Usage = field(default_factory=Usage)
    max_retries: int = 8

    #: Minimum seconds between embedding calls. The free tier rate-limits per
    #: minute, and unthrottled batching reached that ceiling after roughly 900
    #: embeddings -- fast enough to look fine and then fail mid-run. Pacing
    #: the calls is cheaper than retrying them, and the cache means a run that
    #: does stop resumes where it left off rather than starting over.
    embed_min_interval: float = 1.2
    complete_min_interval: float = 6.0
    _last_embed: float = 0.0
    _last_complete: float = 0.0

    def __post_init__(self) -> None:
        if not self.api_key:
            env = load_env()
            self.api_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set; fill in .env")
        self._session = requests.Session()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # -- cache -----------------------------------------------------------
    def _key(self, payload: dict, model: str) -> Path:
        blob = json.dumps({"m": model, "p": payload}, sort_keys=True)
        return CACHE_DIR / f"{hashlib.sha256(blob.encode()).hexdigest()[:40]}.json"

    # -- transport -------------------------------------------------------
    def _post(self, url: str, payload: dict, timeout: int = 180) -> dict:
        """POST with backoff.

        A 429 is a rate limit, not a failure, and neither is a read timeout --
        the API stalls rather than refusing when it is busy, and an
        unretried timeout kills a twenty-case run at case eleven.
        """
        last = ""
        for attempt in range(self.max_retries):
            try:
                r = self._session.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = f"{type(exc).__name__}: {str(exc)[:160]}"
                if attempt == self.max_retries - 1:
                    break
                wait = min(90, 5 * (2**attempt))
                print(f"    llm: {type(exc).__name__}, waiting {wait}s "
                      f"(attempt {attempt + 1}/{self.max_retries})", flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            last = f"[{r.status_code}] {r.text[:300]}"
            if r.status_code in (429, 500, 502, 503, 504):
                # Honour the server's own RetryInfo when it sends one.
                wait = min(120, 5 * (2**attempt))
                try:
                    for d in r.json().get("error", {}).get("details", []):
                        delay = str(d.get("retryDelay", ""))
                        if delay.endswith("s"):
                            wait = max(wait, float(delay[:-1]) + 1)
                except Exception:  # noqa: BLE001
                    pass
                print(f"    llm: {r.status_code}, waiting {wait:.0f}s "
                      f"(attempt {attempt + 1}/{self.max_retries})", flush=True)
                time.sleep(wait)
                continue
            break
        raise LLMError(f"Gemini request failed: {last}")

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_embed
        if gap < self.embed_min_interval:
            time.sleep(self.embed_min_interval - gap)
        self._last_embed = time.monotonic()

    def _throttle_complete(self) -> None:
        """Pace generateContent to stay under the per-minute burst ceiling.

        The free tier's limit is per-minute, not per-day: probing it a minute
        later succeeds. What sinks a run is bursting -- several calls per case,
        twenty cases, plus embeddings, all at once. Spacing calls a few seconds
        apart keeps the run under the ceiling and turns the rate limit from a
        wall into a slowdown. A cache hit does not count and is not throttled.
        """
        gap = time.monotonic() - self._last_complete
        if gap < self.complete_min_interval:
            time.sleep(self.complete_min_interval - gap)
        self._last_complete = time.monotonic()

    # -- generation ------------------------------------------------------
    def complete(
        self,
        prompt: str,
        schema: dict | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8000,
        model: str | None = None,
    ) -> Any:
        """Return parsed JSON when `schema` is given, otherwise text."""
        model = model or self.model
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if schema:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = schema

        path = self._key(payload, model)
        if self.cache and path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            self.usage.add(cached.get("usage", {}), from_cache=True)
            return cached["value"]

        self._throttle_complete()
        data = self._post(f"{GEMINI_BASE}/models/{model}:generateContent", payload)
        meta = data.get("usageMetadata", {})
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError(f"no candidates returned: {json.dumps(data)[:300]}")
        parts = cands[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        finish = cands[0].get("finishReason", "")

        # Gemini 3.x counts internal reasoning against maxOutputTokens: a call
        # can spend 1,400 tokens thinking and 300 answering, so a budget that
        # looks generous truncates the answer mid-sentence. Detect it and
        # retry once with room rather than returning half a SAR narrative.
        if finish == "MAX_TOKENS" and max_tokens < 32_000:
            thought = int(meta.get("thoughtsTokenCount") or 0)
            bigger = min(32_000, max(max_tokens * 3, thought + 4_000))
            print(f"    llm: truncated after {thought} thinking tokens, "
                  f"retrying with {bigger}", flush=True)
            self.usage.add(meta)
            return self.complete(prompt, schema=schema, system=system,
                                 temperature=temperature, max_tokens=bigger, model=model)
        if not text:
            raise LLMError(f"empty response (finishReason={finish or 'unknown'})")

        value: Any = text
        if schema:
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"response was not valid JSON: {exc}: {text[:300]}") from exc

        self.usage.add(meta)
        if self.cache and finish in ("STOP", ""):
            # Only a cleanly finished response is worth keeping. Caching a
            # truncated one is worse than not caching at all: the prompt hash
            # is unchanged, so every later run replays the bad answer and the
            # retry that would have fixed it never executes.
            path.write_text(
                json.dumps({"value": value, "usage": meta}), encoding="utf-8"
            )
        return value

    # -- embeddings ------------------------------------------------------
    def _embed_payload(self, text: str, model: str, dim: int) -> dict:
        return {
            "model": f"models/{model}",
            "content": {"parts": [{"text": text[:20_000]}]},
            "outputDimensionality": dim,
        }

    def embed(self, text: str, model: str | None = None, dim: int = EMBED_DIM) -> list[float]:
        model = model or self.embed_model
        payload = self._embed_payload(text, model, dim)
        path = self._key(payload, f"embed:{model}")
        if self.cache and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["value"]

        data = self._post(f"{GEMINI_BASE}/models/{model}:embedContent", payload, timeout=120)
        vec = data.get("embedding", {}).get("values")
        if not vec:
            raise LLMError(f"no embedding returned: {json.dumps(data)[:200]}")
        if self.cache:
            path.write_text(json.dumps({"value": vec}), encoding="utf-8")
        return vec

    def embed_many(
        self,
        texts: list[str],
        model: str | None = None,
        dim: int = EMBED_DIM,
        batch: int = 64,
        progress_every: int = 1_000,
    ) -> list[list[float]]:
        """Batch embedding -- about 6x the throughput of one call per text.

        5,565 closed-case narratives take roughly ten minutes this way rather
        than an hour, which is the difference between embedding the history
        and deciding not to bother.
        """
        model = model or self.embed_model
        out: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        pending: list[int] = []

        for i, t in enumerate(texts):
            path = self._key(self._embed_payload(t, model, dim), f"embed:{model}")
            if self.cache and path.exists():
                out[i] = json.loads(path.read_text(encoding="utf-8"))["value"]
            else:
                pending.append(i)

        if pending and progress_every:
            print(f"    {len(texts) - len(pending):,} cached, {len(pending):,} to embed",
                  flush=True)

        done = 0
        for start in range(0, len(pending), batch):
            idxs = pending[start:start + batch]
            payload = {
                "requests": [self._embed_payload(texts[i], model, dim) for i in idxs]
            }
            self._throttle()
            data = self._post(
                f"{GEMINI_BASE}/models/{model}:batchEmbedContents", payload, timeout=180
            )
            embs = data.get("embeddings") or []
            if len(embs) != len(idxs):
                raise LLMError(f"batch returned {len(embs)} embeddings for {len(idxs)} texts")
            for i, e in zip(idxs, embs):
                vec = e.get("values")
                out[i] = vec
                if self.cache:
                    self._key(
                        self._embed_payload(texts[i], model, dim), f"embed:{model}"
                    ).write_text(json.dumps({"value": vec}), encoding="utf-8")
            done += len(idxs)
            if progress_every and done % progress_every < batch:
                print(f"    embedded {done:,}/{len(pending):,}", flush=True)
        return out


def cache_stats() -> dict[str, Any]:
    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    return {
        "entries": len(files),
        "bytes": sum(f.stat().st_size for f in files),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    args = ap.parse_args()

    if args.clear_cache:
        n = 0
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
            n += 1
        print(f"cleared {n} cache entries")
        return 0

    g = Gemini()
    print(f"model : {g.model}")
    print(f"embed : {g.embed_model}")
    st = cache_stats()
    print(f"cache : {st['entries']} entries, {st['bytes'] / 1024:,.0f} KB\n")

    schema = {
        "type": "OBJECT",
        "properties": {
            "verdict": {"type": "STRING", "enum": ["fraud", "legitimate", "uncertain"]},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["verdict", "confidence"],
    }
    out = g.complete(
        "A card made four online charges of $478.95, $456.96, $488.04 and $482.12 "
        "within 30 minutes. Assess.",
        schema=schema,
    )
    print(f"  structured output: {out}")
    vec = g.embed("cardholder disputed a recurring charge")
    print(f"  embedding: {len(vec)} dimensions")
    print(f"  usage: {g.usage}")
    print("\nOK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
