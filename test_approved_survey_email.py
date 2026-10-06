import base64
import unittest
from datetime import date, datetime
from email import message_from_bytes
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from extensions import db
import google_drive
import utils.approved_survey_email as approved_email


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
            patch.object(approved_email, "send_gmail_email") as send,
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "waiting_for_approval")
        send.assert_not_called()

    def test_approved_email_sends_only_the_survey_form_pdf(self):
        survey = self.make_survey()
        with (
            patch.object(approved_email.Survey, "query", self.make_query(survey)),
            patch.object(approved_email, "download_file_from_drive", return_value=b"%PDF"),
            patch.object(approved_email, "send_gmail_email", return_value="gmail-id") as send,
            patch.object(approved_email.db.session, "commit"),
        ):
            result = approved_email.send_approved_survey_email(survey.id)

        self.assertEqual(result, "sent")
        self.assertEqual(send.call_args.kwargs["to_email"], "ishit1561@gmail.com")
        self.assertEqual(send.call_args.kwargs["from_email"], "adordashcam@gmail.com")
        self.assertEqual(send.call_args.kwargs["attachment_bytes"], b"%PDF")
        self.assertTrue(send.call_args.kwargs["attachment_filename"].endswith(".pdf"))
        self.assertEqual(survey.raw_video_email_message_id, "gmail-id")

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