from gourdsworth.llm import pick_ollama_model


def test_prefers_configured_when_present():
    names = ["qwen2.5:14b", "llama3.1:8b", "gemma2:27b"]
    assert pick_ollama_model(names, "llama3.1:8b") == "llama3.1:8b"


def test_prefers_8b_instruct_over_14b():
    names = ["qwen2.5:14b", "qwen2.5:7b-instruct", "gemma2:27b"]
    assert pick_ollama_model(names, "missing:8b") == "qwen2.5:7b-instruct"


def test_falls_back_to_14b_when_that_is_smallest_instruct():
    names = ["qwen2.5:14b", "nomic-embed-text", "llava:34b"]
    assert pick_ollama_model(names, "llama3.1:8b") == "qwen2.5:14b"
