from __future__ import annotations

import re
from time import perf_counter

import requests

# Prefer already-pulled ~7B/8B instruct; larger instruct (e.g. 14B) is OK if that is all we have.
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([bm])\b", re.I)
_INSTRUCT_HINT = re.compile(r"(instruct|chat|it\b|:it\b|-it\b)", re.I)
_EXCLUDE = re.compile(r"(embed|code|vision|llava|clip|uncensored)", re.I)


def _size_billions(name: str) -> float | None:
    m = _SIZE_RE.search(name.replace(":", " "))
    if not m:
        # common tags like qwen2.5:14b
        m = re.search(r":(\d+(?:\.\d+)?)[bB]\b", name)
        if not m:
            return None
        return float(m.group(1))
    val = float(m.group(1))
    unit = m.group(2).lower()
    return val if unit == "b" else val / 1000.0


def _score_model(name: str) -> tuple[int, float, str]:
    """Lower tuple sorts first. Prefer 7–8B instruct, then other instruct ≤14B, then anything."""
    low = name.lower()
    if _EXCLUDE.search(low):
        return (90, 999.0, name)
    size = _size_billions(name)
    instruct = 0 if _INSTRUCT_HINT.search(name) or "instruct" in low else 1
    if size is None:
        return (50 + instruct, 50.0, name)
    if 6.0 <= size <= 9.0:
        band = 0
    elif 3.0 <= size < 6.0:
        band = 1
    elif 9.0 < size <= 14.5:
        band = 2
    elif size <= 20.0:
        band = 3
    else:
        band = 4
    # Prefer closer to 8B within a band
    distance = abs(size - 8.0)
    return (band + instruct * 5, distance, name)


def format_user_message(user_text: str, visual_note: str | None = None) -> str:
    """Current-turn user content. Visual notes stay off history on purpose."""
    user = user_text or "(silence)"
    note = (visual_note or "").strip()
    if not note:
        return user
    return f"{note}\n\n{user}"


def pick_ollama_model(names: list[str], preferred: str | None = None) -> str:
    """Choose a usable local model. Prefer `preferred` if present; else best 7/8B instruct."""
    cleaned = [n for n in names if n]
    if not cleaned:
        raise RuntimeError("Ollama has no models pulled")
    if preferred:
        for n in cleaned:
            if n == preferred or n.startswith(preferred) or preferred in n:
                return n
    ranked = sorted(cleaned, key=_score_model)
    return ranked[0]


class LocalMayor:
    def __init__(self, host: str, model: str, num_predict: int, temperature: float, system: str):
        self.host = host.rstrip("/")
        self.model = model
        self.num_predict = num_predict
        self.temperature = temperature
        self.system = system

    def list_models(self) -> list[str]:
        r = requests.get(f"{self.host}/api/tags", timeout=3)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]

    def resolve_model(self, allow_missing_preferred: bool = True) -> str:
        names = self.list_models()
        try:
            chosen = pick_ollama_model(names, self.model or None)
        except RuntimeError:
            raise
        if self.model and chosen != self.model and not any(
            self.model in n or n.startswith(self.model) for n in names
        ):
            if not allow_missing_preferred:
                available = ", ".join(names) or "(none)"
                raise RuntimeError(
                    f"Ollama has no model matching {self.model!r}. Pulled: {available}"
                )
            print(
                f"  config model {self.model!r} not pulled; auto-selected {chosen!r}"
            )
        self.model = chosen
        return chosen

    def ping(self) -> str:
        return self.resolve_model(allow_missing_preferred=True)

    def warm(self) -> float:
        """One-token keep_alive ping so the first real turn is not a cold start. Returns ms."""
        t0 = perf_counter()
        r = requests.post(
            f"{self.host}/api/generate",
            json={
                "model": self.model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": "2h",
                "options": {"num_predict": 1, "temperature": 0.0},
            },
            timeout=120,
        )
        r.raise_for_status()
        return (perf_counter() - t0) * 1000

    def reply(
        self, user_text: str, history: list[dict], visual_note: str | None = None
    ) -> tuple[str, float, float]:
        t0 = perf_counter()
        chunks: list[str] = []
        ttft = None
        for piece, piece_ttft, done in self.reply_stream(
            user_text, history, visual_note=visual_note
        ):
            if piece_ttft is not None and ttft is None:
                ttft = piece_ttft
            if piece:
                chunks.append(piece)
        total = (perf_counter() - t0) * 1000
        return "".join(chunks).strip(), (ttft or total), total

    def reply_stream(
        self, user_text: str, history: list[dict], visual_note: str | None = None
    ):
        """Yield (piece, ttft_ms_or_None, done). ttft set on first non-empty piece only."""
        import json

        messages = [{"role": "system", "content": self.system}]
        messages.extend(history)
        messages.append(
            {"role": "user", "content": format_user_message(user_text, visual_note)}
        )
        t0 = perf_counter()
        ttft_sent = False
        with requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": True,
                "keep_alive": "2h",
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.num_predict,
                },
            },
            stream=True,
            timeout=60,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                payload = json.loads(line)
                piece = payload.get("message", {}).get("content", "")
                done = bool(payload.get("done"))
                ttft = None
                if piece and not ttft_sent:
                    ttft = (perf_counter() - t0) * 1000
                    ttft_sent = True
                if piece or done:
                    yield piece, ttft, done
                if done:
                    break

