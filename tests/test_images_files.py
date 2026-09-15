"""Drawing normalisation at intake and Files-API transport with inline fallback."""
from __future__ import annotations

import io
import json
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image

from claim_agent.models.request import MaterialBundle, RunRequest
from claim_agent.provider.base import CallSpec, GenParams, ImagePart
from claim_agent.provider.files import FileStore
from claim_agent.provider.gemini import GeminiProvider
from claim_agent.sources.images import normalize_image


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_large_drawing_is_bounded_and_small_one_untouched(tmp_path):
    big, small = _png(4000, 3000), _png(800, 600)
    n = normalize_image(big, "image/png", 2048)
    assert n.resized and n.size == (2048, 1536) and n.original_size == (4000, 3000) and len(n.data) < len(big)
    m = normalize_image(small, "image/png", 2048)
    assert not m.resized and m.data == small
    assert normalize_image(b"not an image", "image/png").note.startswith("정규화 실패")


def test_bundle_records_both_hashes_and_digest_follows_the_bound(tmp_path):
    p = tmp_path / "도1.png"
    p.write_bytes(_png(3000, 3000))
    (tmp_path / "설명.md").write_text("발명 설명", encoding="utf-8")
    req = RunRequest(request_text="r", invention_sources=[str(tmp_path / "설명.md")], drawings=[str(p)])
    b1 = MaterialBundle.load(req, 2048)
    img = [i for i in b1.items if i.kind == "image"][0]
    assert img.normalized_sha256 and img.normalized_sha256 != img.sha256 and img.image_meta["resized"] and img.image_meta["size"] == [2048, 2048] or img.image_meta["size"] == (2048, 2048)
    assert img.as_meta()["normalized_sha256"] == img.normalized_sha256
    b2 = MaterialBundle.load(req, 0)      # no bound: original bytes, different input_revision
    assert b1.digest() != b2.digest() and [i for i in b2.items if i.kind == "image"][0].normalized_sha256 is None


class FakeFiles:
    def __init__(self, fail=False):
        self.uploads, self.fail = [], fail

    def upload(self, file, config):
        if self.fail:
            raise RuntimeError("quota")
        self.uploads.append(config)
        return SimpleNamespace(name=f"files/{len(self.uploads)}", uri=f"https://files/{len(self.uploads)}", state="ACTIVE")


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        usage = SimpleNamespace(prompt_token_count=10, cached_content_token_count=0, thoughts_token_count=1, candidates_token_count=3, total_token_count=14)
        return SimpleNamespace(text=json.dumps({"status": "PASS"}), usage_metadata=usage, candidates=[SimpleNamespace(finish_reason="STOP")], automatic_function_calling_history=[])


def _spec(images):
    return CallSpec(role="oa-strategy-reviewer", scope="INDEPENDENT", model="m", system_instruction="SYS", packet_text="PKT", sources_block="", json_schema={"type": "object"}, gen=GenParams(0.1, "HIGH", 100), use_cache=False, images=images)


def test_files_api_uploads_once_and_references_by_uri(tmp_path):
    client = SimpleNamespace(models=FakeModels(), caches=None, files=FakeFiles())
    store = FileStore(client, tmp_path / "files.json")
    gp = GeminiProvider(client, None, files=store)
    img = ImagePart("image/png", b"drawing-bytes", "[drawing] 도1.png", "sha-1")
    r1 = gp.generate(_spec([img]))
    r2 = gp.generate(_spec([img, replace(img, label="again")]))
    assert len(client.files.uploads) == 1 and store.uploads == 1 and store.reuses == 2
    assert r1.raw["image_transport"] == "files_api" and r1.raw["inline_image_bytes"] == 0 and r2.raw["image_transport"] == "files_api"
    parts = client.models.calls[0][0].parts
    assert parts[-1].file_data.file_uri == "https://files/1" and parts[-1].file_data.mime_type == "image/png"
    # a new process reuses the registry entry without uploading again
    store2 = FileStore(client, tmp_path / "files.json")
    assert store2.get("sha-1", b"drawing-bytes", "image/png").uri == "https://files/1" and len(client.files.uploads) == 1


def test_files_api_failure_falls_back_to_inline(tmp_path):
    client = SimpleNamespace(models=FakeModels(), caches=None, files=FakeFiles(fail=True))
    gp = GeminiProvider(client, None, files=FileStore(client, tmp_path / "files.json"))
    r = gp.generate(_spec([ImagePart("image/png", b"drawing-bytes", "도1", "sha-2")]))
    assert r.raw["image_transport"] == "inline" and r.raw["inline_image_bytes"] == len(b"drawing-bytes")
    assert client.models.calls[0][0].parts[-1].inline_data.data == b"drawing-bytes"
    gp2 = GeminiProvider(client, None, files=FileStore(client, tmp_path / "off.json", enabled=False))
    assert gp2.generate(_spec([ImagePart("image/png", b"x", "d", "sha-3")])).raw["image_transport"] == "inline"
