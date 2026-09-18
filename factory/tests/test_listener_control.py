import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from dotfactory.cli import main
from dotfactory.listener_control import ListenerControlError, manage_listener


class Response:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class ListenerControlTests(unittest.TestCase):
    def test_status_allowlists_output_and_uses_bearer_token(self):
        payload = {
            "id": "srv-listener123", "name": "dotfactory-linear-listener",
            "url": "https://listener.example", "suspended": "not_suspended",
            "updatedAt": "2026-09-17T10:00:00Z",
            "secret": "must-not-escape",
        }
        with patch(
            "dotfactory.listener_control._urlopen",
            return_value=Response(json.dumps(payload).encode()),
        ) as open_url:
            receipt = manage_listener(
                "status", service_id="srv-listener123",
                environment={"RENDER_API_KEY": "render-secret"},
            )
        request = open_url.call_args.args[0]
        self.assertEqual("GET", request.method)
        self.assertEqual(
            "https://api.render.com/v1/services/srv-listener123",
            request.full_url,
        )
        self.assertEqual("Bearer render-secret", request.get_header("Authorization"))
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertEqual("not_suspended", receipt["service"]["suspended"])

    def test_suspend_and_resume_post_without_changing_disk(self):
        for action in ("suspend", "resume"):
            with self.subTest(action=action), patch(
                "dotfactory.listener_control._urlopen",
                return_value=Response(status=202),
            ) as open_url:
                receipt = manage_listener(
                    action, service_id="srv-listener123",
                    environment={"RENDER_API_KEY": "render-secret"},
                )
            request = open_url.call_args.args[0]
            self.assertEqual("POST", request.method)
            self.assertEqual(
                f"https://api.render.com/v1/services/srv-listener123/{action}",
                request.full_url,
            )
            self.assertEqual("unchanged", receipt["disk_action"])
            self.assertTrue(receipt["accepted"])

    def test_requires_valid_service_id_and_token_before_network(self):
        with patch(
            "dotfactory.listener_control._urlopen"
        ) as open_url:
            for environment, service_id, message in (
                ({"RENDER_API_KEY": "secret"}, None, "service ID"),
                ({"RENDER_API_KEY": "secret"}, "other-123", "start with srv-"),
                ({}, "srv-listener123", "RENDER_API_KEY is required"),
            ):
                with self.subTest(message=message), self.assertRaisesRegex(
                    ListenerControlError, message
                ):
                    manage_listener(
                        "status", service_id=service_id, environment=environment
                    )
            open_url.assert_not_called()

    def test_http_error_is_bounded_and_does_not_echo_body_or_token(self):
        error = urllib.error.HTTPError(
            "https://api.render.com", 401, "unauthorized render-secret",
            {}, io.BytesIO(b'{"message":"render-secret"}'),
        )
        with patch(
            "dotfactory.listener_control._urlopen",
            side_effect=error,
        ), self.assertRaisesRegex(ListenerControlError, "HTTP 401") as raised:
            manage_listener(
                "status", service_id="srv-listener123",
                environment={"RENDER_API_KEY": "render-secret"},
            )
        self.assertNotIn("render-secret", str(raised.exception))

    def test_rejects_redirects_and_invalid_timeout(self):
        handler = __import__(
            "dotfactory.listener_control", fromlist=["_RejectRedirects"]
        )._RejectRedirects()
        self.assertIsNone(handler.redirect_request())
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                ListenerControlError, "positive finite"
            ):
                manage_listener(
                    "status", service_id="srv-listener123", timeout=timeout,
                    environment={"RENDER_API_KEY": "render-secret"},
                )

    def test_status_rejects_wrong_service_record(self):
        payload = json.dumps({"id": "srv-other123"}).encode()
        with patch(
            "dotfactory.listener_control._urlopen",
            return_value=Response(payload),
        ), self.assertRaisesRegex(ListenerControlError, "wrong service"):
            manage_listener(
                "status", service_id="srv-listener123",
                environment={"RENDER_API_KEY": "render-secret"},
            )

    def test_cli_routes_listener_command_and_prints_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "dotfactory.listener_control.manage_listener",
            return_value={"action": "suspend", "accepted": True},
        ) as manage, redirect_stdout(stdout), redirect_stderr(stderr):
            result = main([
                "listener", "suspend", "--service-id", "srv-listener123",
                "--token-env", "CUSTOM_RENDER_TOKEN", "--timeout", "4",
            ])
        self.assertEqual(0, result)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual("suspend", json.loads(stdout.getvalue())["action"])
        manage.assert_called_once_with(
            "suspend", service_id="srv-listener123",
            token_env="CUSTOM_RENDER_TOKEN", timeout=4.0,
        )

    def test_cli_reports_safe_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "dotfactory.listener_control.manage_listener",
            side_effect=ListenerControlError("RENDER_API_KEY is required"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["listener", "status", "--service-id", "srv-listener123"])
        self.assertEqual(1, result)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("RENDER_API_KEY is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
