from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import paper_service.ops_runtime as ops_runtime_module

from paper_service.operations import (
    advance_incident_state,
    build_generic_webhook_payload,
    build_worker_health_snapshot,
    initial_incident_state,
    worker_heartbeat,
)
from paper_service.ops_runtime import (
    MonitorCycleResult,
    OpsRuntimeConfig,
    OpsRuntimeError,
    ServiceMonitorResult,
    WebhookBatchResult,
    WebhookDeliveryConfig,
    WorkerHeartbeat,
    configure_monitor_expected_workers,
    dispatch_webhook_outbox,
    load_runtime_config,
    run_monitor_cycle,
    run_monitor_loop,
    worker_heartbeat_from_env,
)


OUTBOX_ID = "843040a6-753c-45cb-8307-0f5bccb10bd8"
SECOND_OUTBOX_ID = "871ddab5-e8c4-428e-af87-d26b10df647a"
LEASE_TOKEN = "d889dfdf-f777-4ad2-a8b8-a2a748496162"
SECRET = "this-is-a-private-bearer-token"
URL = "https://hooks.example.com/private-webhook-token"


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def webhook_payload():
    snapshot = build_worker_health_snapshot(
        [],
        expected_worker_ids=["reader"],
        evaluated_at="2026-08-29T10:00:00Z",
        service_id="paper-reader-v2",
    )
    _state, events = advance_incident_state(
        initial_incident_state("paper-reader-v2"), snapshot
    )
    return build_generic_webhook_payload(events[0])


def lease(outbox_id=OUTBOX_ID):
    payload = webhook_payload()
    return {
        "id": outbox_id,
        "lease_token": LEASE_TOKEN,
        "payload_sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        "payload": payload,
    }


def disabled_webhook():
    return WebhookDeliveryConfig(
        url=None,
        allowed_hosts=(),
        auth_token=None,
        worker_id="ops-webhook",
        timeout_seconds=10,
        response_bytes=4096,
        lease_seconds=60,
        retry_delay_seconds=60,
        max_per_cycle=10,
    )


def enabled_webhook(*, max_per_cycle=10, response_bytes=4096):
    return WebhookDeliveryConfig(
        url=URL,
        allowed_hosts=("hooks.example.com",),
        auth_token=SECRET,
        worker_id="ops-webhook",
        timeout_seconds=10,
        response_bytes=response_bytes,
        lease_seconds=60,
        retry_delay_seconds=60,
        max_per_cycle=max_per_cycle,
    )


def runtime_config(*, webhook=None, services=None):
    return OpsRuntimeConfig(
        services=(
            (("paper-reader-v2", ("reader",)),)
            if services is None
            else services
        ),
        monitor_interval_seconds=5,
        stale_after_seconds=10,
        startup_grace_seconds=0,
        webhook=disabled_webhook() if webhook is None else webhook,
    )


class FakeResponse:
    def __init__(self, *, status=204, body=b"", url=URL, headers=None):
        self.status = status
        self.body = body
        self.url = url
        self.headers = {} if headers is None else headers

    def read(self, amount=-1):
        return self.body[:amount]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class WorkerHeartbeatTests(unittest.TestCase):
    def test_env_helper_is_disabled_by_default_and_never_touches_database(self):
        report = Mock(side_effect=AssertionError("must not report"))
        with worker_heartbeat_from_env(
            "reader", environment={}, report=report
        ) as heartbeat:
            heartbeat.processing(7)
            result = heartbeat.idle(successful=True)
        self.assertEqual(result.result_code, "heartbeat_disabled")
        self.assertEqual(heartbeat.snapshot().state, "stopped")
        report.assert_not_called()

    def test_env_helper_enables_validated_reporter_and_rejects_bad_config(self):
        payloads = []
        heartbeat = worker_heartbeat_from_env(
            "fallback",
            environment={
                "PAPER_OPS_HEARTBEAT_ENABLED": "true",
                "PAPER_OPS_HEARTBEAT_SERVICE_ID": "paper-reader-v2",
                "PAPER_OPS_HEARTBEAT_WORKER_ID": "reader",
                "PAPER_OPS_HEARTBEAT_INTERVAL_SECONDS": "15",
            },
            report=lambda payload, **_kwargs: (payloads.append(payload) or payload),
            time_ns=lambda: 1000,
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        )
        self.assertIsInstance(heartbeat, WorkerHeartbeat)
        self.assertEqual(heartbeat.interval_seconds, 15)
        self.assertEqual(heartbeat.tick().result_code, "heartbeat_reported")
        self.assertEqual(len(payloads), 1)

        invalid = (
            {"PAPER_OPS_HEARTBEAT_ENABLED": "1"},
            {"PAPER_OPS_HEARTBEAT_UNKNOWN": "true"},
            {"PAPER_OPS_HEARTBEAT_WORKER_ID": "bad worker"},
            {"PAPER_OPS_HEARTBEAT_INTERVAL_SECONDS": "01"},
        )
        for environment in invalid:
            with self.subTest(environment=environment):
                with self.assertRaises(OpsRuntimeError):
                    worker_heartbeat_from_env("reader", environment=environment)

    def test_tick_uses_epoch_nanoseconds_and_is_monotonic_across_restart(self):
        first_payloads = []
        values = iter([1_800_000_000_000_000_000, 1_800_000_000_000_000_000])
        reporter = WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            report=lambda payload, **_kwargs: (first_payloads.append(payload) or payload),
            time_ns=lambda: next(values),
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        )
        first = reporter.tick()
        second = reporter.tick()
        self.assertEqual(first.sequence, 1_800_000_000_000_000_000)
        self.assertEqual(second.sequence, first.sequence + 1)

        restarted_payloads = []
        restarted = WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            report=lambda payload, **_kwargs: (restarted_payloads.append(payload) or payload),
            time_ns=lambda: 1_800_000_000_100_000_000,
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        )
        after_restart = restarted.tick()
        self.assertGreater(after_restart.sequence, second.sequence)
        self.assertEqual(first_payloads[0]["state"], "idle")
        self.assertEqual(restarted_payloads[0]["worker_id"], "reader")

    def test_all_states_are_explicit_and_invalid_state_never_reports_success(self):
        payloads = []
        sequence = iter(range(1000, 1010))
        reporter = WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            report=lambda payload, **_kwargs: (payloads.append(payload) or payload),
            time_ns=lambda: next(sequence),
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        )
        reporter.processing(7)
        reporter.tick()
        reporter.degraded("extractor_unavailable", active_paper_id=7)
        reporter.tick()
        reporter.idle(successful=True)
        reporter.tick()
        reporter.stop()
        reporter.tick()
        self.assertEqual(
            [item["state"] for item in payloads],
            ["processing", "degraded", "idle", "stopped"],
        )
        self.assertEqual(payloads[0]["active_paper_id"], 7)
        self.assertIsNotNone(payloads[2]["last_success_at"])
        count = reporter.snapshot().report_count
        with self.assertRaisesRegex(
            OpsRuntimeError, "heartbeat_processing_requires_paper"
        ):
            reporter._transition(
                state="processing",
                active_paper_id=None,
                error_code=None,
                mark_success=False,
            )
        self.assertEqual(reporter.snapshot().report_count, count)

    def test_database_and_emitter_failures_never_escape_or_leak_details(self):
        calls = 0
        events = []

        def report(_payload, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(f"database failed {SECRET} /private/paper.pdf")
            return _payload

        def broken_emit(event):
            events.append(event)
            raise RuntimeError("logger failed with " + SECRET)

        values = iter([1000, 1001])
        reporter = WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            report=report,
            time_ns=lambda: next(values),
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=broken_emit,
        )
        failed = reporter.tick()
        succeeded = reporter.tick()
        self.assertEqual(failed.result_code, "heartbeat_store_unavailable")
        self.assertEqual(failed.failure_count, 1)
        self.assertEqual(succeeded.result_code, "heartbeat_reported")
        self.assertEqual(succeeded.report_count, 1)
        public = repr((failed, succeeded, events))
        self.assertNotIn(SECRET, public)
        self.assertNotIn("/private", public)

    def test_mismatched_store_ack_is_never_counted_as_a_success(self):
        reporter = WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            report=lambda payload, **_kwargs: {**payload, "state": "stopped"},
            time_ns=lambda: 1000,
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        )
        result = reporter.tick()
        self.assertEqual(result.report_count, 0)
        self.assertEqual(result.failure_count, 1)
        self.assertEqual(result.result_code, "heartbeat_store_unavailable")

    def test_background_context_reports_final_stopped_without_blocking_worker(self):
        states = []
        stopped = threading.Event()
        counter = 2_000

        def next_sequence():
            nonlocal counter
            counter += 1
            return counter

        def report(payload, **_kwargs):
            states.append(payload["state"])
            if payload["state"] == "stopped":
                stopped.set()
            return payload

        with WorkerHeartbeat(
            service_id="paper-reader-v2",
            worker_id="reader",
            interval_seconds=30,
            report=report,
            time_ns=next_sequence,
            clock=lambda: datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
            emit=lambda _event: None,
        ) as reporter:
            reporter.processing(7)
        self.assertTrue(stopped.wait(1))
        self.assertEqual(states[-1], "stopped")
        self.assertFalse(reporter._thread.is_alive())


class RuntimeConfigTests(unittest.TestCase):
    def test_strict_environment_is_canonical_and_disabled_webhook_holds_no_secret(self):
        config = load_runtime_config(
            {
                "PAPER_OPS_RUNTIME_SERVICES_JSON": (
                    '{"paper-reader-v2":["sync","reader"]}'
                ),
                "PAPER_OPS_RUNTIME_MONITOR_INTERVAL_SECONDS": "30",
                "PAPER_OPS_RUNTIME_STALE_AFTER_SECONDS": "120",
            }
        )
        self.assertEqual(
            config.services,
            (("paper-reader-v2", ("reader", "sync")),),
        )
        self.assertIsNone(config.webhook.url)
        self.assertIsNone(config.webhook.auth_token)
        self.assertEqual(config.startup_grace_seconds, 90)
        self.assertEqual(repr(config), "OpsRuntimeConfig(service_count=1, webhook='disabled')")

    def test_unknown_duplicate_noncanonical_and_unsafe_values_fail_closed(self):
        base = {"PAPER_OPS_RUNTIME_SERVICES_JSON": '{"svc":["worker"]}'}
        cases = [
            {**base, "PAPER_OPS_RUNTIME_UNKNOWN": "1"},
            {
                "PAPER_OPS_RUNTIME_SERVICES_JSON": (
                    '{"svc":["worker"],"svc":["worker-two"]}'
                )
            },
            {**base, "PAPER_OPS_RUNTIME_MONITOR_INTERVAL_SECONDS": "05"},
            {**base, "PAPER_OPS_RUNTIME_STARTUP_GRACE_SECONDS": "29"},
            {
                **base,
                "PAPER_OPS_RUNTIME_MONITOR_INTERVAL_SECONDS": "30",
                "PAPER_OPS_RUNTIME_STALE_AFTER_SECONDS": "59",
            },
            {**base, "PAPER_OPS_RUNTIME_WEBHOOK_URL": "http://hooks.example.com/x"},
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://127.0.0.1/x",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": '["127.0.0.1"]',
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": (
                    "https://169.254.169.254/latest/meta-data"
                ),
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                    '["169.254.169.254"]'
                ),
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://[::1]/x",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": '["::1"]',
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": URL,
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://user@hooks.example.com/x",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                    '["hooks.example.com"]'
                ),
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://hooks.example.com/x#",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                    '["hooks.example.com"]'
                ),
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://paper_internal/x",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": '["paper_internal"]',
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": "https://metadata.google.internal/x",
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                    '["metadata.google.internal"]'
                ),
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_URL": URL,
                "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                    '["other.example.com"]'
                ),
            },
            {
                **base,
                "PAPER_OPS_RUNTIME_WEBHOOK_AUTH_TOKEN_FILE": "/private/token",
            },
        ]
        for environment in cases:
            with self.subTest(environment=sorted(environment)):
                with self.assertRaises(OpsRuntimeError):
                    load_runtime_config(environment)

    def test_private_token_file_is_loaded_but_never_appears_in_repr(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "webhook-token"
            path.write_text(SECRET, encoding="utf-8")
            path.chmod(0o600)
            config = load_runtime_config(
                {
                    "PAPER_OPS_RUNTIME_SERVICES_JSON": '{"svc":["worker"]}',
                    "PAPER_OPS_RUNTIME_WEBHOOK_URL": URL,
                    "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                        '["hooks.example.com"]'
                    ),
                    "PAPER_OPS_RUNTIME_WEBHOOK_AUTH_TOKEN_FILE": str(path),
                }
            )
            self.assertEqual(config.webhook.auth_token, SECRET)
            rendered = repr((config, config.webhook))
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(URL, rendered)
            self.assertNotIn(str(path), rendered)

            path.chmod(0o644)
            with self.assertRaisesRegex(
                OpsRuntimeError, "webhook_auth_token_file_invalid"
            ):
                load_runtime_config(
                    {
                        "PAPER_OPS_RUNTIME_SERVICES_JSON": '{"svc":["worker"]}',
                        "PAPER_OPS_RUNTIME_WEBHOOK_URL": URL,
                        "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON": (
                            '["hooks.example.com"]'
                        ),
                        "PAPER_OPS_RUNTIME_WEBHOOK_AUTH_TOKEN_FILE": str(path),
                    }
                )


class WebhookDispatchTests(unittest.TestCase):
    def test_disabled_webhook_never_claims_and_preserves_pending_queue(self):
        claim = Mock(side_effect=AssertionError("must not claim"))
        result = dispatch_webhook_outbox(disabled_webhook(), claim=claim)
        self.assertEqual(result.result_code, "webhook_disabled")
        self.assertEqual(result.claimed_count, 0)
        claim.assert_not_called()

    def test_dns_private_destination_is_rejected_before_socket_or_token_send(self):
        claims = iter([lease(), None])
        socket_factory = Mock(side_effect=AssertionError("must not connect"))
        nacks = []

        def open_private(request, timeout):
            return ops_runtime_module._default_open(
                request,
                timeout,
                resolver=lambda *_args: [
                    (
                        ops_runtime_module.socket.AF_INET,
                        ops_runtime_module.socket.SOCK_STREAM,
                        ops_runtime_module.socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", 443),
                    )
                ],
                socket_factory=socket_factory,
            )

        result = dispatch_webhook_outbox(
            enabled_webhook(),
            claim=lambda **_kwargs: next(claims),
            ack=lambda *_args, **_kwargs: self.fail("must not ack"),
            nack=lambda *args, **kwargs: nacks.append((args, kwargs)),
            open_request=open_private,
        )

        self.assertEqual(result.nacked_count, 1)
        self.assertEqual(
            nacks[0][1]["error_code"], "webhook_destination_not_public"
        )
        socket_factory.assert_not_called()
        self.assertNotIn(SECRET, repr((result, nacks)))

    def test_connected_peer_is_rechecked_before_tls_to_close_rebinding_gap(self):
        class ReboundSocket:
            def __init__(self):
                self.closed = False
                self.connected_to = None

            def settimeout(self, _timeout):
                return None

            def connect(self, address):
                self.connected_to = address

            def getpeername(self):
                return ("10.0.0.9", 443)

            def close(self):
                self.closed = True

        rebound = ReboundSocket()
        request = ops_runtime_module.urllib.request.Request(
            URL,
            data=b"{}",
            headers={"Authorization": f"Bearer {SECRET}"},
            method="POST",
        )
        with self.assertRaisesRegex(
            ops_runtime_module.WebhookTransportError,
            "webhook_destination_not_public",
        ):
            ops_runtime_module._default_open(
                request,
                10,
                resolver=lambda *_args: [
                    (
                        ops_runtime_module.socket.AF_INET,
                        ops_runtime_module.socket.SOCK_STREAM,
                        ops_runtime_module.socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    )
                ],
                socket_factory=lambda *_args: rebound,
            )

        self.assertEqual(rebound.connected_to, ("93.184.216.34", 443))
        self.assertTrue(rebound.closed)

    def test_https_2xx_is_bounded_and_acknowledged_with_exact_digest(self):
        item = lease()
        claims = iter([item, None])
        acknowledgements = []

        def open_request(request, timeout):
            self.assertEqual(request.full_url, URL)
            self.assertEqual(request.method, "POST")
            self.assertEqual(timeout, 10)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET}")
            self.assertEqual(json.loads(request.data), webhook_payload())
            return FakeResponse(status=204)

        result = dispatch_webhook_outbox(
            enabled_webhook(),
            claim=lambda **_kwargs: next(claims),
            ack=lambda *args, **kwargs: acknowledgements.append((args, kwargs)),
            nack=lambda *_args, **_kwargs: self.fail("must not nack"),
            open_request=open_request,
        )
        self.assertEqual(result.acknowledged_count, 1)
        self.assertEqual(result.nacked_count, 0)
        self.assertEqual(
            acknowledgements[0][1]["expected_payload_sha256"],
            item["payload_sha256"],
        )
        rendered = repr(result)
        self.assertNotIn(URL, rendered)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("reason_codes", rendered)

    def test_transport_failure_nacks_and_does_not_block_next_item(self):
        first = lease()
        second = lease(SECOND_OUTBOX_ID)
        claims = iter([first, second, None])
        opened = 0
        nacks = []
        acks = []

        def open_request(_request, _timeout):
            nonlocal opened
            opened += 1
            if opened == 1:
                raise RuntimeError(f"network detail {SECRET} /private/paper.pdf")
            return FakeResponse(status=200, body=b"ok")

        result = dispatch_webhook_outbox(
            enabled_webhook(),
            claim=lambda **_kwargs: next(claims),
            ack=lambda *args, **kwargs: acks.append((args, kwargs)),
            nack=lambda *args, **kwargs: nacks.append((args, kwargs)),
            open_request=open_request,
        )
        self.assertEqual(result.claimed_count, 2)
        self.assertEqual(result.nacked_count, 1)
        self.assertEqual(result.acknowledged_count, 1)
        self.assertEqual(nacks[0][1]["error_code"], "webhook_unavailable")
        self.assertEqual(acks[0][0][0], SECOND_OUTBOX_ID)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn("/private", repr(result))

    def test_redirect_and_oversized_response_are_never_acked(self):
        cases = [
            FakeResponse(status=200, url="https://other.example.com/redirect"),
            FakeResponse(status=200, body=b"12345", headers={"Content-Length": "5"}),
        ]
        expected_codes = ["webhook_redirect_rejected", "webhook_response_size_invalid"]
        for response, expected_code in zip(cases, expected_codes):
            with self.subTest(expected_code=expected_code):
                claims = iter([lease(), None])
                nacks = []
                result = dispatch_webhook_outbox(
                    enabled_webhook(response_bytes=4),
                    claim=lambda **_kwargs: next(claims),
                    ack=lambda *_args, **_kwargs: self.fail("must not ack"),
                    nack=lambda *args, **kwargs: nacks.append((args, kwargs)),
                    open_request=lambda _request, _timeout, response=response: response,
                )
                self.assertEqual(result.nacked_count, 1)
                self.assertEqual(nacks[0][1]["error_code"], expected_code)

    def test_non_operational_or_digest_mismatched_payload_is_never_sent(self):
        item = lease()
        item["payload"] = {**item["payload"], "paper_title": "private title"}
        claims = iter([item, None])
        network = Mock(side_effect=AssertionError("must not send"))
        result = dispatch_webhook_outbox(
            enabled_webhook(),
            claim=lambda **_kwargs: next(claims),
            ack=lambda *_args, **_kwargs: self.fail("must not ack"),
            nack=lambda *_args, **_kwargs: self.fail("must not nack unknown lease"),
            open_request=network,
        )
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.items[0].result_code, "webhook_lease_invalid")
        network.assert_not_called()
        self.assertNotIn("private title", repr(result))


class MonitorRuntimeTests(unittest.TestCase):
    def test_loop_retries_only_unprimed_services_before_any_cycle(self):
        calls = []
        attempts = {"service-a": 0, "service-b": 0}

        class ImmediateEvent(threading.Event):
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return self.is_set()

        stopper = ImmediateEvent()
        config = runtime_config(
            services=(
                ("service-a", ("worker-a",)),
                ("service-b", ("worker-b",)),
            )
        )

        def configure(_workers, *, service_id):
            attempts[service_id] += 1
            calls.append(("configure", service_id))
            if service_id == "service-a" and attempts[service_id] < 3:
                raise RuntimeError(f"transient database {SECRET}")

        def cycle(_config):
            calls.append(("cycle",))
            stopper.set()
            return MonitorCycleResult(
                service_count=2,
                successful_service_count=2,
                failed_service_count=0,
                services=(),
                webhook=WebhookBatchResult(
                    "webhook_disabled", 0, 0, 0, 0, ()
                ),
                result_code="monitor_cycle_ok",
            )

        result = run_monitor_loop(
            config,
            stop_event=stopper,
            cycle=cycle,
            configure=configure,
            emit=lambda _event: None,
        )

        self.assertEqual(attempts, {"service-a": 3, "service-b": 1})
        self.assertEqual(
            [item for item in calls if item[0] == "wait"],
            [("wait", 1), ("wait", 2), ("wait", 5)],
        )
        self.assertLess(calls.index(("wait", 2)), calls.index(("cycle",)))
        self.assertEqual(result.cycle_count, 1)
        self.assertNotIn(SECRET, repr(result))

    def test_loop_stop_event_interrupts_bootstrap_retry_without_cycle(self):
        calls = []

        class StopOnWaitEvent(threading.Event):
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                self.set()
                return True

        stopper = StopOnWaitEvent()
        result = run_monitor_loop(
            runtime_config(),
            stop_event=stopper,
            cycle=lambda _config: self.fail("cycle must not run"),
            configure=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            ),
            emit=lambda _event: None,
        )

        self.assertEqual(calls, [("wait", 1)])
        self.assertEqual(result.cycle_count, 0)
        self.assertEqual(result.result_code, "monitor_loop_stopped")

    def test_loop_primes_expected_workers_before_startup_grace(self):
        calls = []

        class ImmediateEvent(threading.Event):
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return False

        stopper = ImmediateEvent()
        config = OpsRuntimeConfig(
            services=(("paper-reader-v2", ("reader",)),),
            monitor_interval_seconds=30,
            stale_after_seconds=120,
            startup_grace_seconds=90,
            webhook=disabled_webhook(),
        )

        def configure(workers, *, service_id):
            calls.append(("configure", service_id, tuple(workers)))

        def cycle(_config):
            calls.append(("cycle",))
            stopper.set()
            return MonitorCycleResult(
                service_count=1,
                successful_service_count=1,
                failed_service_count=0,
                services=(),
                webhook=WebhookBatchResult("webhook_disabled", 0, 0, 0, 0, ()),
                result_code="monitor_cycle_ok",
            )

        run_monitor_loop(
            config,
            stop_event=stopper,
            cycle=cycle,
            configure=configure,
            emit=lambda _event: None,
        )
        self.assertEqual(
            calls[:3],
            [
                ("configure", "paper-reader-v2", ("reader",)),
                ("wait", 90),
                ("cycle",),
            ],
        )

    def test_startup_configuration_is_once_and_failure_isolated(self):
        configured = []
        events = []
        config = runtime_config(
            services=(
                ("service-a", ("worker-a",)),
                ("service-b", ("worker-b",)),
            )
        )

        def configure(workers, *, service_id):
            configured.append((service_id, tuple(workers)))
            if service_id == "service-a":
                raise RuntimeError(f"database {SECRET} /private/db")

        result = configure_monitor_expected_workers(
            config,
            configure=configure,
            emit=events.append,
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].result_code, "monitor_service_prime_failed")
        self.assertEqual(result[1].result_code, "monitor_service_primed")
        self.assertEqual(len(configured), 2)
        self.assertNotIn(SECRET, repr((result, events)))

    def test_service_failure_is_isolated_and_webhook_still_runs(self):
        services = (
            ("service-a", ("worker-a",)),
            ("service-b", ("worker-b",)),
        )
        checked = []
        delivered = []

        def health(*, evaluated_at, stale_after_seconds, service_id):
            checked.append(service_id)
            if service_id == "service-a":
                raise RuntimeError(f"database {SECRET} /private/db")
            return build_worker_health_snapshot(
                [
                    worker_heartbeat(
                        worker_id="worker-b",
                        observed_at=evaluated_at,
                        sequence=1,
                        state="idle",
                    )
                ],
                expected_worker_ids=["worker-b"],
                evaluated_at=evaluated_at,
                stale_after_seconds=stale_after_seconds,
                service_id=service_id,
            )

        result = run_monitor_cycle(
            runtime_config(services=services),
            evaluated_at="2026-08-29T10:00:00Z",
            health=health,
            record=lambda _snapshot: {"events": []},
            webhook_dispatch=lambda _config: (
                delivered.append(True)
                or WebhookBatchResult("webhook_disabled", 0, 0, 0, 0, ())
            ),
            emit=lambda _event: None,
        )
        self.assertEqual(result.successful_service_count, 1)
        self.assertEqual(result.failed_service_count, 1)
        self.assertEqual(result.services[0].result_code, "monitor_service_failed")
        self.assertEqual(result.services[1].result_code, "monitor_healthy")
        self.assertEqual(checked, ["service-a", "service-b"])
        self.assertEqual(delivered, [True])
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn("/private", repr(result))

    def test_webhook_dispatch_failure_does_not_discard_service_result(self):
        result = run_monitor_cycle(
            runtime_config(),
            evaluated_at="2026-08-29T10:00:00Z",
            health=lambda **kwargs: build_worker_health_snapshot(
                [
                    worker_heartbeat(
                        worker_id="reader",
                        observed_at=kwargs["evaluated_at"],
                        sequence=1,
                        state="idle",
                    )
                ],
                expected_worker_ids=["reader"],
                evaluated_at=kwargs["evaluated_at"],
                stale_after_seconds=kwargs["stale_after_seconds"],
                service_id=kwargs["service_id"],
            ),
            record=lambda _snapshot: {"events": []},
            webhook_dispatch=lambda _config: (_ for _ in ()).throw(
                RuntimeError(SECRET)
            ),
            emit=lambda _event: None,
        )
        self.assertEqual(result.successful_service_count, 1)
        self.assertEqual(result.webhook.result_code, "webhook_dispatch_failed")
        self.assertEqual(result.result_code, "monitor_cycle_partial")
        self.assertNotIn(SECRET, repr(result))

    def test_loop_is_immediate_strict_and_stops_without_sleeping_full_interval(self):
        stopper = threading.Event()
        calls = []

        def cycle(_config):
            calls.append(True)
            stopper.set()
            return MonitorCycleResult(
                service_count=1,
                successful_service_count=1,
                failed_service_count=0,
                services=(
                    ServiceMonitorResult("paper-reader-v2", 1, 1, 0, "monitor_healthy"),
                ),
                webhook=WebhookBatchResult("webhook_disabled", 0, 0, 0, 0, ()),
                result_code="monitor_cycle_ok",
            )

        result = run_monitor_loop(
            runtime_config(),
            stop_event=stopper,
            cycle=cycle,
            configure=lambda *_args, **_kwargs: None,
            emit=lambda _event: None,
        )
        self.assertEqual(calls, [True])
        self.assertEqual(result.cycle_count, 1)
        self.assertEqual(result.failed_cycle_count, 0)
        self.assertEqual(result.result_code, "monitor_loop_stopped")

    def test_runtime_entrypoint_migrates_then_runs_validated_configuration(self):
        config = runtime_config()
        with (
            patch.object(ops_runtime_module, "ensure_schema") as ensure,
            patch.object(
                ops_runtime_module,
                "load_runtime_config",
                return_value=config,
            ) as load,
            patch.object(ops_runtime_module, "run_monitor_loop") as loop,
        ):
            ops_runtime_module.run_runtime()
        ensure.assert_called_once_with()
        load.assert_called_once_with()
        loop.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
