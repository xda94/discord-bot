"""LLM configuration routes."""

import logging

from flask import Blueprint, jsonify, request

from db import get_setting, set_setting
from llm.client import LlamaCppError, get_allowed_models, get_mention_model
from web.auth import require_token

logger = logging.getLogger("flask_api")
blueprint = Blueprint("llm", __name__)


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
