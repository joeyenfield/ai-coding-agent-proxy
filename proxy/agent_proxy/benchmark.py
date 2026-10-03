from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import ROOT, Settings


@dataclass
class TestDefinition:
    name: str
    prompt: str
    runs: int
    temperature: float
    max_tokens: int
    models: list[str]
    backend: str | None
    timeout: float


def load_test(path: Path) -> TestDefinition:
    config_path = path / "test.yaml"
    prompt_path = path / "prompt.md"
    if not config_path.exists() or not prompt_path.exists():
        raise ValueError(f"{path} must contain prompt.md and test.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return TestDefinition(
        name=data.get("name", path.name),
        prompt=prompt_path.read_text(encoding="utf-8"),
        runs=int(data.get("runs", 3)),
        temperature=float(data.get("temperature", 0)),
        max_tokens=int(data.get("max_tokens", 1024)),
        models=list(data.get("models") or []),
        backend=data.get("backend"),
        timeout=float(data.get("timeout", 300)),
    )


def run_test(
    client: httpx.Client,
    proxy_url: str,
    definition: TestDefinition,
    output_root: Path,
    model_override: str | None = None,
    backend_override: str | None = None,
) -> dict[str, Any]:
    models = [model_override] if model_override else definition.models
    if not models:
        raise ValueError(f"No models configured for {definition.name}")
    backend = backend_override or definition.backend or Settings.load().default_backend
    test_dir = output_root / definition.name
    test_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for model in models:
        for run_number in range(1, definition.runs + 1):
            print(f"{definition.name}: {model} run {run_number}/{definition.runs}", flush=True)
            result = _run_once(client, proxy_url, definition, model, backend, run_number)
            results.append(result)
            stem = f"{_safe_name(model)}-run{run_number}"
            (test_dir / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            (test_dir / f"{stem}.md").write_text(_run_markdown(result), encoding="utf-8")
    return {
        "test": definition.name,
        "backend": backend,
        "runs_per_model": definition.runs,
        "results": results,
        "models": {model: summarize([item for item in results if item["model"] == model]) for model in models},
    }


def _run_once(
    client: httpx.Client,
    proxy_url: str,
    definition: TestDefinition,
    model: str,
    backend: str,
    run_number: int,
) -> dict[str, Any]:
    session_response = client.post(f"{proxy_url}/api/sessions", json={
        "client": "benchmark", "project": definition.name, "model": model,
        "backend": backend, "trace": False,
        "tags": {"test": definition.name, "run": run_number},
    })
    session_response.raise_for_status()
    session = session_response.json()
    session_id = session["session_id"]
    text_parts: list[str] = []
    error: str | None = None
    status = 0
    try:
        with client.stream("POST", f"{proxy_url}/session/{session_id}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": definition.prompt}],
            "stream": True, "stream_options": {"include_usage": True},
            "temperature": definition.temperature, "max_tokens": definition.max_tokens,
        }, timeout=definition.timeout) as response:
            status = response.status_code
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    continue
                try:
                    obj = json.loads(value)
                    for choice in obj.get("choices", []):
                        text_parts.append(choice.get("delta", {}).get("content", "") or choice.get("text", ""))
                except ValueError:
                    continue
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        client.patch(f"{proxy_url}/api/sessions/{session_id}", json={"ended": True, "exit_status": 0 if error is None else 1})
    telemetry_response = client.get(f"{proxy_url}/api/sessions/{session_id}/telemetry")
    records = [json.loads(line) for line in telemetry_response.text.splitlines() if line.strip()]
    metrics = records[-1] if records else {}
    return {
        "test": definition.name, "model": model, "backend": backend, "run": run_number,
        "session_id": session_id, "status": metrics.get("status", status), "error": error or metrics.get("error_type"),
        "response": "".join(text_parts), "metrics": metrics,
        "parameters": {"temperature": definition.temperature, "max_tokens": definition.max_tokens},
    }


METRICS = (
    "input_tokens", "output_tokens", "ttft_ms", "prompt_eval_ms", "generation_ms",
    "total_ms", "prompt_tps", "generation_tps", "response_bytes",
)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"runs": len(results), "successful": sum(not item["error"] for item in results)}
    for metric in METRICS:
        values = [item["metrics"].get(metric) for item in results]
        numeric = [float(value) for value in values if value is not None]
        if numeric:
            summary[metric] = {"average": statistics.mean(numeric), "minimum": min(numeric), "maximum": max(numeric)}
    return summary


def _summary_markdown(report: dict[str, Any]) -> str:
    lines = ["# Benchmark Results", "", f"Generated: {report['generated_at']}", ""]
    for test in report["tests"]:
        lines += [f"## {test['test']}", "", f"Backend: `{test['backend']}`", ""]
        for model, values in test["models"].items():
            lines += [f"### {model}", "", f"Runs: {values['runs']} ({values['successful']} successful)", "", "| Metric | Average | Minimum | Maximum |", "|---|---:|---:|---:|"]
            for metric in METRICS:
                stats = values.get(metric)
                if stats:
                    lines.append(f"| {metric} | {stats['average']:.2f} | {stats['minimum']:.2f} | {stats['maximum']:.2f} |")
            lines.append("")
    return "\n".join(lines) + "\n"


def _run_markdown(result: dict[str, Any]) -> str:
    metric_lines = "\n".join(f"- {key}: {value}" for key, value in result["metrics"].items())
    return f"# {result['test']} — {result['model']} — run {result['run']}\n\nSession: `{result['session_id']}`\n\n## Metrics\n\n{metric_lines}\n\n## Response\n\n{result['response']}\n"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark models through the AI proxy")
    parser.add_argument("test", nargs="?", help="Test directory name")
    parser.add_argument("--all", action="store_true", help="Run all benchmark tests")
    parser.add_argument("--model", help="Override configured models")
    parser.add_argument("--backend", help="Override configured backend")
    parser.add_argument("--proxy", help="Override proxy.url from config/backends.yaml")
    parser.add_argument("--tests-dir", type=Path, default=ROOT / "benchmarks" / "tests")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "benchmarks" / "results")
    args = parser.parse_args()
    settings = Settings.load()
    proxy_url = (args.proxy or settings.proxy_url).rstrip("/")
    if not args.all and not args.test:
        parser.error("provide a test name or --all")
    paths = sorted(path for path in args.tests_dir.iterdir() if path.is_dir()) if args.all else [args.tests_dir / args.test]
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    output_root = args.results_dir / timestamp
    output_root.mkdir(parents=True, exist_ok=False)
    try:
        with httpx.Client() as client:
            client.get(f"{proxy_url}/health").raise_for_status()
            tests = [run_test(client, proxy_url, load_test(path), output_root, args.model, args.backend) for path in paths]
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    report = {"generated_at": datetime.now().astimezone().isoformat(), "proxy": proxy_url, "tests": tests}
    (output_root / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output_root / "summary.md").write_text(_summary_markdown(report), encoding="utf-8")
    print(f"Results: {output_root}")


if __name__ == "__main__":
    main()
