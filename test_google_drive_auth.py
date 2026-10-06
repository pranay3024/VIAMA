import unittest
from unittest.mock import patch

import google_drive


class GoogleDriveCredentialSelectionTests(unittest.TestCase):
    def test_local_oauth_token_takes_precedence_over_service_account(self):
        with (
            patch.object(google_drive, "_clients", {}),
            patch.object(google_drive.os, "getenv", return_value=""),
            patch.object(google_drive.os.path, "isfile", return_value=True),
            patch.object(google_drive, "_creds_from_pickle", return_value="oauth-creds") as oauth,
            patch.object(google_drive, "_creds_from_service_account") as service_account,
            patch.object(google_drive, "build", return_value="drive-client"),
        ):
            client = google_drive._client("drive")

        self.assertEqual(client, "drive-client")
        oauth.assert_called_once_with(google_drive.DRIVE_TOKEN_PATH, "Drive")
        service_account.assert_not_called()

    def test_service_account_remains_fallback_without_oauth_token(self):
        with (
            patch.object(google_drive, "_clients", {}),
            patch.object(google_drive.os, "getenv", return_value=""),
            patch.object(google_drive.os.path, "isfile", return_value=False),
            patch.object(google_drive, "_creds_from_service_account", return_value="service-creds") as service_account,
            patch.object(google_drive, "build", return_value="drive-client"),
        ):
            client = google_drive._client("drive")

        self.assertEqual(client, "drive-client")
        service_account.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()