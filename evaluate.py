"""Run prompt evaluations against a Copilot Studio agent from a CSV file.

CSV format:
    prompt,expected_response,match_method
    "What is your name?","Help Desk","contains"

Match methods:
    exact        - response must equal expected (case-insensitive)
    contains     - response must contain expected substring (case-insensitive)
    not_contains - response must NOT contain the expected substring
    regex        - expected is a regex pattern matched against the response
    fuzzy        - similarity ratio >= threshold (default 70%). Use "expected|80" to set custom threshold
    partial      - best partial substring match >= threshold (default 70%). Use "expected|80" for custom
"""

import asyncio
import csv
import difflib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from microsoft_agents.activity import ActivityTypes
from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

from config import AgentSettings
from chat import acquire_token


@dataclass
class EvalCase:
    prompt: str
    expected_response: str
    match_method: str  # exact, contains, regex, not_contains

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
            print(f"  Actual       : {r.actual_response[:200]}")
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
            ))
    return cases


async def collect_response(client: CopilotClient, question: str) -> str:
    """Send a question and collect the full text response."""
    parts: list[str] = []
    async for activity in client.ask_question(question):
        if activity.type == ActivityTypes.message and activity.text:
            parts.append(activity.text)
        elif activity.type == ActivityTypes.end_of_conversation:
            break
    return "\n".join(parts)


async def run_evaluation(csv_path: str, output_path: str | None = None) -> EvalReport:
    settings = AgentSettings.from_env()
    conn = ConnectionSettings(
        environment_id=settings.environment_id,
        agent_identifier=settings.schema_name,
    )
    token = acquire_token(settings)
    client = CopilotClient(conn, token)

    # Start conversation
    print("Starting conversation with agent...")
    async for activity in client.start_conversation(emit_start_conversation_event=True):
        if activity.type == ActivityTypes.message and activity.text:
            print(f"  Agent greeting: {activity.text[:100]}")

    cases = load_cases(csv_path)
    print(f"Loaded {len(cases)} evaluation cases from {csv_path}\n")

    report = EvalReport()
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] Sending: {case.prompt[:80]}...", flush=True)
        try:
            actual = await collect_response(client, case.prompt)
            passed = case.check(actual)
            report.results.append(EvalResult(case=case, actual_response=actual, passed=passed))
            status = "PASS" if passed else "FAIL"
            print(f"  -> [{status}]")
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
        print("\nCSV columns: prompt, expected_response, match_method")
        print("Match methods: exact, contains, not_contains, regex, fuzzy, partial")
        sys.exit(1)

    csv_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(run_evaluation(csv_path, output_path))


if __name__ == "__main__":
    main()
