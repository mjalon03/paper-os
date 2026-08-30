from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from paper_service.deepseek_client import (
    DeepSeekCompletion,
    DeepSeekTokenUsage,
    DeepSeekTransportError,
)
from paper_service.knowledge_runner import execute_one, run_worker
from paper_service.knowledge_store import KnowledgeLeaseLost
from paper_service.knowledge_worker import (
    GenerationPolicy,
    KnowledgeGenerationError,
    KnowledgeGenerationRequest,
)


JOB_ID = "21d91ab9-44e8-4422-8e4a-a8092437485d"
LEASE_TOKEN = "bd356141-8894-4643-82d8-c0f35ddd0fc4"


class Heartbeat:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def __enter__(self):
        self.events.append(("enter",))
        return self

    def __exit__(self, *_args):
        self.events.append(("exit",))

    def processing(self, paper_id):
        self.events.append(("processing", paper_id))
        if self.fail:
            raise RuntimeError("private heartbeat storage failure")

    def idle(self, *, successful=False):
        self.events.append(("idle", successful))
        if self.fail:
            raise RuntimeError("private heartbeat storage failure")


def job(**overrides):
    value = {
        "id": JOB_ID,
        "paper_id": 7,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "include_personal_annotations": False,
        "max_source_pages": 8,
        "max_source_characters": 30_000,
        "max_output_tokens": 4_096,
        "lease_token": LEASE_TOKEN,
    }
    value.update(overrides)
    return value


def generation_request(model="deepseek-v4-pro"):
    policy = GenerationPolicy()
    revision_id = str(uuid4())
    return KnowledgeGenerationRequest(
        provider="deepseek",
        model=model,
        revision_id=revision_id,
        input_payload={"source_nodes": []},
        source_nodes={},
        policy=policy,
        request_digest="a" * 64,
    )


def completion():
    return DeepSeekCompletion(
        content="{}",
        usage=DeepSeekTokenUsage(100, 20, 120),
        request_sha256="b" * 64,
        response_sha256="c" * 64,
        provider_request_id="request-1",
    )


class KnowledgeRunnerTests(unittest.TestCase):
    def test_idle_queue_does_not_read_source_or_call_provider(self):
        with patch(
            "paper_service.knowledge_runner.claim_generation_job",
            return_value=None,
        ), patch(
            "paper_service.knowledge_runner.complete_deepseek_request"
        ) as provider:
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
            )
        self.assertEqual(result.status, "idle")
        provider.assert_not_called()

    def test_request_is_bound_before_provider_and_pro_uses_thinking(self):
        request = generation_request()
        events = []
        heartbeat = Heartbeat()

        def bind(*_args, **_kwargs):
            events.append("bind")

        def provider(body, **_kwargs):
            events.append("provider")
            self.assertEqual(body["thinking"], {"type": "enabled"})
            return completion()

        canonical = {"revision_id": request.revision_id}
        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch("paper_service.knowledge_runner.list_annotations") as annotations,
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ),
            patch("paper_service.knowledge_runner.bind_generation_request", bind),
            patch(
                "paper_service.knowledge_runner.build_deepseek_compatible_request",
                return_value={"model": request.model},
            ),
            patch(
                "paper_service.knowledge_runner.complete_deepseek_request",
                provider,
            ),
            patch(
                "paper_service.knowledge_runner.parse_generation_response",
                return_value=canonical,
            ),
            patch(
                "paper_service.knowledge_runner.record_generation_usage"
            ) as record_usage,
            patch(
                "paper_service.knowledge_runner.complete_generation_job"
            ) as complete_job,
        ):
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
                heartbeat=heartbeat,
            )

        self.assertEqual(events, ["bind", "provider"])
        self.assertEqual(result.status, "succeeded")
        annotations.assert_not_called()
        usage = complete_job.call_args.kwargs["usage"]
        self.assertEqual(usage.total_tokens, 120)
        self.assertEqual(
            complete_job.call_args.kwargs["generation_request"], request
        )
        self.assertEqual(
            complete_job.call_args.kwargs["generation_response_digest"],
            "c" * 64,
        )
        self.assertEqual(
            record_usage.call_args.kwargs["generation_request_digest"],
            request.request_digest,
        )
        self.assertEqual(
            heartbeat.events,
            [("processing", 7), ("idle", True)],
        )

    def test_annotations_are_loaded_only_after_explicit_job_opt_in(self):
        request = generation_request()
        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(include_personal_annotations=True),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch(
                "paper_service.knowledge_runner.list_annotations",
                return_value=[{"id": str(uuid4())}],
            ) as annotations,
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ) as build,
            patch("paper_service.knowledge_runner.bind_generation_request"),
            patch(
                "paper_service.knowledge_runner.build_deepseek_compatible_request",
                return_value={"model": request.model},
            ),
            patch(
                "paper_service.knowledge_runner.complete_deepseek_request",
                return_value=completion(),
            ),
            patch(
                "paper_service.knowledge_runner.parse_generation_response",
                return_value={"revision_id": request.revision_id},
            ),
            patch("paper_service.knowledge_runner.record_generation_usage"),
            patch("paper_service.knowledge_runner.complete_generation_job"),
        ):
            execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
            )

        annotations.assert_called_once_with(7)
        self.assertEqual(
            build.call_args.kwargs["annotations"],
            annotations.return_value,
        )

    def test_retryable_provider_failure_is_nacked_with_only_safe_code(self):
        request = generation_request()
        failed = DeepSeekTransportError("deepseek_rate_limited", retryable=True)
        heartbeat = Heartbeat()
        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ),
            patch("paper_service.knowledge_runner.bind_generation_request"),
            patch(
                "paper_service.knowledge_runner.build_deepseek_compatible_request",
                return_value={"model": request.model},
            ),
            patch(
                "paper_service.knowledge_runner.complete_deepseek_request",
                side_effect=failed,
            ),
            patch(
                "paper_service.knowledge_runner.nack_generation_job",
                return_value={"status": "retry_wait"},
            ) as nack,
        ):
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
                heartbeat=heartbeat,
            )

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.error_code, "deepseek_rate_limited")
        self.assertEqual(nack.call_args.kwargs["retry_delay_seconds"], 60)
        self.assertEqual(
            heartbeat.events,
            [("processing", 7), ("idle", False)],
        )

    def test_provider_usage_is_recorded_even_when_response_contract_is_invalid(self):
        request = generation_request()
        events = []

        def record(*_args, **_kwargs):
            events.append("usage")

        def reject(*_args, **_kwargs):
            events.append("nack")
            return {"status": "retry_wait"}

        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ),
            patch("paper_service.knowledge_runner.bind_generation_request"),
            patch(
                "paper_service.knowledge_runner.build_deepseek_compatible_request",
                return_value={"model": request.model},
            ),
            patch(
                "paper_service.knowledge_runner.complete_deepseek_request",
                return_value=completion(),
            ),
            patch("paper_service.knowledge_runner.record_generation_usage", record),
            patch(
                "paper_service.knowledge_runner.parse_generation_response",
                side_effect=KnowledgeGenerationError(
                    "generation_response_invalid_json"
                ),
            ),
            patch("paper_service.knowledge_runner.nack_generation_job", reject),
            patch("paper_service.knowledge_runner.complete_generation_job") as complete_job,
        ):
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
            )

        self.assertEqual(events, ["usage", "nack"])
        self.assertEqual(result.error_code, "generation_response_invalid_json")
        complete_job.assert_not_called()

    def test_lost_lease_is_not_nacked_by_a_stale_worker(self):
        request = generation_request()
        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ),
            patch(
                "paper_service.knowledge_runner.bind_generation_request",
                side_effect=KnowledgeLeaseLost("generation_job_lease_lost"),
            ),
            patch("paper_service.knowledge_runner.nack_generation_job") as nack,
            patch("paper_service.knowledge_runner.complete_deepseek_request") as provider,
        ):
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
            )

        self.assertEqual(result.status, "lease_lost")
        nack.assert_not_called()
        provider.assert_not_called()

    def test_missing_api_key_is_rejected_before_claim(self):
        with patch("paper_service.knowledge_runner.claim_generation_job") as claim_job:
            with self.assertRaisesRegex(RuntimeError, "deepseek_not_configured"):
                execute_one(
                    worker_id="knowledge-worker-1",
                    data_dir="/data",
                    api_key="",
                )
        claim_job.assert_not_called()

    def test_heartbeat_transition_failure_does_not_fail_generation(self):
        request = generation_request()
        heartbeat = Heartbeat(fail=True)
        with (
            patch(
                "paper_service.knowledge_runner.claim_generation_job",
                return_value=job(),
            ),
            patch(
                "paper_service.knowledge_runner._paper_for_generation",
                return_value={"id": 7},
            ),
            patch("paper_service.knowledge_runner.read_article", return_value={}),
            patch(
                "paper_service.knowledge_runner.build_generation_request",
                return_value=request,
            ),
            patch("paper_service.knowledge_runner.bind_generation_request"),
            patch(
                "paper_service.knowledge_runner.build_deepseek_compatible_request",
                return_value={"model": request.model},
            ),
            patch(
                "paper_service.knowledge_runner.complete_deepseek_request",
                return_value=completion(),
            ),
            patch("paper_service.knowledge_runner.record_generation_usage"),
            patch(
                "paper_service.knowledge_runner.parse_generation_response",
                return_value={"revision_id": request.revision_id},
            ),
            patch("paper_service.knowledge_runner.complete_generation_job"),
        ):
            result = execute_one(
                worker_id="knowledge-worker-1",
                data_dir="/data",
                api_key="sk-" + "x" * 40,
                heartbeat=heartbeat,
            )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(heartbeat.events, [("processing", 7)])

    def test_worker_loop_is_wrapped_by_environment_heartbeat(self):
        heartbeat = Heartbeat()
        with patch.dict(
            os.environ,
            {
                "PAPER_KNOWLEDGE_WORKER_ID": "knowledge-worker-1",
                "PAPER_KNOWLEDGE_POLL_SECONDS": "5",
                "PAPER_KNOWLEDGE_LEASE_SECONDS": "300",
                "PAPER_DATA_DIR": "/data",
            },
            clear=True,
        ), patch(
            "paper_service.knowledge_runner.ensure_schema"
        ), patch(
            "paper_service.knowledge_runner.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.knowledge_runner.deepseek_api_key", return_value=None
        ), patch(
            "paper_service.knowledge_runner.time.sleep",
            side_effect=StopIteration,
        ):
            with self.assertRaises(StopIteration):
                run_worker()
        heartbeat_factory.assert_called_once_with("knowledge-worker-1")
        self.assertEqual(heartbeat.events, [("enter",), ("exit",)])


if __name__ == "__main__":
    unittest.main()
