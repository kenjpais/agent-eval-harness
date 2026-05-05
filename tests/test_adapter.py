"""Tests for EvalHub FrameworkAdapter."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_eval.agent.base import RunResult
from agent_eval.config import EvalConfig
from agent_eval.evalhub.s3_dataset import DatasetInfo


def _make_eval_yaml(tmpdir: Path, skill: str = "test-skill", arguments: str = "--input {prompt}") -> Path:
    """Create a minimal eval.yaml for testing."""
    config = {
        "name": "test-eval",
        "skill": skill,
        "execution": {
            "mode": "case",
            "arguments": arguments,
            "timeout": 300,
            "max_budget_usd": 2.0,
        },
        "runner": {"type": "claude-code"},
        "permissions": {"allow": ["Bash", "Read"]},
        "dataset": {"path": "cases"},
    }
    path = tmpdir / "eval.yaml"
    path.write_text(yaml.dump(config))
    return path


def _make_case_dir(cases_dir: Path, case_id: str, input_data: dict) -> Path:
    """Create a case directory with input.yaml."""
    case_dir = cases_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.yaml").write_text(yaml.dump(input_data))
    return case_dir


def _make_run_result(exit_code: int = 0, duration: float = 10.0) -> RunResult:
    """Create a RunResult for testing."""
    return RunResult(
        exit_code=exit_code,
        stdout="output text",
        stderr="",
        duration_s=duration,
        cost_usd=0.05,
        num_turns=5,
        resolved_model="claude-sonnet-4-20250514",
    )


# Stub classes for evalhub SDK types
class _StubModelConfig:
    def __init__(self, name="claude-sonnet-4", url="https://api.anthropic.com"):
        self.name = name
        self.url = url
        self.auth = None


class _StubJobSpec:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "job-123")
        self.provider_id = kwargs.get("provider_id", "agent-eval")
        self.benchmark_id = kwargs.get("benchmark_id", "skill-eval-v1")
        self.benchmark_index = kwargs.get("benchmark_index", 0)
        self.model = kwargs.get("model", _StubModelConfig())
        self.parameters = kwargs.get("parameters", {})
        self.callback_url = kwargs.get("callback_url", "")
        self.num_examples = kwargs.get("num_examples", None)
        self.experiment_name = kwargs.get("experiment_name", "")
        self.tags = kwargs.get("tags", {})
        self.exports = kwargs.get("exports", {})


class _StubJobCallbacks:
    def __init__(self):
        self.statuses = []
        self.results = []

    def report_status(self, update):
        self.statuses.append(update)

    def report_results(self, results):
        self.results.append(results)

    def create_oci_artifact(self, spec):
        return None


def test_adapter_runs_skill_and_scores():
    """Adapter runs skill per case and returns JobResults."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(tmpdir)

        # Create case directories
        cases_dir = tmpdir / "cases"
        _make_case_dir(cases_dir, "case-001", {"prompt": "hello world"})
        _make_case_dir(cases_dir, "case-002", {"prompt": "goodbye world"})

        dataset_info = DatasetInfo(
            num_cases=2,
            case_ids=["case-001", "case-002"],
            dest=cases_dir,
        )

        run_result = _make_run_result(exit_code=0)

        mock_runner = MagicMock()
        mock_runner.run_skill.return_value = run_result

        real_config = EvalConfig.from_yaml(eval_yaml)

        config = _StubJobSpec(
            id="job-abc",
            benchmark_id="bench-1",
            benchmark_index=0,
            model=_StubModelConfig(name="claude-sonnet-4"),
            parameters={"s3_bucket": "test-bucket", "s3_prefix": "dataset/v1"},
        )
        callbacks = _StubJobCallbacks()

        with (
            patch("agent_eval.evalhub.adapter.download_dataset", return_value=dataset_info),
            patch("agent_eval.evalhub.adapter.boto3") as mock_boto3,
            patch("agent_eval.evalhub.adapter.ClaudeCodeRunner", return_value=mock_runner),
            patch("agent_eval.evalhub.adapter.EvalConfig") as mock_eval_config_cls,
            patch("agent_eval.evalhub.adapter._framework_adapter_init"),
        ):
            mock_eval_config_cls.from_yaml.return_value = real_config

            from agent_eval.evalhub.adapter import AgentEvalAdapter

            adapter = AgentEvalAdapter(eval_config_path=str(eval_yaml))
            result = adapter.run_benchmark_job(config, callbacks)

        # Runner called once per case
        assert mock_runner.run_skill.call_count == 2

        # Verify args resolved from input.yaml
        calls = mock_runner.run_skill.call_args_list
        for call in calls:
            # run_skill is called with keyword args
            args_val = call.kwargs.get("args", "")
            assert args_val in ("--input hello world", "--input goodbye world")

        # Status reported multiple times (at least INITIALIZING, LOADING_DATA, RUNNING_EVALUATION, POST_PROCESSING, COMPLETED)
        assert len(callbacks.statuses) >= 4

        # JobResults has correct fields
        assert result.id == "job-abc"
        assert result.model_name == "claude-sonnet-4"
        assert result.num_examples_evaluated == 2
        assert result.benchmark_id == "bench-1"


def test_adapter_handles_runner_failure():
    """Adapter returns results even when runner exits with error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        eval_yaml = _make_eval_yaml(tmpdir)

        cases_dir = tmpdir / "cases"
        _make_case_dir(cases_dir, "case-001", {"prompt": "fail case"})

        dataset_info = DatasetInfo(
            num_cases=1,
            case_ids=["case-001"],
            dest=cases_dir,
        )

        # Runner returns exit_code=1 (failure)
        run_result = _make_run_result(exit_code=1, duration=5.0)

        mock_runner = MagicMock()
        mock_runner.run_skill.return_value = run_result

        real_config = EvalConfig.from_yaml(eval_yaml)

        config = _StubJobSpec(
            id="job-fail",
            benchmark_id="bench-1",
            model=_StubModelConfig(name="claude-sonnet-4"),
            parameters={"s3_bucket": "test-bucket", "s3_prefix": "dataset/v1"},
        )
        callbacks = _StubJobCallbacks()

        with (
            patch("agent_eval.evalhub.adapter.download_dataset", return_value=dataset_info),
            patch("agent_eval.evalhub.adapter.boto3") as mock_boto3,
            patch("agent_eval.evalhub.adapter.ClaudeCodeRunner", return_value=mock_runner),
            patch("agent_eval.evalhub.adapter.EvalConfig") as mock_eval_config_cls,
            patch("agent_eval.evalhub.adapter._framework_adapter_init"),
        ):
            mock_eval_config_cls.from_yaml.return_value = real_config

            from agent_eval.evalhub.adapter import AgentEvalAdapter

            adapter = AgentEvalAdapter(eval_config_path=str(eval_yaml))
            result = adapter.run_benchmark_job(config, callbacks)

        # Results returned despite failure
        assert result is not None
        assert result.id == "job-fail"
        assert result.num_examples_evaluated == 1

        # exit_code=1 present in evaluation results
        exit_metric = next(
            (r for r in result.results if r.metric_name == "exit_code"),
            None,
        )
        assert exit_metric is not None
        assert exit_metric.metric_value == 1
