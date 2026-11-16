"""Specs for the Ollama vision describer adapter (vision-on-demand V1.3).

Every spec runs against ``httpx.MockTransport``; the real Ollama is never
called.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import httpx
import pytest

from opencohost.core.vision import describer as describer_mod
from opencohost.core.vision.describer import (
    DESCRIBER_SYSTEM_PROMPT,
    MULTI_FRAME_HINT,
    OllamaVisionDescriber,
    VisionDescribeError,
    VisionDescriber,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


def _ok(content: str = "La barra de vida marca 75/100.") -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True})


def _describer(handler, **kwargs) -> OllamaVisionDescriber:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    # English unless a spec says otherwise: keeps the suite independent of the active locale.
    kwargs.setdefault("lang", "en")
    return OllamaVisionDescriber(client=client, **kwargs)


class _BlockingStream(httpx.SyncByteStream):
    """Body that never arrives until the response is closed."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self):
        self.closed.wait(5.0)
        if False:  # pragma: no cover - generator marker
            yield b""

    def close(self) -> None:
        self.closed.set()


class TestRequestShape:
    def test_posts_chat_payload_to_own_port(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["body"] = json.loads(request.content)
            return _ok()

        d = _describer(handler)
        d.describe(JPEG, "¿cuánta vida tengo?", timeout_s=5.0)

        assert seen["method"] == "POST"
        assert seen["url"] == "http://127.0.0.1:11435/api/chat"
        body = seen["body"]
        assert body["model"] == "qwen3-vl:4b-instruct"
        assert body["stream"] is False
        assert "think" not in body
        # V1.8: same residency the controller warmed with, so a describe never shortens it
        assert body["keep_alive"] == -1
        assert body["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 350}

        system, user = body["messages"]
        assert system["role"] == "system"
        assert system["content"] == DESCRIBER_SYSTEM_PROMPT
        assert "images" not in system
        assert user["role"] == "user"
        assert user["images"] == [base64.b64encode(JPEG).decode("ascii")]
        assert "Player question (for relevance only): ¿cuánta vida tengo?" in user["content"]

    def _capture_body(self, jpeg):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return _ok()

        _describer(handler).describe(jpeg, "¿qué pasó?", timeout_s=5.0)
        return seen["body"]

    def test_single_image_list_matches_the_bytes_form(self):
        assert self._capture_body([JPEG]) == self._capture_body(JPEG)

    def test_single_image_has_no_multi_frame_wording(self):
        user = self._capture_body(JPEG)["messages"][1]
        assert "time order" not in user["content"]

    def test_several_frames_go_in_one_request_in_time_order(self):
        frames = [JPEG + b"1", JPEG + b"2", JPEG + b"3"]
        body = self._capture_body(frames)

        system, user = body["messages"]
        assert user["images"] == [base64.b64encode(f).decode("ascii") for f in frames]
        assert MULTI_FRAME_HINT in user["content"]
        assert MULTI_FRAME_HINT == (
            "These are frames in time order; describe what is visible now and what changed."
        )
        # the descriptive-only rules and language matching are unchanged
        assert system["content"] == DESCRIBER_SYSTEM_PROMPT
        assert "Player question (for relevance only): ¿qué pasó?" in user["content"]

    def test_no_frames_raises_empty_without_a_request(self):
        def handler(request):  # pragma: no cover - must not be reached
            raise AssertionError("no request expected")

        with pytest.raises(VisionDescribeError) as err:
            _describer(handler).describe([], "q", timeout_s=1.0)
        assert err.value.reason == "empty"

    def test_custom_construction_parameters(self):
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return _ok()

        d = _describer(
            handler,
            base_url="http://127.0.0.1:12000/",
            model="other-vl",
            num_ctx=2048,
            num_predict=64,
            keep_alive="5m",
        )
        d.describe(JPEG, "q", timeout_s=5.0)
        assert seen["url"] == "http://127.0.0.1:12000/api/chat"
        assert seen["body"]["model"] == "other-vl"
        assert seen["body"]["keep_alive"] == "5m"
        assert seen["body"]["options"]["num_ctx"] == 2048
        assert seen["body"]["options"]["num_predict"] == 64

    def test_never_targets_main_ollama_port_by_default(self):
        d = OllamaVisionDescriber()
        assert ":11434" not in d.base_url

    def test_satisfies_protocol(self):
        assert isinstance(_describer(lambda r: _ok()), VisionDescriber)


class TestDescriberPrompt:
    @pytest.mark.parametrize(
        "phrase",
        ["describe", "never answer", "recommend", "advise", "roleplay", "address the player",
         "not visible", "always write everything in english"],
    )
    def test_system_prompt_is_purely_descriptive(self, phrase):
        assert phrase in DESCRIBER_SYSTEM_PROMPT.lower()


    # V1.21: room to read on-screen text instead of a two-sentence cap.
    def test_prompt_asks_to_transcribe_visible_text_verbatim(self):
        text = DESCRIBER_SYSTEM_PROMPT.lower()
        assert "faithfully" in text and "translating it into english" in text
        assert "titles" in text and "paragraphs" in text and "labels" in text

    def test_prompt_no_longer_caps_at_two_short_sentences(self):
        text = DESCRIBER_SYSTEM_PROMPT.lower()
        assert "2 short sentences" not in text
        assert "compact paragraph" in text

    def test_default_num_predict_leaves_room_for_the_text(self):
        from opencohost.core.vision.describer import DEFAULT_NUM_PREDICT

        assert DEFAULT_NUM_PREDICT == 350


class TestSuccess:
    def test_returns_stripped_content(self):
        d = _describer(lambda r: _ok("  La vida marca 75/100.  \n"))
        assert d.describe(JPEG, "q", timeout_s=5.0) == "La vida marca 75/100."


class TestErrors:
    @pytest.mark.parametrize("content", ["", "   \n"])
    def test_empty_content_raises_empty(self, content):
        d = _describer(lambda r: _ok(content))
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "empty"

    def test_missing_message_raises_empty(self):
        d = _describer(lambda r: httpx.Response(200, json={"done": True}))
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "empty"

    def test_http_500_raises_http(self):
        d = _describer(lambda r: httpx.Response(500, text="boom"))
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "http"

    def test_non_json_body_raises_http(self):
        d = _describer(lambda r: httpx.Response(200, text="not json"))
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "http"

    def test_transport_timeout_raises_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        d = _describer(handler)
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "timeout"

    def test_connect_error_raises_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        d = _describer(handler)
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0)
        assert exc.value.reason == "unavailable"

    def test_wall_clock_deadline_aborts_stalled_body(self):
        stream = _BlockingStream()
        d = _describer(lambda r: httpx.Response(200, stream=stream))
        started = time.monotonic()
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=0.2)
        assert exc.value.reason == "timeout"
        assert time.monotonic() - started < 2.0
        assert stream.closed.is_set()

    def test_error_message_carries_no_payload_text(self):
        d = _describer(lambda r: httpx.Response(500, text="secret screen text"))
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "my private question", timeout_s=5.0)
        assert "secret" not in str(exc.value)
        assert "private" not in str(exc.value)


class TestCancel:
    def test_pre_set_cancel_raises_without_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return _ok()

        cancel = threading.Event()
        cancel.set()
        d = _describer(handler)
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=5.0, cancel=cancel)
        assert exc.value.reason == "cancelled"
        assert calls == []

    def test_cancel_from_background_thread_aborts_in_flight_request(self):
        stream = _BlockingStream()
        d = _describer(lambda r: httpx.Response(200, stream=stream))
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=10.0, cancel=cancel)
        assert exc.value.reason == "cancelled"
        assert time.monotonic() - started < 2.0
        assert stream.closed.is_set()

    def test_unset_cancel_does_not_interfere(self):
        d = _describer(lambda r: _ok("ok text"))
        assert d.describe(JPEG, "q", timeout_s=5.0, cancel=threading.Event()) == "ok text"


class TestPrivacy:
    def test_never_logs_question_description_or_image(self, caplog):
        caplog.set_level("DEBUG")
        d = _describer(lambda r: _ok("SECRET_DESCRIPTION"))
        d.describe(JPEG, "SECRET_QUESTION", timeout_s=5.0)
        b64 = base64.b64encode(JPEG).decode("ascii")
        for record in caplog.records:
            msg = record.getMessage()
            assert "SECRET_DESCRIPTION" not in msg
            assert "SECRET_QUESTION" not in msg
            assert b64 not in msg

    def test_injected_client_is_not_closed(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: _ok()))
        OllamaVisionDescriber(client=client).describe(JPEG, "q", timeout_s=5.0)
        assert not client.is_closed


class TestRealSocketAbort:
    """Owned-client path against a loopback server that never answers."""

    @pytest.fixture
    def stalling_server(self):
        import socket

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        peer_closed = threading.Event()
        stop = threading.Event()

        def serve():
            srv.settimeout(5.0)
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(0.05)
            with conn:
                while not stop.is_set():
                    try:
                        if conn.recv(65536) == b"":
                            peer_closed.set()
                            return
                    except socket.timeout:
                        continue
                    except OSError:
                        peer_closed.set()
                        return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{srv.getsockname()[1]}", peer_closed
        stop.set()
        srv.close()
        thread.join(timeout=2.0)

    def test_cancel_closes_the_socket(self, stalling_server):
        base_url, peer_closed = stalling_server
        d = OllamaVisionDescriber(base_url=base_url)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=10.0, cancel=cancel)
        assert exc.value.reason == "cancelled"
        assert peer_closed.wait(2.0)

    def test_deadline_closes_the_socket(self, stalling_server):
        base_url, peer_closed = stalling_server
        d = OllamaVisionDescriber(base_url=base_url)
        with pytest.raises(VisionDescribeError) as exc:
            d.describe(JPEG, "q", timeout_s=0.3)
        assert exc.value.reason == "timeout"
        assert peer_closed.wait(2.0)


class TestLocalizedPrompts:
    """V1.33: describer prompts are native per app locale and always translate."""

    @staticmethod
    def _body(lang_kwargs, frames=JPEG, **describe_kwargs):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return _ok()

        d = _describer(handler, **({"lang": None} | lang_kwargs))
        d.describe(frames, "q", timeout_s=5.0, **describe_kwargs)
        return seen["body"]

    @staticmethod
    def _patch_locale(monkeypatch, code=None, fail=False):
        from opencohost.i18n import active as i18n_active

        class _Bundle:
            pass

        def fake():
            if fail:
                raise RuntimeError("boom")
            b = _Bundle()
            b.code = code
            return b

        monkeypatch.setattr(i18n_active, "get_active_bundle", fake)

    def test_spanish_locale_uses_spanish_prompt_that_translates(self, monkeypatch):
        self._patch_locale(monkeypatch, "es")
        body = self._body({})
        system, user = body["messages"]
        assert system["content"] == describer_mod.DESCRIBER_SYSTEM_PROMPT_ES
        assert "traduci" in system["content"].lower()
        assert "español" in system["content"].lower()
        assert "Pregunta del jugador" in user["content"]

    def test_english_locale_uses_english_prompt_that_translates(self, monkeypatch):
        self._patch_locale(monkeypatch, "en")
        system, user = self._body({})["messages"]
        assert system["content"] == DESCRIBER_SYSTEM_PROMPT
        assert "translating it into english" in system["content"].lower()
        assert "Player question (for relevance only): q" in user["content"]

    @pytest.mark.parametrize("code", ["fr", "pt-BR", "", None, "xx-YY"])
    def test_other_locale_falls_back_to_english(self, monkeypatch, code):
        self._patch_locale(monkeypatch, code)
        assert self._body({})["messages"][0]["content"] == DESCRIBER_SYSTEM_PROMPT

    def test_regional_spanish_tag_selects_spanish(self, monkeypatch):
        self._patch_locale(monkeypatch, "es-MX")
        assert self._body({})["messages"][0]["content"] == describer_mod.DESCRIBER_SYSTEM_PROMPT_ES

    def test_accessor_failure_falls_back_to_english_without_raising(self, monkeypatch):
        self._patch_locale(monkeypatch, fail=True)
        assert self._body({})["messages"][0]["content"] == DESCRIBER_SYSTEM_PROMPT

    def test_explicit_lang_overrides_locale(self, monkeypatch):
        self._patch_locale(monkeypatch, "en")
        body = self._body({"lang": "es"})
        assert body["messages"][0]["content"] == describer_mod.DESCRIBER_SYSTEM_PROMPT_ES

    def test_multi_frame_mode_is_localized(self):
        frames = [JPEG + b"1", JPEG + b"2"]
        es = self._body({"lang": "es"}, frames)["messages"][1]["content"]
        en = self._body({"lang": "en"}, frames)["messages"][1]["content"]
        assert describer_mod.MULTI_FRAME_HINT_ES in es and MULTI_FRAME_HINT not in es
        assert MULTI_FRAME_HINT in en

    def test_clip_mode_hint_is_localized(self):
        es = self._body({"lang": "es"}, hint=describer_mod.MOSAIC_HINT)["messages"][1]["content"]
        en = self._body({"lang": "en"}, hint=describer_mod.MOSAIC_HINT)["messages"][1]["content"]
        assert describer_mod.MOSAIC_HINT_ES in es and describer_mod.MOSAIC_HINT not in es
        assert describer_mod.MOSAIC_HINT in en

    def test_text_mode_overrides_keep_the_localized_system_prompt(self):
        body = self._body({"lang": "es"}, num_ctx=4096, num_predict=1000)
        assert body["messages"][0]["content"] == describer_mod.DESCRIBER_SYSTEM_PROMPT_ES
        assert body["options"]["num_predict"] == 1000

    def test_both_prompts_keep_the_descriptive_constraints(self):
        es = describer_mod.DESCRIBER_SYSTEM_PROMPT_ES.lower()
        for phrase in ("nunca responde", "recomiend", "aconsej", "se ve", "no se ve",
                       "párrafo compacto", "números"):
            assert phrase in es
