"""Unit tests for the story evidence recorder (hardware-free).

Exercises tests/stories/conftest.py's StoryRecorder visual-assessment
logic: blank detection, duplicate detection, state sidecars, and the
attach() rename away from the "unknown" artifacts dir. The recorder is
what keeps the "film well" principle honest — a black or byte-identical
screenshot must be marked degraded, never silently accepted as proof
(the sentinel-garbage lesson from the bench record).
"""
import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "story_conftest", REPO_ROOT / "tests" / "stories" / "conftest.py")
assert _spec is not None and _spec.loader is not None
story_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(story_conftest)


class FakeDevice:
    name = "fake-device"

    def __init__(self, image_factory, state=None):
        self._image_factory = image_factory
        self._state = state

    def screenshot(self, path):
        if self._image_factory is None:
            return False
        self._image_factory(path)
        return True

    def state_text(self):
        return self._state


def _black_frame(path):
    # no color arg -> PIL fills with zeros (black); stub-clean
    Image.new("RGB", (720, 1520)).save(path)


def _content_frame(seed):
    def write(path):
        img = Image.new("RGB", (720, 1520))
        for y in range(0, 1520, 7):
            for x in range(0, 720, 5):
                img.putpixel(
                    (x, y), ((x * y + seed) % 256, seed % 256,
                             (x + seed) % 256))
        img.save(path)
    return write


def _recorder(tmp_path, device):
    art_dir = tmp_path / "artifacts" / "unknown"
    art_dir.mkdir(parents=True)
    rec = story_conftest.StoryRecorder(str(art_dir))
    rec.attach(device)
    return rec


def test_blank_screenshot_flagged_degraded(tmp_path):
    rec = _recorder(tmp_path, FakeDevice(_black_frame))
    rec.shot("01-portal", "portal visible")
    assert rec.steps[0]["visual"] == "degraded:blank"
    assert rec.steps[0]["evidence_ok"] is False


def test_duplicate_screenshot_flagged_degraded(tmp_path):
    rec = _recorder(tmp_path, FakeDevice(_content_frame(seed=3)))
    rec.shot("01-first", "state A")
    rec.shot("02-second", "state B — must differ")
    assert rec.steps[0]["visual"] == "ok"
    assert rec.steps[1]["visual"] == "degraded:duplicate"


def test_real_content_passes(tmp_path):
    dev = FakeDevice(_content_frame(seed=1))
    rec = _recorder(tmp_path, dev)
    rec.shot("01", "claim")
    rec.write_manifest()
    assert rec.steps[0]["visual"] == "ok"
    assert rec.steps[0]["evidence_ok"] is True


def test_state_sidecar_written_and_referenced(tmp_path):
    dev = FakeDevice(_content_frame(seed=5),
                     state="validation: VALIDATED\nwifi: fake\n")
    rec = _recorder(tmp_path, dev)
    rec.shot("04-os-validated", "OS validated")
    state_path = Path(rec.steps[0]["state"])
    assert state_path.exists()
    assert "VALIDATED" in state_path.read_text()


def test_attach_renames_unknown_artifacts_dir(tmp_path):
    rec = _recorder(tmp_path, FakeDevice(_content_frame(seed=9)))
    assert rec.art_dir.endswith("fake-device")
    assert (tmp_path / "artifacts" / "fake-device").is_dir()
    assert not (tmp_path / "artifacts" / "unknown").exists()


def test_manifest_records_degraded_visual(tmp_path):
    rec = _recorder(tmp_path, FakeDevice(_black_frame))
    rec.shot("01", "claim")
    rec.write_manifest()
    data = json.loads((Path(rec.art_dir) / "evidence-steps.json").read_text())
    assert data["steps"][0]["visual"] == "degraded:blank"


def test_size_fallback_without_pil(tmp_path, monkeypatch):
    monkeypatch.setattr(story_conftest, "Image", None)
    rec = _recorder(tmp_path, FakeDevice(_black_frame))
    rec.shot("01", "claim")
    assert rec.steps[0]["visual"] == "degraded:blank"


def test_failed_capture_recorded_loudly_with_state_sidecar(tmp_path):
    dev = FakeDevice(None, state="validation: VALIDATED\nwifi: fake\n")
    rec = _recorder(tmp_path, dev)
    path = rec.shot("01-portal", "portal visible")
    assert path is None
    entry = rec.steps[0]
    assert entry["visual"] == "capture-failed"
    assert entry["evidence_ok"] is False
    assert Path(entry["state"]).exists()
    rec.write_manifest()
    data = json.loads((Path(rec.art_dir) / "evidence-steps.json").read_text())
    assert data["steps"][0]["visual"] == "capture-failed"
