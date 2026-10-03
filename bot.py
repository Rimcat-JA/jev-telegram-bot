"""Jev selects a Telegram reply from configurable options. Python 3.10+, stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
INSTRUCTIONS = "チャットのメッセージへの返答として、提示された選択肢から最も適切なものを1つ選んでください。"


class BotError(Exception):
    """Safe diagnostic: never include raw URLs, response bodies, or credentials."""


class APIError(BotError):
    def __init__(self, service: str, code: int = 0, retry_after: float = 0):
        self.service = service
        self.code = code
        self.retry_after = retry_after
        self.retryable = code == 0 or code == 429 or code >= 500
        super().__init__(f"{service}: API/connection error (code={code})")


def load_env(path: Path = ROOT / ".env") -> None:
    """Small .env reader; environment variables take precedence. No shell evaluation."""
    if not path.exists():
        return
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise BotError(f"Invalid .env syntax at line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("REPLACE_"):
        raise BotError(f"Set {name} in .env before starting")
    return value


def validate_choices(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 255:
        raise BotError("Choices must be a JSON array of 2 to 255 strings")
    for choice in value:
        if not isinstance(choice, str) or not choice.strip():
            raise BotError("Each choice must be a non-empty string")
        try:
            size = len(choice.encode("utf-16-le")) // 2
        except UnicodeError:
            raise BotError("Choices contain invalid Unicode") from None
        if size > 4096:
            raise BotError("Each choice must fit Telegram's 4096-character text limit")
    if len(set(value)) != len(value):
        raise BotError("Choices must be unique")
    return tuple(value)


def load_choices(path: Path) -> tuple[str, ...]:
    try:
        return validate_choices(json.loads(path.read_text(encoding="utf-8-sig")))
    except (ValueError, UnicodeError):
        raise BotError("Choices file is not valid UTF-8 JSON") from None


def ensure_reply(value: Any, choices: tuple[str, ...]) -> str:
    # No trimming, substring extraction, explanations, fallback, or coercion.
    if not isinstance(value, str) or value not in choices:
        raise BotError("Jev returned a reply outside the configured choices")
    return value


def retry_delay(value: Any) -> float:
    try:
        delay = float(value)
        return delay if 0 < delay < float("inf") else 0
    except (ValueError, TypeError):
        return 0


class JSONHTTP:
    def post(self, url: str, payload: dict, *, service: str,
             token: str | None = None, timeout: int = 30,
             safe_to_retry: bool = True) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.load(response)
                if not isinstance(data, dict):
                    raise BotError(f"{service}: expected a JSON object")
                if service == "Telegram" and data.get("ok") is not True:
                    raise APIError(service, int(data.get("error_code", 0)),
                                   retry_delay(data.get("parameters", {}).get("retry_after")))
                return data
            except urllib.error.HTTPError as exc:
                wait = retry_delay(exc.headers.get("Retry-After")) if exc.headers else 0
                # Telegram's JSON 429 body contains the recommended delay.
                if exc.code == 429:
                    try:
                        body = json.load(exc)
                        wait = retry_delay(body.get("parameters", {}).get("retry_after")) or wait
                    except (ValueError, AttributeError, OSError):
                        pass
                error = APIError(service, exc.code, wait)
            except (urllib.error.URLError, TimeoutError, OSError):
                error = APIError(service)
            except APIError as exc:
                error = exc
            except (ValueError, UnicodeError):
                raise BotError(f"{service}: invalid JSON response") from None
            # Retry sendMessage only on explicit rate limiting. A timeout/5xx may
            # have delivered a message already, so do not blindly resend it.
            if attempt == 2 or not error.retryable or (not safe_to_retry and error.code != 429):
                raise error from None
            time.sleep(max(error.retry_after, 2 ** attempt))
        raise AssertionError("unreachable")


class JevClassifier:
    def __init__(self, key: str, choices: tuple[str, ...], model: str = "jev-latest",
                 http: JSONHTTP | None = None):
        self.key, self.model = key, model
        self.choices = validate_choices(choices)
        self.http = http or JSONHTTP()

    def classify(self, text: str) -> str:
        data = self.http.post(
            JEV_ENDPOINT,
            {"model": self.model, "state": text,
             "questions": {"reply": {"type": "choice", "instructions": INSTRUCTIONS,
                                      "criteria": dict.fromkeys(self.choices)}}},
            service="Jev", token=self.key,
        )
        try:
            answer = data["answers"]["reply"]
            if answer["type"] != "choice":
                raise BotError("Jev returned an unexpected answer type")
            return ensure_reply(answer["choice"], self.choices)
        except (KeyError, TypeError):
            raise BotError("Jev response is missing the Choice answer") from None


class Telegram:
    def __init__(self, token: str, choices: tuple[str, ...], http: JSONHTTP | None = None):
        # Validate before inserting a token into a URL.
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise BotError("TELEGRAM_BOT_TOKEN has an invalid format")
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.choices = validate_choices(choices)
        self.http = http or JSONHTTP()

    def call(self, method: str, payload: dict | None = None, *, timeout: int = 30) -> Any:
        data = self.http.post(self.base_url + method, payload or {}, service="Telegram",
                              timeout=timeout, safe_to_retry=method != "sendMessage")
        if "result" not in data:
            raise BotError("Telegram response is missing result")
        return data["result"]

    def reply(self, message: dict, text: str) -> None:
        payload = {
            "chat_id": message["chat"]["id"],
            "text": ensure_reply(text, self.choices),
            "reply_parameters": {"message_id": message["message_id"]},
        }
        if "message_thread_id" in message:
            payload["message_thread_id"] = message["message_thread_id"]
        self.call("sendMessage", payload)


class Bot:
    def __init__(self, telegram: Telegram, classifier: JevClassifier, state_path: Path):
        self.telegram, self.classifier, self.state_path = telegram, classifier, state_path
        self.offset = 0
        if state_path.exists():
            try:
                self.offset = int(state_path.read_text(encoding="utf-8"))
                if self.offset < 0:
                    raise ValueError
            except ValueError:
                raise BotError("Invalid polling offset file; inspect .state before restarting") from None

    def save_offset(self, offset: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(str(offset), encoding="utf-8")
        temporary.replace(self.state_path)
        self.offset = offset

    def process(self, update: dict) -> None:
        message = update.get("message")
        if not message or message.get("from", {}).get("is_bot"):
            return
        text = message.get("text") or message.get("caption")
        if not isinstance(text, str) or not text.strip():
            return
        reply = self.classifier.classify(text)
        try:
            self.telegram.reply(message, reply)
        except APIError as exc:
            if exc.code in (401, 409, 429):
                raise
            # Deleted messages, blocked bot, or ambiguous send failure: record
            # completion, with a local diagnostic and no extra Telegram reply.
            logging.warning("Reply delivery failed: code=%s; no automatic resend", exc.code)

    def run(self) -> None:
        self.telegram.call("getMe")
        if self.telegram.call("getWebhookInfo").get("url"):
            raise BotError("A webhook is configured; remove it before using polling")
        logging.info("Bot started; press Ctrl+C to stop")
        backoff = 1
        while True:
            try:
                updates = self.telegram.call(
                    "getUpdates", {"offset": self.offset, "timeout": 30,
                                   "allowed_updates": ["message"]}, timeout=45,
                )
                if not isinstance(updates, list):
                    raise BotError("Telegram returned invalid updates")
                for update in updates:
                    update_id = update["update_id"]
                    if update_id < self.offset:
                        continue
                    self.process(update)
                    self.save_offset(update_id + 1)
                backoff = 1
            except APIError as exc:
                if not exc.retryable:
                    raise
                logging.warning("%s; retrying without advancing the pending update", exc)
                time.sleep(max(backoff, exc.retry_after))
                backoff = min(backoff * 2, 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jev Telegram bot: reply using only configured choices")
    parser.add_argument("--choices", type=Path, default=ROOT / "choices.json",
                        metavar="FILE", help="JSON array of allowed replies (default: choices.json)")
    parser.add_argument("--classify", metavar="TEXT", help="Call Jev once without sending to Telegram")
    parser.add_argument("--check-config", action="store_true", help="Validate local configuration only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_env()
        choices = load_choices(args.choices)
        classifier = JevClassifier(required_env("TYPESAFE_API_KEY"), choices,
                                   os.environ.get("JEV_MODEL", "jev-latest").strip() or "jev-latest")
        if args.classify is not None:
            if not args.classify.strip():
                raise BotError("Provide non-empty text to --classify")
            print(classifier.classify(args.classify))
            return 0
        token = required_env("TELEGRAM_BOT_TOKEN")
        telegram = Telegram(token, choices)
        if args.check_config:
            print(f"Local configuration OK: {len(choices)} choices (API connectivity not checked)")
            return 0
        identity = hashlib.sha256(token.encode()).hexdigest()[:16]
        Bot(telegram, classifier, ROOT / ".state" / f"{identity}.offset").run()
    except KeyboardInterrupt:
        logging.info("Bot stopped")
        return 0
    except (BotError, OSError) as exc:
        # OSError filenames can contain user-controlled paths; keep that generic.
        logging.error("%s", str(exc) if isinstance(exc, BotError) else "Local file/network error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
