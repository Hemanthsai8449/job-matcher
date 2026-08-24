from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from ..extensions import csrf, db, limiter
from ..models import Application, AuditLog, Job, User, as_utc
from ..services.alert_service import dispatch_user_alerts
from ..services.telegram_service import (
    TelegramLinkError,
    TelegramUpdateClaimLost,
    answer_callback,
    bind_contact,
    claim_update,
    contact_request_markup,
    finish_update,
    parse_start_payload,
    pop_update_claim,
    prepare_contact_link,
    push_update_claim,
    remove_keyboard_markup,
    renew_update_claim,
    send_message,
    verify_webhook_secret,
)
from ..utils import utcnow

bp = Blueprint("integrations", __name__, url_prefix="/integrations")
MAX_TELEGRAM_UPDATE_BYTES = 1 * 1024 * 1024


def _public_url(path: str) -> str:
    base = str(current_app.config.get("APP_BASE_URL", "http://127.0.0.1:5000")).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _linked_user(chat_id: Any, telegram_user_id: Any) -> User | None:
    return db.session.scalar(
        db.select(User).where(
            User.telegram_chat_id == str(chat_id),
            User.telegram_user_id == str(telegram_user_id),
            User.phone_verified_at.is_not(None),
            User.email_verified_at.is_not(None),
            User.deleted_at.is_(None),
        )
    )


def _send_connect_message(chat_id: Any) -> None:
    register_url = _public_url("auth/register")
    send_message(
        chat_id,
        "<b>Connect from Job Matcher first</b>\n\n"
        "For your privacy, this bot sends job links only to a verified number registered "
        "on Job Matcher. "
        f'<a href="{html.escape(register_url, quote=True)}">Open Job Matcher</a>, '
        "sign in, and choose Connect Telegram.",
        parse_mode="HTML",
    )


def _private_message(update: Mapping[str, Any]) -> tuple[Mapping[str, Any], Any, Any] | None:
    message = update.get("message")
    if not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
        return None
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if chat.get("type") != "private" or chat_id is None or sender_id is None:
        return None
    # A private Telegram chat belongs to the sending Telegram account.
    if str(chat_id) != str(sender_id):
        return None
    return message, chat_id, sender_id


def _handle_message(update: Mapping[str, Any]) -> None:
    unpacked = _private_message(update)
    if unpacked is None:
        return
    message, chat_id, sender_id = unpacked
    sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
    contact = message.get("contact")
    if isinstance(contact, Mapping):
        existing_user = _linked_user(chat_id, sender_id)
        if existing_user is not None:
            send_message(
                chat_id,
                "Your registered number is already connected to Job Matcher.",
                reply_markup=remove_keyboard_markup(),
            )
            return
        result = bind_contact(
            chat_id,
            sender_id,
            contact,
            username=str(sender.get("username") or ""),
        )
        send_message(
            chat_id,
            html.escape(result.message),
            parse_mode="HTML",
            reply_markup=remove_keyboard_markup(),
        )
        if result.success:
            send_message(
                chat_id,
                "<b>You're ready.</b> Your personalized alerts are active for seven days. "
                "Use /jobs for new matches, /pause to pause alerts, or /settings to "
                "change preferences.",
                parse_mode="HTML",
            )
        return

    text = str(message.get("text") or "").strip()
    start_token = parse_start_payload(text)
    if start_token:
        try:
            prepare_contact_link(start_token, chat_id, sender_id)
        except TelegramLinkError as exc:
            send_message(chat_id, html.escape(str(exc)), parse_mode="HTML")
        else:
            send_message(
                chat_id,
                "<b>Verify your registered number</b>\n\n"
                "Tap the button below to share your own Telegram contact. It must exactly match "
                "the mobile number in your Job Matcher account.",
                parse_mode="HTML",
                reply_markup=contact_request_markup(),
            )
        return

    user = _linked_user(chat_id, sender_id)
    if user is None:
        _send_connect_message(chat_id)
        return

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if command in {"/start", "/help"}:
        send_message(
            chat_id,
            f"<b>Hello, {html.escape(user.full_name)}.</b>\n\n"
            "Use /jobs for fresh matched jobs, /pause to pause daily alerts, and /settings "
            "to update your profile or renew the seven-day alert period.",
            parse_mode="HTML",
        )
    elif command == "/jobs":
        result = dispatch_user_alerts(user)
        if result["reason"] == "alerts_inactive":
            send_message(
                chat_id,
                "Your alert period is paused or expired. Renew it securely from "
                '<a href="{}">Job Matcher settings</a>.'.format(
                    html.escape(_public_url("settings"), quote=True)
                ),
                parse_mode="HTML",
            )
        elif result["sent"] == 0:
            send_message(
                chat_id,
                "No new qualifying jobs are available right now. Previously sent links are "
                "never repeated; "
                "we'll check again at your preferred alert time.",
                parse_mode="HTML",
            )
        else:
            send_message(
                chat_id,
                f"Sent {int(result['sent'])} new match{'es' if result['sent'] != 1 else ''}. "
                "Review each employer and role before applying.",
                parse_mode="HTML",
            )
    elif command == "/pause":
        if user.alert_status != "paused":
            user.alert_status = "paused"
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="alerts.paused_from_telegram",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
        send_message(chat_id, "Daily job alerts are paused. Your saved jobs remain available.")
    elif command == "/resume":
        current_time = utcnow()
        active_until = as_utc(user.active_until)
        if not active_until or active_until <= current_time:
            send_message(
                chat_id,
                "Your seven-day alert period has ended. For security and fresh preferences, "
                "renew from "
                '<a href="{}">Job Matcher settings</a>.'.format(
                    html.escape(_public_url("settings"), quote=True)
                ),
                parse_mode="HTML",
            )
            return
        if user.alert_status != "active":
            user.alert_status = "active"
            db.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    event="alerts.resumed_from_telegram",
                    entity_type="user",
                    entity_id=str(user.id),
                )
            )
            db.session.commit()
        send_message(
            chat_id,
            "Job alerts are active. You can review the end date and delivery time in "
            f'<a href="{html.escape(_public_url("settings"), quote=True)}">Settings</a>.',
            parse_mode="HTML",
        )
    elif command == "/settings":
        send_message(
            chat_id,
            '<a href="{}">Open Job Matcher settings</a> '.format(
                html.escape(_public_url("settings"), quote=True)
            )
            + "to update your roles, job count, delivery time, resume, or privacy choices.",
            parse_mode="HTML",
        )
    else:
        send_message(
            chat_id, "I didn't recognize that command. Use /help to see available actions."
        )


def _handle_callback(update: Mapping[str, Any]) -> None:
    callback = update.get("callback_query")
    if not isinstance(callback, Mapping):
        return
    callback_id = callback.get("id")
    sender = callback.get("from")
    message = callback.get("message")
    if not callback_id or not isinstance(sender, Mapping) or not isinstance(message, Mapping):
        return
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        answer_callback(
            callback_id,
            "This action is available only in a private chat.",
            show_alert=True,
        )
        return
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    user = _linked_user(chat_id, sender_id)
    if user is None:
        answer_callback(callback_id, "Connect your registered number first.", show_alert=True)
        return
    parts = str(callback.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] != "job" or parts[1] not in {"save", "not_interested"}:
        answer_callback(callback_id, "That action is no longer available.")
        return
    try:
        job_id = int(parts[2])
    except ValueError:
        answer_callback(callback_id, "That job could not be found.", show_alert=True)
        return
    job = db.session.get(Job, job_id)
    if job is None:
        answer_callback(callback_id, "That job could not be found.", show_alert=True)
        return
    application = db.session.scalar(
        db.select(Application).where(
            Application.user_id == user.id,
            Application.job_id == job.id,
        )
    )
    target_status = "saved" if parts[1] == "save" else "not_interested"
    changed = application is None or application.status != target_status
    if application is None:
        application = Application(user_id=user.id, job_id=job.id)
        db.session.add(application)
    if changed:
        application.status = target_status
        db.session.add(
            AuditLog(
                actor_user_id=user.id,
                event=f"job.{target_status}_from_telegram",
                entity_type="job",
                entity_id=str(job.id),
            )
        )
        db.session.commit()
    label = "Saved" if target_status == "saved" else "Removed from future alerts"
    answer_callback(callback_id, label)


@bp.post("/telegram/webhook")
@csrf.exempt
@limiter.exempt
def telegram_webhook():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not verify_webhook_secret(secret):
        return jsonify(ok=False), 403
    if request.content_length and request.content_length > MAX_TELEGRAM_UPDATE_BYTES:
        return jsonify(ok=False), 413
    if request.mimetype != "application/json":
        return jsonify(ok=False), 415
    raw_update = request.stream.read(MAX_TELEGRAM_UPDATE_BYTES + 1)
    if len(raw_update) > MAX_TELEGRAM_UPDATE_BYTES:
        return jsonify(ok=False), 413
    try:
        update = json.loads(raw_update)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(ok=False), 400
    if not isinstance(update, Mapping) or "update_id" not in update:
        return jsonify(ok=False), 400
    claim_attempt: int | None = None
    claim_token = None
    try:
        claim_attempt = claim_update(update["update_id"], commit=True)
        if claim_attempt is None:
            return jsonify(ok=True)
        if claim_attempt == 0:
            # A non-2xx response asks Telegram to retry if the current worker
            # later fails; acknowledging here could otherwise lose the update.
            return jsonify(ok=False, retry=True), 503
        claim_token = push_update_claim(update["update_id"], claim_attempt)
        renew_update_claim()
        if "callback_query" in update:
            _handle_callback(update)
        else:
            _handle_message(update)
        if not finish_update(
            update["update_id"],
            claim_attempt=claim_attempt,
            success=True,
        ):
            raise TelegramUpdateClaimLost(
                "This webhook update completed under a newer worker."
            )
    except TelegramUpdateClaimLost:
        db.session.rollback()
        if claim_attempt is not None:
            finish_update(
                update["update_id"],
                claim_attempt=claim_attempt,
                success=False,
                error="Webhook claim was superseded by a newer worker.",
            )
        return jsonify(ok=False, retry=True), 503
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Telegram update processing failed")
        if claim_attempt is not None:
            finish_update(
                update["update_id"],
                claim_attempt=claim_attempt,
                success=False,
                error=f"{exc.__class__.__name__}: update processing failed",
            )
        return jsonify(ok=False), 500
    finally:
        if claim_token is not None:
            pop_update_claim(claim_token)
    return jsonify(ok=True)
