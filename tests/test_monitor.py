from datetime import date, datetime, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError

from src.monitor import (
	DailyState,
	PageClient,
	MonitorError,
	PageKind,
	PageResult,
	TelegramClient,
	classify_tour_page,
	format_availability,
	format_unexpected,
	main,
	run_check,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
	return (FIXTURES / name).read_text(encoding="utf-8")


class PageClassificationTests(unittest.TestCase):
	def test_exact_observed_block_is_no_tours(self):
		result = classify_tour_page(load_fixture("no_tours.html"))
		self.assertEqual(PageKind.NO_TOURS, result.kind)

	def test_negative_phrase_plus_extra_content_is_not_no_tours(self):
		source = load_fixture("no_tours.html").replace(
			"В настоящий момент доступных экскурсий нет",
			"В настоящий момент доступных экскурсий нет <p>Дополнительный текст</p>",
		)
		self.assertEqual(PageKind.UNEXPECTED_FORMAT, classify_tour_page(source).kind)

	def test_actionable_tour_is_available(self):
		result = classify_tour_page(load_fixture("tours_available.html"))
		self.assertEqual(PageKind.TOURS_AVAILABLE, result.kind)
		self.assertIn("15 августа, 14:00", result.text)
		self.assertIn("Обзорная экскурсия", result.text)

	def test_missing_boundary_is_unexpected(self):
		result = classify_tour_page(
			"<h1>Записаться на экскурсию</h1><p>Технические работы</p>"
		)
		self.assertEqual(PageKind.UNEXPECTED_FORMAT, result.kind)
		self.assertTrue(result.fingerprint)


class StateTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_dir.cleanup)
		self.state_path = Path(self.temp_dir.name) / "status.json"

	def test_state_resets_for_new_moscow_date(self):
		self.state_path.write_text(
			json.dumps(
				{
					"date": "2026-08-13",
					"alert_sent": True,
					"daily_report_sent": True,
					"unexpected_hashes": ["abc"],
				}
			),
			encoding="utf-8",
		)

		state = DailyState.for_date(self.state_path, date(2026, 8, 14))

		self.assertEqual(date(2026, 8, 14), state.day)
		self.assertFalse(state.alert_sent)
		self.assertFalse(state.daily_report_sent)
		self.assertEqual(set(), state.unexpected_hashes)

	def test_state_round_trip_is_stable_and_contains_no_credentials(self):
		state = DailyState(
			day=date(2026, 8, 14),
			alert_sent=True,
			daily_report_sent=False,
			unexpected_hashes={"def", "abc"},
		)

		state.save(self.state_path)
		loaded = DailyState.for_date(self.state_path, date(2026, 8, 14))

		self.assertEqual(state, loaded)
		content = self.state_path.read_text(encoding="utf-8")
		self.assertNotIn("token", content.lower())
		self.assertLess(content.index('"abc"'), content.index('"def"'))


class _FakeResponse:
	def __init__(self, payload: bytes):
		self.payload = payload

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc, traceback):
		return False

	def read(self):
		return self.payload


class TelegramTests(unittest.TestCase):
	def test_availability_message_contains_details_and_link(self):
		message = format_availability(
			PageResult(PageKind.TOURS_AVAILABLE, "15 августа, 14:00", "abc")
		)
		self.assertIn("Появились экскурсии", message)
		self.assertIn("15 августа, 14:00", message)
		self.assertIn("https://mus-col.com/contacts/tours.php", message)

	def test_unexpected_message_contains_limited_excerpt_and_link(self):
		message = format_unexpected(
			PageResult(PageKind.UNEXPECTED_FORMAT, "x" * 1200, "abc")
		)
		self.assertIn("Не удалось распознать", message)
		self.assertIn("https://mus-col.com/contacts/tours.php", message)
		self.assertLess(len(message), 1200)

	def test_send_posts_chat_and_text(self):
		captured = {}

		def opener(request, timeout):
			captured["url"] = request.full_url
			captured["data"] = request.data.decode("utf-8")
			captured["timeout"] = timeout
			return _FakeResponse(b'{"ok": true, "result": {}}')

		TelegramClient("secret-token", "123", opener=opener).send("test message")

		self.assertIn("secret-token", captured["url"])
		self.assertIn("chat_id=123", captured["data"])
		self.assertIn("text=test+message", captured["data"])
		self.assertGreater(captured["timeout"], 0)

	def test_telegram_error_does_not_expose_token(self):
		def failing_opener(request, timeout):
			raise URLError("offline")

		client = TelegramClient("secret-token", "123", opener=failing_opener)
		with self.assertRaises(MonitorError) as raised:
			client.send("test")

		self.assertNotIn("secret-token", str(raised.exception))


class _FakePage:
	def __init__(self, result=None, error=None):
		self.result = result
		self.error = error

	def fetch_and_classify(self):
		if self.error:
			raise self.error
		return self.result


class _FakeTelegram:
	def __init__(self, error=None):
		self.messages = []
		self.error = error

	def send(self, text):
		if self.error:
			raise self.error
		self.messages.append(text)


class CheckFlowTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_dir.cleanup)
		self.state_path = Path(self.temp_dir.name) / "status.json"
		self.telegram = _FakeTelegram()
		self.now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
		self.no_tours = PageResult(PageKind.NO_TOURS, "нет", "no")
		self.available = PageResult(
			PageKind.TOURS_AVAILABLE, "15 августа, 14:00", "available"
		)
		self.unexpected = PageResult(
			PageKind.UNEXPECTED_FORMAT, "Технические работы", "hash-1"
		)

	def test_first_available_tour_alerts_and_duplicate_is_suppressed(self):
		self.assertEqual(
			0,
			run_check(_FakePage(self.available), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(
			0,
			run_check(_FakePage(self.available), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(1, len(self.telegram.messages))
		self.assertIn("Появились экскурсии", self.telegram.messages[0])

	def test_changed_unexpected_content_alerts_again_but_identical_does_not(self):
		second = PageResult(
			PageKind.UNEXPECTED_FORMAT, "15 августа, новая форма", "hash-2"
		)

		self.assertEqual(
			2,
			run_check(_FakePage(self.unexpected), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(
			2,
			run_check(_FakePage(self.unexpected), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(
			2,
			run_check(_FakePage(second), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(2, len(self.telegram.messages))

	def test_available_tour_after_error_still_alerts(self):
		run_check(_FakePage(self.unexpected), self.telegram, self.state_path, self.now)

		code = run_check(
			_FakePage(self.available), self.telegram, self.state_path, self.now
		)

		self.assertEqual(0, code)
		self.assertIn("Появились экскурсии", self.telegram.messages[-1])

	def test_network_error_does_not_mutate_state(self):
		self.assertEqual(
			2,
			run_check(
				_FakePage(error=MonitorError("Museum page request failed")),
				self.telegram,
				self.state_path,
				self.now,
			),
		)
		self.assertFalse(self.state_path.exists())

	def test_page_client_classifies_http_response(self):
		def opener(request, timeout):
			self.assertEqual("https://mus-col.com/contacts/tours.php", request.full_url)
			return _FakeResponse(load_fixture("no_tours.html").encode("utf-8"))

		result = PageClient(opener=opener).fetch_and_classify()

		self.assertEqual(PageKind.NO_TOURS, result.kind)

	def test_test_notification_cli_sends_without_changing_state(self):
		requests = []

		def opener(request, timeout):
			requests.append(request)
			return _FakeResponse(b'{"ok": true, "result": {}}')

		code = main(
			["test-notification"],
			environ={
				"TELEGRAM_BOT_TOKEN": "secret-token",
				"TELEGRAM_CHAT_ID": "123",
			},
			opener=opener,
		)

		self.assertEqual(0, code)
		self.assertEqual(1, len(requests))
		self.assertIn("%E2%9C%85", requests[0].data.decode("ascii"))

	def test_cli_rejects_missing_secrets_without_printing_values(self):
		output = io.StringIO()
		with redirect_stdout(output):
			code = main(["test-notification"], environ={}, opener=None)
		self.assertEqual(3, code)
		self.assertIn("Missing required Telegram configuration", output.getvalue())

	def test_telegram_error_leaves_alert_unset_for_retry(self):
		failing = _FakeTelegram(MonitorError("Telegram delivery failed"))

		self.assertEqual(
			3,
			run_check(_FakePage(self.available), failing, self.state_path, self.now),
		)
		self.assertFalse(self.state_path.exists())
		self.assertEqual(
			0,
			run_check(_FakePage(self.available), self.telegram, self.state_path, self.now),
		)
		self.assertEqual(1, len(self.telegram.messages))

	def test_no_tours_does_not_send_or_write_state(self):
		self.assertEqual(
			0,
			run_check(_FakePage(self.no_tours), self.telegram, self.state_path, self.now),
		)
		self.assertEqual([], self.telegram.messages)
		self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
	unittest.main()
