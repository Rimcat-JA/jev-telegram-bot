import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import bot


EXAMPLE_CHOICES = ("好", "不好", "羡慕", "不羡慕")
TOKEN = "123456:fake_test_token"


def message(text="任意のメッセージ", **extra):
    return {"message_id": 42, "chat": {"id": -123, "type": "supergroup"},
            "from": {"id": 7, "is_bot": False}, "text": text, **extra}


def answer(choice):
    return {"answers": {"reply": {"type": "choice", "choice": choice,
                                  "confidence": 0.01}}}


def response(data):
    return io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8"))


class ChoiceTests(unittest.TestCase):
    def test_choices_support_different_sizes_and_full_sentences(self):
        for choices in [("yes", "no"), ("了解です", "ありがとう！", "詳しく教えてください"),
                        EXAMPLE_CHOICES, tuple(f"option {i}" for i in range(255))]:
            with self.subTest(n=len(choices)):
                self.assertEqual(bot.validate_choices(choices), choices)

    def test_invalid_config_is_rejected(self):
        for choices in [[], ["one"], ["same", "same"], ["", "no"], ["  ", "no"],
                        [1, "no"], {"yes": "a rubric", "no": "another rubric"},
                        ["x" * 4097, "no"], ["😀" * 2049, "no"],
                        ["\ud800", "no"], [str(i) for i in range(256)]]:
            with self.subTest(choices_type=type(choices).__name__):
                with self.assertRaises(bot.BotError):
                    bot.validate_choices(choices)

    def test_choices_file_supports_utf8_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "choices.json"
            path.write_text(json.dumps(EXAMPLE_CHOICES, ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual(bot.load_choices(path), EXAMPLE_CHOICES)
            path.write_text("broken JSON", encoding="utf-8")
            with self.assertRaises(bot.BotError):
                bot.load_choices(path)

    def test_input_and_choices_are_sent_without_rubrics(self):
        http = Mock()
        http.post.return_value = answer("不羡慕")
        classifier = bot.JevClassifier("fake-key", EXAMPLE_CHOICES, http=http)
        text = "好という単語が含まれていても、選ぶのはJev。"
        self.assertEqual(classifier.classify(text), "不羡慕")
        args, kwargs = http.post.call_args
        self.assertEqual(args[0], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(args[1], {
            "model": "jev-latest", "state": text,
            "questions": {"reply": {
                "type": "choice",
                "instructions": "チャットのメッセージへの返答として、提示された選択肢から最も適切なものを1つ選んでください。",
                "criteria": {choice: None for choice in EXAMPLE_CHOICES},
            }},
        })
        self.assertEqual(kwargs["token"], "fake-key")

    def test_out_of_set_or_malformed_model_output_is_rejected(self):
        http = Mock()
        classifier = bot.JevClassifier("fake", EXAMPLE_CHOICES, http=http)
        for data in [answer("好。"), answer("好\n"), answer(" 好"), answer("解释：好"),
                     answer(None), answer(["好"]), {}, {"answers": None},
                     {"answers": {"reply": {"type": "score", "choice": "好"}}}]:
            with self.subTest(data=data):
                http.post.return_value = data
                with self.assertRaises(bot.BotError):
                    classifier.classify("text")


class EndToEndTests(unittest.TestCase):
    def test_each_selected_reply_is_sent_exactly_including_custom_options(self):
        for choices in [EXAMPLE_CHOICES, ("Yes", "No"),
                        ("ありがとうございます！", "詳しく教えてください", "👍")]:
            for chosen in choices:
                with self.subTest(chosen=chosen), tempfile.TemporaryDirectory() as folder:
                    captured = []

                    def urlopen(request, timeout):
                        captured.append((request, json.loads(request.data)))
                        return response(answer(chosen) if len(captured) == 1
                                        else {"ok": True, "result": {"message_id": 99}})

                    tg = bot.Telegram(TOKEN, choices)
                    instance = bot.Bot(tg, bot.JevClassifier("fake-key", choices),
                                       Path(folder) / "offset")
                    with patch("bot.urllib.request.urlopen", side_effect=urlopen):
                        instance.process({"update_id": 50, "message": message(message_thread_id=17)})
                    self.assertEqual(len(captured), 2)
                    request, payload = captured[0]
                    self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
                    self.assertEqual(payload["questions"]["reply"]["criteria"], dict.fromkeys(choices))
                    request, payload = captured[1]
                    self.assertTrue(request.full_url.endswith("/sendMessage"))
                    self.assertEqual(payload, {"chat_id": -123, "text": chosen,
                                              "reply_parameters": {"message_id": 42},
                                              "message_thread_id": 17})
                    self.assertIsNone(request.get_header("Authorization"))

    def test_send_boundary_rejects_extra_text(self):
        http = Mock()
        tg = bot.Telegram(TOKEN, EXAMPLE_CHOICES, http=http)
        with self.assertRaises(bot.BotError):
            tg.reply(message(), "好、ありがとうございます")
        http.post.assert_not_called()

    def test_model_failure_sends_nothing(self):
        tg, classifier = Mock(), Mock()
        classifier.classify.side_effect = bot.APIError("Jev", 503)
        with tempfile.TemporaryDirectory() as folder:
            instance = bot.Bot(tg, classifier, Path(folder) / "offset")
            with self.assertRaises(bot.APIError):
                instance.process({"message": message()})
        tg.reply.assert_not_called()

    def test_nontext_bot_and_edit_updates_are_ignored(self):
        tg, classifier = Mock(), Mock()
        with tempfile.TemporaryDirectory() as folder:
            instance = bot.Bot(tg, classifier, Path(folder) / "offset")
            for update in [{}, {"edited_message": message()}, {"message": message("")},
                           {"message": message("   ")},
                           {"message": message(**{"from": {"is_bot": True}})}]:
                instance.process(update)
        classifier.classify.assert_not_called()
        tg.reply.assert_not_called()

    def test_caption_is_classified_without_a_local_rule(self):
        tg, classifier = Mock(), Mock()
        classifier.classify.return_value = "No"
        with tempfile.TemporaryDirectory() as folder:
            instance = bot.Bot(tg, classifier, Path(folder) / "offset")
            instance.process({"message": message(None, caption="photo caption")})
        classifier.classify.assert_called_once_with("photo caption")
        self.assertEqual(tg.reply.call_args.args[1], "No")


class PollingTests(unittest.TestCase):
    def test_processed_offset_is_saved_and_repeated_updates_ignored(self):
        tg, classifier = Mock(), Mock()
        classifier.classify.return_value = "好"
        update = {"update_id": 50, "message": message()}
        tg.call.side_effect = [{"id": 1}, {"url": ""}, [update], [update], KeyboardInterrupt()]
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "offset"
            instance = bot.Bot(tg, classifier, state)
            with self.assertRaises(KeyboardInterrupt):
                instance.run()
            self.assertEqual(state.read_text(), "51")
            self.assertEqual(bot.Bot(tg, classifier, state).offset, 51)
            self.assertEqual(tg.call.call_args_list[3].args[1]["offset"], 51)
        tg.reply.assert_called_once()

    def test_jev_failure_keeps_update_pending_until_success(self):
        tg, classifier = Mock(), Mock()
        classifier.classify.side_effect = [bot.APIError("Jev", 503), "不好"]
        update = {"update_id": 50, "message": message()}
        tg.call.side_effect = [{"id": 1}, {"url": ""}, [update], [update], KeyboardInterrupt()]
        with tempfile.TemporaryDirectory() as folder, patch("bot.time.sleep") as sleep:
            state = Path(folder) / "offset"
            instance = bot.Bot(tg, classifier, state)
            with self.assertRaises(KeyboardInterrupt):
                instance.run()
            self.assertEqual(tg.call.call_args_list[3].args[1]["offset"], 0)
            self.assertEqual(state.read_text(), "51")
            sleep.assert_called_once()
        tg.reply.assert_called_once_with(update["message"], "不好")

    def test_webhook_conflict_stops_before_polling(self):
        tg = Mock()
        tg.call.side_effect = [{"id": 1}, {"url": "https://example.invalid/webhook"}]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(bot.BotError):
                bot.Bot(tg, Mock(), Path(folder) / "offset").run()
        self.assertEqual(tg.call.call_count, 2)

    def test_ambiguous_send_failure_is_not_replied_to_or_resent(self):
        tg, classifier = Mock(), Mock()
        classifier.classify.return_value = "好"
        tg.reply.side_effect = bot.APIError("Telegram", 0)
        with tempfile.TemporaryDirectory() as folder:
            bot.Bot(tg, classifier, Path(folder) / "offset").process({"message": message()})
        tg.reply.assert_called_once()


class TransportTests(unittest.TestCase):
    def test_jev_503_is_retried(self):
        failure = urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, None)
        with (patch("bot.urllib.request.urlopen", side_effect=[failure, response(answer("yes"))]) as call,
              patch("bot.time.sleep") as sleep):
            data = bot.JSONHTTP().post(bot.JEV_ENDPOINT, {}, service="Jev")
        self.assertEqual(data, answer("yes"))
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_send_timeout_is_not_retried_and_diagnostic_has_no_token(self):
        failure = urllib.error.URLError("https://api.telegram.org/botSECRET/sendMessage")
        with patch("bot.urllib.request.urlopen", side_effect=failure) as call:
            with self.assertRaises(bot.APIError) as error:
                bot.JSONHTTP().post("https://api.telegram.org/botSECRET/sendMessage", {},
                                    service="Telegram", safe_to_retry=False)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("SECRET", str(error.exception))

    def test_explicit_send_rate_limit_honors_retry_after(self):
        failure = urllib.error.HTTPError("https://example.invalid", 429, "Rate limited", {},
                                         response({"parameters": {"retry_after": 7}}))
        with (patch("bot.urllib.request.urlopen", side_effect=[failure, response({"ok": True})]) as call,
              patch("bot.time.sleep") as sleep):
            bot.JSONHTTP().post("https://example.invalid", {}, service="Telegram", safe_to_retry=False)
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_authentication_failure_is_not_retried(self):
        failure = urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, None)
        with patch("bot.urllib.request.urlopen", side_effect=failure) as call:
            with self.assertRaises(bot.APIError):
                bot.JSONHTTP().post(bot.JEV_ENDPOINT, {}, service="Jev")
        self.assertEqual(call.call_count, 1)


class ConfigTests(unittest.TestCase):
    def test_env_loading_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / ".env"
            path.write_text('# comment\nTYPESAFE_API_KEY="file-key"\nJEV_MODEL=jev-latest\n', encoding="utf-8")
            os.environ["TYPESAFE_API_KEY"] = "env-key"
            bot.load_env(path)
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "env-key")
            self.assertEqual(os.environ["JEV_MODEL"], "jev-latest")

    def test_classify_cli_requires_only_jev_key_and_prints_choice_only(self):
        output = io.StringIO()
        with (patch.dict(os.environ, {"TYPESAFE_API_KEY": "fake"}, clear=True),
              patch("bot.load_env"), patch("bot.JevClassifier.classify", return_value="羡慕"),
              contextlib.redirect_stdout(output)):
            self.assertEqual(bot.main(["--classify", "a message"]), 0)
        self.assertEqual(output.getvalue(), "羡慕\n")


if __name__ == "__main__":
    unittest.main()
