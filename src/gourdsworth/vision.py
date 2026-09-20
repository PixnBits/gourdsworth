"""Local vision sidecar: one RAM JPEG, closed-list costume labels.

Never on the voice path. Never writes a frame. Never guesses identity.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

NOTE_PREFIX = "Visual note (may be wrong, never name a person):"

PRIVACY_SIGN = (
    "A camera looks only when someone is at the crate. "
    "No faces are saved. Nothing leaves this house."
)

DEFAULT_LABELS: tuple[str, ...] = (
    "princess",
    "prince",
    "astronaut",
    "dinosaur",
    "firefighter",
    "police",
    "witch",
    "wizard",
    "ghost",
    "superhero",
    "ninja",
    "pirate",
    "animal",
    "bear",
    "sports",
    "robot",
    "food",
    "hot dog",
    "pumpkin",
    "homemade",
    "group",
)

# Scan the costume clause only — the prefix itself contains the word "name".
IDENTITY_RE = re.compile(
    r"\b("
    r"names?|named|identity|"
    r"faces?|facial|"
    r"ages?|years?\s+old|year-old|"
    r"girl|boy|woman|man|lady|guy|male|female|"
    r"child|children|kid|kids|toddler|teen|teenager|adult|"
    r"who\s+they\s+are|"
    r"school|address|street|phone|"
    r"mister|miss|mrs|sir"
    r")\b",
    re.I,
)

_TEXT_TEMPLATES = (
    "a Halloween costume of a {}",
    "a photo of a {} costume",
)


@dataclass
class VisionResult:
    label: str = "homemade"
    score: float = 0.0
    top3: list[tuple[str, float]] = field(default_factory=list)
    note: str = ""
    vision_ms: float = 0.0
    skip_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.skip_reason is None and bool(self.note)


def package_labels_path() -> Path:
    return Path(__file__).with_name("costume_labels.txt")


def load_labels(path: str | Path | None = None) -> list[str]:
    target = Path(path) if path else package_labels_path()
    if not target.is_file():
        return list(DEFAULT_LABELS)
    labels: list[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        labels.append(line.lower())
    return labels or list(DEFAULT_LABELS)


def note_contains_identity(note: str) -> bool:
    """True if the costume clause (not the boilerplate prefix) guesses identity."""
    text = note or ""
    if text.startswith(NOTE_PREFIX):
        text = text[len(NOTE_PREFIX) :]
    return bool(IDENTITY_RE.search(text))


def format_visual_note(label: str) -> str:
    lab = (label or "homemade").strip().lower() or "homemade"
    if note_contains_identity(lab):
        lab = "homemade"
    if lab == "group":
        phrase = "group costumes"
    elif lab.endswith("costume"):
        phrase = lab
    else:
        phrase = f"{lab} costume"
    note = f"{NOTE_PREFIX} {phrase}."
    if note_contains_identity(note):
        note = f"{NOTE_PREFIX} homemade costume."
    return note


def safe_visual_note(note: str | None) -> str | None:
    """Return the note only if it is non-empty and identity-free."""
    text = (note or "").strip()
    if not text or note_contains_identity(text):
        return None
    return text


def attach_visual_note(user_text: str, note: str | None) -> str:
    user = (user_text or "").strip() or "(silence)"
    safe = safe_visual_note(note)
    if not safe:
        return user
    return f"{safe}\n\n{user}"


def format_top3(top3: Sequence[tuple[str, float]]) -> str:
    return ", ".join(f"{lab} {score:.2f}" for lab, score in list(top3)[:3])


def missing_vision_deps() -> str | None:
    missing: list[str] = []
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python-headless")
    try:
        import open_clip  # noqa: F401
    except ImportError:
        missing.append("open-clip-torch")
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("pillow")
    if not missing:
        return None
    return (
        "vision extras not installed ("
        + ", ".join(missing)
        + "). pip install 'gourdsworth[vision]'"
    )


def _limit_vision_cpu() -> None:
    """Keep CLIP/OpenCV from starving the voice loop on the same box."""
    try:
        import torch

        torch.set_num_threads(max(1, min(2, torch.get_num_threads())))
    except Exception:
        pass
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:
        pass


def capture_jpeg_ram(
    camera_index: int = 0, width: int = 640, height: int = 480
) -> bytes:
    """Grab one frame and encode JPEG in RAM. Never writes a file."""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "vision extras not installed. pip install 'gourdsworth[vision]'"
        ) from exc

    _limit_vision_cpu()
    cap = cv2.VideoCapture(int(camera_index))
    frame = None
    try:
        if not cap.isOpened():
            raise RuntimeError(f"camera {camera_index} could not be opened")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        ok = False
        for _ in range(3):
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"camera {camera_index} produced no frame")
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 85],
        )
        if not ok:
            raise RuntimeError("jpeg encode into RAM failed")
        return bytes(buf)
    finally:
        cap.release()
        del frame


def classify_costume(
    jpeg: bytes,
    labels: Sequence[str],
    *,
    classify_fn: Callable[[bytes, Sequence[str]], Sequence[tuple[str, float]]],
    min_score: float = 0.15,
) -> VisionResult:
    """Map a RAM JPEG to a closed-list label. Drops the bytes; never logs them."""
    allowed = [str(x).strip().lower() for x in labels if str(x).strip()]
    if not allowed:
        allowed = list(DEFAULT_LABELS)

    ranked = [
        (str(lab).strip().lower(), float(score))
        for lab, score in classify_fn(jpeg, allowed)
    ]
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    top3 = ranked[:3]

    if not ranked:
        label, score = "homemade", 0.0
    else:
        label, score = ranked[0]
        allowed_set = set(allowed)
        if label not in allowed_set or score < min_score or note_contains_identity(label):
            homemade_score = next((s for lab, s in ranked if lab == "homemade"), score)
            label, score = "homemade", homemade_score

    note = format_visual_note(label)
    safe = safe_visual_note(note)
    if safe is None:
        label = "homemade"
        note = format_visual_note("homemade")
    return VisionResult(label=label, score=score, top3=top3, note=note)


def run_still(
    *,
    capture_fn: Callable[[], bytes],
    classify_fn: Callable[[bytes, Sequence[str]], Sequence[tuple[str, float]]],
    labels: Sequence[str],
    min_score: float = 0.15,
) -> VisionResult:
    t0 = perf_counter()
    jpeg = capture_fn()
    try:
        result = classify_costume(
            jpeg, labels, classify_fn=classify_fn, min_score=min_score
        )
    finally:
        del jpeg
    result.vision_ms = (perf_counter() - t0) * 1000
    return result


def take_ready(future: Future | None) -> VisionResult | None:
    """Return a result only if the future is already done. Never waits."""
    if future is None or not future.done():
        return None
    try:
        result = future.result(timeout=0)
    except Exception as exc:
        return VisionResult(skip_reason=f"vision failed: {exc}")
    return result


class OpenClipClassifier:
    """Lazy local CLIP. Weights download on first load(); never call a cloud API."""

    def __init__(self, model_name: str = "ViT-B-32", pretrained: str = "openai"):
        self.model_name = model_name
        self.pretrained = pretrained
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._device = "cpu"
        self._cached_labels: tuple[str, ...] | None = None
        self._cached_text = None

    def load(self) -> float:
        missing = missing_vision_deps()
        if missing:
            raise RuntimeError(missing)
        import open_clip
        import torch

        _limit_vision_cpu()
        t0 = perf_counter()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _, preprocess = open_clip.create_model_and_transforms(
            self.model_name, pretrained=self.pretrained
        )
        model = model.to(self._device)
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(self.model_name)
        return (perf_counter() - t0) * 1000

    def classify_jpeg(
        self, jpeg: bytes, labels: Sequence[str]
    ) -> list[tuple[str, float]]:
        import torch
        from PIL import Image

        if self._model is None or self._preprocess is None or self._tokenizer is None:
            raise RuntimeError("CLIP model not loaded")
        image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        try:
            tensor = self._preprocess(image).unsqueeze(0).to(self._device)
            text_features = self._text_features(labels)
            with torch.no_grad():
                image_features = self._model.encode_image(tensor)
                image_features = image_features / image_features.norm(
                    dim=-1, keepdim=True
                )
                scale = 100.0
                if hasattr(self._model, "logit_scale"):
                    scale = self._model.logit_scale.exp()
                logits = scale * image_features @ text_features.T
                probs = logits.softmax(dim=-1)[0].detach().cpu().tolist()
            return [(str(lab), float(p)) for lab, p in zip(labels, probs)]
        finally:
            image.close()
            del image

    def _text_features(self, labels: Sequence[str]):
        import torch

        key = tuple(str(x) for x in labels)
        if self._cached_labels == key and self._cached_text is not None:
            return self._cached_text
        texts = [tmpl.format(lab) for lab in key for tmpl in _TEXT_TEMPLATES]
        tokens = self._tokenizer(texts).to(self._device)
        with torch.no_grad():
            feats = self._model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True)
            feats = feats.reshape(len(key), len(_TEXT_TEMPLATES), -1).mean(dim=1)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        self._cached_labels = key
        self._cached_text = feats
        return feats


class VisionSidecar:
    """Fire-and-forget still: Talk starts a daemon thread; the voice loop never joins it."""

    def __init__(
        self,
        *,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
        labels: Sequence[str] | None = None,
        min_score: float = 0.15,
        model_name: str = "ViT-B-32",
        pretrained: str = "openai",
    ):
        self.camera_index = int(camera_index)
        self.width = int(width)
        self.height = int(height)
        self.labels = [str(x).strip().lower() for x in (labels or DEFAULT_LABELS) if str(x).strip()]
        self.min_score = float(min_score)
        self.model_name = model_name
        self.pretrained = pretrained
        self.load_ms = 0.0
        self.skip_reason: str | None = None
        self._classifier = OpenClipClassifier(model_name, pretrained)
        self._future: Future | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> VisionSidecar:
        v = cfg.get("vision") or {}
        raw_labels = v.get("labels")
        if raw_labels:
            labels = [str(x).strip().lower() for x in raw_labels if str(x).strip()]
        else:
            labels_file = v.get("labels_file")
            labels = load_labels(labels_file)
        return cls(
            camera_index=int(v.get("camera_index", 0)),
            width=int(v.get("width", 640)),
            height=int(v.get("height", 480)),
            labels=labels,
            min_score=float(v.get("min_score", 0.15)),
            model_name=str(v.get("model", "ViT-B-32")),
            pretrained=str(v.get("pretrained", "openai")),
        )

    def prepare(self) -> tuple[bool, str]:
        """Load CLIP. Do not open the camera. Camera stays off until Talk / --snap."""
        missing = missing_vision_deps()
        if missing:
            self.skip_reason = missing
            return False, missing
        try:
            self.load_ms = self._classifier.load()
        except Exception as exc:
            self.skip_reason = f"CLIP model failed to load ({exc})"
            return False, self.skip_reason
        self.skip_reason = None
        return True, ""

    def snap(self) -> VisionResult:
        def _classify(jpeg: bytes, labels: Sequence[str]):
            return self._classifier.classify_jpeg(jpeg, labels)

        try:
            return run_still(
                capture_fn=lambda: capture_jpeg_ram(
                    self.camera_index, self.width, self.height
                ),
                classify_fn=_classify,
                labels=self.labels,
                min_score=self.min_score,
            )
        except Exception as exc:
            return VisionResult(skip_reason=str(exc))

    def submit_snap(self) -> Future | None:
        """Start a still on a daemon thread. Skip this turn if the previous still is in flight."""
        if self._future is not None and not self._future.done():
            return None
        future: Future = Future()

        def worker() -> None:
            try:
                future.set_result(self.snap())
            except Exception as exc:  # pragma: no cover
                if not future.done():
                    future.set_exception(exc)

        threading.Thread(
            target=worker, name="gourdsworth-vision", daemon=True
        ).start()
        self._future = future
        return future

    def close(self) -> None:
        # Daemon worker; nothing to join. Camera is released inside capture_jpeg_ram.
        return
