"""LLM configuration, feedback, and persistent-memory routes."""

import logging

from flask import Blueprint, jsonify, request

from db import (
    delete_all_llm_user_memory, delete_llm_memory_channel_observations,
    get_enabled_llm_memory_channels, get_llm_feedback_summary,
    get_llm_memory_entries, get_llm_memory_preference, get_llm_user_memory,
    get_setting, is_llm_memory_channel_enabled, purge_guild_llm_user_memories,
    set_llm_memory_channel_enabled, set_llm_memory_preference, set_setting,
)
from llm.client import LlamaCppError, get_allowed_models, get_mention_model
from web.auth import require_token
from web.helpers import discord_id as _discord_id

logger = logging.getLogger("flask_api")
blueprint = Blueprint("memory_llm", __name__)

@blueprint.route("/llm/mention-model", methods=["GET"])
@require_token
def api_get_mention_model():
    try:
        allowed = get_allowed_models()
        stored = get_setting("mention_model")
        selected = stored if stored in allowed else get_mention_model()
        return jsonify({"model": selected, "allowed_models": list(allowed)})
    except LlamaCppError as exc:
        logger.error("Could not read mention-model configuration: %s", exc)
        return jsonify({"error": str(exc)}), 503


@blueprint.route("/llm/mention-model", methods=["PUT"])
@require_token
def api_set_mention_model():
    data = request.get_json()
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or not model.strip():
        return jsonify({"error": "model must be a non-empty string"}), 400
    try:
        allowed = get_allowed_models()
    except LlamaCppError as exc:
        return jsonify({"error": str(exc)}), 503
    model = model.strip()
    if model not in allowed:
        return jsonify({"error": "Model is not allowed", "allowed_models": list(allowed)}), 400
    set_setting("mention_model", model)
    logger.info("Mention model changed through API")
    return jsonify({"status": "updated", "model": model})


@blueprint.route("/llm/feedback/summary", methods=["GET"])
@require_token
def api_llm_feedback_summary():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    rows = get_llm_feedback_summary(guild_id)
    groups = []
    for category, model, prompt_version, total, positive, negative in rows:
        groups.append(
            {
                "category": category,
                "model": model,
                "prompt_version": prompt_version,
                "ratings": total,
                "up": positive,
                "down": negative,
                "approval_percent": round(positive / total * 100, 1) if total else 0,
                "ready_to_compare": total >= 10,
            }
        )
    return jsonify({"guild_id": guild_id, "groups": groups})

def _memory_scope_from_query():
    value = request.args.get("scope_id")
    if value is None:
        raise ValueError("scope_id must be a non-negative Discord server ID or 0 for DMs")
    return _discord_id(value, "scope_id")


def _serialize_memory_user(scope_id, user_id):
    entries = get_llm_memory_entries(scope_id, user_id)
    return {
        "scope_id": scope_id,
        "user_id": user_id,
        "preference": get_llm_memory_preference(scope_id, user_id),
        "legacy_profile": get_llm_user_memory(scope_id, user_id),
        "entries": [
            {
                "id": entry_id,
                "kind": kind,
                "content": content,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            for entry_id, kind, content, _source, created_at, updated_at in entries
        ],
        # Retained as an empty field for API compatibility. Raw chat is held
        # only as pending synthesis input and is never exposed as a transcript.
        "transcript": [],
    }


@blueprint.route("/memory/channels", methods=["GET"])
@require_token
def api_get_memory_channels():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    channels = [
        {"channel_id": channel_id, "enabled": True}
        for saved_guild_id, channel_id in get_enabled_llm_memory_channels()
        if saved_guild_id == guild_id
    ]
    return jsonify({"guild_id": guild_id, "channels": channels})


@blueprint.route("/memory/channels/<int:guild_id>/<int:channel_id>", methods=["GET"])
@require_token
def api_get_memory_channel(guild_id, channel_id):
    return jsonify(
        {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "enabled": is_llm_memory_channel_enabled(guild_id, channel_id),
        }
    )


@blueprint.route("/memory/channels/<int:guild_id>/<int:channel_id>", methods=["PUT"])
@require_token
def api_set_memory_channel(guild_id, channel_id):
    data = request.get_json()
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    if not set_llm_memory_channel_enabled(guild_id, channel_id, enabled):
        return jsonify({"error": "Could not update memory channel"}), 500
    if not enabled:
        # Prevent persisted observations from a disabled channel being learned
        # if it is enabled again later.
        delete_llm_memory_channel_observations(guild_id, channel_id)
    return jsonify(
        {
            "status": "updated",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "enabled": enabled,
        }
    )


@blueprint.route("/memory/users/<int:user_id>", methods=["GET"])
@require_token
def api_get_memory_user(user_id):
    try:
        scope_id = _memory_scope_from_query()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_serialize_memory_user(scope_id, user_id))


@blueprint.route("/memory/users/<int:user_id>", methods=["DELETE"])
@require_token
def api_forget_memory_user(user_id):
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "scope_id is required"}), 400
    try:
        scope_id = _discord_id(data.get("scope_id"), "scope_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    removed = delete_all_llm_user_memory(scope_id, user_id)
    if removed is None:
        return jsonify({"error": "Could not delete user memory"}), 500
    return jsonify(
        {"status": "forgotten", "scope_id": scope_id, "user_id": user_id, "removed": removed}
    )


@blueprint.route("/memory/users/<int:user_id>/preference", methods=["PUT"])
@require_token
def api_set_memory_user_preference(user_id):
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "scope_id and enabled are required"}), 400
    try:
        scope_id = _discord_id(data.get("scope_id"), "scope_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    if not set_llm_memory_preference(scope_id, user_id, enabled):
        return jsonify({"error": "Could not update memory preference"}), 500
    removed = 0
    if not enabled:
        removed = delete_all_llm_user_memory(scope_id, user_id)
        if removed is None:
            return jsonify({"error": "Preference updated but memory deletion failed"}), 500
    return jsonify(
        {
            "status": "updated",
            "scope_id": scope_id,
            "user_id": user_id,
            "enabled": enabled,
            "removed": removed,
        }
    )


@blueprint.route("/memory/guilds/<int:guild_id>", methods=["DELETE"])
@require_token
def api_purge_memory_guild(guild_id):
    data = request.get_json()
    confirmation = data.get("confirmation") if isinstance(data, dict) else None
    if confirmation != "PURGE":
        return jsonify({"error": "Type PURGE exactly to confirm"}), 400
    removed = purge_guild_llm_user_memories(guild_id)
    if removed is None:
        return jsonify({"error": "Could not purge guild memory"}), 500
    return jsonify({"status": "purged", "guild_id": guild_id, "removed_users": removed})
