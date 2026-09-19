from __future__ import annotations

from time import perf_counter

import requests


class LocalMayor:
    def __init__(self, host: str, model: str, num_predict: int, temperature: float, system: str):
        self.host = host.rstrip("/")
        self.model = model
        self.num_predict = num_predict
        self.temperature = temperature
        self.system = system

    def ping(self) -> str:
        r = requests.get(f"{self.host}/api/tags", timeout=3)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(self.model in n or n.startswith(self.model) for n in names):
            available = ", ".join(names) or "(none)"
            raise RuntimeError(
                f"Ollama has no model matching {self.model!r}. Pulled: {available}"
            )
        return self.model

    def reply(self, user_text: str, history: list[dict]) -> tuple[str, float, float]:
        messages = [{"role": "system", "content": self.system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text or "(silence)"})
        t0 = perf_counter()
        ttft = None
        chunks: list[str] = []
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
                import json

                payload = json.loads(line)
                piece = payload.get("message", {}).get("content", "")
                if piece:
                    if ttft is None:
                        ttft = (perf_counter() - t0) * 1000
                    chunks.append(piece)
                if payload.get("done"):
                    break
        total = (perf_counter() - t0) * 1000
        return "".join(chunks).strip(), (ttft or total), total
