"""Run prompt evaluations against a Copilot Studio agent from a CSV file.

CSV format:
    prompt,expected_response,match_method[,conversation_id][,attachment][,skip]
    "What is your name?","Help Desk","contains"

Each row gets a fresh conversation by default. To run multiple prompts in the
same conversation (multi-turn), give them the same ``conversation_id``.

Match methods:
    Deterministic (no external service):
        exact            - response must equal expected (case-insensitive)
        contains         - response must contain expected substring (case-insensitive)
        not_contains     - response must NOT contain the expected substring
        regex            - expected is a regex pattern matched against the response
        fuzzy            - similarity ratio >= threshold (default 70%). Use "expected|80"
        partial          - best partial substring match >= threshold (default 70%)

    LLM-as-a-Judge (requires JUDGE_* env vars — Azure OpenAI, OpenAI, Ollama, etc.):
        general_quality  - score response quality against criteria (default 70)
        text_similarity  - semantic similarity score (default 70)
        compare_meaning  - whether texts convey the same meaning (default 70)

Threshold syntax: append "|N" where N is 0-100 (e.g., "helpful answer|80").
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
from chat import acquire_token, is_consent_card, handle_consent_card


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

    def check(self, actual: str, settings: "AgentSettings | None" = None) -> bool:
        method = self.match_method.lower().strip().split("|", 1)[0]
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
        elif method in ("general_quality", "text_similarity", "compare_meaning"):
            if settings is None or not settings.has_judge_config:
                print(f"  [ERROR] Match method '{method}' requires JUDGE_* env vars")
                return False
            return self._llm_judge(method, actual, settings)
        else:
            print(f"  [WARNING] Unknown match method '{method}', defaulting to 'contains'")
            return self.expected_response.lower() in actual.lower()

    def _llm_judge(self, method: str, actual: str, settings: "AgentSettings") -> bool:
        """Delegate to the LLM judge module for general_quality / text_similarity / compare_meaning."""
        from judge import (
            judge_general_quality,
            judge_text_similarity,
            judge_compare_meaning,
        )

        expected, threshold = self._parse_threshold()
        threshold_pct = threshold * 100  # judge returns 0-100, threshold is 0-1

        try:
            if method == "general_quality":
                result = judge_general_quality(settings, self.prompt, expected, actual)
            elif method == "text_similarity":
                result = judge_text_similarity(settings, expected, actual)
            else:  # compare_meaning
                result = judge_compare_meaning(settings, expected, actual)
        except Exception as e:
            print(f"  [ERROR] LLM judge call failed: {e}")
            return False

        passed = result.score >= threshold_pct
        status = "PASS" if passed else "FAIL"
        print(f"  Judge ({method}): {result.score:.0f}/100 [{status}] threshold={threshold_pct:.0f}")
        if result.reasoning:
            print(f"  Reasoning: {result.reasoning[:200]}")
        return passed


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

    def save_html(self, path: Path) -> None:
        """Write a self-contained HTML report."""
        import html as _html
        path.parent.mkdir(parents=True, exist_ok=True)

        rows_html = []
        for i, r in enumerate(self.results, 1):
            status = "PASS" if r.passed else "FAIL"
            row_class = "pass" if r.passed else "fail"
            error_html = ""
            if r.error:
                error_html = f'<div class="error">⚠ {_html.escape(r.error)}</div>'
            rows_html.append(f"""
            <tr class="row {row_class}">
                <td class="num">{i}</td>
                <td class="status"><span class="badge {row_class}">{status}</span></td>
                <td class="method">{_html.escape(r.case.match_method)}</td>
                <td class="prompt"><div class="cell-content">{_html.escape(r.case.prompt)}</div></td>
                <td class="expected"><div class="cell-content">{_html.escape(r.case.expected_response)}</div></td>
                <td class="actual"><div class="cell-content">{_html.escape(r.actual_response)}</div>{error_html}</td>
                <td class="conv">{_html.escape(r.case.conversation_id or "—")}</td>
            </tr>""")

        pass_pct = (self.passed / self.total * 100) if self.total else 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Eval Report — {ts}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 24px; background: #f6f8fa; color: #24292f; }}
h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
.meta {{ color: #57606a; font-size: 13px; margin-bottom: 16px; }}
.summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
.card {{ background: white; border: 1px solid #d0d7de; border-radius: 6px;
         padding: 12px 16px; min-width: 100px; }}
.card .label {{ font-size: 11px; text-transform: uppercase; color: #57606a; letter-spacing: 0.5px; }}
.card .value {{ font-size: 24px; font-weight: 600; margin-top: 2px; }}
.card.pass .value {{ color: #1a7f37; }}
.card.fail .value {{ color: #cf222e; }}
.card.rate .value {{ color: #0969da; }}
table {{ width: 100%; border-collapse: collapse; background: white;
         border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden;
         font-size: 13px; }}
thead {{ background: #f6f8fa; }}
th {{ text-align: left; padding: 10px 12px; font-weight: 600;
      border-bottom: 1px solid #d0d7de; cursor: pointer; user-select: none; }}
th:hover {{ background: #eaeef2; }}
th.sorted-asc::after {{ content: " ▲"; color: #57606a; }}
th.sorted-desc::after {{ content: " ▼"; color: #57606a; }}
td {{ padding: 10px 12px; vertical-align: top; border-bottom: 1px solid #eaeef2; }}
tr:last-child td {{ border-bottom: none; }}
tr.pass {{ background: #f6fff8; }}
tr.fail {{ background: #fff8f8; }}
.cell-content {{ max-width: 360px; max-height: 120px; overflow: auto; white-space: pre-wrap;
                 word-wrap: break-word; font-family: ui-monospace, "Cascadia Code", monospace;
                 font-size: 12px; line-height: 1.45; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
          font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }}
.badge.pass {{ background: #dafbe1; color: #1a7f37; }}
.badge.fail {{ background: #ffebe9; color: #cf222e; }}
.num {{ color: #57606a; width: 40px; }}
.status {{ width: 70px; }}
.method {{ width: 130px; font-family: ui-monospace, monospace; font-size: 12px; color: #6639ba; }}
.conv {{ font-family: ui-monospace, monospace; font-size: 12px; color: #57606a; }}
.error {{ margin-top: 6px; padding: 6px 8px; background: #ffebe9; color: #cf222e;
          border-radius: 4px; font-size: 12px; }}
.filter {{ margin-bottom: 12px; }}
.filter input {{ padding: 6px 10px; border: 1px solid #d0d7de; border-radius: 6px;
                 font-size: 13px; min-width: 240px; }}
.filter button {{ margin-left: 8px; padding: 6px 12px; border: 1px solid #d0d7de;
                  border-radius: 6px; background: white; cursor: pointer; font-size: 13px; }}
.filter button.active {{ background: #0969da; color: white; border-color: #0969da; }}
</style>
</head>
<body>
<h1>Evaluation Report</h1>
<div class="meta">{ts}</div>

<div class="summary">
    <div class="card"><div class="label">Total</div><div class="value">{self.total}</div></div>
    <div class="card pass"><div class="label">Passed</div><div class="value">{self.passed}</div></div>
    <div class="card fail"><div class="label">Failed</div><div class="value">{self.failed}</div></div>
    <div class="card rate"><div class="label">Pass rate</div><div class="value">{pass_pct:.0f}%</div></div>
</div>

<div class="filter">
    <input type="text" id="search" placeholder="Filter prompts/responses…">
    <button id="all" class="active">All</button>
    <button id="pass-only">Pass</button>
    <button id="fail-only">Fail</button>
</div>

<table id="results">
    <thead>
        <tr>
            <th>#</th>
            <th>Status</th>
            <th>Method</th>
            <th>Prompt</th>
            <th>Expected</th>
            <th>Actual</th>
            <th>Conv ID</th>
        </tr>
    </thead>
    <tbody>{''.join(rows_html)}</tbody>
</table>

<script>
const rows = Array.from(document.querySelectorAll('#results tbody tr'));
const search = document.getElementById('search');
const btnAll = document.getElementById('all');
const btnPass = document.getElementById('pass-only');
const btnFail = document.getElementById('fail-only');
let mode = 'all';

function applyFilter() {{
    const q = search.value.toLowerCase();
    rows.forEach(r => {{
        const text = r.textContent.toLowerCase();
        const matchText = !q || text.includes(q);
        const matchMode = mode === 'all'
            || (mode === 'pass' && r.classList.contains('pass'))
            || (mode === 'fail' && r.classList.contains('fail'));
        r.style.display = (matchText && matchMode) ? '' : 'none';
    }});
}}
search.addEventListener('input', applyFilter);
[btnAll, btnPass, btnFail].forEach(b => b.addEventListener('click', () => {{
    [btnAll, btnPass, btnFail].forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    mode = b.id === 'pass-only' ? 'pass' : b.id === 'fail-only' ? 'fail' : 'all';
    applyFilter();
}}));

// Click-to-sort columns
document.querySelectorAll('#results th').forEach((th, idx) => {{
    th.addEventListener('click', () => {{
        const tbody = document.querySelector('#results tbody');
        const allRows = Array.from(tbody.querySelectorAll('tr'));
        const asc = !th.classList.contains('sorted-asc');
        document.querySelectorAll('#results th').forEach(x => x.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
        allRows.sort((a, b) => {{
            const av = a.children[idx].textContent.trim();
            const bv = b.children[idx].textContent.trim();
            const an = parseFloat(av), bn = parseFloat(bv);
            if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
            return asc ? av.localeCompare(bv) : bv.localeCompare(av);
        }});
        allRows.forEach(r => tbody.appendChild(r));
    }});
}});
</script>
</body>
</html>"""
        path.write_text(html_doc, encoding="utf-8")
        print(f"HTML report saved to {path}")


def load_cases(csv_path: str) -> tuple[list[EvalCase], int]:
    """Load eval cases from CSV, returning (cases, skipped_count)."""
    cases = []
    skipped = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skip = (row.get("skip") or "").strip().lower()
            if skip in ("true", "yes", "1"):
                skipped += 1
                continue
            cases.append(EvalCase(
                prompt=row["prompt"],
                expected_response=row["expected_response"],
                match_method=row.get("match_method", "contains"),
                conversation_id=row.get("conversation_id", "").strip(),
                attachment=row.get("attachment", "").strip(),
            ))
    return cases, skipped


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


def _extract_card_text(content_type: str, content) -> str | None:
    """Extract readable text from a Bot Framework card attachment."""
    card_type = content_type.rsplit(".", 1)[-1] if "." in content_type else content_type
    body = content if isinstance(content, dict) else {}

    if card_type == "card.adaptive":
        lines = []
        for block in body.get("body", []):
            if block.get("text"):
                lines.append(block["text"])
        for action in body.get("actions", []):
            if action.get("title"):
                lines.append(f"[Action: {action['title']}]")
        return " ".join(lines) if lines else f"[Adaptive Card]"
    elif card_type == "card.signin":
        return f"[Sign-in Card] {body.get('text', 'Sign in required')}"
    elif card_type == "card.oauth":
        return f"[OAuth Card] {body.get('text', 'Authentication required')}"
    else:
        return f"[{card_type}] {body.get('title', body.get('text', ''))}"


async def _collect_activities(response_gen, client: CopilotClient) -> str:
    """Iterate an async activity generator and join message texts.

    Automatically approves consent cards so the conversation can proceed.
    """
    parts: list[str] = []
    async for activity in response_gen:
        if activity.type == ActivityTypes.message:
            if activity.text:
                parts.append(activity.text)
            if getattr(activity, "attachments", None):
                for att in activity.attachments:
                    ct = getattr(att, "content_type", "") or ""
                    content = getattr(att, "content", None)
                    if content and "application/vnd.microsoft.card" in ct:
                        card_text = _extract_card_text(ct, content)
                        if card_text:
                            parts.append(card_text)
                            print(f"  {card_text}")
            # Auto-approve consent cards (may need multiple rounds)
            if is_consent_card(activity):
                follow_ups = await handle_consent_card(client, activity)
                for fu in follow_ups:
                    if fu.type == ActivityTypes.message:
                        if fu.text:
                            parts.append(fu.text)
                        # Handle chained consent cards
                        if is_consent_card(fu):
                            follow_ups2 = await handle_consent_card(client, fu)
                            for fu2 in follow_ups2:
                                if fu2.type == ActivityTypes.message and fu2.text:
                                    parts.append(fu2.text)
                                if fu2.type == ActivityTypes.end_of_conversation:
                                    return "\n".join(parts)
                    if fu.type == ActivityTypes.end_of_conversation:
                        return "\n".join(parts)
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

    return await asyncio.wait_for(_collect_activities(response_gen, client), timeout=timeout)


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


async def _run_group(
    conv_id: str,
    group: list[EvalCase],
    conn: ConnectionSettings,
    token: str,
    timeout: int,
    settings: AgentSettings,
    semaphore: asyncio.Semaphore,
) -> list[EvalResult]:
    """Run all cases in one conversation group sequentially.

    Multi-turn ordering must be preserved within a group. The semaphore
    limits how many groups run in parallel.
    """
    results: list[EvalResult] = []
    async with semaphore:
        label = conv_id if not conv_id.startswith("_solo_") else ""
        try:
            client = await start_new_conversation(conn, token, label, timeout)
        except Exception as e:
            # If we can't even start the conversation, mark all cases in the group as failed
            for case in group:
                results.append(EvalResult(
                    case=case, actual_response="", passed=False,
                    error=f"Failed to start conversation: {e}",
                ))
            return results

        for case in group:
            print(f"[{conv_id[:20] if not conv_id.startswith('_solo_') else 'solo'}] "
                  f"Sending: {case.prompt[:60]}...", flush=True)
            try:
                actual = await collect_response(client, case, timeout)
                passed = case.check(actual, settings)
                results.append(EvalResult(case=case, actual_response=actual, passed=passed))
                status = "PASS" if passed else "FAIL"
                print(f"  -> [{status}] {case.prompt[:60]}")
            except TimeoutError:
                results.append(EvalResult(
                    case=case, actual_response="", passed=False,
                    error=f"Timed out after {timeout}s",
                ))
                print(f"  -> [TIMEOUT] {case.prompt[:60]}")
            except Exception as e:
                results.append(EvalResult(
                    case=case, actual_response="", passed=False, error=str(e)
                ))
                print(f"  -> [ERROR] {e}")
    return results


async def run_evaluation(
    csv_path: str,
    output_path: str | None = None,
    concurrency: int = 1,
    open_html: bool = True,
) -> EvalReport:
    settings = AgentSettings.from_env()
    conn = ConnectionSettings(
        environment_id=settings.environment_id,
        agent_identifier=settings.schema_name,
    )
    token = acquire_token(settings)
    timeout = settings.timeout

    cases, skipped = load_cases(csv_path)
    groups = group_cases_by_conversation(cases)
    total = len(cases)
    multi_turn_groups = sum(1 for g in groups.values() if len(g) > 1)
    solo_count = sum(1 for g in groups.values() if len(g) == 1)
    concurrency = max(1, min(concurrency, len(groups)))
    print(f"Loaded {total} evaluation cases from {csv_path}" +
          (f" ({skipped} skipped)" if skipped else ""))
    print(f"  {solo_count} independent prompt(s), {multi_turn_groups} multi-turn conversation(s)")
    print(f"  Concurrency: {concurrency} (groups in parallel)")
    print(f"  Timeout: {timeout}s per call")
    if settings.has_judge_config:
        print(f"  LLM judge: {settings.judge_provider} ({settings.judge_model})")
    print()

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _run_group(conv_id, group, conn, token, timeout, settings, semaphore)
        for conv_id, group in groups.items()
    ]
    group_results = await asyncio.gather(*tasks)

    # Flatten results in original CSV order using a case identity map
    report = EvalReport()
    case_to_result = {id(r.case): r for results in group_results for r in results}
    for case in cases:
        result = case_to_result.get(id(case))
        if result is not None:
            report.results.append(result)

    report.print_summary()

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_out = f"results/eval_{ts}.csv"
        html_out = f"results/eval_{ts}.html"
    else:
        out_path = Path(output_path)
        if out_path.suffix.lower() == ".html":
            html_out = output_path
            csv_out = str(out_path.with_suffix(".csv"))
        else:
            csv_out = output_path
            html_out = str(out_path.with_suffix(".html"))

    report.save_csv(Path(csv_out))
    report.save_html(Path(html_out))

    if open_html:
        try:
            import webbrowser
            webbrowser.open(Path(html_out).resolve().as_uri())
        except Exception:
            pass

    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Run prompt evaluations against a Copilot Studio agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CSV columns: prompt, expected_response, match_method[, conversation_id][, attachment][, skip]

Match methods (deterministic): exact, contains, not_contains, regex, fuzzy, partial
Match methods (LLM judge):     general_quality, text_similarity, compare_meaning

Rows with the same conversation_id share one conversation (multi-turn).
Rows without a conversation_id each get a fresh conversation.
Attachment: URL or local file path (optional). Local files are base64-encoded.
Skip: set to true/yes/1 to skip a row without removing it from the CSV.
LLM judge methods require JUDGE_* env vars (see README).
""",
    )
    parser.add_argument("input_csv", help="Path to the evaluation CSV file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output path (.csv or .html). Both are written; default: results/eval_<timestamp>.csv")
    parser.add_argument("--concurrency", "-c", type=int, default=1,
                        help="Number of conversation groups to run in parallel (default: 1)")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not auto-open the HTML report in a browser")
    args = parser.parse_args()

    asyncio.run(run_evaluation(
        csv_path=args.input_csv,
        output_path=args.output,
        concurrency=args.concurrency,
        open_html=not args.no_open,
    ))


if __name__ == "__main__":
    main()
