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
import google_drive
import utils.approved_survey_email as approved_email
import utils.microsoft_graph_mail as graph_mail
import utils.raw_xlsx as raw_xlsx


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


if __name__ == "__main__":
    unittest.main()