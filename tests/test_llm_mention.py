import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
from PIL import Image, ImageFile, features

from llm.client import LlamaCppError, get_default_model, query_llm
from features.llm_mention import (
    AskJob,
    LLMMentionFeature,
    DISCORD_MESSAGE_LIMIT,
    DISCORD_SAFE_LIMIT,
    MAX_IMAGE_BYTES,
    IMAGE_DESCRIPTION_REQUEST,
    budget_chat_history,
    prepare_image,
    get_ask_cooldown_seconds,
    get_selected_model,
    select_image_attachments,
    split_discord_messages,
)
from llm.responses import build_mention_prompt


def _image_bytes(image_format, *, mode="RGB", color="red", size=(3, 2), **save_kwargs):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format=image_format, **save_kwargs)
    return output.getvalue()


PNG_BYTES = _image_bytes("PNG")
JPEG_BYTES = _image_bytes("JPEG")


def _attachment(
    *,
    filename="image.png",
    content_type="image/png",
    size=len(PNG_BYTES),
    width=100,
    height=100,
    data=PNG_BYTES,
):
    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=size,
        width=width,
        height=height,
        read=AsyncMock(return_value=data),
    )


def _mention_message(*, text="", attachments=None, user_id=123):
    bot = SimpleNamespace(id=999888777, display_name="Bot")
    content = f"<@{bot.id}> {text}".strip()
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, display_name="Alice"),
        content=content,
        clean_content=content,
        mentions=[bot],
        role_mentions=[],
        channel_mentions=[],
        attachments=list(attachments or []),
        channel=SimpleNamespace(id=456),
        guild=None,
        reply=AsyncMock(),
    )


def _handling_feature(*, processing=False, queue_size=0):
    feature = object.__new__(LLMMentionFeature)
    feature.bot_id = 999888777
    feature._user_pending = set()
    feature._user_last_ask = {}
    feature._processing = processing
    feature._queue = SimpleNamespace(qsize=lambda: queue_size)
    feature._enqueue_job = AsyncMock(return_value=True)
    return feature


@pytest.mark.parametrize("summon_only", [False, True])
@pytest.mark.parametrize("label", ["", "Robeeque: "])
def test_mention_reply_tags_requester(summon_only, label):
    feature = object.__new__(LLMMentionFeature)
    original = SimpleNamespace(reply=AsyncMock())
    user = SimpleNamespace(id=123, display_name="Robeeque")
    job = AskJob(
        user=user, question="hello", model="discord-bot", reply_to=original,
        summon_only=summon_only,
    )

    asyncio.run(feature._reply_mention(job, label + "Salut!"))

    sent = original.reply.await_args
    assert sent.args == ("<@123> Salut!",)
    assert sent.kwargs["mention_author"] is False
    assert sent.kwargs["allowed_mentions"].to_dict() == {
        "parse": [], "users": [123]
    }


def test_long_mention_reply_only_pings_requester_in_first_chunk():
    feature = object.__new__(LLMMentionFeature)
    original = SimpleNamespace(reply=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="hello", model="discord-bot", reply_to=original, channel=channel,
    )
    text = "@everyone <@456> <@&789> " + "x" * 5000

    asyncio.run(feature._reply_mention(job, text))

    first = original.reply.await_args
    chunks = [first.args[0]] + [call.args[0] for call in channel.send.await_args_list]
    assert len(chunks) > 1
    assert chunks[0].startswith("<@123> ")
    assert "".join(chunks).replace(" ", "") == ("<@123> " + text).replace(" ", "")
    assert all(len(chunk) <= DISCORD_SAFE_LIMIT for chunk in chunks)
    assert first.kwargs["allowed_mentions"].to_dict() == {"parse": [], "users": [123]}
    for call in channel.send.await_args_list:
        assert call.kwargs["reference"] is original
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_get_ask_cooldown_seconds(monkeypatch):
    monkeypatch.setenv("ASK_COOLDOWN_SECONDS", "90")
    assert get_ask_cooldown_seconds() == 90.0


def test_worker_admission_accepts_three_global_reservations():
    async def scenario():
        feature = object.__new__(LLMMentionFeature)
        feature._processing = False
        feature._queue = asyncio.PriorityQueue()
        feature._queue_sequence = 0
        feature._ensure_worker = MagicMock()

        jobs = [
            AskJob(user=SimpleNamespace(id=index), question="hello", model="discord-bot")
            for index in range(4)
        ]
        try:
            assert [await feature._put_job(0, job) for job in jobs] == [
                True, True, True, False,
            ]
            assert feature._queue.qsize() == 3
            assert [feature._queue.get_nowait()[2] for _ in range(3)] == jobs[:3]
        finally:
            for job in jobs:
                if job.reservation is not None:
                    job.reservation.release()

    asyncio.run(scenario())


def test_stale_stored_model_is_replaced_with_llama_cpp_default(tmp_db):
    import db

    db.set_setting("mention_model", "old-ollama-model")

    assert get_selected_model() == "discord-bot"
    assert db.get_setting("mention_model") == "discord-bot"


@pytest.mark.parametrize(
    ("data", "mime"), [(PNG_BYTES, "image/png"), (JPEG_BYTES, "image/jpeg")]
)
def test_prepare_image_preserves_valid_png_and_jpeg(data, mime):
    prepared, actual_mime = prepare_image(data)

    assert prepared is data
    assert actual_mime == mime
    with Image.open(io.BytesIO(prepared)) as image:
        image.load()
        assert image.size == (3, 2)


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_large_images_are_resized_proportionally_before_inference(monkeypatch, image_format):
    monkeypatch.delenv("LLM_IMAGE_MAX_EDGE", raising=False)
    data = _image_bytes(image_format, size=(2048, 1024))
    prepared, mime = prepare_image(data)
    assert mime == ("image/jpeg" if image_format == "JPEG" else "image/png")
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.size == (1280, 640)
        red, green, blue = image.convert("RGB").getpixel((640, 320))
        assert red > 240 and green < 10 and blue < 10


def test_resize_preserves_transparency_and_can_be_configured(monkeypatch):
    monkeypatch.setenv("LLM_IMAGE_MAX_EDGE", "32")
    data = _image_bytes("PNG", size=(40, 80), mode="RGBA", color=(255, 0, 0, 128))
    prepared, mime = prepare_image(data)
    assert mime == "image/png"
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.size == (16, 32)
        assert image.mode == "RGBA"
        assert image.getpixel((8, 16))[3] == 128
    monkeypatch.setenv("LLM_IMAGE_MAX_EDGE", "100")
    assert prepare_image(data)[0] is data


def test_resize_applies_phone_photo_orientation(monkeypatch):
    monkeypatch.setenv("LLM_IMAGE_MAX_EDGE", "32")
    exif = Image.Exif()
    exif[274] = 6
    data = _image_bytes("JPEG", size=(80, 40), exif=exif)
    prepared, mime = prepare_image(data)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.size == (16, 32)
        assert image.getexif().get(274, 1) == 1


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "BMP", "TIFF"])
def test_prepare_image_normalizes_other_supported_formats(image_format):
    data = _image_bytes(image_format)

    prepared, mime = prepare_image(data)

    assert mime == "image/png"
    with Image.open(io.BytesIO(prepared)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (3, 2)
        red, green, blue = image.getpixel((0, 0))
        assert red > 240 and green < 10 and blue < 10


def test_prepare_image_supports_webp_decoder():
    assert features.check("webp")
    assert prepare_image(_image_bytes("WEBP"))[1] == "image/png"


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "TIFF"])
def test_prepare_image_preserves_transparency(image_format):
    data = _image_bytes(image_format, mode="RGBA", color=(255, 0, 0, 0))

    prepared, mime = prepare_image(data)

    assert mime == "image/png"
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "TIFF", "PNG"])
def test_prepare_image_uses_first_animation_frame_or_page(image_format):
    data = _image_bytes(
        image_format,
        save_all=True,
        append_images=[Image.new("RGB", (3, 2), "blue")],
        duration=100,
        loop=0,
        lossless=True,
    )

    prepared, mime = prepare_image(data)

    assert mime == "image/png"
    with Image.open(io.BytesIO(prepared)) as image:
        image.load()
        assert image.n_frames == 1
        assert image.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize(
    "data",
    [
        b"not an image",
        b"\x89PNG\r\n\x1a\nimage",
        b"\xff\xd8\xffimage",
        b"GIF89aimage",
        b"RIFF-webp",
        PNG_BYTES[:50],
        JPEG_BYTES[:-10],
    ],
)
def test_prepare_image_rejects_corrupt_or_signature_only_data(data):
    with pytest.raises(ValueError):
        prepare_image(data)
    assert not ImageFile.LOAD_TRUNCATED_IMAGES


def test_prepare_image_rejects_png_with_corrupt_checksum():
    data = bytearray(PNG_BYTES)
    crc_offset = data.index(b"IDAT") + 4
    data[crc_offset] ^= 1

    with pytest.raises(ValueError):
        prepare_image(bytes(data))


def test_prepare_image_rejects_unsupported_decodable_format():
    with pytest.raises(ValueError, match="JPEG, PNG, GIF, WebP, BMP, or TIFF"):
        prepare_image(_image_bytes("PPM"))


@pytest.mark.parametrize("data", [b"", b"x" * (MAX_IMAGE_BYTES + 1)], ids=["empty", "oversized"])
def test_prepare_image_enforces_input_byte_limit(data):
    with pytest.raises(ValueError, match="8 MiB"):
        prepare_image(data)


def test_prepare_image_enforces_decoded_pixel_limit(monkeypatch):
    monkeypatch.setattr("features.llm_mention.MAX_IMAGE_PIXELS", 5)

    with pytest.raises(ValueError, match="25 megapixels"):
        prepare_image(PNG_BYTES)


def test_prepare_image_enforces_normalized_byte_limit(monkeypatch):
    data = _image_bytes("GIF")
    expected_png_size = len(_image_bytes("PNG"))
    assert len(data) < expected_png_size
    monkeypatch.setattr("features.llm_mention.MAX_IMAGE_BYTES", expected_png_size - 1)

    with pytest.raises(ValueError, match="8 MiB limit after conversion"):
        prepare_image(data)


@pytest.mark.parametrize("pillow_limit", [5, 2])
def test_prepare_image_keeps_pillow_decompression_safety(monkeypatch, pillow_limit):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pillow_limit)

    with pytest.raises(ValueError, match="25 megapixels"):
        prepare_image(PNG_BYTES)
    assert Image.MAX_IMAGE_PIXELS == pillow_limit


def test_image_selection_preserves_candidate_order_and_ignores_other_files():
    other = _attachment(filename="notes.txt", content_type="text/plain")
    first = _attachment(filename="first.heic", content_type="image/heic")
    second = _attachment(filename="second.png", content_type="image/jpeg")

    candidates, count, error = select_image_attachments([other, first, second])

    assert candidates == (first, second)
    assert count == 2
    assert error is None
    for attachment in [other, first, second]:
        attachment.read.assert_not_awaited()


@pytest.mark.parametrize("suffix", ["png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"])
def test_image_selection_accepts_known_extensions_without_image_mime(suffix):
    attachment = _attachment(filename=f"image.{suffix.upper()}", content_type="application/octet-stream")

    candidates, count, error = select_image_attachments([attachment])

    assert candidates == (attachment,)
    assert count == 1
    assert error is None


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("image.jpg", "image/png; charset=binary"), ("image.bin", " IMAGE/GIF ")],
)
def test_image_selection_accepts_conflicting_metadata(filename, content_type):
    attachment = _attachment(filename=filename, content_type=content_type)

    candidates, count, error = select_image_attachments([attachment])

    assert candidates == (attachment,)
    assert count == 1
    assert error is None


@pytest.mark.parametrize(
    "metadata",
    [{"width": None}, {"height": None}, {"width": 0}, {"height": True}, {"size": None}],
)
def test_image_selection_defers_missing_or_invalid_metadata(metadata):
    attachment = _attachment(**metadata)

    candidates, count, error = select_image_attachments([attachment])

    assert candidates == (attachment,)
    assert count == 1
    assert error is None


def test_image_selection_skips_known_oversize_candidates():
    oversized_bytes = _attachment(size=MAX_IMAGE_BYTES + 1)
    oversized_pixels = _attachment(width=5001, height=5001)
    valid = _attachment()

    candidates, count, error = select_image_attachments(
        [oversized_bytes, oversized_pixels, valid]
    )

    assert candidates == (valid,)
    assert count == 3
    assert error is None
    for attachment in [oversized_bytes, oversized_pixels, valid]:
        attachment.read.assert_not_awaited()


def test_image_selection_without_candidates_returns_text_path():
    attachment = _attachment(filename="notes.txt", content_type="text/plain")

    assert select_image_attachments([attachment]) == ((), 0, None)


@pytest.mark.parametrize(
    ("attachment", "message"),
    [
        (_attachment(size=MAX_IMAGE_BYTES + 1), "under 8 MiB"),
        (_attachment(width=5001, height=5001), "under 25 megapixels"),
    ],
)
def test_image_selection_rejects_known_excessive_images(
    attachment, message
):
    candidates, count, error = select_image_attachments([attachment])

    assert candidates == ()
    assert count == 1
    assert message in error


def test_image_selection_mixed_limit_failures_names_both_limits():
    candidates, count, error = select_image_attachments([
        _attachment(size=MAX_IMAGE_BYTES + 1),
        _attachment(width=5001, height=5001),
    ])

    assert candidates == ()
    assert count == 2
    assert "under 8 MiB and 25 megapixels" in error


@pytest.mark.parametrize(
    ("text", "expected_question"),
    [
        ("", IMAGE_DESCRIPTION_REQUEST),
        ("What error does this show?", "What error does this show?"),
    ],
)
def test_image_mention_enqueues_description_or_caption(
    monkeypatch, text, expected_question
):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    attachment = _attachment()
    message = _mention_message(text=text, attachments=[attachment])

    assert asyncio.run(feature.handle_message(message)) is True

    job = feature._enqueue_job.await_args.args[0]
    assert job.question == expected_question
    assert job.summon_only is False
    assert job.image_attachments == (attachment,)
    attachment.read.assert_not_awaited()
    message.reply.assert_not_awaited()


def test_attached_image_reaches_http_request_and_retry_before_discord_reply(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    message = _mention_message(text="Ce părere ai?", attachments=[attachment])
    props = MagicMock(ok=True)
    props.json.return_value = {"modalities": {"vision": True}}
    monkeypatch.setattr(requests, "get", MagicMock(return_value=props))
    replies = []
    for answer in ("Nu pot să văd poza. Trimite un PNG sau JPEG.", "Un dreptunghi roșu."):
        response = MagicMock(ok=True)
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"text": answer, "reaction": None})}}],
        }
        replies.append(response)
    post = MagicMock(side_effect=replies)
    monkeypatch.setattr(requests, "post", post)

    async def process():
        assert await feature.handle_message(message)
        job = feature._enqueue_job.await_args.args[0]
        await feature._process_job(job)
        return job

    job = asyncio.run(process())
    assert post.call_count == 2
    for call in post.call_args_list:
        parts = call.kwargs["json"]["messages"][-1]["content"]
        image = next(part["image_url"]["url"] for part in parts if part["type"] == "image_url")
        assert image.startswith("data:image/png;base64,")
        assert base64.b64decode(image.split(",", 1)[1]) == PNG_BYTES
    attachment.read.assert_awaited_once_with()
    feature._reply_mention.assert_awaited_once_with(job, "Un dreptunghi roșu.")
    feature._reply_job_error.assert_not_awaited()


def test_image_http_timeout_is_localized_and_never_retried(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_VISION_TIMEOUT", "600")
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    monkeypatch.setattr("features.llm_mention.record_for", AsyncMock())
    feature = _handling_feature()
    feature._reply_mention = AsyncMock()
    message = _mention_message(text="Ce vezi în poză?", attachments=[_attachment()])
    props = MagicMock(ok=True)
    props.json.return_value = {"modalities": {"vision": True}}
    monkeypatch.setattr(requests, "get", MagicMock(return_value=props))
    post = MagicMock(side_effect=requests.exceptions.ReadTimeout)
    monkeypatch.setattr(requests, "post", post)

    async def process():
        assert await feature.handle_message(message)
        await feature._process_job(feature._enqueue_job.await_args.args[0])

    asyncio.run(process())
    post.assert_called_once()
    assert post.call_args.kwargs["timeout"] == (10, 600)
    message.reply.assert_awaited_once()
    assert "Modelul nu a terminat analiza imaginii în 600 de secunde" in message.reply.await_args.args[0]
    feature._reply_mention.assert_not_awaited()


def test_busy_multi_image_request_can_be_queued(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature(processing=True, queue_size=1)
    first = _attachment(filename="first.png")
    second = _attachment(filename="second.jpg", content_type="image/jpeg")
    message = _mention_message(attachments=[first, second])

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_awaited_once()
    assert feature._enqueue_job.await_args.args[0].image_attachments == (first, second)
    notice = message.reply.await_args.args[0]
    assert "first image I can process" in notice
    assert message.reply.await_args.kwargs["mention_author"] is False
    first.read.assert_not_awaited()
    second.read.assert_not_awaited()


def test_image_mention_queues_candidates_after_known_oversize_image(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    oversized = _attachment(size=MAX_IMAGE_BYTES + 1)
    candidate = _attachment(filename="image.tiff", width=None, height=None)
    message = _mention_message(attachments=[oversized, candidate])

    assert asyncio.run(feature.handle_message(message)) is True

    assert feature._enqueue_job.await_args.args[0].image_attachments == (candidate,)
    assert "first image I can process" in message.reply.await_args.args[0]
    oversized.read.assert_not_awaited()
    candidate.read.assert_not_awaited()


@pytest.mark.parametrize("text", ["", "hello"])
def test_non_image_attachment_preserves_text_and_summon_paths(monkeypatch, text):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    attachment = _attachment(filename="notes.txt", content_type="text/plain")
    message = _mention_message(text=text, attachments=[attachment])

    assert asyncio.run(feature.handle_message(message)) is True

    job = feature._enqueue_job.await_args.args[0]
    assert job.image_attachments == ()
    assert job.summon_only is (not text)
    assert job.question == text
    attachment.read.assert_not_awaited()
    message.reply.assert_not_awaited()


def test_mention_losing_admission_race_is_not_queued(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature._enqueue_job.return_value = False
    message = _mention_message(text="hello")

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_awaited_once()
    assert "became busy" in message.reply.await_args.args[0]


def test_pending_user_is_not_allowed_to_enqueue_another_image(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature._user_pending.add(123)
    message = _mention_message(attachments=[_attachment()])

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_not_awaited()
    assert "already working" in message.reply.await_args.args[0]


def test_invalid_image_is_rejected_before_queueing(monkeypatch):
    feature = _handling_feature()
    message = _mention_message(
        attachments=[_attachment(size=MAX_IMAGE_BYTES + 1)]
    )

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_not_awaited()
    assert "under 8 MiB" in message.reply.await_args.args[0]
    message.attachments[0].read.assert_not_awaited()


def test_vision_job_stops_before_download_when_projector_is_disabled(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    later = _attachment(data=JPEG_BYTES)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment, later),
    )
    supports_vision = MagicMock(return_value=False)
    generate = MagicMock()
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", supports_vision)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", generate)

    asyncio.run(feature._process_job(job))

    attachment.read.assert_not_awaited()
    later.read.assert_not_awaited()
    supports_vision.assert_called_once_with()
    generate.assert_not_called()
    assert "vision support is disabled" in feature._reply_job_error.await_args.args[1]


def test_vision_job_stops_before_download_when_capability_check_fails(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )

    def unavailable():
        raise LlamaCppError("server unavailable")

    monkeypatch.setattr("features.llm_mention.llama_supports_vision", unavailable)

    asyncio.run(feature._process_job(job))

    attachment.read.assert_not_awaited()
    assert "vision service is unavailable" in feature._reply_job_error.await_args.args[1]


def test_vision_job_rechecks_downloaded_size(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment(data=PNG_BYTES + b"x" * MAX_IMAGE_BYTES)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "exceeds the 8 MiB limit" in feature._reply_job_error.await_args.args[1]


def test_vision_job_reports_deleted_attachment(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    attachment.read.side_effect = RuntimeError("deleted")
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "upload it again" in feature._reply_job_error.await_args.args[1]


def test_vision_job_rejects_spoofed_image_bytes(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment(data=b"not an image")
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "could not decode" in feature._reply_job_error.await_args.args[1]


@pytest.mark.parametrize(
    "failure", ["download", "decode", "bytes", "format", "pixels", "converted_bytes"]
)
def test_vision_job_falls_back_to_first_usable_image(monkeypatch, failure):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    events = []
    rejected_data = {
        "download": PNG_BYTES,
        "decode": b"not an image",
        "bytes": b"x" * (MAX_IMAGE_BYTES + 1),
        "format": _image_bytes("PPM"),
        "pixels": _image_bytes("PNG", size=(4, 4)),
        "converted_bytes": _image_bytes("WEBP", size=(128, 128), lossless=True),
    }[failure]
    selected_data = PNG_BYTES if failure == "converted_bytes" else JPEG_BYTES
    first = _attachment(data=rejected_data)
    second = _attachment(data=selected_data)
    later = _attachment()

    async def read_first():
        events.append("first")
        if failure == "download":
            raise RuntimeError("deleted")
        return rejected_data

    async def read_second():
        events.append("second")
        return selected_data

    first.read.side_effect = read_first
    second.read.side_effect = read_second
    if failure == "pixels":
        monkeypatch.setattr("features.llm_mention.MAX_IMAGE_PIXELS", 12)
    elif failure == "converted_bytes":
        limit = max(len(rejected_data), len(PNG_BYTES))
        monkeypatch.setattr("features.llm_mention.MAX_IMAGE_BYTES", limit)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(first, second, later),
    )
    supports_vision = MagicMock(return_value=True)

    def generate(*args, **kwargs):
        events.append("inference")
        return SimpleNamespace(text="A red image.", reaction=None)

    inference = MagicMock(side_effect=generate)
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", supports_vision)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", inference)

    asyncio.run(feature._process_job(job))

    assert events == ["first", "second", "inference"]
    first.read.assert_awaited_once_with()
    second.read.assert_awaited_once_with()
    later.read.assert_not_awaited()
    supports_vision.assert_called_once_with()
    inference.assert_called_once()
    assert inference.call_args.kwargs["image_bytes"] == selected_data
    assert inference.call_args.kwargs["image_mime"] == (
        "image/png" if failure == "converted_bytes" else "image/jpeg"
    )
    feature._reply_job_error.assert_not_awaited()
    feature._reply_mention.assert_awaited_once_with(job, "A red image.")


@pytest.mark.parametrize(
    "data, filename, content_type, expected_mime",
    [
        (JPEG_BYTES, "image.png", "image/png", "image/jpeg"),
        (PNG_BYTES, "image.jpg", "image/jpeg", "image/png"),
        (PNG_BYTES, "image.tiff", "application/octet-stream", "image/png"),
    ],
)
def test_vision_job_uses_actual_image_mime(
    monkeypatch, data, filename, content_type, expected_mime
):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    attachment = _attachment(data=data, filename=filename, content_type=content_type)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    inference = MagicMock(
        return_value=SimpleNamespace(text="A red image.", reaction=None)
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", inference)

    asyncio.run(feature._process_job(job))

    inference.assert_called_once()
    assert inference.call_args.kwargs["image_bytes"] == data
    assert inference.call_args.kwargs["image_mime"] == expected_mime
    feature._reply_job_error.assert_not_awaited()


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "BMP", "TIFF"])
def test_vision_job_sends_normalized_formats_as_png(monkeypatch, image_format):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    data = (
        _image_bytes(image_format, lossless=True)
        if image_format == "WEBP"
        else _image_bytes(image_format)
    )
    attachment = _attachment(data=data)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    inference = MagicMock(
        return_value=SimpleNamespace(text="A red image.", reaction=None)
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", inference)

    asyncio.run(feature._process_job(job))

    inference.assert_called_once()
    assert inference.call_args.kwargs["image_mime"] == "image/png"
    with Image.open(io.BytesIO(inference.call_args.kwargs["image_bytes"])) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (3, 2)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    feature._reply_job_error.assert_not_awaited()


@pytest.mark.parametrize("reverse_order", [False, True])
def test_vision_job_all_failures_get_deterministic_error_without_inference(
    monkeypatch, reverse_order
):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    deleted = _attachment()
    deleted.read.side_effect = RuntimeError("deleted")
    attachments = (
        deleted,
        _attachment(data=b"not an image"),
        _attachment(data=b"x" * (MAX_IMAGE_BYTES + 1)),
        _attachment(data=_image_bytes("PPM")),
    )
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=attachments[::-1] if reverse_order else attachments,
    )
    supports_vision = MagicMock(return_value=True)
    inference = MagicMock()
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", supports_vision)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", inference)

    asyncio.run(feature._process_job(job))

    for attachment in attachments:
        attachment.read.assert_awaited_once_with()
    supports_vision.assert_called_once_with()
    inference.assert_not_called()
    feature._reply_mention.assert_not_awaited()
    feature._reply_job_error.assert_awaited_once_with(
        job,
        "I couldn't inspect any of the attached images. Please upload a valid "
        "JPEG, PNG, GIF, WebP, BMP, or TIFF image under 8 MiB and 25 megapixels. "
        "If an image was deleted or could not be downloaded, please upload it again.",
    )


def test_vision_inference_failure_gets_user_facing_reply(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachments=(attachment,),
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: None,
    )

    asyncio.run(feature._process_job(job))

    assert "couldn't process that image" in feature._reply_job_error.await_args.args[1]


def test_process_job_passes_live_bot_name(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_mention = AsyncMock()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question="who are you?",
        model="discord-bot",
        bot_names=("Nova", "nova-bot"),
    )
    reply = MagicMock(return_value=SimpleNamespace(text="I'm Nova.", reaction=None))
    monkeypatch.setattr("features.llm_mention.generate_mention_result", reply)

    asyncio.run(feature._process_job(job))

    assert reply.call_args.kwargs["bot_name"] == "Nova"


def test_split_discord_messages_splits_long_text():
    text = "word " * 800
    chunks = split_discord_messages(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_split_discord_messages_includes_prefix_in_first_chunk_only():
    text = "x" * 5000
    prefix = "**llama3.2:3b**\n"
    chunks = split_discord_messages(text, first_prefix=prefix)
    assert chunks[0].startswith(prefix)
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert prefix not in "".join(chunks[1:])
    assert len("".join(chunks)) == len(prefix) + len(text)


def test_split_discord_messages_respects_limit_with_prefix():
    text = "a" * DISCORD_MESSAGE_LIMIT
    prefix = "**model**\n"
    chunks = split_discord_messages(text, first_prefix=prefix)
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert len(chunks) > 1


def test_query_llm_rejects_unknown_model():
    with pytest.raises(LlamaCppError, match="not allowed"):
        query_llm("hi", model="unknown-model")


def test_query_llm_success(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello from llama.cpp"}}]
    }
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr(requests, "post", mock_post)

    answer = query_llm(
        "hi", model=get_default_model(), base_url="http://llama-server:8080"
    )

    assert answer == "Hello from llama.cpp"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://llama-server:8080/v1/chat/completions"
    assert kwargs["json"]["model"] == get_default_model()
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert all(message["role"] != "system" for message in kwargs["json"]["messages"])
    assert kwargs["json"]["stream"] is False


def test_query_llm_server_error(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 404
    mock_response.text = "model not found"
    mock_response.reason = "Not Found"
    monkeypatch.setattr(requests, "post", MagicMock(return_value=mock_response))

    with pytest.raises(LlamaCppError, match="HTTP 404"):
        query_llm(
            "hi", model=get_default_model(), base_url="http://llama-server:8080"
        )


def test_mention_history_marks_the_bots_own_messages(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    monkeypatch.setenv("LLM_CONTEXT_MESSAGES", "5")
    feature = _handling_feature()
    feature.client = SimpleNamespace(user=SimpleNamespace(display_name="Bot", name="bot"))
    message = _mention_message(text="is that true?")
    newest_first = [
        SimpleNamespace(author=SimpleNamespace(id=999888777, display_name="Bot"), clean_content="No."),
        SimpleNamespace(author=SimpleNamespace(id=5, display_name="Alex"), clean_content="@Bot is slow"),
    ]

    async def history(*, limit, before):
        for past in newest_first[:limit]:
            yield past

    message.channel.history = history

    asyncio.run(feature.handle_message(message))

    job = feature._enqueue_job.await_args.args[0]
    assert job.context_messages == ['Alex said: "Bot (you) is slow"', 'You (Bot) said: "No."']


def test_every_mention_prompt_starts_with_the_shared_head():
    from llm.responses import build_mention_prompt_head

    history = ['Alex said: "hi"']
    head = build_mention_prompt_head(has_history=True)
    for kwargs in ({}, {"has_image": True}, {"replied_message": "Alex said: \"z\""}):
        assert build_mention_prompt("Dan", "who?", history, **kwargs).startswith(head)


def test_chat_history_budget_keeps_newest_messages():
    assert budget_chat_history(["old", "new"], 3) == ["new"]
    assert budget_chat_history(["old", "new"], 6) == ["old", "new"]


def test_chat_history_budget_counts_escaped_size_and_truncates_newest():
    assert budget_chat_history(["<<<<"], 8) == ["<<"]
