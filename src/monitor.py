from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TARGET_HEADING = "Записаться на экскурсию"
TARGET_END_MARKER = "Контактная информация"
PAGE_URL = "https://mus-col.com/contacts/tours.php"


def normalize_text(value: str) -> str:
    return " ".join(unescape(value).split())


NO_TOURS_TEXT = normalize_text(
    """
    Записаться на экскурсию
    Запись на новые экскурсии открывается ежедневно по будням в 13:00.
    В настоящий момент доступных экскурсий нет
    """
)


class PageKind(Enum):
    NO_TOURS = "no_tours"
    TOURS_AVAILABLE = "tours_available"
    UNEXPECTED_FORMAT = "unexpected_format"


@dataclass(frozen=True)
class PageResult:
    kind: PageKind
    text: str
    fingerprint: str


class MonitorError(RuntimeError):
    pass


@dataclass
class DailyState:
    day: date
    alert_sent: bool = False
    daily_report_sent: bool = False
    unexpected_hashes: set[str] | None = None

    def __post_init__(self) -> None:
        if self.unexpected_hashes is None:
            self.unexpected_hashes = set()

    @classmethod
    def for_date(cls, path: Path, day: date) -> DailyState:
        if not path.exists():
            return cls(day=day)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored_day = date.fromisoformat(payload["date"])
            if stored_day != day:
                return cls(day=day)
            return cls(
                day=stored_day,
                alert_sent=bool(payload.get("alert_sent", False)),
                daily_report_sent=bool(payload.get("daily_report_sent", False)),
                unexpected_hashes=set(payload.get("unexpected_hashes", [])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise MonitorError("Monitor state is invalid") from error

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        payload = {
            "date": self.day.isoformat(),
            "alert_sent": self.alert_sent,
            "daily_report_sent": self.daily_report_sent,
            "unexpected_hashes": sorted(self.unexpected_hashes or set()),
        }
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        opener: Callable[..., object] = urlopen,
        timeout: float = 15.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._opener = opener
        self._timeout = timeout

    def send(self, text: str) -> None:
        data = urlencode({"chat_id": self._chat_id, "text": text}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=data,
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MonitorError("Telegram delivery failed") from error
        if payload.get("ok") is not True:
            raise MonitorError("Telegram rejected the message")


class PageClient:
    def __init__(self, opener: Callable[..., object] = urlopen, timeout: float = 15.0):
        self._opener = opener
        self._timeout = timeout

    def fetch_and_classify(self) -> PageResult:
        request = Request(
            PAGE_URL,
            headers={"User-Agent": "museum-tours-monitor/1.0 (+personal notifier)"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                source = response.read().decode("utf-8")
        except (HTTPError, URLError, OSError, UnicodeDecodeError) as error:
            raise MonitorError("Museum page request failed") from error
        return classify_tour_page(source)


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class RunSummary:
    successes: int
    errors: int
    terminal_runs: int
    observed_runs: int

    @property
    def error_ratio(self) -> float:
        return self.errors / 21


class GitHubClient:
    def __init__(
        self,
        token: str,
        repository: str,
        opener: Callable[..., object] = urlopen,
        timeout: float = 15.0,
    ) -> None:
        self._token = token
        self._repository = repository
        self._opener = opener
        self._timeout = timeout

    def list_monitor_runs(self, day: date, now_utc: datetime) -> list[WorkflowRun]:
        moscow = ZoneInfo("Europe/Moscow")
        start = datetime.combine(day, time(12, 40), moscow).astimezone(timezone.utc)
        end = now_utc.astimezone(timezone.utc) + timedelta(minutes=1)
        query = urlencode(
            {
                "event": "schedule",
                "created": f"{start.isoformat()}..{end.isoformat()}",
                "per_page": "100",
            }
        )
        request = Request(
            "https://api.github.com/repos/"
            f"{self._repository}/actions/workflows/museum-tours-monitor.yml/runs?{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "museum-tours-monitor/1.0",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_runs = payload["workflow_runs"]
            if not isinstance(raw_runs, list):
                raise TypeError("workflow_runs must be a list")
            unique: dict[int, WorkflowRun] = {}
            for item in raw_runs:
                if item.get("event") != "schedule":
                    continue
                run = WorkflowRun(
                    run_id=int(item["id"]),
                    status=str(item["status"]),
                    conclusion=item.get("conclusion"),
                )
                unique[run.run_id] = run
            return list(unique.values())
        except (
            HTTPError,
            URLError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise MonitorError("GitHub Actions history request failed") from error


def format_availability(result: PageResult) -> str:
    return (
        "🎟 Появились экскурсии в музее «Собрание»!\n\n"
        f"{result.text}\n\n"
        f"Записаться: {PAGE_URL}"
    )


def format_unexpected(result: PageResult) -> str:
    excerpt = result.text[:800]
    return (
        "⚠️ Не удалось распознать формат страницы экскурсий. "
        "Мониторинг продолжится по расписанию.\n\n"
        f"Фрагмент: {excerpt}\n\n"
        f"Проверить страницу: {PAGE_URL}"
    )


def summarize_runs(runs: Sequence[WorkflowRun], expected: int = 21) -> RunSummary:
    if expected != 21:
        raise MonitorError("The report contract requires exactly 21 checks")
    unique = {run.run_id: run for run in runs}
    if len(unique) > expected:
        raise MonitorError("More scheduled monitor runs found than expected")
    terminal = [
        run
        for run in unique.values()
        if run.status == "completed" and run.conclusion is not None
    ]
    successes = sum(run.conclusion == "success" for run in terminal)
    return RunSummary(
        successes=successes,
        errors=expected - successes,
        terminal_runs=len(terminal),
        observed_runs=len(unique),
    )


def format_daily_report(summary: RunSummary) -> str:
    if summary.errors == 0:
        return "ℹ️ Сегодня до 14:30 МСК экскурсии не появились."
    if summary.errors < 5:
        error_text = (
            "1 проверка завершилась ошибкой"
            if summary.errors == 1
            else f"{summary.errors} проверки завершились ошибкой"
        )
        return (
            "ℹ️ В успешных проверках экскурсии не обнаружены.\n"
            f"{summary.successes} успешных проверок; "
            f"{error_text}."
        )
    percentage = summary.error_ratio * 100
    return (
        "⚠️ Надёжно проверить наличие экскурсий не удалось.\n"
        f"Ошибок: {summary.errors} из 21 ({percentage:.1f}%). "
        f"Успешных проверок: {summary.successes}.\n"
        "Мониторинг продолжится по расписанию."
    )


def _moscow_day(now_utc: datetime) -> date:
    if now_utc.tzinfo is None:
        raise MonitorError("Current time must include a time zone")
    return now_utc.astimezone(ZoneInfo("Europe/Moscow")).date()


def run_check(
    page_client: object,
    telegram: object,
    state_path: Path,
    now_utc: datetime,
) -> int:
    try:
        result = page_client.fetch_and_classify()
    except MonitorError:
        return 2

    state = DailyState.for_date(state_path, _moscow_day(now_utc))
    if result.kind is PageKind.NO_TOURS:
        return 0

    if result.kind is PageKind.TOURS_AVAILABLE:
        if state.alert_sent:
            return 0
        try:
            telegram.send(format_availability(result))
        except MonitorError:
            return 3
        state.alert_sent = True
        state.save(state_path)
        return 0

    if result.fingerprint not in (state.unexpected_hashes or set()):
        try:
            telegram.send(format_unexpected(result))
        except MonitorError:
            return 3
        state.unexpected_hashes.add(result.fingerprint)
        state.save(state_path)
    return 2


def run_report(
    github: GitHubClient,
    telegram: TelegramClient,
    state_path: Path,
    now_utc: datetime,
    final_attempt: bool,
) -> int:
    day = _moscow_day(now_utc)
    state = DailyState.for_date(state_path, day)
    if state.daily_report_sent:
        return 0
    try:
        runs = github.list_monitor_runs(day, now_utc)
        summary = summarize_runs(runs)
    except MonitorError:
        return 3

    if not final_attempt and (
        summary.observed_runs < 21 or summary.terminal_runs < 21
    ):
        return 4

    if state.alert_sent and summary.errors < 5:
        state.daily_report_sent = True
        state.save(state_path)
        return 0

    try:
        telegram.send(format_daily_report(summary))
    except MonitorError:
        return 3
    state.daily_report_sent = True
    state.save(state_path)
    return 0


def main(
    argv: list[str] | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
    opener: Callable[..., object] | None = urlopen,
) -> int:
    parser = argparse.ArgumentParser(description="Monitor museum tour availability")
    parser.add_argument("command", choices=("check", "report", "test-notification"))
    parser.add_argument("--final-attempt", choices=("true", "false"), default="false")
    arguments = parser.parse_args(argv)

    token = environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or opener is None:
        print("Missing required Telegram configuration")
        return 3

    telegram = TelegramClient(token, chat_id, opener=opener)
    if arguments.command == "test-notification":
        try:
            telegram.send("✅ Тест: монитор экскурсий может отправлять сообщения.")
        except MonitorError as error:
            print(error)
            return 3
        return 0

    state_path = Path(environ.get("MONITOR_STATE_PATH", "state/status.json"))
    if arguments.command == "report":
        github_token = environ.get("GITHUB_TOKEN")
        repository = environ.get("GITHUB_REPOSITORY")
        if not github_token or not repository:
            print("Missing required GitHub configuration")
            return 3
        return run_report(
            GitHubClient(github_token, repository, opener=opener),
            telegram,
            state_path,
            datetime.now(timezone.utc),
            final_attempt=arguments.final_attempt == "true",
        )

    return run_check(
        PageClient(opener=opener),
        telegram,
        state_path,
        datetime.now(timezone.utc),
    )


@dataclass(frozen=True)
class _TextToken:
    text: str
    actionable: bool


class _VisibleTextParser(HTMLParser):
    _IGNORED_TAGS = {"script", "style", "template"}
    _ACTIONABLE_TAGS = {"a", "button", "form", "input", "select"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_TextToken] = []
        self._ignored_depth = 0
        self._actionable_depth = 0
        self._body_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "body":
            self._body_depth += 1
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        if tag in self._ACTIONABLE_TAGS:
            self._actionable_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ACTIONABLE_TAGS and self._actionable_depth:
            self._actionable_depth -= 1
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "body" and self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._body_depth:
            return
        text = normalize_text(data)
        if text:
            self.tokens.append(_TextToken(text, self._actionable_depth > 0))


def _fingerprint(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def classify_tour_page(source: str) -> PageResult:
    parser = _VisibleTextParser()
    parser.feed(source)
    parser.close()

    start = next(
        (index for index, token in enumerate(parser.tokens) if token.text == TARGET_HEADING),
        None,
    )
    end = None
    if start is not None:
        end = next(
            (
                index
                for index, token in enumerate(parser.tokens[start + 1 :], start + 1)
                if token.text == TARGET_END_MARKER
            ),
            None,
        )

    if start is None or end is None:
        page_text = normalize_text(" ".join(token.text for token in parser.tokens))
        return PageResult(
            PageKind.UNEXPECTED_FORMAT,
            page_text[:800],
            _fingerprint(page_text),
        )

    block = parser.tokens[start:end]
    block_text = normalize_text(" ".join(token.text for token in block))
    if block_text == NO_TOURS_TEXT:
        kind = PageKind.NO_TOURS
    elif any(token.actionable for token in block) and block_text != TARGET_HEADING:
        kind = PageKind.TOURS_AVAILABLE
    else:
        kind = PageKind.UNEXPECTED_FORMAT

    return PageResult(kind, block_text[:800], _fingerprint(block_text))


if __name__ == "__main__":
    raise SystemExit(main())
