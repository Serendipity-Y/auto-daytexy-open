"""Offline smoke coverage for the public reporting and workbench entry points."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import fxiaoke_remote_workorder

from apps.reports import daily_report, weekly_report
from apps.workbench.webapp.app import create_app
from apps.workbench.webapp.config import WebConfig
from integrations.fxiaoke import crm_enabled
from integrations.fxiaoke.tenant import require_service_owner, tenant_value


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "daily-report.md"
BRIEF = (
    "客户：示例实验室；销售：示例联系人；是否外勤：否；"
    "事项：连接偶尔断开；完成：更换网线后暂未复现；"
    "处理结果：进行中；明日：继续观察"
)


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_daily_preview_and_save_without_crm(self):
        report = self.root / "日报.md"
        memory = self.root / "sales.json"
        args = ["--no-clipboard", "--path", str(report), BRIEF]
        with (
            patch.dict(os.environ, {"FXIAOKE_ENABLED": "", "DAILY_REPORT_SALES_MEMORY_PATH": str(memory)}),
            patch.object(daily_report, "get_api_key", return_value=None),
            patch.object(daily_report, "build_fxiaoke_pending_alert") as pending,
            patch.object(daily_report, "sync_fxiaoke_after_save_fast") as crm,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(daily_report.main(["--preview-only", *args]), 0)
            self.assertIn("暂未复现", output.getvalue())
            self.assertFalse(report.exists())
            self.assertEqual(daily_report.main(["-y", *args]), 0)
            pending.assert_not_called()
            crm.assert_not_called()
        self.assertIn("处理结果：进行中", report.read_text(encoding="utf-8"))

    def test_daily_sync_requires_opt_in(self):
        report = self.root / "日报.md"
        with (
            patch.dict(os.environ, {"FXIAOKE_ENABLED": ""}),
            patch.object(daily_report, "run_fxiaoke_subprocess") as crm,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertFalse(crm_enabled())
            self.assertNotEqual(daily_report.main(["--fxiaoke-login-check", "--path", str(report)]), 0)
            self.assertIn("FXIAOKE_ENABLED=1", output.getvalue())
            crm.assert_not_called()

    def test_weekly_preview_with_a_mock_model(self):
        report = self.root / "日报.md"
        week = self.root / "周报.md"
        first_day = weekly_report.get_report_week_days()[0].strftime("%Y.%m.%d")
        sample = EXAMPLE.read_text(encoding="utf-8").split("\n---\n")[0]
        report.write_text(sample.replace("2026.11.02", first_day), encoding="utf-8")
        message = SimpleNamespace(content="本周总结：\n完成示例实验室连接排查。")
        result = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)])
        argv = [
            "--preview-only", "--no-clipboard", "--daily-path", str(report),
            "--path", str(week), "--reflection", "继续复测", "归档排查记录",
        ]
        with (
            patch.object(weekly_report, "get_weekly_api_key", return_value="test"),
            patch.object(weekly_report, "OpenAI"),
            patch.object(weekly_report, "request_chat_completion", return_value=result) as model,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(weekly_report.main(argv), 0)
            request = model.call_args.args[1]["messages"][0]["content"]
            self.assertIn("示例实验室", request)
            self.assertIn("继续复测", output.getvalue())
            self.assertFalse(week.exists())

    def test_remote_form_requires_local_settings_before_browser_actions(self):
        config = self.root / "tenant.json"
        config.write_text("{}", encoding="utf-8")
        page = Mock()
        with patch.dict(os.environ, {"FXIAOKE_CRM_CONFIG_PATH": str(config), "FXIAOKE_SERVICE_OWNER": ""}):
            with self.assertRaisesRegex(RuntimeError, "FXIAOKE_SERVICE_OWNER"):
                require_service_owner()
            with self.assertRaisesRegex(RuntimeError, "remote_workorder.record_type"):
                fxiaoke_remote_workorder.open_new_remote_workorder_form(page)
        self.assertEqual(page.mock_calls, [])

    def test_reads_synthetic_tenant_values(self):
        config = self.root / "tenant.json"
        config.write_text('{"remote_workorder":{"record_type":"synthetic_type"}}', encoding="utf-8")
        with patch.dict(os.environ, {
            "FXIAOKE_CRM_CONFIG_PATH": str(config),
            "FXIAOKE_SERVICE_OWNER": "示例负责人",
        }):
            self.assertEqual(require_service_owner(), "示例负责人")
            record_type = tenant_value("remote_workorder", "record_type", required=True, token=True)
            self.assertEqual(record_type, "synthetic_type")

    def test_workbench_indexes_fixture_and_blocks_crm_by_default(self):
        report = self.root / "日报.md"
        report.write_bytes(EXAMPLE.read_bytes())
        config = WebConfig(ROOT, report, self.root / "workbench.sqlite3")
        with patch.dict(os.environ, {"FXIAOKE_ENABLED": ""}):
            app = create_app(config)
            client = app.test_client()
            self.assertTrue(client.get("/healthz").json["ok"])
            calendar = client.get("/?month=2026-11")
            self.assertIn("/days/2026-11-02", calendar.get_data(as_text=True))
            day = client.get("/days/2026-11-02")
            self.assertIn("示例实验室", day.get_data(as_text=True))
            response = client.post(
                "/days/2026-11-02/verify",
                headers={"X-Local-Request-Token": app.config["LOCAL_REQUEST_TOKEN"]},
            )
            self.assertGreaterEqual(response.status_code, 400)
            self.assertIn("FXIAOKE_ENABLED=1", str(response.get_json()))


if __name__ == "__main__":
    unittest.main()
