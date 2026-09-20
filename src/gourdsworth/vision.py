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
    "vampire",
    "skeleton",
    "mummy",
    "fairy",
    "unicorn",
    "mermaid",
    "dragon",
    "knight",
    "cat",
    "cowboy",
    "pop star",
    "minecraft",
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
    person_count: int | None = None
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


def _phrase_for_label(lab: str) -> str:
    lab = (lab or "homemade").strip().lower() or "homemade"
    if lab == "group":
        return "group"
    if lab.endswith("costume"):
        return lab
    return f"{lab} costume"


def format_visual_note(
    label: str | Sequence[str],
    *,
    person_count: int | None = None,
) -> str:
    """One line for the mayor. Optional person_count is YOLO/presence — not CLIP."""
    if isinstance(label, str):
        labs = [label]
    else:
        labs = list(label)
    cleaned: list[str] = []
    for raw in labs:
        lab = (raw or "").strip().lower()
        if not lab or note_contains_identity(lab):
            continue
        if lab == "group":
            continue  # headcount comes from person_count, not the CLIP "group" tag
        if lab not in cleaned:
            cleaned.append(lab)
    if not cleaned:
        cleaned = ["homemade"]
    phrases = [_phrase_for_label(lab) for lab in cleaned]
    parts: list[str] = []
    if person_count is not None and person_count >= 0:
        n = min(int(person_count), 8)
        if n == 0:
            parts.append("walk looks empty")
        elif n == 1:
            parts.append("about 1 citizen")
        else:
            parts.append(f"about {n} citizens")
    if len(phrases) == 1:
        parts.append(phrases[0])
    else:
        parts.append(", ".join(phrases))
    body = "; ".join(parts)
    note = f"{NOTE_PREFIX} {body}."
    if note_contains_identity(note):
        note = f"{NOTE_PREFIX} homemade costume."
    return note


def select_costume_labels(
    ranked: Sequence[tuple[str, float]],
    *,
    min_score: float = 0.15,
    max_labels: int = 2,
    relative_floor: float = 0.40,
    person_count: int | None = None,
) -> list[str]:
    """Pick a short costume list. Cap by person_count when known (1 person → 1 label)."""
    if person_count is not None:
        if person_count <= 0:
            return ["homemade"]
        max_labels = min(max_labels, max(1, int(person_count)))
    if not ranked:
        return ["homemade"]
    top_lab, top_score = ranked[0]
    if top_score < min_score or note_contains_identity(top_lab):
        return ["homemade"]

    # Strong seconds only — weak CLIP tails were inventing "three costumes / three people"
    floor = max(float(min_score), float(top_score) * float(relative_floor))
    picked: list[str] = []
    for lab, score in ranked:
        lab = (lab or "").strip().lower()
        if not lab or note_contains_identity(lab) or lab == "group":
            continue
        if score < floor and lab != top_lab:
            continue
        if lab == "homemade" and picked:
            continue
        if lab not in picked:
            picked.append(lab)
        if len(picked) >= max_labels:
            break
    return picked or ["homemade"]


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
    # Set offline *before* importing open_clip / huggingface_hub.
    _prefer_local_hub()
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


def _clip_weights_cached() -> bool:
    """True when open_clip / CLIP weights already live under ~/.cache."""
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    if not hub.is_dir():
        return False
    keys = ("vit_base_patch32_clip", "open_clip", "clip_224", "openai")
    for p in hub.iterdir():
        name = p.name.lower()
        if any(k in name for k in keys) and p.is_dir():
            # Require an actual weights file, not an empty stub
            if any(p.rglob("*.safetensors")) or any(p.rglob("*.bin")):
                return True
    return False


def _prefer_local_hub() -> None:
    """Do not phone Hugging Face when weights are already on disk (porch privacy UX)."""
    import os

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if _clip_weights_cached():
        os.environ["HF_HUB_OFFLINE"] = "1"


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


def detect_person_boxes(
    jpeg: bytes,
    *,
    model=None,
    conf: float = 0.35,
) -> list[tuple[int, int, int, int]] | None:
    """Return person boxes (x1,y1,x2,y2) from a RAM JPEG. None if YOLO unavailable."""
    if model is None:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return None
    try:
        results = model.predict(frame, classes=[0], conf=float(conf), verbose=False)
        if not results:
            return []
        boxes = getattr(results[0], "boxes", None)
        if boxes is None or boxes.xyxy is None:
            return []
        out: list[tuple[int, int, int, int]] = []
        h, w = frame.shape[:2]
        for row in boxes.xyxy.detach().cpu().tolist():
            x1, y1, x2, y2 = (int(v) for v in row[:4])
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w - 1, x2), min(h - 1, y2)
            if x2 > x1 and y2 > y1:
                out.append((x1, y1, x2, y2))
        # Largest first — helps when one person dominates
        out.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        return out[:6]
    except Exception:
        return None
    finally:
        del frame


def count_persons_jpeg(
    jpeg: bytes,
    *,
    model=None,
    conf: float = 0.35,
) -> int | None:
    """Count COCO 'person' boxes in a RAM JPEG. None if YOLO is unavailable."""
    boxes = detect_person_boxes(jpeg, model=model, conf=conf)
    if boxes is None:
        return None
    return len(boxes)


def _crop_jpeg(
    jpeg: bytes, box: tuple[int, int, int, int], *, pad: float = 0.12
) -> bytes | None:
    """Crop one person box (+pad) to a new RAM JPEG. Never writes a file."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return None
    try:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        bw, bh = x2 - x1, y2 - y1
        px, py = int(bw * pad), int(bh * pad)
        x1, y1 = max(0, x1 - px), max(0, y1 - py)
        x2, y2 = min(w, x2 + px), min(h, y2 + py)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return None
        return bytes(buf)
    finally:
        del frame


def classify_persons_then_frame(
    jpeg: bytes,
    labels: Sequence[str],
    *,
    classify_fn: Callable[[bytes, Sequence[str]], Sequence[tuple[str, float]]],
    min_score: float = 0.15,
    person_model=None,
    person_conf: float = 0.35,
) -> VisionResult:
    """Unify count + costumes: CLIP each YOLO person crop, else full-frame CLIP."""
    boxes = detect_person_boxes(jpeg, model=person_model, conf=person_conf)
    person_count = None if boxes is None else len(boxes)

    per_labels: list[str] = []
    per_scores: list[float] = []
    if boxes:
        for box in boxes:
            crop = _crop_jpeg(jpeg, box)
            if not crop:
                continue
            try:
                ranked = sorted(
                    (
                        (str(lab).strip().lower(), float(score))
                        for lab, score in classify_fn(crop, labels)
                    ),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
            finally:
                del crop
            if not ranked:
                continue
            lab, score = ranked[0]
            if lab == "group" or note_contains_identity(lab):
                continue
            if score < min_score:
                lab = "homemade"
            per_labels.append(lab)
            per_scores.append(score)

    if per_labels:
        # One costume per detected person; keep order, allow duplicates only once in note
        unique: list[str] = []
        for lab in per_labels:
            if lab not in unique:
                unique.append(lab)
        label = unique[0]
        score = per_scores[0]
        # Full-frame top3 still useful for logs
        full_ranked = sorted(
            (
                (str(lab).strip().lower(), float(sc))
                for lab, sc in classify_fn(jpeg, labels)
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        top3 = full_ranked[:3]
        note = format_visual_note(unique, person_count=person_count)
        if safe_visual_note(note) is None:
            note = format_visual_note("homemade", person_count=person_count)
            label = "homemade"
        return VisionResult(
            label=label,
            score=score,
            top3=top3,
            note=note,
            person_count=person_count,
        )

    # No usable per-person crops → whole-frame CLIP (existing path)
    return classify_costume(
        jpeg,
        labels,
        classify_fn=classify_fn,
        min_score=min_score,
        person_count=person_count,
    )



def classify_costume(
    jpeg: bytes,
    labels: Sequence[str],
    *,
    classify_fn: Callable[[bytes, Sequence[str]], Sequence[tuple[str, float]]],
    min_score: float = 0.15,
    person_count: int | None = None,
) -> VisionResult:
    """Map a RAM JPEG to closed-list label(s). Drops the bytes; never logs them."""
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
        labels_for_note = ["homemade"]
    else:
        label, score = ranked[0]
        allowed_set = set(allowed)
        if label not in allowed_set or score < min_score or note_contains_identity(label):
            homemade_score = next((s for lab, s in ranked if lab == "homemade"), score)
            label, score = "homemade", homemade_score
            labels_for_note = ["homemade"]
        else:
            labels_for_note = select_costume_labels(
                ranked,
                min_score=min_score,
                max_labels=2,
                person_count=person_count,
            )
            label = labels_for_note[0]
            score = next(s for lab, s in ranked if lab == label)

    note = format_visual_note(labels_for_note, person_count=person_count)
    safe = safe_visual_note(note)
    if safe is None:
        label = "homemade"
        note = format_visual_note("homemade", person_count=person_count)
    return VisionResult(
        label=label,
        score=score,
        top3=top3,
        note=note,
        person_count=person_count,
    )


def run_still(
    *,
    capture_fn: Callable[[], bytes],
    classify_fn: Callable[[bytes, Sequence[str]], Sequence[tuple[str, float]]],
    labels: Sequence[str],
    min_score: float = 0.15,
    person_model=None,
    person_conf: float = 0.35,
) -> VisionResult:
    t0 = perf_counter()
    jpeg = capture_fn()
    try:
        result = classify_persons_then_frame(
            jpeg,
            labels,
            classify_fn=classify_fn,
            min_score=min_score,
            person_model=person_model,
            person_conf=person_conf,
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
        _prefer_local_hub()
        missing = missing_vision_deps()
        if missing:
            raise RuntimeError(missing)
        import open_clip
        import torch

        _limit_vision_cpu()
        t0 = perf_counter()
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                self.model_name, pretrained=self.pretrained
            )
        except Exception:
            # First-time install: allow one weight download, then stay offline next run.
            import os

            os.environ.pop("HF_HUB_OFFLINE", None)
            print(
                "  (one-time) downloading CLIP weights to local cache — "
                "not porch audio/video; nothing from the camera is uploaded."
            )
            model, _, preprocess = open_clip.create_model_and_transforms(
                self.model_name, pretrained=self.pretrained
            )
            os.environ["HF_HUB_OFFLINE"] = "1"
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
        self._person_model = None
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
        # Optional YOLOv8n person counter (V1). CLIP still works if YOLO is missing.
        try:
            from ultralytics import YOLO

            self._person_model = YOLO("yolov8n.pt")
        except Exception:
            self._person_model = None
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
                person_model=self._person_model,
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

    def submit_jpeg(self, jpeg: bytes) -> Future | None:
        """Classify a RAM JPEG from the crate. Never opens a camera. Never waits.

        Same in-flight skip as submit_snap. Caller should drop ``jpeg`` after
        this returns; the worker keeps its own copy until CLIP finishes.
        """
        if self.skip_reason:
            return None
        if self._future is not None and not self._future.done():
            return None
        blob = bytes(jpeg)
        future: Future = Future()

        def worker() -> None:
            nonlocal blob

            def _capture() -> bytes:
                return blob

            def _classify(frame: bytes, labels: Sequence[str]):
                return self._classifier.classify_jpeg(frame, labels)

            try:
                future.set_result(
                    run_still(
                        capture_fn=_capture,
                        classify_fn=_classify,
                        labels=self.labels,
                        min_score=self.min_score,
                        person_model=self._person_model,
                    )
                )
            except Exception as exc:  # pragma: no cover
                if not future.done():
                    future.set_result(VisionResult(skip_reason=str(exc)))
            finally:
                blob = b""

        threading.Thread(
            target=worker, name="gourdsworth-vision-jpeg", daemon=True
        ).start()
        self._future = future
        return future

    def close(self) -> None:
        # Daemon worker; nothing to join. Camera is released inside capture_jpeg_ram.
        return
