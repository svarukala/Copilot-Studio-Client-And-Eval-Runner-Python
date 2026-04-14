"""Run prompt evaluations against a Copilot Studio agent from a CSV file.

CSV format:
    prompt,expected_response,match_method[,conversation_id][,attachment]
    "What is your name?","Help Desk","contains"

Each row gets a fresh conversation by default. To run multiple prompts in the
same conversation (multi-turn), give them the same ``conversation_id``:

    prompt,expected_response,match_method,conversation_id,attachment
    "Hi","hello",contains,greeting_flow,
    "What are your hours?","9am",contains,greeting_flow,
    "Reset my password","done",contains,,
    "Analyze this","summary",contains,,report.pdf
    "Describe image","cat",contains,,https://example.com/photo.png

Rows without a conversation_id (or with an empty value) each start their own
conversation. Rows sharing the same conversation_id are sent sequentially
within one conversation, in CSV order.

The optional ``attachment`` column accepts:
  - A URL (http/https) — sent as-is via ``content_url``
  - A local file path — base64-encoded into a data URI

Match methods:
    exact        - response must equal expected (case-insensitive)
    contains     - response must contain expected substring (case-insensitive)
    not_contains - response must NOT contain the expected substring
    regex        - expected is a regex pattern matched against the response
    fuzzy        - similarity ratio >= threshold (default 70%). Use "expected|80" to set custom threshold
    partial      - best partial substring match >= threshold (default 70%). Use "expected|80" for custom
"""

import asyncio
import base64
import csv
import difflib
import mimetypes
import re
import sys
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from microsoft_agents.activity import Activity, Attachment, ActivityTypes, ConversationAccount
from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

from config import AgentSettings
from chat import acquire_token


@dataclass
class EvalCase:
    prompt: str
    expected_response: str
    match_method: str  # exact, contains, regex, not_contains
    conversation_id: str = ""  # rows with same id share a conversation
    attachment: str = ""  # URL or local file path

    def _parse_threshold(self) -> tuple[str, float]:
        """Parse 'expected|threshold' format. Returns (expected_text, threshold_pct)."""
        if "|" in self.expected_response:
            parts = self.expected_response.rsplit("|", 1)
            try:
                return parts[0], float(parts[1]) / 100.0
            except ValueError:
                pass
        return self.expected_response, 0.70

    def check(self, actual: str) -> bool:
        method = self.match_method.lower().strip()
        if method == "exact":
            return actual.strip().lower() == self.expected_response.strip().lower()
        elif method == "contains":
            return self.expected_response.lower() in actual.lower()
        elif method == "not_contains":
            return self.expected_response.lower() not in actual.lower()
        elif method == "regex":
            return bool(re.search(self.expected_response, actual, re.IGNORECASE))
        elif method == "fuzzy":
            expected, threshold = self._parse_threshold()
            ratio = difflib.SequenceMatcher(None, expected.lower(), actual.lower()).ratio()
            print(f"  Fuzzy score: {ratio:.1%} (threshold: {threshold:.0%})")
            return ratio >= threshold
        elif method == "partial":
            expected, threshold = self._parse_threshold()
            words = expected.lower().split()
            actual_lower = actual.lower()
            # Find best matching substring of similar length to expected
            best = 0.0
            for i in range(len(actual_lower)):
                chunk = actual_lower[i:i + len(expected.lower()) + 20]
                score = difflib.SequenceMatcher(None, expected.lower(), chunk).ratio()
                best = max(best, score)
            # Also check if most expected words appear in the response
            word_hits = sum(1 for w in words if w in actual_lower) / max(len(words), 1)
            score = max(best, word_hits)
            print(f"  Partial score: {score:.1%} (threshold: {threshold:.0%})")
            return score >= threshold
        else:
            print(f"  [WARNING] Unknown match method '{method}', defaulting to 'contains'")
            return self.expected_response.lower() in actual.lower()


@dataclass
class EvalResult:
    case: EvalCase
    actual_response: str
    passed: bool
    error: str = ""


@dataclass
class EvalReport:
    results: list[EvalResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("EVALUATION REPORT")
        print("=" * 60)
        for i, r in enumerate(self.results, 1):
            status = "PASS" if r.passed else "FAIL"
            print(f"\n[{status}] #{i}: {r.case.prompt}")
            print(f"  Match method : {r.case.match_method}")
            print(f"  Expected     : {r.case.expected_response}")
            print(f"  Actual       : {r.actual_response[:500]}")
            if r.error:
                print(f"  Error        : {r.error}")
        print("\n" + "-" * 60)
        print(f"Total: {self.total}  |  Passed: {self.passed}  |  Failed: {self.failed}")
        if self.total > 0:
            print(f"Pass rate: {self.passed / self.total * 100:.1f}%")
        print("=" * 60)

    def save_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["prompt", "expected_response", "match_method", "actual_response", "passed", "error"])
            for r in self.results:
                writer.writerow([
                    r.case.prompt,
                    r.case.expected_response,
                    r.case.match_method,
                    r.actual_response,
                    r.passed,
                    r.error,
                ])
        print(f"\nResults saved to {path}")


def load_cases(csv_path: str) -> list[EvalCase]:
    cases = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cases.append(EvalCase(
                prompt=row["prompt"],
                expected_response=row["expected_response"],
                match_method=row.get("match_method", "contains"),
                conversation_id=row.get("conversation_id", "").strip(),
                attachment=row.get("attachment", "").strip(),
            ))
    return cases


def group_cases_by_conversation(cases: list[EvalCase]) -> OrderedDict[str, list[EvalCase]]:
    """Group cases by conversation_id, preserving CSV order.

    Cases without a conversation_id each get a unique key so they run in
    their own fresh conversation.
    """
    groups: OrderedDict[str, list[EvalCase]] = OrderedDict()
    for case in cases:
        key = case.conversation_id if case.conversation_id else f"_solo_{uuid.uuid4().hex}"
        groups.setdefault(key, []).append(case)
    return groups


def _to_data_uri(content_type: str, data: bytes) -> str:
    """Encode raw bytes as a base64 data URI."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{b64}"


def build_attachment(raw: str) -> Attachment:
    """Build an Attachment from a URL or local file path.

    Both URLs and local files are base64-encoded into a ``data:`` URI so the
    file content travels inline in the JSON payload.  The Direct-to-Engine API
    does not fetch external URLs on behalf of the agent.
    """
    if raw.startswith(("http://", "https://")):
        import urllib.request
        name = raw.rsplit("/", 1)[-1].split("?")[0] or "attachment"
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        print(f"  Downloading {raw} ...")
        with urllib.request.urlopen(raw) as resp:
            data = resp.read()
            # Use content-type from server if available
            server_ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if server_ct:
                content_type = server_ct
        data_uri = _to_data_uri(content_type, data)
        return Attachment(content_type=content_type, content_url=data_uri, name=name)

    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"Attachment file not found: {raw}")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data_uri = _to_data_uri(content_type, path.read_bytes())
    return Attachment(content_type=content_type, content_url=data_uri, name=path.name)


async def _collect_activities(response_gen) -> str:
    """Iterate an async activity generator and join message texts."""
    parts: list[str] = []
    async for activity in response_gen:
        if activity.type == ActivityTypes.message and activity.text:
            parts.append(activity.text)
        elif activity.type == ActivityTypes.end_of_conversation:
            break
    return "\n".join(parts)


async def collect_response(client: CopilotClient, case: EvalCase, timeout: int) -> str:
    """Send a prompt (with optional attachment) and collect the full text response."""
    if case.attachment:
        attachment = build_attachment(case.attachment)
        activity = Activity(
            type="message",
            text=case.prompt,
            attachments=[attachment],
            conversation=ConversationAccount(id=client._current_conversation_id),
        )
        print(f"  [attachment: {attachment.name}]")
        response_gen = client.ask_question_with_activity(activity)
    else:
        response_gen = client.ask_question(case.prompt)

    return await asyncio.wait_for(_collect_activities(response_gen), timeout=timeout)


async def start_new_conversation(conn: ConnectionSettings, token: str, conv_label: str, timeout: int) -> CopilotClient:
    """Create a new CopilotClient and consume the greeting."""
    client = CopilotClient(conn, token)
    print(f"\n--- Starting conversation{f' [{conv_label}]' if conv_label else ''} ---")

    async def _consume_greeting():
        async for activity in client.start_conversation(emit_start_conversation_event=True):
            if activity.type == ActivityTypes.message and activity.text:
                print(f"  Agent greeting: {activity.text[:100]}")

    await asyncio.wait_for(_consume_greeting(), timeout=timeout)
    return client


async def run_evaluation(csv_path: str, output_path: str | None = None) -> EvalReport:
    settings = AgentSettings.from_env()
    conn = ConnectionSettings(
        environment_id=settings.environment_id,
        agent_identifier=settings.schema_name,
    )
    token = acquire_token(settings)
    timeout = settings.timeout

    cases = load_cases(csv_path)
    groups = group_cases_by_conversation(cases)
    total = len(cases)
    multi_turn_groups = sum(1 for g in groups.values() if len(g) > 1)
    solo_count = sum(1 for g in groups.values() if len(g) == 1)
    print(f"Loaded {total} evaluation cases from {csv_path}")
    print(f"  {solo_count} independent prompt(s), {multi_turn_groups} multi-turn conversation(s)")
    print(f"  Timeout: {timeout}s per call\n")

    report = EvalReport()
    case_num = 0
    for conv_id, group in groups.items():
        label = conv_id if not conv_id.startswith("_solo_") else ""
        client = await start_new_conversation(conn, token, label, timeout)

        for case in group:
            case_num += 1
            print(f"[{case_num}/{total}] Sending: {case.prompt[:80]}...", flush=True)
            try:
                actual = await collect_response(client, case, timeout)
                passed = case.check(actual)
                report.results.append(EvalResult(case=case, actual_response=actual, passed=passed))
                status = "PASS" if passed else "FAIL"
                print(f"  -> [{status}]")
            except TimeoutError:
                report.results.append(EvalResult(
                    case=case, actual_response="", passed=False,
                    error=f"Timed out after {timeout}s",
                ))
                print(f"  -> [TIMEOUT] No response within {timeout}s")
            except Exception as e:
                report.results.append(EvalResult(
                    case=case, actual_response="", passed=False, error=str(e)
                ))
                print(f"  -> [ERROR] {e}")

    report.print_summary()

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"results/eval_{ts}.csv"
    report.save_csv(Path(output_path))

    return report


def main():
    if len(sys.argv) < 2:
        print("Usage: python evaluate.py <input.csv> [output.csv]")
        print("\nCSV columns: prompt, expected_response, match_method[, conversation_id][, attachment]")
        print("Match methods: exact, contains, not_contains, regex, fuzzy, partial")
        print("\nRows with the same conversation_id share one conversation (multi-turn).")
        print("Rows without a conversation_id each get a fresh conversation.")
        print("Attachment: URL or local file path (optional). Local files are base64-encoded.")
        sys.exit(1)

    csv_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(run_evaluation(csv_path, output_path))


if __name__ == "__main__":
    main()
