"""Dashboard and core administration routes."""

import logging
import platform
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import psutil
from flask import Blueprint, jsonify, render_template, request

from db import (
    add_joke, add_reminder, add_response, clear_guild_joke_config,
    delete_birthday, delete_joke, delete_reminder, get_all_birthdays,
    get_all_guild_joke_configs, get_all_jokes, get_all_reminders,
    get_all_responses, get_analytics_summary,
    get_guild_joke_config, get_joke_by_id, get_setting, get_top_keywords,
    get_sponsor_tiers, get_top_keywords_by_user, is_guild_inactivity_enabled, remove_response,
    reset_all_guild_joke_sent, set_guild_inactivity_enabled,
    save_sponsor_tier, set_birthday, set_guild_joke_config, set_setting, update_joke,
)
from system_metrics import get_temperature_celsius
from sponsor_tiers import (
    DuplicateTierNameError,
    TierCatalogError,
    TierValidationError,
    UnknownTierError,
)
from web.auth import require_analytics_token, require_token
from web.helpers import discord_id as _discord_id

logger = logging.getLogger("flask_api")
blueprint = Blueprint("administration", __name__)

@blueprint.route("/")
def dashboard():
    return render_template("dashboard.html")


def _optional_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except (AttributeError, FileNotFoundError, NotImplementedError, OSError, RuntimeError, ValueError):
        return None


def _collect_system_stats():
    """Return JSON-friendly PM2 host metrics, tolerating missing sensors/APIs."""
    cpu_percent = _optional_call(psutil.cpu_percent, interval=0.1)
    temperature_celsius = get_temperature_celsius()
    memory = _optional_call(psutil.virtual_memory)
    disk_path = Path.cwd().anchor or os.path.abspath(os.sep)
    disk = _optional_call(psutil.disk_usage, disk_path)
    boot_time = _optional_call(psutil.boot_time)
    now = time.time()
    timezone = datetime.now().astimezone().tzinfo
    return {
        "cpu_percent": cpu_percent,
        "temperature_celsius": temperature_celsius,
        "memory": None if memory is None else {
            "total": memory.total,
            "used": memory.used,
            "percent": memory.percent,
        },
        "disk": None if disk is None else {
            "path": disk_path,
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
        "uptime_seconds": None if boot_time is None else max(0, now - boot_time),
        "timezone": str(timezone) if timezone is not None else None,
        "platform": platform.system() or None,
        "collected_at": now,
    }


@blueprint.route("/system/stats", methods=["GET"])
@require_token
def api_system_stats():
    return jsonify(_collect_system_stats())


@blueprint.route("/analytics/summary", methods=["GET"])
@require_analytics_token
def api_analytics_summary():
    period = request.args.get("period", "30d")
    raw_guild_id = request.args.get("guild_id")
    try:
        guild_id = (
            None if raw_guild_id in (None, "") else _discord_id(raw_guild_id, "guild_id")
        )
        return jsonify(get_analytics_summary(period, guild_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def _serialize_scraped_item(item):
    if item is None:
        return None
    return {
        "id": item[0],
        "user_id": item[1],
        "url": item[2],
        "last_price": item[3],
        "in_stock": bool(item[4]) if item[4] is not None else None,
        "title": item[5],
        "currency": item[6],
        "last_alert_kind": item[7],
        "last_alert_price": item[8],
        "target_price": item[9],
        "target_currency": item[10],
        "target_alerted": bool(item[11]),
        "restock_only": bool(item[12]),
        "last_checked_at": item[13],
        "last_check_status": item[14],
    }


def _validate_flight_tracker_payload(data):
    """Pure validation shared by the HTTP flight configuration routes."""
    required = ("user_id", "origin", "destination", "start_date", "end_date")
    if not data or any(name not in data for name in required):
        raise ValueError("Missing user_id, origin, destination, start_date, or end_date")
    user_id = _discord_id(data["user_id"], "user_id")

    origin = normalize_iata(data["origin"])
    destination = normalize_iata(data["destination"])
    if origin == destination:
        raise ValueError("Origin and destination must be different")
    start = parse_iso_date(data["start_date"])
    end = parse_iso_date(data["end_date"])
    if start < date.today():
        raise ValueError("The start date cannot be in the past")
    if end <= start:
        raise ValueError("The end date must be after the start date")
    adults = data.get("adults", 1)
    if not _is_int(adults) or not 1 <= adults <= 9:
        raise ValueError("adults must be an integer between 1 and 9")
    currency = str(data.get("currency", "EUR")).upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}"
        )
    return {
        "user_id": user_id,
        "origin": origin,
        "destination": destination,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "adults": adults,
        "currency": currency,
    }


# --- Keywords Routes ---

@blueprint.route("/keywords/add", methods=["POST"])
@require_token
def api_keywords_add():
    data = request.get_json()
    if not data or "keyword" not in data or "response" not in data or "guild_id" not in data:
        logger.warning(
            f"BadRequest: Missing fields in POST /keywords/add. IP: {request.remote_addr}"
        )
        return jsonify({"error": "Missing keyword, response, or guild_id"}), 400

    try:
        guild_id = _discord_id(data["guild_id"], "guild_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not add_response(data["keyword"], data["response"], guild_id):
        return jsonify({"error": "Failed to save keyword"}), 500
    logger.info(
        f"Keyword added via API: '{data['keyword']}' for guild {guild_id} "
        f"from {request.remote_addr}"
    )
    return jsonify({"status": "ok"})


@blueprint.route("/keywords/delete", methods=["DELETE"])
@require_token
def api_keywords_delete():
    data = request.get_json()
    if not data or "keyword" not in data or "guild_id" not in data:
        return jsonify({"error": "Missing keyword or guild_id"}), 400

    try:
        guild_id = _discord_id(data["guild_id"], "guild_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    keyword = data["keyword"]
    response = data.get("response")

    success = remove_response(keyword, guild_id, response)
    if success:
        logger.info(f"Keyword removed via API: '{keyword}' in guild {guild_id}")
        return jsonify({"status": "deleted"})
    else:
        logger.warning(
            f"DELETE /keywords/delete: '{keyword}' not found in guild {guild_id}."
        )
        return jsonify({"error": "Keyword or response not found"}), 404


@blueprint.route("/keywords/get", methods=["GET"])
@require_token
def api_keywords_get():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    logger.info(
        f"Fetching keywords for guild {guild_id}. Requested by {request.remote_addr}"
    )
    responses = get_all_responses(guild_id)
    return jsonify(responses)


@blueprint.route("/keywords/top", methods=["GET"])
@require_token
def api_keywords_top():
    guild_id = request.args.get("guild_id", type=int)
    user_id = request.args.get("user_id", type=int)
    limit = request.args.get("limit", default=10, type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    if limit is None or not 1 <= limit <= 100:
        return jsonify({"error": "limit must be an integer between 1 and 100"}), 400
    rows = (
        get_top_keywords_by_user(guild_id, user_id, limit)
        if user_id is not None
        else get_top_keywords(guild_id, limit)
    )
    return jsonify(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "keywords": [{"keyword": keyword, "count": count} for keyword, count in rows],
        }
    )


# --- Reminder Routes ---

@blueprint.route("/reminders/add", methods=["POST"])
@require_token
def api_add_reminder():
    data = request.get_json()
    required = ["user_id", "channel_id", "remind_at", "message"]
    if not isinstance(data,dict) or not all(k in data for k in required):
        logger.warning("Invalid payload for /reminders/add")
        return jsonify({"error": "Missing required fields"}), 400

    try:
        user_id = _discord_id(data["user_id"], "user_id")
        channel_id = _discord_id(data["channel_id"], "channel_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    from db import reminders as reminder_store
    import db
    from assistant_profiles import effective_profile
    profile=effective_profile(db.get_assistant_profile(user_id))
    recurrence=data.get('recurrence')
    zone=data.get('timezone',profile.timezone)
    if (recurrence or data.get('local_time')) and not (data.get('timezone') or profile.timezone_configured):
        return jsonify({'error':'An explicit timezone is required for recurrence'}),400
    try:
        if data.get('local_time'):
            from command_time import calendar_time
            if not isinstance(data['local_time'],str):
                raise ValueError('local_time must be a string')
            data['remind_at'] = calendar_time(data['local_time'].replace('T',' '), zone)
            if data['remind_at'] is None:
                raise ValueError('Use a future, unambiguous date and time')
        from i18n import language_for
        rid=reminder_store.create(user_id,channel_id,data['remind_at'],data['message'],creator_id=_discord_id(data.get('creator_id',user_id),'creator_id'),timezone=zone,language=data.get('language',language_for(data['message'],profile)),recurrence=recurrence)
    except (ValueError,TypeError,KeyError) as exc:
        return jsonify({'error':str(exc)}),400
    return jsonify({'status':'reminder_set','id':rid})

@blueprint.route("/reminders/delete/<int:reminder_id>", methods=["DELETE"])
@require_token
def api_delete_reminder(reminder_id):
    delete_reminder(reminder_id)
    logger.info(f"Reminder {reminder_id} deleted via API")
    return jsonify({"status": "deleted", "id": reminder_id})

@blueprint.route("/reminders/all", methods=["GET"])
@require_token
def api_get_all_reminders():
    try:
        from db import reminders as reminder_store
        result=reminder_store.list_for()
        return jsonify(result)
    except Exception:
        logger.exception("Error in /reminders/all")
        return jsonify({"error": "Internal server error"}), 500


# --- Birthday Routes ---

def _serialize_birthday(row):
    return {
        "user_id": row[0],
        "channel_id": row[1],
        "guild_id": row[2],
        "month": row[3],
        "day": row[4],
        "last_sent_year": row[6],
    }


@blueprint.route("/birthdays", methods=["GET"])
@require_token
def api_get_birthdays():
    try:
        return jsonify([_serialize_birthday(row) for row in get_all_birthdays()])
    except sqlite3.Error:
        logger.exception("Error in GET /birthdays")
        return jsonify({"error": "Internal server error"}), 500


@blueprint.route("/birthdays/<int:user_id>", methods=["PUT"])
@require_token
def api_set_birthday(user_id):
    data = request.get_json(silent=True)
    if not data or any(key not in data for key in ("channel_id", "month", "day")):
        return jsonify({"error": "Missing channel_id, month, or day"}), 400

    try:
        user_id = _discord_id(user_id, "user_id")
        channel_id = _discord_id(data["channel_id"], "channel_id")
        raw_guild_id = data.get("guild_id")
        guild_id = (
            None
            if raw_guild_id in (None, "")
            else _discord_id(raw_guild_id, "guild_id")
        )
        month = data["month"]
        day = data["day"]
        if (
            not isinstance(month, int)
            or isinstance(month, bool)
            or not isinstance(day, int)
            or isinstance(day, bool)
        ):
            raise ValueError("month and day must be integers")
        set_birthday(user_id, channel_id, guild_id, month, day)
    except (OverflowError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.Error:
        logger.exception("Could not save birthday through API for user=%s", user_id)
        return jsonify({"error": "Could not save birthday"}), 500

    return jsonify({"status": "saved", "user_id": user_id})


@blueprint.route("/birthdays/<int:user_id>", methods=["DELETE"])
@require_token
def api_delete_birthday(user_id):
    try:
        removed = delete_birthday(user_id)
    except sqlite3.Error:
        logger.exception("Could not delete birthday through API for user=%s", user_id)
        return jsonify({"error": "Could not delete birthday"}), 500
    if not removed:
        return jsonify({"error": "Birthday not found"}), 404
    return jsonify({"status": "deleted", "user_id": user_id})

# --- Joke Routes ---

@blueprint.route("/jokes", methods=["GET"])
@require_token
def api_get_all_jokes():
    try:
        jokes = get_all_jokes()
        result = [{"id": j[0], "text": j[1], "sent": bool(j[2])} for j in jokes]
        logger.info(f"All jokes fetched. Count: {len(result)}")
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /jokes")
        return jsonify({"error": "Internal server error"}), 500

@blueprint.route("/jokes/<int:joke_id>", methods=["GET"])
@require_token
def api_get_joke(joke_id):
    joke = get_joke_by_id(joke_id)
    if joke is None:
        return jsonify({"error": "Joke not found"}), 404
    return jsonify({"id": joke[0], "text": joke[1], "sent": bool(joke[2])})

@blueprint.route("/jokes", methods=["POST"])
@require_token
def api_add_joke():
    data = request.get_json()
    if not data or "text" not in data:
        logger.warning(f"BadRequest: Missing 'text' in POST /jokes. IP: {request.remote_addr}")
        return jsonify({"error": "Missing 'text' field"}), 400

    add_joke(data["text"])
    logger.info(f"Joke added via API from {request.remote_addr}")
    return jsonify({"status": "ok"}), 201

@blueprint.route("/jokes/<int:joke_id>", methods=["PUT"])
@require_token
def api_update_joke(joke_id):
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "Missing 'text' field"}), 400

    success = update_joke(joke_id, data["text"])
    if success:
        logger.info(f"Joke {joke_id} updated via API")
        return jsonify({"status": "updated", "id": joke_id})
    else:
        return jsonify({"error": "Joke not found"}), 404

@blueprint.route("/jokes/<int:joke_id>", methods=["DELETE"])
@require_token
def api_delete_joke(joke_id):
    success = delete_joke(joke_id)
    if success:
        logger.info(f"Joke {joke_id} deleted via API")
        return jsonify({"status": "deleted", "id": joke_id})
    else:
        return jsonify({"error": "Joke not found"}), 404

@blueprint.route("/jokes/reset", methods=["POST"])
@require_token
def api_reset_jokes():
    """Wipe per-guild sent-joke history for every guild so all pools
    recycle simultaneously. The joke pool itself (the `jokes` table) is
    untouched — this only clears the "which jokes has each guild
    already received" tracking."""
    reset_all_guild_joke_sent()
    logger.info("All guild joke histories reset via API")
    return jsonify({"status": "reset"})


# --- Per-Guild Joke Schedule Routes ---
#
# `GET /jokes/guilds` — list every guild that has /joke-activation set
# `GET /jokes/guilds/<guild_id>` — one guild's config, or 404
# `PUT /jokes/guilds/<guild_id>` — activate / update (body: channel_id, send_time)
# `DELETE /jokes/guilds/<guild_id>` — deactivate that guild

def _serialize_guild_joke_config(cfg):
    """Public-facing shape for the per-guild config dicts the DB
    returns (kept as a tiny helper so GET-one and GET-all stay
    consistent if we ever add fields)."""
    return {
        "guild_id": cfg["guild_id"],
        "channel_id": cfg["channel_id"],
        "send_time": cfg["send_time"],
        "last_sent_date": cfg["last_sent_date"],
    }


@blueprint.route("/jokes/guilds", methods=["GET"])
@require_token
def api_get_all_guild_joke_configs():
    try:
        configs = get_all_guild_joke_configs()
        result = [_serialize_guild_joke_config(c) for c in configs]
        logger.info(f"Per-guild joke configs fetched. Count: {len(result)}")
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /jokes/guilds")
        return jsonify({"error": "Internal server error"}), 500


@blueprint.route("/jokes/guilds/<int:guild_id>", methods=["GET"])
@require_token
def api_get_guild_joke_config(guild_id):
    cfg = get_guild_joke_config(guild_id)
    if cfg is None:
        return jsonify({"error": "Guild not configured"}), 404
    return jsonify(_serialize_guild_joke_config(cfg))


@blueprint.route("/jokes/guilds/<int:guild_id>", methods=["PUT"])
@require_token
def api_put_guild_joke_config(guild_id):
    """Create or update a guild's daily-joke schedule.

    Body: `{ "channel_id": int, "send_time": "HH:MM" }`.

    Both fields are required because the upsert in the DB sets both
    unconditionally — a partial PUT would silently drop the omitted
    field on update. Use a fresh PUT to change either."""
    data = request.get_json()
    if not data or "channel_id" not in data or "send_time" not in data:
        logger.warning(
            f"BadRequest: PUT /jokes/guilds/{guild_id} missing channel_id "
            f"or send_time. IP: {request.remote_addr}"
        )
        return jsonify({"error": "Missing channel_id or send_time"}), 400

    try:
        channel_id = _discord_id(data["channel_id"], "channel_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    send_time = data["send_time"]

    try:
        datetime.strptime(send_time, "%H:%M")
    except (TypeError, ValueError):
        return jsonify({"error": "send_time must match HH:MM (e.g. 14:00)"}), 400

    set_guild_joke_config(guild_id, channel_id, send_time)
    logger.info(
        f"Guild {guild_id} joke config set via API: channel={channel_id} time={send_time}"
    )
    return jsonify({
        "status": "ok",
        "guild_id": guild_id,
        "channel_id": channel_id,
        "send_time": send_time,
    }), 200


@blueprint.route("/jokes/guilds/<int:guild_id>", methods=["DELETE"])
@require_token
def api_delete_guild_joke_config(guild_id):
    removed = clear_guild_joke_config(guild_id)
    if not removed:
        return jsonify({"error": "Guild not configured"}), 404
    logger.info(f"Guild {guild_id} joke config cleared via API")
    return jsonify({"status": "deleted", "guild_id": guild_id})


# --- LLM and inactivity configuration --------------------------------------

@blueprint.route("/inactivity/guilds/<int:guild_id>", methods=["GET"])
@require_token
def api_get_guild_inactivity(guild_id):
    return jsonify({"guild_id": guild_id, "enabled": is_guild_inactivity_enabled(guild_id)})


@blueprint.route("/inactivity/guilds/<int:guild_id>", methods=["PUT"])
@require_token
def api_set_guild_inactivity(guild_id):
    data = request.get_json()
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    set_guild_inactivity_enabled(guild_id, enabled)
    return jsonify({"status": "updated", "guild_id": guild_id, "enabled": enabled})


# --- Persistent LLM memory administration ----------------------------------

_SPONSOR_TIER_FIELDS = frozenset(("name", "price_per_year", "chance"))


def _serialize_sponsor_tier(tier):
    return {
        "id": tier["id"],
        "name": tier["name"],
        "price_per_year": tier["price_per_year"],
        "chance": tier["chance"],
    }


def _sponsor_tier_payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise TierValidationError("JSON object required")
    if set(data) != _SPONSOR_TIER_FIELDS:
        missing = _SPONSOR_TIER_FIELDS - set(data)
        extra = set(data) - _SPONSOR_TIER_FIELDS
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unsupported " + ", ".join(sorted(extra)))
        raise TierValidationError("; ".join(details))
    if isinstance(data["chance"], bool) or not isinstance(data["chance"], (int, float)):
        raise TierValidationError("chance must be a numeric probability from 0 to 1")
    return data


def _tier_error_response(exc):
    if isinstance(exc, DuplicateTierNameError):
        return jsonify({"error": str(exc)}), 409
    if isinstance(exc, UnknownTierError):
        return jsonify({"error": str(exc)}), 404
    if isinstance(exc, (TierCatalogError, sqlite3.Error)):
        logger.exception("Sponsor tier configuration/storage failure")
        return jsonify({"error": "Sponsor tier configuration could not be loaded or saved"}), 500
    if isinstance(exc, (TierValidationError, ValueError)):
        return jsonify({"error": str(exc)}), 400
    logger.exception("Unexpected sponsor tier failure")
    return jsonify({"error": "Sponsor tier configuration could not be loaded or saved"}), 500


@blueprint.route("/sponsors/tiers", methods=["GET"])
@require_token
def api_get_sponsor_tiers():
    try:
        return jsonify([_serialize_sponsor_tier(tier) for tier in get_sponsor_tiers().values()])
    except Exception as exc:
        return _tier_error_response(exc)


@blueprint.route("/sponsors/tiers", methods=["POST"])
@require_token
def api_create_sponsor_tier():
    try:
        data = _sponsor_tier_payload()
        tier = save_sponsor_tier(None, data["name"], data["price_per_year"], data["chance"])
        return jsonify(_serialize_sponsor_tier(tier)), 201
    except Exception as exc:
        return _tier_error_response(exc)


@blueprint.route("/sponsors/tiers/<tier_id>", methods=["PUT"])
@require_token
def api_update_sponsor_tier(tier_id):
    try:
        data = _sponsor_tier_payload()
        tier = save_sponsor_tier(tier_id, data["name"], data["price_per_year"], data["chance"])
        return jsonify(_serialize_sponsor_tier(tier))
    except Exception as exc:
        return _tier_error_response(exc)

@blueprint.route("/settings/<key>", methods=["GET"])
@require_token
def api_get_setting(key):
    value = get_setting(key)
    if value is None:
        return jsonify({"error": "Setting not found"}), 404
    return jsonify({"key": key, "value": value})


@blueprint.route("/settings/<key>", methods=["PUT"])
@require_token
def api_set_setting(key):
    if key == "sponsor_tiers":
        return jsonify({"error": "Use the typed /sponsors/tiers routes for sponsor tier changes"}), 400
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing 'value' field"}), 400

    set_setting(key, data["value"])
    logger.info(f"Setting '{key}' updated via API from {request.remote_addr}")
    return jsonify({"status": "updated", "key": key, "value": data["value"]})


@blueprint.route('/reminders/<int:reminder_id>',methods=['PATCH'])
@require_token
def api_edit_reminder(reminder_id):
    from db import reminders as store
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify({'error':'JSON object required'}),400
    try:
        if 'local_time' in data:
            from command_time import calendar_time
            row = store.get(reminder_id)
            if row is None:
                return jsonify({'error':'Reminder unavailable'}),404
            zone = data.get('timezone',row['timezone'])
            if not isinstance(data['local_time'],str):
                raise ValueError('local_time must be a string')
            data['remind_at'] = calendar_time(data.pop('local_time').replace('T',' '), zone)
            if data['remind_at'] is None:
                raise ValueError('Use a future, unambiguous date and time')
        ok=store.edit(reminder_id,**data)
    except (ValueError,TypeError,KeyError) as exc:
        return jsonify({'error':str(exc)}),400
    if not ok:
        return jsonify({'error':'Reminder unavailable'}),404
    return jsonify(store.get(reminder_id))


@blueprint.route('/reminders/<int:reminder_id>/retry',methods=['POST'])
@require_token
def api_retry_reminder(reminder_id):
    from db import reminders as store
    if not store.retry(reminder_id):
        return jsonify({'error':'Failed reminder not found'}),404
    return jsonify(store.get(reminder_id))


@blueprint.route('/assistant-profiles/<user_id>',methods=['GET','PATCH'])
@require_token
def api_assistant_profile(user_id):
    import db
    from assistant_profiles import effective_profile,validate_profile_updates
    try:
        user_id=_discord_id(user_id,'user_id')
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    if request.method=='PATCH':
        data=request.get_json(silent=True)
        if not isinstance(data,dict):
            return jsonify({'error':'JSON object required'}),400
        try:
            updates=validate_profile_updates(**data)
        except (ValueError,TypeError,KeyError) as exc:
            return jsonify({'error':str(exc)}),400
        if 'timezone_configured' in updates and 'timezone' not in updates:
            return jsonify({'error':'Provide a timezone to configure it'}),400
        if db.set_assistant_profile(user_id,**updates) is None:
            return jsonify({'error':'Invalid profile; quiet hours need both bounds and an explicit timezone'}),400
    return jsonify(effective_profile(db.get_assistant_profile(user_id)).as_dict())


@blueprint.route('/notifications',methods=['GET'])
@require_token
def api_notifications():
    from db.connection import _connect
    from db.notifications import summary
    raw_user=request.args.get('user_id')
    try:
        user_id=_discord_id(raw_user,'user_id') if raw_user else None
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    with _connect() as c:
        query='SELECT id,user_id,source,item_key,event_kind,observed_at,due_at,state,attempts,last_error FROM notification_outbox'
        rows=c.execute(query+(' WHERE user_id=?' if user_id is not None else '')+' ORDER BY id DESC LIMIT 100',[user_id] if user_id is not None else []).fetchall()
    keys=('id','user_id','source','item_key','event_kind','observed_at','due_at','state','attempts','last_error')
    return jsonify({'counts':summary(user_id),'items':[dict(zip(keys,r)) for r in rows]})


@blueprint.route('/notifications/<int:notification_id>/retry',methods=['POST'])
@require_token
def api_retry_notification(notification_id):
    from db.notifications import retry
    if not retry(notification_id):
        return jsonify({'error':'Failed notification not found'}),404
    return jsonify({'status':'pending'})


@blueprint.route('/analytics/natural',methods=['GET'])
@require_analytics_token
def api_natural_analytics():
    from db.connection import _connect
    period = request.args.get('period', '30d')
    try:
        guild = request.args.get('guild_id')
        guild_id = _discord_id(guild, 'guild_id') if guild else None
        bounds = get_analytics_summary(period, guild_id)['period']
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    query = "SELECT activity,SUM(count) FROM analytics_daily WHERE activity LIKE 'natural/%' AND day>=? AND day<=?"
    args = [bounds['start'], bounds['end']]
    if guild_id is not None:
        query += ' AND guild_id=?'
        args.append(guild_id)
    with _connect() as c:
        rows = c.execute(query + ' GROUP BY activity', args).fetchall()
        reminder_states = dict(c.execute('SELECT state,COUNT(*) FROM reminders GROUP BY state').fetchall())
        notification_states = dict(c.execute('SELECT state,COUNT(*) FROM notification_outbox GROUP BY state').fetchall())
    return jsonify({'delivery':{'reminders':reminder_states,'notifications':notification_states},'outcomes':[{'intent':a.split('/')[1],'language':a.split('/')[2],'route':a.split('/')[3],'outcome':a.split('/')[4],'count':n} for a,n in rows]})
