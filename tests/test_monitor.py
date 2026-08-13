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
	GitHubClient,
	PageClient,
	MonitorError,
	PageKind,
	PageResult,
	TelegramClient,
	WorkflowRun,
	classify_tour_page,
	format_daily_report,
	format_availability,
	format_unexpected,
	main,
	run_check,
	run_report,
	summarize_runs,
)


FIXTURES = Path(__file__).parent / "fixtures"
REPOSITORY_ROOT = Path(__file__).parents[1]
WORKFLOW_MONITOR = REPOSITORY_ROOT / ".github/workflows/museum-tours-monitor.yml"
WORKFLOW_REPORT = REPOSITORY_ROOT / ".github/workflows/museum-tours-report.yml"
README = REPOSITORY_ROOT / "README.md"


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

	def test_generic_link_inside_target_block_is_not_tour_availability(self):
		source = """
		<html><body>
		<h1>Записаться на экскурсию</h1>
		<p>Технические работы</p><a href="/news">Подробнее</a>
		<a href="/contacts/">Контактная информация</a>
		</body></html>
		"""

		self.assertEqual(PageKind.UNEXPECTED_FORMAT, classify_tour_page(source).kind)


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

	def test_empty_initial_date_starts_fresh_state(self):
		self.state_path.write_text(
			json.dumps(
				{
					"date": "",
					"alert_sent": False,
					"daily_report_sent": False,
					"unexpected_hashes": [],
				}
			),
			encoding="utf-8",
		)

		state = DailyState.for_date(self.state_path, date(2026, 8, 14))

		self.assertEqual(date(2026, 8, 14), state.day)
		self.assertFalse(state.alert_sent)

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


def _workflow_runs(successes=0, failures=0, in_progress=0):
	runs = [
		WorkflowRun(index, "completed", "success") for index in range(successes)
	]
	start = len(runs)
	runs.extend(
		WorkflowRun(start + index, "completed", "failure")
		for index in range(failures)
	)
	start = len(runs)
	runs.extend(
		WorkflowRun(start + index, "in_progress", None)
		for index in range(in_progress)
	)
	return runs


class _FakeGitHub:
	def __init__(self, runs=None, error=None):
		self.runs = runs or []
		self.error = error

	def list_monitor_runs(self, day, now_utc):
		if self.error:
			raise self.error
		return self.runs


class ReportTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_dir.cleanup)
		self.state_path = Path(self.temp_dir.name) / "status.json"
		self.telegram = _FakeTelegram()
		self.now = datetime(2026, 8, 14, 11, 40, tzinfo=timezone.utc)

	def test_twenty_one_successes_report_no_tours(self):
		summary = summarize_runs(_workflow_runs(successes=21))
		self.assertEqual((21, 0), (summary.successes, summary.errors))
		self.assertIn("экскурсии не появились", format_daily_report(summary))

	def test_three_errors_report_partial_observation(self):
		summary = summarize_runs(_workflow_runs(successes=18, failures=3))
		message = format_daily_report(summary)
		self.assertIn("18 успешных", message)
		self.assertIn("3 проверки завершились ошибкой", message)

	def test_one_error_uses_singular_wording(self):
		message = format_daily_report(
			summarize_runs(_workflow_runs(successes=20, failures=1))
		)
		self.assertIn("1 проверка завершилась ошибкой", message)

	def test_five_errors_report_unreliable_result(self):
		summary = summarize_runs(_workflow_runs(successes=16, failures=5))
		self.assertGreaterEqual(summary.error_ratio, 0.20)
		self.assertIn("Надёжно проверить", format_daily_report(summary))

	def test_missing_and_incomplete_runs_are_errors(self):
		summary = summarize_runs(_workflow_runs(successes=17, in_progress=1))
		self.assertEqual(4, summary.errors)

	def test_non_final_attempt_defers_until_all_runs_are_terminal(self):
		code = run_report(
			_FakeGitHub(_workflow_runs(successes=20, in_progress=1)),
			self.telegram,
			self.state_path,
			self.now,
			final_attempt=False,
		)
		self.assertEqual(4, code)
		self.assertEqual([], self.telegram.messages)
		self.assertFalse(self.state_path.exists())

	def test_final_attempt_counts_missing_runs_and_sends_partial_report(self):
		code = run_report(
			_FakeGitHub(_workflow_runs(successes=18)),
			self.telegram,
			self.state_path,
			self.now,
			final_attempt=True,
		)
		self.assertEqual(0, code)
		self.assertIn("18 успешных", self.telegram.messages[0])
		self.assertTrue(
			DailyState.for_date(self.state_path, date(2026, 8, 14)).daily_report_sent
		)

	def test_availability_suppresses_negative_but_not_high_error_warning(self):
		state = DailyState(day=date(2026, 8, 14), alert_sent=True)
		state.save(self.state_path)
		self.assertEqual(
			0,
			run_report(
				_FakeGitHub(_workflow_runs(successes=18, failures=3)),
				self.telegram,
				self.state_path,
				self.now,
				final_attempt=True,
			),
		)
		self.assertEqual([], self.telegram.messages)

		state = DailyState.for_date(self.state_path, date(2026, 8, 14))
		state.daily_report_sent = False
		state.save(self.state_path)
		self.assertEqual(
			0,
			run_report(
				_FakeGitHub(_workflow_runs(successes=16, failures=5)),
				self.telegram,
				self.state_path,
				self.now,
				final_attempt=True,
			),
		)
		self.assertIn("Надёжно проверить", self.telegram.messages[0])

	def test_sent_report_suppresses_retry(self):
		DailyState(
			day=date(2026, 8, 14), daily_report_sent=True
		).save(self.state_path)
		code = run_report(
			_FakeGitHub(error=AssertionError("must not query")),
			self.telegram,
			self.state_path,
			self.now,
			final_attempt=True,
		)
		self.assertEqual(0, code)
		self.assertEqual([], self.telegram.messages)

	def test_telegram_failure_leaves_report_unsent(self):
		code = run_report(
			_FakeGitHub(_workflow_runs(successes=21)),
			_FakeTelegram(MonitorError("Telegram delivery failed")),
			self.state_path,
			self.now,
			final_attempt=True,
		)
		self.assertEqual(3, code)
		self.assertFalse(self.state_path.exists())

	def test_github_client_filters_manual_runs_and_deduplicates_ids(self):
		requests = []
		payload = {
			"workflow_runs": [
				{
					"id": 10,
					"event": "schedule",
					"status": "completed",
					"conclusion": "success",
				},
				{
					"id": 10,
					"event": "schedule",
					"status": "completed",
					"conclusion": "success",
				},
				{
					"id": 11,
					"event": "workflow_dispatch",
					"status": "completed",
					"conclusion": "success",
				},
			]
		}

		def opener(request, timeout):
			requests.append(request)
			return _FakeResponse(json.dumps(payload).encode("utf-8"))

		runs = GitHubClient("github-token", "owner/repo", opener=opener).list_monitor_runs(
			date(2026, 8, 14), self.now
		)

		self.assertEqual([WorkflowRun(10, "completed", "success")], runs)
		self.assertIn("museum-tours-monitor.yml/runs", requests[0].full_url)
		self.assertIn("event=schedule", requests[0].full_url)
		self.assertEqual("Bearer github-token", requests[0].get_header("Authorization"))


class WorkflowContractTests(unittest.TestCase):
	def test_monitor_workflow_has_exact_daily_schedule_and_shared_lock(self):
		monitor = WORKFLOW_MONITOR.read_text(encoding="utf-8")
		self.assertIn("'50,55 9 * * *'", monitor)
		self.assertIn("'*/5 10 * * *'", monitor)
		self.assertIn("'0-30/5 11 * * *'", monitor)
		self.assertIn("group: museum-tours-state", monitor)
		self.assertIn("cancel-in-progress: false", monitor)
		self.assertIn("contents: write", monitor)
		self.assertIn("TELEGRAM_BOT_TOKEN", monitor)
		self.assertIn("TELEGRAM_CHAT_ID", monitor)
		self.assertIn("python3 src/monitor.py", monitor)
		minutes = [50, 55] + list(range(0, 60, 5)) + list(range(0, 31, 5))
		self.assertEqual(21, len(minutes))

	def test_report_workflow_has_retries_final_attempt_and_actions_read_access(self):
		report = WORKFLOW_REPORT.read_text(encoding="utf-8")
		self.assertIn("'40,50 11 * * *'", report)
		self.assertIn("'5 12 * * *'", report)
		self.assertIn("group: museum-tours-state", report)
		self.assertIn("cancel-in-progress: false", report)
		self.assertIn("actions: read", report)
		self.assertIn("contents: write", report)
		self.assertIn("--final-attempt", report)
		self.assertIn("GITHUB_TOKEN", report)
		self.assertIn("TELEGRAM_BOT_TOKEN", report)

	def test_both_workflows_commit_only_the_state_file(self):
		for path in (WORKFLOW_MONITOR, WORKFLOW_REPORT):
			workflow = path.read_text(encoding="utf-8")
			self.assertIn("git add state/status.json", workflow)
			self.assertIn("git pull --rebase origin main", workflow)
			self.assertIn("git push origin HEAD:main", workflow)


class DocumentationTests(unittest.TestCase):
	def test_readme_documents_setup_schedule_alerts_and_shutdown(self):
		readme = README.read_text(encoding="utf-8")
		for required in (
			"TELEGRAM_BOT_TOKEN",
			"TELEGRAM_CHAT_ID",
			"Europe/Moscow",
			"20%",
			"Museum Tours Monitor",
			"Museum Tours Daily Report",
			"Disable workflow",
			"https://mus-col.com/contacts/tours.php",
			"UNEXPECTED_FORMAT",
		):
			with self.subTest(required=required):
				self.assertIn(required, readme)


class SourceLayoutTests(unittest.TestCase):
	def test_script_entrypoint_is_after_all_runtime_definitions(self):
		source = (REPOSITORY_ROOT / "src/monitor.py").read_text(encoding="utf-8")
		self.assertLess(
			source.index("def classify_tour_page"),
			source.index('if __name__ == "__main__"'),
		)

if __name__ == "__main__":
	unittest.main()
