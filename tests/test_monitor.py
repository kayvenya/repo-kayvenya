from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError

from src.monitor import (
	DailyState,
	MonitorError,
	PageKind,
	PageResult,
	TelegramClient,
	classify_tour_page,
	format_availability,
	format_unexpected,
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


if __name__ == "__main__":
	unittest.main()
