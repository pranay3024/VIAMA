import base64
import os
import unittest
from datetime import date, datetime
from email import message_from_bytes
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from flask import Flask

from extensions import db
from models.db_models import Survey
import google_drive
import utils.approved_survey_email as approved_email
import utils.approved_survey_defect_email as defect_email
import utils.defect_report_api as defect_api
import utils.microsoft_graph_mail as graph_mail
import utils.raw_xlsx as raw_xlsx
import utils.received_timestamps as received_timestamps
from routes.sync import sync_bp


class ApprovedSurveyEmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        db.init_app(cls.app)

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def make_survey(self, dates_approved=True):
        return SimpleNamespace(
            id=42,
            raw_video_email_sent_at=None,
            raw_video_email_message_id=None,
            defect_report_email_sent_at=None,
            defect_report_email_message_id=None,
            survey_dates_approved=dates_approved,
            survey_form_approved=True,
            extracted_survey_start_date=date(2026, 10, 1),
            extracted_survey_end_date=date(2026, 10, 2),
            end_survey_pdf="https://drive.google.com/file/d/abc123/view",
            start_time=datetime(2026, 10, 1),
            upc_code="TEST-UPC",
            section_no="S-1",
            cycle_no=1,
            stretch_code="Test Stretch",
            survey_type="Day",
            nh_number="NH-1",
            piu="PIU",
            ro="RO",
        )

    def make_query(self, survey):
        query = MagicMock()
        query.filter_by.return_value.with_for_update.return_value.first.return_value = survey
        return query

    def test_email_waits_until_both_approvals_are_yes(self):
        survey = self.make_survey(dates_approved=False)
        with (
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(approved_email.db.session, "rollback"),
            patch.object(approved_email, "send_microsoft_graph_email") as send,
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "waiting_for_approval")
        send.assert_not_called()

    def test_approved_email_sends_survey_pdf_and_raw_xlsx(self):
        survey = self.make_survey()
        with (
            patch.dict(os.environ, {"DEFECT_REPORT_EMAIL": "0"}),
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(approved_email, "download_file_from_drive", return_value=b"%PDF"),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                return_value=[{"label": "S-1", "cycle": 1, "dn": ""}],
            ),
            patch.object(
                approved_email,
                "raw_xlsx_is_listed",
                return_value=True,
            ),
            patch.object(
                approved_email,
                "download_raw_xlsx",
                return_value=(b"PK-xlsx", "Raw_Video_S-1_Cycle1.xlsx"),
            ),
            patch.object(
                approved_email,
                "send_microsoft_graph_email",
                return_value="graph-id",
            ) as send,
            patch.object(
                defect_email,
                "send_approved_survey_defect_email",
                return_value="waiting_for_defect_report",
            ) as defect_send,
            patch.object(approved_email.db.session, "commit"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "sent")
        self.assertEqual(send.call_args.kwargs["to_email"], "mail.hunterrr@gmail.com")
        self.assertEqual(send.call_args.kwargs["from_email"], "projects9@adorpower.com")
        self.assertEqual(send.call_args.kwargs["attachment_bytes"], b"%PDF")
        self.assertTrue(send.call_args.kwargs["attachment_filename"].endswith(".pdf"))
        self.assertEqual(
            send.call_args.kwargs["additional_attachments"],
            [(b"PK-xlsx", "Raw_Video_S-1_Cycle1.xlsx")],
        )
        self.assertEqual(survey.raw_video_email_message_id, "graph-id")
        # The defect-report mail is switched off by default.
        defect_send.assert_not_called()

    def test_approved_email_chains_defect_email_when_switched_on(self):
        survey = self.make_survey()
        with (
            patch.dict(os.environ, {"DEFECT_REPORT_EMAIL": "1"}),
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(approved_email, "download_file_from_drive", return_value=b"%PDF"),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                return_value=[{"label": "S-1", "cycle": 1, "dn": ""}],
            ),
            patch.object(
                approved_email,
                "raw_xlsx_is_listed",
                return_value=True,
            ),
            patch.object(
                approved_email,
                "download_raw_xlsx",
                return_value=(b"PK-xlsx", "Raw_Video_S-1_Cycle1.xlsx"),
            ),
            patch.object(
                approved_email,
                "send_microsoft_graph_email",
                return_value="graph-id",
            ),
            patch.object(
                defect_email,
                "send_approved_survey_defect_email",
                return_value="sent",
            ) as defect_send,
            patch.object(approved_email.db.session, "commit"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "sent")
        defect_send.assert_called_once_with(survey.id)

    def test_defect_email_waits_until_raw_email_was_sent(self):
        survey = self.make_survey()
        with (
            patch.object(Survey, "query", self.make_query(survey)),
            patch.object(defect_email, "list_defect_report_files") as inventory,
            patch.object(defect_email.db.session, "rollback"),
        ):
            result = defect_email.send_approved_survey_defect_email(survey.id)

        self.assertEqual(result, "waiting_for_raw_email")
        inventory.assert_not_called()

    def test_defect_email_sends_matching_report_using_defect_template(self):
        survey = self.make_survey()
        survey.raw_video_email_sent_at = datetime(2026, 10, 16)
        report = {"label": "S-1", "cycle": 1, "dn": "day"}
        with (
            patch.object(Survey, "query", self.make_query(survey)),
            patch.object(defect_email, "list_defect_report_files", return_value=[report]),
            patch.object(defect_email, "find_defect_report_file", return_value=report),
            patch.object(
                defect_email,
                "download_defect_report",
                return_value=(b"PK-defect", "Defect_Report_S-1_Cycle1_Day.xlsx"),
            ),
            patch.object(
                defect_email,
                "send_microsoft_graph_email",
                return_value="defect-graph-id",
            ) as send,
            patch.object(defect_email.db.session, "commit"),
        ):
            result = defect_email.send_approved_survey_defect_email(survey.id)

        self.assertEqual(result, "sent")
        self.assertEqual(send.call_args.kwargs["to_email"], "mail.hunterrr@gmail.com")
        self.assertEqual(send.call_args.kwargs["from_email"], "projects9@adorpower.com")
        self.assertIn("Submission of Survey Report", send.call_args.kwargs["subject"])
        self.assertEqual(send.call_args.kwargs["attachment_bytes"], b"PK-defect")
        self.assertEqual(survey.defect_report_email_message_id, "defect-graph-id")

    def test_defect_download_requests_day_report_with_matching_cycle(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"PK-defect"
        with (
            patch.dict(os.environ, {"RV_DEFECT_REPORT_API_KEY": "defect-key"}),
            patch.object(defect_api, "urlopen", return_value=response) as open_url,
        ):
            content, filename = defect_api.download_defect_report(
                "N0908601002UP", 7, "Day"
            )

        request = open_url.call_args.args[0]
        self.assertEqual(content, b"PK-defect")
        self.assertEqual(
            filename,
            "Defect_Report_N0908601002UP_Cycle7_Day.xlsx",
        )
        self.assertEqual(
            parse_qs(urlparse(request.full_url).query),
            {"stretch": ["N0908601002UP"], "cycle": ["7"], "dn": ["day"]},
        )
        self.assertEqual(request.get_header("X-api-key"), "defect-key")

    def test_pending_defect_processor_sends_only_matching_inventory_rows(self):
        query = MagicMock()
        query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (1900, "N/09086/01002/UP", "N/09086/01002/UP", 7, "Day"),
        ]
        report = {"label": "N0908601002UP", "cycle": 7, "dn": "day"}
        with (
            patch.object(defect_email.db.session, "query", return_value=query),
            patch.object(defect_email, "list_defect_report_files", return_value=[report]),
            patch.object(defect_email, "find_defect_report_file", return_value=report),
            patch.object(
                defect_email,
                "send_approved_survey_defect_email",
                return_value="sent",
            ) as send,
        ):
            result = defect_email.process_pending_approved_defect_emails(limit=20)

        self.assertEqual(
            result,
            {"checked": 1, "sent": 1, "waiting_for_defect_report": 0, "failed": 0},
        )
        send.assert_called_once_with(1900, inventory=[report])

    def test_email_waits_without_error_when_raw_xlsx_is_not_listed(self):
        survey = self.make_survey()
        with (
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                return_value=[],
            ),
            patch.object(approved_email, "download_file_from_drive") as download_pdf,
            patch.object(approved_email, "send_microsoft_graph_email") as send,
            patch.object(approved_email.db.session, "rollback"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "waiting_for_raw_xlsx")
        download_pdf.assert_not_called()
        send.assert_not_called()

    def test_email_waits_without_error_when_inventory_is_unavailable(self):
        survey = self.make_survey()
        with (
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                side_effect=raw_xlsx.RawXlsxUnavailable("API unavailable"),
            ),
            patch.object(approved_email, "send_microsoft_graph_email") as send,
            patch.object(approved_email.db.session, "rollback"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "waiting_for_raw_xlsx")
        send.assert_not_called()

    def test_pending_processor_skips_missing_sheet_and_sends_ready_survey(self):
        query = MagicMock()
        query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (41, "124", "124", 2, "Day"),
            (42, "N/09086/01002/UP", "N/09086/01002/UP", 7, "Day"),
        ]
        with (
            patch.object(approved_email.db.session, "query", return_value=query),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                return_value=[{"label": "N0908601002UP", "cycle": 7, "dn": ""}],
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.process_pending_approved_survey_emails(limit=1)

        self.assertEqual(
            result,
            {"checked": 1, "sent": 1, "waiting_for_raw_xlsx": 1, "failed": 0},
        )
        send.assert_called_once_with(42, inventory=[
            {"label": "N0908601002UP", "cycle": 7, "dn": ""},
        ])

    def test_email_waits_without_error_when_raw_xlsx_download_is_unavailable(self):
        survey = self.make_survey()
        with (
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(
                approved_email,
                "list_raw_xlsx_files",
                return_value=[{"label": "S-1", "cycle": 1, "dn": ""}],
            ),
            patch.object(approved_email, "raw_xlsx_is_listed", return_value=True),
            patch.object(approved_email, "download_file_from_drive", return_value=b"%PDF"),
            patch.object(
                approved_email,
                "download_raw_xlsx",
                side_effect=raw_xlsx.RawXlsxUnavailable("API down"),
            ),
            patch.object(approved_email, "send_microsoft_graph_email") as send,
            patch.object(approved_email.db.session, "rollback"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "waiting_for_raw_xlsx")
        send.assert_not_called()

    def test_raw_xlsx_maps_section_cycle_and_night_type(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"PK-xlsx"
        with (
            patch.dict(os.environ, {"RV_RAW_XLSX_API_KEY": "test-key"}),
            patch.object(raw_xlsx, "urlopen", return_value=response) as open_url,
        ):
            content, filename = raw_xlsx.download_raw_xlsx("140", 14, "Night")

        request = open_url.call_args.args[0]
        self.assertEqual(content, b"PK-xlsx")
        self.assertEqual(filename, "Raw_Video_140_Cycle14_Night.xlsx")
        self.assertEqual(
            parse_qs(urlparse(request.full_url).query),
            {"stretch": ["140"], "cycle": ["14"], "dn": ["night"]},
        )
        self.assertEqual(request.get_header("X-api-key"), "test-key")

    def test_raw_xlsx_inventory_matches_normalized_upc_and_cycle(self):
        files = [
            {"label": "N0908601002UP", "cycle": 7, "dn": ""},
            {"label": "124", "cycle": 2, "dn": ""},
        ]

        self.assertTrue(
            raw_xlsx.raw_xlsx_is_listed(
                "N/09086/01002/UP", "N/09086/01002/UP", 7, "Day", files
            )
        )
        self.assertFalse(
            raw_xlsx.raw_xlsx_is_listed(
                "N/09086/01002/UP", "N/09086/01002/UP", 6, "Day", files
            )
        )
        self.assertFalse(
            raw_xlsx.raw_xlsx_is_listed(
                "N/09086/01002/UP", "N/09086/01002/UP", 7, "Night", files
            )
        )

    def test_defect_inventory_matches_section_upc_cycle_and_day_night(self):
        files = [
            {"label": "N0908601002UP", "cycle": 7, "dn": "day"},
            {"label": "124", "cycle": 2, "dn": "night"},
        ]

        self.assertEqual(
            defect_api.find_defect_report_file(
                "N/09086/01002/UP", "N/09086/01002/UP", 7, "Day", files
            ),
            files[0],
        )
        self.assertEqual(
            defect_api.find_defect_report_file("124", "124", 2, "Night", files),
            files[1],
        )
        self.assertIsNone(
            defect_api.find_defect_report_file("124", "124", 2, "Day", files)
        )

    def test_graph_sender_uses_app_token_and_both_attachments(self):
        token_response = MagicMock()
        token_response.json.return_value = {"access_token": "test-access-token"}
        send_response = MagicMock(status_code=202, headers={"request-id": "request-1"})
        with (
            patch.dict(
                os.environ,
                {
                    "M365_TENANT_ID": "tenant-id",
                    "M365_CLIENT_ID": "client-id",
                    "M365_CLIENT_SECRET": "test-secret",
                },
            ),
            patch.object(
                graph_mail.requests,
                "post",
                side_effect=[token_response, send_response],
            ) as post,
        ):
            message_id = graph_mail.send_microsoft_graph_email(
                to_email="mail.hunterrr@gmail.com",
                from_email="projects9@adorpower.com",
                subject="Approved survey",
                html_body="<p>Body</p>",
                attachment_bytes=b"%PDF",
                attachment_filename="form.pdf",
                additional_attachments=[(b"PK-xlsx", "raw.xlsx")],
                message_id="<viama-survey-42@viama.local>",
            )

        self.assertEqual(message_id, "<viama-survey-42@viama.local>")
        self.assertEqual(post.call_count, 2)
        token_request = post.call_args_list[0]
        self.assertEqual(
            token_request.kwargs["data"]["scope"],
            "https://graph.microsoft.com/.default",
        )
        send_request = post.call_args_list[1]
        self.assertIn("projects9%40adorpower.com/sendMail", send_request.args[0])
        self.assertEqual(
            send_request.kwargs["headers"]["Authorization"],
            "Bearer test-access-token",
        )
        message = send_request.kwargs["json"]["message"]
        self.assertEqual(
            message["toRecipients"][0]["emailAddress"]["address"],
            "mail.hunterrr@gmail.com",
        )
        self.assertEqual(
            [item["name"] for item in message["attachments"]],
            ["form.pdf", "raw.xlsx"],
        )
        self.assertEqual(
            message["internetMessageHeaders"][0]["name"],
            "X-VIAMA-Message-ID",
        )

    def test_graph_403_includes_server_error_details(self):
        token_response = MagicMock(ok=True)
        token_response.json.return_value = {"access_token": "test-access-token"}
        graph_response = MagicMock(
            ok=False,
            status_code=403,
            headers={"request-id": "graph-request-id"},
        )
        graph_response.json.return_value = {
            "error": {
                "code": "ErrorAccessDenied",
                "message": "Access is denied.",
            }
        }
        with (
            patch.dict(
                os.environ,
                {
                    "M365_TENANT_ID": "tenant-id",
                    "M365_CLIENT_ID": "client-id",
                    "M365_CLIENT_SECRET": "test-secret",
                },
            ),
            patch.object(
                graph_mail.requests,
                "post",
                side_effect=[token_response, graph_response],
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "ErrorAccessDenied.*Access is denied.*graph-request-id",
            ):
                graph_mail.send_microsoft_graph_email(
                    to_email="mail.hunterrr@gmail.com",
                    from_email="projects9@adorpower.com",
                    subject="Subject",
                    html_body="<p>Body</p>",
                    attachment_bytes=b"%PDF",
                    attachment_filename="form.pdf",
                )

    def test_gmail_message_has_no_cc_or_bcc(self):
        gmail = MagicMock()
        gmail.users.return_value.messages.return_value.send.return_value.execute.return_value = {
            "id": "gmail-id"
        }
        with patch.object(google_drive, "get_gmail", return_value=gmail):
            message_id = google_drive.send_gmail_email(
                to_email="ishit1561@gmail.com",
                from_email="adordashcam@gmail.com",
                subject="Subject",
                html_body="<p>Body</p>",
                attachment_bytes=b"%PDF",
                attachment_filename="form.pdf",
            )

        raw = gmail.users.return_value.messages.return_value.send.call_args.kwargs[
            "body"
        ]["raw"]
        message = message_from_bytes(base64.urlsafe_b64decode(raw.encode()))
        self.assertEqual(message_id, "gmail-id")
        self.assertEqual(message["To"], "ishit1561@gmail.com")
        self.assertEqual(message["From"], "adordashcam@gmail.com")
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])
        self.assertTrue(any(part.get_filename() == "form.pdf" for part in message.walk()))

    def test_gmail_send_sets_excel_mimetype_for_second_attachment(self):
        gmail = MagicMock()
        gmail.users.return_value.messages.return_value.send.return_value.execute.return_value = {
            "id": "gmail-id"
        }
        with patch.object(google_drive, "get_gmail", return_value=gmail):
            google_drive.send_gmail_email(
                to_email="mail.hunterrr@gmail.com",
                from_email="projects9@adorpower.com",
                subject="Subject",
                html_body="<p>Body</p>",
                attachment_bytes=b"%PDF",
                attachment_filename="form.pdf",
                additional_attachments=[(b"PK-xlsx", "raw.xlsx")],
            )

        raw = gmail.users.return_value.messages.return_value.send.call_args.kwargs[
            "body"
        ]["raw"]
        message = message_from_bytes(base64.urlsafe_b64decode(raw.encode()))
        parts = {
            part.get_filename(): part
            for part in message.walk()
            if part.get_filename()
        }
        self.assertEqual(parts["form.pdf"].get_content_type(), "application/pdf")
        self.assertEqual(
            parts["raw.xlsx"].get_content_type(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_gmail_send_reconciles_connection_abort_without_resending(self):
        gmail = MagicMock()
        send_request = gmail.users.return_value.messages.return_value.send.return_value
        send_request.execute.side_effect = ConnectionAbortedError(
            "simulated connection abort"
        )
        search_request = gmail.users.return_value.messages.return_value.list.return_value
        search_request.execute.side_effect = [
            {"messages": []},
            {"messages": [{"id": "gmail-id"}]},
        ]
        with (
            patch.object(google_drive, "get_gmail", return_value=gmail),
        ):
            message_id = google_drive.send_gmail_email(
                to_email="ishit1561@gmail.com",
                from_email="adordashcam@gmail.com",
                subject="Subject",
                html_body="<p>Body</p>",
                attachment_bytes=b"%PDF",
                attachment_filename="form.pdf",
                message_id="<viama-survey-42@viama.local>",
            )

        self.assertEqual(message_id, "gmail-id")
        self.assertEqual(send_request.execute.call_count, 1)
        self.assertEqual(search_request.execute.call_count, 2)


class InstantRawEmailPickupTests(unittest.TestCase):
    """The dashboard pickup that mails a file the moment it lands."""

    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        db.init_app(cls.app)

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def candidates(self, count=1):
        return [
            (100 + index, "S-1", "S-1", 1 + index, "Day") for index in range(count)
        ]

    def test_pickup_sends_the_email_when_the_file_has_landed(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(1),
            ),
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=False,
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                return_value={
                    "raw": [{"label": "S-1", "cycle": 1, "dn": ""}],
                    "defect": [],
                },
            ),
            patch.object(received_timestamps, "record_received_timestamps"),
            patch.object(approved_email, "raw_xlsx_is_listed", return_value=True),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["sent"], 1)
        send.assert_called_once()

    def test_pickup_sends_at_most_one_email_per_load(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(3),
            ),
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=False,
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                return_value={
                    "raw": [{"label": "S-1", "cycle": 1, "dn": ""}],
                    "defect": [],
                },
            ),
            patch.object(received_timestamps, "record_received_timestamps"),
            patch.object(approved_email, "raw_xlsx_is_listed", return_value=True),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["sent"], 1)
        self.assertEqual(send.call_count, 1)

    def test_pickup_does_not_call_the_api_when_nothing_is_pending(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=[],
            ),
            patch.object(
                defect_email,
                "defect_candidates_pending",
                return_value=False,
            ),
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=False,
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
            ) as inventory,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["sent"], 0)
        inventory.assert_not_called()

    def test_pickup_never_raises_when_the_inventory_api_is_broken(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(1),
            ),
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=False,
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                side_effect=RuntimeError("RV_RAW_XLSX_API_KEY is not configured"),
            ),
            patch.object(approved_email.db.session, "rollback"),
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["sent"], 0)
        self.assertIn("skipped", result)

    def test_pickup_stands_down_when_another_pickup_is_running(self):
        with (
            patch("core.engine._job_lock", return_value=False),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(1),
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["sent"], 0)
        self.assertIn("skipped", result)
        send.assert_not_called()

    def test_tick_job_reports_pending_without_sending_in_dry_run(self):
        from core.endpoints import _raw_emails

        with (
            patch.object(
                approved_email,
                "pending_raw_email_count",
                return_value=26,
            ),
            patch.object(
                approved_email,
                "send_pending_raw_emails_now",
            ) as pickup,
        ):
            result = _raw_emails(True)

        self.assertEqual(result, {"skipped": "dry_run", "pending": 26})
        pickup.assert_not_called()

    def test_pickup_mails_the_defect_report_when_the_switch_is_on(self):
        with (
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=True,
            ),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=[],
            ),
            patch.object(
                defect_email,
                "defect_candidates_pending",
                return_value=True,
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                return_value={"raw": [], "defect": [{"label": "S-1", "cycle": 1}]},
            ),
            patch.object(received_timestamps, "record_received_timestamps"),
            patch.object(
                defect_email,
                "process_pending_approved_defect_emails",
                return_value={"checked": 1, "sent": 1, "failed": 0},
            ) as defect_send,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertEqual(result["defect_report_emails"]["sent"], 1)
        defect_send.assert_called_once()

    def test_pickup_leaves_the_defect_report_alone_when_the_switch_is_off(self):
        with (
            patch.object(
                approved_email,
                "defect_report_email_enabled",
                return_value=False,
            ),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(1),
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                return_value={"raw": [], "defect": []},
            ),
            patch.object(received_timestamps, "record_received_timestamps"),
            patch.object(
                defect_email,
                "process_pending_approved_defect_emails",
            ) as defect_send,
        ):
            result = approved_email.send_pending_raw_emails_now()

        self.assertIn("skipped", result["defect_report_emails"])
        defect_send.assert_not_called()


class UploadCallbackTests(unittest.TestCase):
    """RoadVision pushes the upload; the portal mails it straight away."""

    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        db.init_app(cls.app)

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def candidates(self):
        return [
            (100, "N/09086/01002/UP", "N/09086/01002/UP", 7, "Day"),
            (101, "140", "140", 14, "Night"),
        ]

    def test_upload_entry_mails_the_matching_survey_only(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(),
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_raw_emails_for_inventory_items(
                [{"label": "N/09086/01002/UP", "cycle": 7, "dn": "day"}]
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["sent"], 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], 100)

    def test_upload_entry_that_matches_nothing_sends_nothing(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(),
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_raw_emails_for_inventory_items(
                [{"label": "999", "cycle": 3, "dn": "day"}]
            )

        self.assertEqual(result["matched"], 0)
        send.assert_not_called()

    def test_night_upload_does_not_mail_the_day_survey(self):
        with (
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=self.candidates(),
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ) as send,
        ):
            result = approved_email.send_raw_emails_for_inventory_items(
                [{"label": "140", "cycle": 14, "dn": "night"}]
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(send.call_args.args[0], 101)

    def test_day_marker_is_accepted_as_well_as_an_empty_one(self):
        self.assertTrue(
            raw_xlsx.raw_xlsx_is_listed(
                "124", "124", 2, "Day", [{"label": "124", "cycle": 2, "dn": "day"}]
            )
        )
        self.assertTrue(
            raw_xlsx.raw_xlsx_is_listed(
                "124", "124", 2, "Day", [{"label": "124", "cycle": 2, "dn": ""}]
            )
        )
        self.assertFalse(
            raw_xlsx.raw_xlsx_is_listed(
                "124", "124", 2, "Night", [{"label": "124", "cycle": 2, "dn": "day"}]
            )
        )


class UploadCallbackEndpointTests(unittest.TestCase):
    """The RoadVision upload callback, mounted where the API lives."""

    @classmethod
    def setUpClass(cls):
        from core.endpoints import bp
        from core.engine import register_error_handlers

        cls.app = Flask(__name__)
        cls.app.register_blueprint(bp, url_prefix="/api/v1")
        register_error_handlers(cls.app)

    def test_upload_callback_needs_the_secret(self):
        with patch.dict(os.environ, {"RV_WEBHOOK_SECRET": "hook-secret"}):
            client = self.app.test_client()
            response = client.post(
                "/api/v1/jobs/raw-video-upload",
                json={"label": "140", "cycle": 14, "dn": "night"},
            )

        self.assertEqual(response.status_code, 403)

    def test_upload_callback_sends_the_matching_survey(self):
        with (
            patch.dict(os.environ, {"RV_WEBHOOK_SECRET": "hook-secret"}),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=[(101, "140", "140", 14, "Night")],
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ),
        ):
            client = self.app.test_client()
            response = client.post(
                "/api/v1/jobs/raw-video-upload",
                json={"label": "140", "cycle": 14, "dn": "night"},
                headers={"X-Webhook-Secret": "hook-secret"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["sent"], 1)
        self.assertEqual(data["matched"], 1)
        self.assertEqual(data["entries"], 1)

    def test_upload_callback_accepts_a_list_of_entries(self):
        with (
            patch.dict(os.environ, {"RV_WEBHOOK_SECRET": "hook-secret"}),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=[],
            ),
            patch.object(
                approved_email,
                "send_pending_raw_emails_now",
                return_value={"sent": 0},
            ) as fallback,
        ):
            client = self.app.test_client()
            response = client.post(
                "/api/v1/jobs/raw-video-upload",
                json={"files": [{"stretch": "140", "cycle": 14, "dn": "night"}]},
                headers={"X-Webhook-Secret": "hook-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["entries"], 1)
        # Nothing matched the label, so the ordinary pickup runs instead.
        fallback.assert_called_once()

    def test_upload_callback_does_not_fall_back_when_it_matched(self):
        with (
            patch.dict(os.environ, {"RV_WEBHOOK_SECRET": "hook-secret"}),
            patch.object(
                approved_email,
                "pending_raw_email_candidates",
                return_value=[(101, "140", "140", 14, "Night")],
            ),
            patch.object(
                approved_email,
                "send_approved_survey_email",
                return_value="sent",
            ),
            patch.object(
                approved_email,
                "send_pending_raw_emails_now",
            ) as fallback,
        ):
            client = self.app.test_client()
            response = client.post(
                "/api/v1/jobs/raw-video-upload",
                json={"label": "140", "cycle": 14, "dn": "night"},
                headers={"X-Webhook-Secret": "hook-secret"},
            )

        self.assertEqual(response.status_code, 200)
        fallback.assert_not_called()


class ApprovedSurveyCronTests(unittest.TestCase):
    """The cron sends the raw-video mail; the defect mail is one switch away."""

    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.register_blueprint(sync_bp)

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def call_cron(self, defect_switch):
        with (
            patch.dict(
                os.environ,
                {
                    "CRON_SECRET": "cron-secret",
                    "DEFECT_REPORT_EMAIL": defect_switch,
                },
            ),
            patch.object(
                received_timestamps,
                "fetch_inventories",
                return_value={"raw": [], "defect": []},
            ),
            patch.object(received_timestamps, "record_received_timestamps"),
            patch.object(
                approved_email,
                "process_pending_approved_survey_emails",
                return_value={
                    "checked": 2,
                    "sent": 2,
                    "waiting_for_raw_xlsx": 0,
                    "failed": 0,
                },
            ) as raw_send,
            patch.object(
                defect_email,
                "process_pending_approved_defect_emails",
                return_value={
                    "checked": 1,
                    "sent": 1,
                    "waiting_for_defect_report": 0,
                    "failed": 0,
                },
            ) as defect_send,
        ):
            client = self.app.test_client()
            response = client.get(
                "/cron/approved-survey-emails",
                headers={"X-Cron-Secret": "cron-secret"},
            )
        return response, raw_send, defect_send

    def test_cron_sends_raw_emails_and_skips_defect_emails_when_switched_off(self):
        response, raw_send, defect_send = self.call_cron("0")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["raw_video_emails"]["sent"], 2)
        self.assertIn("skipped", payload["defect_report_emails"])
        raw_send.assert_called_once()
        defect_send.assert_not_called()

    def test_cron_runs_the_defect_phase_when_switched_on(self):
        response, raw_send, defect_send = self.call_cron("1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["raw_video_emails"]["sent"], 2)
        self.assertEqual(payload["defect_report_emails"]["sent"], 1)
        raw_send.assert_called_once()
        defect_send.assert_called_once()

    def test_cron_rejects_a_request_without_the_secret(self):
        with patch.dict(os.environ, {"CRON_SECRET": "cron-secret"}):
            client = self.app.test_client()
            response = client.get("/cron/approved-survey-emails")

        self.assertEqual(response.status_code, 403)


class ReceivedTimestampTests(unittest.TestCase):
    """The portal records the moment it first saw each RoadVision file."""

    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        db.init_app(cls.app)
        with cls.app.app_context():
            db.create_all()

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.session.query(Survey).delete()
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.app_context.pop()

    def make_survey(self, cycle_no=1, survey_type="Day"):
        survey = Survey(
            end_survey_pdf="https://drive.google.com/file/d/abc123/view",
            status="completed",
            section_no="N/09086/01002/UP",
            upc_code="UP-NH-09086-01002-00",
            cycle_no=cycle_no,
            survey_type=survey_type,
        )
        db.session.add(survey)
        db.session.commit()
        return survey

    def raw_inventory(self):
        return {"raw": [{"label": "N0908601002UP", "cycle": 1, "dn": ""}], "defect": []}

    def test_the_raw_video_is_stamped_when_it_is_in_the_inventory(self):
        survey = self.make_survey()

        summary = received_timestamps.record_received_timestamps(self.raw_inventory())

        self.assertEqual(summary["raw_video_stamped"], 1)
        db.session.refresh(survey)
        self.assertIsNotNone(survey.raw_video_received_at)
        self.assertIsNone(survey.defect_report_received_at)

    def test_the_defect_report_is_stamped_separately(self):
        survey = self.make_survey()

        summary = received_timestamps.record_received_timestamps(
            {
                "raw": [],
                "defect": [{"label": "N0908601002UP", "cycle": 1, "dn": "day"}],
            }
        )

        self.assertEqual(summary["defect_report_stamped"], 1)
        db.session.refresh(survey)
        self.assertIsNotNone(survey.defect_report_received_at)
        self.assertIsNone(survey.raw_video_received_at)

    def test_a_file_that_is_seen_again_is_not_stamped_twice(self):
        survey = self.make_survey()

        received_timestamps.record_received_timestamps(self.raw_inventory())
        db.session.refresh(survey)
        first_seen = survey.raw_video_received_at

        summary = received_timestamps.record_received_timestamps(self.raw_inventory())

        self.assertEqual(summary["raw_video_stamped"], 0)
        db.session.refresh(survey)
        self.assertEqual(survey.raw_video_received_at, first_seen)

    def test_a_survey_whose_file_has_not_landed_is_not_stamped(self):
        survey = self.make_survey(cycle_no=9)

        summary = received_timestamps.record_received_timestamps(self.raw_inventory())

        self.assertEqual(summary["raw_video_stamped"], 0)
        db.session.refresh(survey)
        self.assertIsNone(survey.raw_video_received_at)

    def test_a_night_survey_does_not_match_a_day_file(self):
        survey = self.make_survey(survey_type="Night")

        summary = received_timestamps.record_received_timestamps(self.raw_inventory())

        self.assertEqual(summary["raw_video_stamped"], 0)
        db.session.refresh(survey)
        self.assertIsNone(survey.raw_video_received_at)

    def test_nothing_is_stamped_when_the_inventory_cannot_be_read(self):
        survey = self.make_survey()

        summary = received_timestamps.record_received_timestamps(
            {"raw": None, "defect": None}
        )

        self.assertEqual(summary["raw_video_stamped"], 0)
        self.assertFalse(summary["raw_inventory"])
        db.session.refresh(survey)
        self.assertIsNone(survey.raw_video_received_at)

    def test_the_upstream_timestamp_is_used_when_the_file_has_one(self):
        survey = self.make_survey()

        received_timestamps.record_received_timestamps(
            {
                "raw": [
                    {
                        "label": "N0908601002UP",
                        "cycle": 1,
                        "dn": "",
                        "updated": "2026-10-19T09:35:54.950000+00:00",
                    }
                ],
                "defect": [],
            }
        )

        db.session.refresh(survey)
        self.assertEqual(
            survey.raw_video_received_at,
            datetime(2026, 10, 19, 9, 35, 54, 950000),
        )

    def test_the_portal_notices_the_file_when_the_upstream_has_no_timestamp(self):
        survey = self.make_survey()

        received_timestamps.record_received_timestamps(self.raw_inventory())

        db.session.refresh(survey)
        self.assertIsNotNone(survey.raw_video_received_at)


if __name__ == "__main__":
    unittest.main()
