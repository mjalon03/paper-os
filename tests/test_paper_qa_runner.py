from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from paper_service.deepseek_client import (
    DeepSeekCompletion,
    DeepSeekTokenUsage,
    DeepSeekTransportError,
)
from paper_service.qa_contract import build_qa_prompt_payload
from paper_service.qa_runner import execute_one, run_worker
from paper_service.search_store import QALeaseLost
from paper_service.selectors import SELECTOR_VERSION


JOB_ID = "29c7bbab-0439-4eed-b3dd-b27f76b89438"
LEASE_TOKEN = "0b4249c4-7ee5-4518-b644-a520634df96f"
REQUEST_DIGEST = "a" * 64
RESPONSE_DIGEST = "c" * 64
NODE_ID = "n-" + "1" * 24
API_KEY = "sk-" + "x" * 40


class Heartbeat:
    def __init__(self):
        self.events = []

    def __enter__(self):
        self.events.append(("enter",))
        return self

    def __exit__(self, *_args):
        self.events.append(("exit",))

    def processing(self, paper_id):
        self.events.append(("processing", paper_id))

    def idle(self, *, successful=False):
        self.events.append(("idle", successful))


def qa_request(*, annotation: bool = False) -> dict:
    source_kind = "user_annotation" if annotation else "source_text"
    return {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "question": "What is supported by the evidence?",
        "source_nodes": [
            {
                "selector_version": SELECTOR_VERSION,
                "source_kind": source_kind,
                "source_id": "chunk-1",
                "node_id": NODE_ID,
                "text": "The measured response increased after stimulation.",
            }
        ],
    }


def claimed_job(**overrides) -> dict:
    value = {
        "id": JOB_ID,
        "paper_id": 7,
        "lease_token": LEASE_TOKEN,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "request_actor_kind": "human",
        "request_actor_id": "access-user-123",
        "request_acknowledged": True,
        "include_personal_annotations": False,
        "attempt_count": 1,
        "request_digest": REQUEST_DIGEST,
    }
    value.update(overrides)
    return value


def loaded_request(*, include_annotations: bool = False, **overrides) -> dict:
    request = qa_request(annotation=include_annotations)
    value = {
        "job_id": JOB_ID,
        "attempt_number": 1,
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "request": request,
        "prompt": build_qa_prompt_payload(
            request,
            include_user_annotations=include_annotations,
        ),
        "request_digest": REQUEST_DIGEST,
    }
    value.update(overrides)
    return value


def valid_answer() -> dict:
    return {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "status": "answered",
        "answer": "The measured response increased.",
        "claims": [
            {
                "text": "Stimulation increased the measured response.",
                "citations": [
                    {
                        "source_kind": "source_text",
                        "source_id": "chunk-1",
                        "node_id": NODE_ID,
                        "exact_quote": "response increased after stimulation",
                    }
                ],
            }
        ],
    }


def completion(content: str | None = None) -> DeepSeekCompletion:
    return DeepSeekCompletion(
        content=content if content is not None else json.dumps(valid_answer()),
        usage=DeepSeekTokenUsage(100, 20, 120),
        request_sha256="b" * 64,
        response_sha256=RESPONSE_DIGEST,
        provider_request_id="request-1",
    )


class QARunnerTests(unittest.TestCase):
    def test_idle_queue_does_not_load_or_call_provider(self):
        with (
            patch("paper_service.qa_runner.claim_qa_job", return_value=None),
            patch("paper_service.qa_runner.load_qa_model_request") as load,
            patch("paper_service.qa_runner.complete_deepseek_request") as provider,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.status, "idle")
        load.assert_not_called()
        provider.assert_not_called()

    def test_live_request_is_loaded_before_provider_and_flash_thinks(self):
        events: list[str] = []
        heartbeat = Heartbeat()

        def load(*_args, **_kwargs):
            events.append("load")
            return loaded_request()

        def provider(body, **kwargs):
            events.append("provider")
            self.assertEqual(body["model"], "deepseek-v4-flash")
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["temperature"], 0)
            self.assertEqual(body["max_tokens"], 4096)
            self.assertIs(body["stream"], False)
            self.assertEqual(
                [message["role"] for message in body["messages"]],
                ["system", "user"],
            )
            self.assertEqual(kwargs["timeout_seconds"], 37)
            return completion()

        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch("paper_service.qa_runner.load_qa_model_request", load),
            patch("paper_service.qa_runner.complete_deepseek_request", provider),
            patch("paper_service.qa_runner.complete_qa_job") as complete,
            patch("paper_service.qa_runner.record_qa_usage") as record,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
                timeout_seconds=37,
                heartbeat=heartbeat,
            )

        self.assertEqual(events, ["load", "provider"])
        self.assertEqual(result.status, "succeeded")
        canonical = complete.call_args.kwargs["response"]
        self.assertEqual(canonical, valid_answer())
        usage = complete.call_args.kwargs["usage"]
        self.assertEqual(usage.total_tokens, 120)
        record.assert_not_called()
        self.assertEqual(
            heartbeat.events,
            [("processing", 7), ("idle", True)],
        )

    def test_default_prompt_does_not_transmit_personal_annotations(self):
        captured: dict = {}

        def provider(body, **_kwargs):
            captured.update(json.loads(body["messages"][1]["content"]))
            return completion()

        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(),
            ),
            patch("paper_service.qa_runner.complete_deepseek_request", provider),
            patch("paper_service.qa_runner.complete_qa_job"),
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.status, "succeeded")
        policy = captured["transmission_policy"]
        self.assertIs(policy["user_annotations_included"], False)
        self.assertNotIn("user_annotation", policy["allowed_source_kinds"])
        transmitted = json.loads(captured["input_json"])
        self.assertEqual(
            [node["source_kind"] for node in transmitted["source_nodes"]],
            ["source_text"],
        )

    def test_explicit_annotation_opt_in_is_preserved_after_revalidation(self):
        captured: dict = {}

        def provider(body, **_kwargs):
            captured.update(json.loads(body["messages"][1]["content"]))
            answer = valid_answer()
            answer["claims"][0]["citations"][0]["source_kind"] = "user_annotation"
            return completion(json.dumps(answer))

        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(include_personal_annotations=True),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(include_annotations=True),
            ),
            patch("paper_service.qa_runner.complete_deepseek_request", provider),
            patch("paper_service.qa_runner.complete_qa_job"),
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertIs(
            captured["transmission_policy"]["user_annotations_included"], True
        )
        transmitted = json.loads(captured["input_json"])
        self.assertEqual(
            transmitted["source_nodes"][0]["source_kind"], "user_annotation"
        )

    def test_invalid_response_records_raw_transport_usage_then_nacks(self):
        events: list[str] = []
        heartbeat = Heartbeat()

        def record(*_args, **kwargs):
            events.append("usage")
            self.assertEqual(kwargs["response_digest"], RESPONSE_DIGEST)
            self.assertEqual(kwargs["usage"].total_tokens, 120)

        def nack(*_args, **kwargs):
            events.append("nack")
            self.assertEqual(kwargs["error_code"], "qa_response_invalid")
            self.assertEqual(kwargs["retry_delay_seconds"], 45)
            return {"status": "retry_wait"}

        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(),
            ),
            patch(
                "paper_service.qa_runner.complete_deepseek_request",
                return_value=completion("{}"),
            ),
            patch("paper_service.qa_runner.record_qa_usage", record),
            patch("paper_service.qa_runner.nack_qa_job", nack),
            patch("paper_service.qa_runner.complete_qa_job") as complete,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
                retry_delay_seconds=45,
                heartbeat=heartbeat,
            )

        self.assertEqual(events, ["usage", "nack"])
        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.error_code, "qa_response_invalid")
        complete.assert_not_called()
        self.assertEqual(
            heartbeat.events,
            [("processing", 7), ("idle", False)],
        )

    def test_lost_lease_during_load_is_never_nacked_or_sent(self):
        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                side_effect=QALeaseLost("qa_lease_lost"),
            ),
            patch("paper_service.qa_runner.complete_deepseek_request") as provider,
            patch("paper_service.qa_runner.nack_qa_job") as nack,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.status, "lease_lost")
        self.assertEqual(result.error_code, "qa_lease_lost")
        provider.assert_not_called()
        nack.assert_not_called()

    def test_lost_lease_while_recording_invalid_usage_is_not_nacked(self):
        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(),
            ),
            patch(
                "paper_service.qa_runner.complete_deepseek_request",
                return_value=completion("{}"),
            ),
            patch(
                "paper_service.qa_runner.record_qa_usage",
                side_effect=QALeaseLost("qa_lease_lost"),
            ),
            patch("paper_service.qa_runner.nack_qa_job") as nack,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.status, "lease_lost")
        nack.assert_not_called()

    def test_retryable_provider_failure_uses_configured_retry_delay(self):
        failure = DeepSeekTransportError("deepseek_rate_limited", retryable=True)
        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(),
            ),
            patch(
                "paper_service.qa_runner.complete_deepseek_request",
                side_effect=failure,
            ),
            patch(
                "paper_service.qa_runner.nack_qa_job",
                return_value={"status": "retry_wait"},
            ) as nack,
            patch("paper_service.qa_runner.record_qa_usage") as record,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
                retry_delay_seconds=91,
            )

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.error_code, "deepseek_rate_limited")
        self.assertEqual(nack.call_args.kwargs["retry_delay_seconds"], 91)
        record.assert_not_called()

    def test_model_is_taken_from_job_and_rejected_before_provider_if_invalid(self):
        with (
            patch(
                "paper_service.qa_runner.claim_qa_job",
                return_value=claimed_job(model="unreviewed-model"),
            ),
            patch(
                "paper_service.qa_runner.load_qa_model_request",
                return_value=loaded_request(model="unreviewed-model"),
            ),
            patch("paper_service.qa_runner.complete_deepseek_request") as provider,
            patch(
                "paper_service.qa_runner.nack_qa_job",
                return_value={"status": "retry_wait"},
            ) as nack,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key=API_KEY,
            )

        self.assertEqual(result.error_code, "qa_model_not_allowed")
        self.assertEqual(nack.call_args.kwargs["retry_delay_seconds"], 1)
        provider.assert_not_called()

    def test_pro_model_is_rejected_for_question_answering(self):
        job = claimed_job(model="deepseek-v4-pro")
        loaded = loaded_request(model="deepseek-v4-pro")
        with (
            patch("paper_service.qa_runner.claim_qa_job", return_value=job),
            patch("paper_service.qa_runner.load_qa_model_request", return_value=loaded),
            patch("paper_service.qa_runner.complete_deepseek_request") as provider,
            patch(
                "paper_service.qa_runner.nack_qa_job",
                return_value={"status": "failed"},
            ) as nack,
        ):
            result = execute_one(
                worker_id="qa-worker-1",
                data_dir="/data",
                api_key="d" * 32,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "qa_model_not_allowed")
        self.assertEqual(nack.call_args.kwargs["retry_delay_seconds"], 1)
        provider.assert_not_called()

    def test_configuration_is_strictly_validated_before_claim(self):
        cases = (
            {"worker_id": "bad worker"},
            {"timeout_seconds": 0},
            {"timeout_seconds": " 20"},
            {"max_output_tokens": 8193},
            {"lease_seconds": True},
        )
        for override in cases:
            arguments = {
                "worker_id": "qa-worker-1",
                "data_dir": "/data",
                "api_key": API_KEY,
            }
            arguments.update(override)
            with self.subTest(override=override), patch(
                "paper_service.qa_runner.claim_qa_job"
            ) as claim:
                with self.assertRaises(RuntimeError):
                    execute_one(**arguments)
                claim.assert_not_called()

    def test_missing_key_is_rejected_before_claim(self):
        with patch("paper_service.qa_runner.claim_qa_job") as claim:
            with self.assertRaisesRegex(RuntimeError, "deepseek_not_configured"):
                execute_one(
                    worker_id="qa-worker-1",
                    data_dir="/data",
                    api_key="",
                )
        claim.assert_not_called()

    def test_worker_loop_is_wrapped_by_environment_heartbeat(self):
        heartbeat = Heartbeat()
        with patch.dict(
            os.environ,
            {
                "PAPER_QA_WORKER_ID": "qa-worker-1",
                "PAPER_QA_POLL_SECONDS": "5",
                "PAPER_QA_LEASE_SECONDS": "300",
                "PAPER_QA_RETRY_DELAY_SECONDS": "60",
                "PAPER_DEEPSEEK_TIMEOUT_SECONDS": "120",
                "PAPER_QA_MAX_OUTPUT_TOKENS": "4096",
                "PAPER_DATA_DIR": "/data",
            },
            clear=True,
        ), patch(
            "paper_service.qa_runner.ensure_schema"
        ), patch(
            "paper_service.qa_runner.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.qa_runner.deepseek_api_key", return_value=None
        ), patch(
            "paper_service.qa_runner.time.sleep",
            side_effect=StopIteration,
        ):
            with self.assertRaises(StopIteration):
                run_worker()
        heartbeat_factory.assert_called_once_with("qa-worker-1")
        self.assertEqual(heartbeat.events, [("enter",), ("exit",)])


if __name__ == "__main__":
    unittest.main()
