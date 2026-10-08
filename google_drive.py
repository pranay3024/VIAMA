"""
Google Drive uploads/downloads and Gmail draft creation.

Credentials are resolved lazily, on first use, rather than at import time.

That matters because this module previously ran two `pickle.load` calls and built
two API clients while being imported, and `routes/admin.py`, `routes/captain.py`
and `routes/roadvision.py` all import it at the top. A missing or expired
`token_drive.pickle` or `token_gmail.pickle` therefore took down the *entire
portal*, not just the feature that needed it - and an expired Gmail token broke
Drive uploads that had nothing to do with Gmail.

Now the two credentials are independent: Drive uploads keep working when the
Gmail token expires, and vice versa. `core/engine.py` already turns a failure
here into a 503 rather than a 500.

Drive credential sources, in order:

1. ``DRIVE_TOKEN_JSON`` - an OAuth user token supplied as JSON.
2. ``token_drive.pickle`` - an OAuth user token resolved relative to this file.
    Local uploads should use an account with access to the destination folder,
    because service accounts do not have personal Drive storage quota.
3. ``GOOGLE_SA_JSON`` / ``GOOGLE_SA_JSON_FILE`` - a service account, suitable
    when the destination is a Shared Drive or otherwise supports service-account
    storage.

Gmail uses ``GMAIL_TOKEN_JSON`` or ``token_gmail.pickle``. ``pickle.load``
executes arbitrary code from token files: treat write access to them as
equivalent to code execution.
"""

import io
import json
import logging
import os
import pickle
import re
import base64
import threading
import requests

from googleapiclient.discovery import build
from googleapiclient.http import (
    MediaIoBaseUpload,
    MediaIoBaseDownload
)

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication


log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))

DRIVE_TOKEN_PATH = os.path.join(_HERE, "token_drive.pickle")
GMAIL_TOKEN_PATH = os.path.join(_HERE, "token_gmail.pickle")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_clients = {}
_lock = threading.Lock()


class GoogleUnavailable(RuntimeError):
    """Google credentials are missing, unreadable or expired."""


# --------------------------------------------------
# GOOGLE AUTH
# --------------------------------------------------

def _creds_from_pickle(path, label):

    if not os.path.exists(path):
        raise GoogleUnavailable(
            "No {} credentials: expected {}".format(label, path)
        )

    try:
        with open(path, "rb") as token:
            return pickle.load(token)

    except Exception as exc:
        raise GoogleUnavailable(
            "{} could not be read: {}: {}".format(
                os.path.basename(path), type(exc).__name__, exc
            )
        ) from exc


def _creds_from_service_account():
    """Drive only. Gmail drafts need a real mailbox, not a service account."""

    raw = (os.getenv("GOOGLE_SA_JSON") or "").strip()

    credential_file = (os.getenv("GOOGLE_SA_JSON_FILE") or "").strip()
    if not raw and credential_file:
        credential_path = credential_file
        if not os.path.isabs(credential_path):
            credential_path = os.path.join(_HERE, credential_path)
        try:
            with open(credential_path, "r", encoding="utf-8") as service_account_file:
                raw = service_account_file.read().strip()
        except OSError as exc:
            raise GoogleUnavailable(
                "GOOGLE_SA_JSON_FILE could not be read: {}: {}".format(
                    type(exc).__name__, exc
                )
            ) from exc

    if not raw:
        return None

    try:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(
            json.loads(raw),
            scopes=DRIVE_SCOPES,
        )

    except Exception as exc:
        raise GoogleUnavailable(
            "service-account credentials could not be parsed: "
            "credentials: {}: {}".format(type(exc).__name__, exc)
        ) from exc


def _client(name):
    """Build and cache one API client. Raises GoogleUnavailable."""

    if name in _clients:
        return _clients[name]

    with _lock:

        if name in _clients:
            return _clients[name]

        if name == "drive":

            raw = (os.getenv("DRIVE_TOKEN_JSON") or "").strip()

            if raw:

                from google.oauth2.credentials import Credentials

                creds = Credentials.from_authorized_user_info(
                    json.loads(raw)
                )

                source = "DRIVE_TOKEN_JSON"

            elif os.path.isfile(DRIVE_TOKEN_PATH):

                creds = _creds_from_pickle(
                    DRIVE_TOKEN_PATH,
                    "Drive",
                )

                source = "token_drive.pickle"

            else:

                creds = _creds_from_service_account()
                source = "GOOGLE_SA_JSON"

                if creds is None:

                    creds = _creds_from_pickle(
                        DRIVE_TOKEN_PATH,
                        "Drive"
                    )

                    source = "token_drive.pickle"

            service, version = "drive", "v3"

        else:

            raw = (os.getenv("GMAIL_TOKEN_JSON") or "").strip()

            if raw:

                from google.oauth2.credentials import Credentials

                creds = Credentials.from_authorized_user_info(
                    json.loads(raw)
                )

                source = "GMAIL_TOKEN_JSON"

            else:

                creds = _creds_from_pickle(
                    GMAIL_TOKEN_PATH,
                    "Gmail"
                )

                source = "token_gmail.pickle"

            granted_scopes = set(getattr(creds, "scopes", None) or [])
            missing_scopes = [
                scope for scope in GMAIL_SCOPES
                if scope not in granted_scopes
            ]
            if missing_scopes:
                raise GoogleUnavailable(
                    "Gmail credentials from {} are missing required scope(s): {}. "
                    "Regenerate the Gmail OAuth token with generate_gmail_token.py "
                    "and update GMAIL_TOKEN_JSON on Vercel."
                    .format(source, ", ".join(missing_scopes))
                )

            service, version = "gmail", "v1"

        try:

            _clients[name] = build(
                service,
                version,
                credentials=creds
            )

        except Exception as exc:

            raise GoogleUnavailable(
                "{} client could not be built from {}: {}: {}".format(
                    name,
                    source,
                    type(exc).__name__,
                    exc
                )
            ) from exc

        log.info(
            "google %s: authenticated via %s",
            name,
            source
        )

        return _clients[name]


def get_drive():
    """The Drive client. Raises GoogleUnavailable."""

    return _client("drive")


def get_gmail():
    """The Gmail client. Raises GoogleUnavailable."""

    return _client("gmail")


# --------------------------------------------------
# RO -> CC EMAIL
# --------------------------------------------------

RO_CC_MAPPING = {

    "UP West": "dashcam.rolucknow@gmail.com",
    "UP East": "dashcam.rovaransi@gmail.com",
    "Varanasi": "dashcam.rovaransi@gmail.com",
    "Lucknow" : "dashcam.rolucknow@gmail.com",

    "Guwahati": "dashcam.roguwahati@gmail.com",
    "Shillong": "dashcam.roguwahati@gmail.com",

    "Patna": "dashcam.ropatna@gmail.com",
    "PATNA": "dashcam.ropatna@gmail.com",

    "Ranchi": "dashcam.roranchi@gmail.com",

    "Delhi": "dashcam.rodelhi@gmail.com",

    "Dehradun": "dashcam.rodehradun@gmail.com",

    "Bhubaneswar": "dashcam.robhubaneswar@gmail.com",
    "Odisha": "dashcam.robhubaneswar@gmail.com",


    "Kolkata": "dashcam.rokolkata@gmail.com"

}


# --------------------------------------------------
# PIU -> EMAIL FOR CANCELLED SURVEY DRAFTS
# --------------------------------------------------

PIU_EMAIL_MAPPING = {

    "Mathura": "mat@nhai.org",

    "Agra": "agra@nhai.org",

    "Aligarh": "aligarh@nhai.org",

    "Ayodhya": "piuayodhya@nhai.org",

    "Azamgarh": "piuazamgarh@nhai.org",

    "Bahraich": "piubahraich@gmail.com",

    "Bareilly": "bareilly@nhai.org",

    "Begusarai": "piubegusarai@nhai.org",

    "Berhampur": "piuberhampur@nhai.org",

    "Bhagalpur": "piubhagalpur@nhai.org",

    "Bhubaneswar": "bhu@nhai.org",

    "Bongaigaon": "bongaigaon@nhai.org",

    "Chandikhole": "piuchandikhole@nhai.org",

    "Chhapra": "piuchhapra@nhai.org",

    "Daltonganj": "piudaltonganj@nhai.org",

    "Deoghar": "piudeoghar@nhai.org",

    "Dhanbad": "dhan@nhai.org",

    "Dhenkanal": "piudhenkanal@nhai.org",

    "Durgapur": "dur@nhai.org, nhaipiudgp@yahoo.com",

    "Gaya": "gaya@nhai.org",

    "Ghazipur": "piughazipur@nhai.org, nhaighazipur@gmail.com",

    "Gorakhpur": "nhaigorakhpur2002@gmail.com, gorakhpur@nhai.org",

    "Gumla": "piugumla@nhai.org",

    "Guwahati": "piughy@nhai.org, piuguwahati@gmail.com",

    "Hazaribagh": "piuhazaribagh@nhai.org",

    "Jalpaiguri": "sil@nhai.org",

    "Jhansi": "jha@nhai.org",

    "Kanpur": "knp@nhai.org",

    "Keonjhar": "pdnhai.kjr@nhai.org",

    "Kharagpur": "kha@nhai.org",

    "Kolkata": "piukol@gmail.com, kol@nhai.org",

    "Koraput": "piukoraput@nhai.org, nhaikoraput@gmail.com",

    "Krishnagar": "krishnagar@nhai.org, piukrishnagar@gmail.com",

    "Lucknow": "luc@nhai.org",

    "Madhubani": "piumadhubani@nhai.org",

    "Malda": "malda@nhai.org",

    "Moradabad": "mor@nhai.org",

    "Motihari": "motihari@nhai.org",

    "Muzaffarpur": "muz@nhai.org",

    "Nabarangpur": "piunabarangpur@nhai.org",

    "Patna": "patna@nhai.org",

    "Prayagraj": "ald@nhai.org",

    "Purnea": "piupurnia@nhai.org",

    "Purulia": "piupurulia@nhai.org",

    "Raebareli": "Email-pdpiuraebareilly@nhai.org",

    "Ranchi": "ranchi@nhai.org",

    "Sambalpur": "nhaisambalpur@gmail.com",

    "Sasaram": "piusasaram@nhai.org",

    "Shillong": "piushillong@nhai.org",

    "Silchar": "nhaihaflong@gmail.com, silchar@nhai.org",

    "Varanasi": "var@nhai.org",
}


def get_cc_email(ro):

    if not ro:
        return ""

    return RO_CC_MAPPING.get(
        ro.strip(),
        ""
    )


# --------------------------------------------------
# UPLOAD TO GOOGLE DRIVE
# --------------------------------------------------

def upload_file_to_drive(
    file_bytes,
    filename,
    folder_id,
    mime_type
):

    drive = get_drive()

    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        resumable=True
    )

    metadata = {
        "name": filename,
        "parents": [folder_id]
    }

    file = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id"
    ).execute(num_retries=5)

    drive.permissions().create(
        fileId=file["id"],
        body={
            "type": "anyone",
            "role": "reader"
        }
    ).execute(num_retries=5)

    return {

        "id": file["id"],

        "view_url":
        f"https://drive.google.com/file/d/{file['id']}/view",

        "image_url":
        f"https://drive.google.com/thumbnail?id={file['id']}&sz=w2000"

    }

def delete_file_from_drive(file_id):

    drive = get_drive()

    drive.files().delete(
        fileId=file_id
    ).execute()


# --------------------------------------------------
# DOWNLOAD FROM DRIVE
# --------------------------------------------------

def download_file_from_drive(view_url):
    if view_url and view_url.startswith(("http://", "https://")):
        drive_match = re.search(r"/d/([a-zA-Z0-9_-]+)", view_url)
        if not drive_match and "supabase.co/storage/" in view_url:
            response = requests.get(view_url, timeout=60)
            response.raise_for_status()
            content = response.content
            if content.startswith(b"%PDF"):
                return content
            raise ValueError("Supabase URL did not return a PDF.")

    match = re.search(
        r"/d/([a-zA-Z0-9_-]+)",
        view_url
    )

    if not match:
        raise ValueError(
            "Invalid Google Drive URL."
        )

    file_id = match.group(1)

    try:
        drive = get_drive()

        request = drive.files().get_media(
            fileId=file_id
        )

        file_data = io.BytesIO()

        downloader = MediaIoBaseDownload(
            file_data,
            request
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()

        return file_data.getvalue()

    except Exception as exc:
        # Public Drive files can still be downloaded when the configured
        # Google project has Drive API access disabled.
        print(
            f"[DRIVE DOWNLOAD] API download failed; trying public link: {exc}",
            flush=True,
        )

    public_urls = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
    ]

    # Drive is prone to dropping connections when a few syncs hit it at once
    # (SSL EOF / RemoteDisconnected). Retry each public URL with backoff before
    # surfacing the failure.
    import time as _time

    for public_url in public_urls:
        for attempt in range(2):
            try:
                response = requests.get(public_url, timeout=60)
                response.raise_for_status()
                content = response.content

                if content.startswith(b"%PDF"):
                    print(
                        f"[DRIVE DOWNLOAD] public download succeeded for file_id={file_id}",
                        flush=True,
                    )
                    return content

                # The page returned but not a PDF - do not retry this URL shape.
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                print(
                    f"[DRIVE DOWNLOAD] attempt {attempt + 1}/2 failed for file_id={file_id}: {exc}",
                    flush=True,
                )
                if attempt < 1:
                    _time.sleep(0.5)
        content = ""

    raise ValueError(
        "The Google Drive file could not be downloaded through the API or public link. "
        "Confirm that the file is shared as Anyone with the link and is a PDF."
    )


# --------------------------------------------------
# CREATE GMAIL DRAFT
# --------------------------------------------------

def create_gmail_draft(

    subject,
    html_body,
    attachment_bytes,
    attachment_filename,
    cc_email=""

):

    gmail = get_gmail()

    message = MIMEMultipart()

    # SUBJECT
    message["Subject"] = subject

    # TO
    message["To"] = (
        "dashcamzone5@nhai.org,"
        "dashcamcell@nhai.org"
    )

    # CC
    if cc_email:
        message["Cc"] = cc_email

    message["Bcc"] = "teamleaderador@gmail.com"

    # HTML BODY
    message.attach(
        MIMEText(
            html_body,
            "html"
        )
    )

    # ATTACHMENT (Optional)
    if attachment_bytes:

     attachment = MIMEApplication(
        attachment_bytes
    )

     attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=attachment_filename
    )

     message.attach(
        attachment
    )

    raw = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode()

    draft = gmail.users().drafts().create(

        userId="me",

        body={
            "message": {
                "raw": raw
            }
        }

    ).execute(num_retries=5)

    log.info("gmail draft created: %s", draft.get("id"))

    return draft["id"]


def send_gmail_email(
    to_email,
    from_email,
    subject,
    html_body,
    attachment_bytes,
    attachment_filename,
    message_id=None,
):
    """Send one HTML email with one attachment and no CC or BCC."""
    gmail = get_gmail()
    message = MIMEMultipart()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    message.attach(MIMEText(html_body, "html"))

    attachment = MIMEApplication(attachment_bytes, _subtype="pdf")
    attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=attachment_filename,
    )
    message.attach(attachment)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    send_request = gmail.users().messages().send(
        userId="me",
        body={"raw": raw},
    )
    from utils.defect_report_delay import _gmail_call

    if message_id:
        search_request = gmail.users().messages().list(
            userId="me",
            q=f"rfc822msgid:{message_id.strip('<>')}",
            maxResults=1,
        )
        existing = _gmail_call(search_request, attempts=2, quota_max_wait=5)
        if existing.get("messages"):
            return existing["messages"][0]["id"]

    try:
        sent = _gmail_call(send_request, attempts=1, quota_max_wait=5)
        return sent["id"]
    except Exception:
        if not message_id:
            raise
        existing = _gmail_call(
            gmail.users().messages().list(
                userId="me",
                q=f"rfc822msgid:{message_id.strip('<>')}",
                maxResults=1,
            ),
            attempts=2,
            quota_max_wait=5,
        )
        if existing.get("messages"):
            return existing["messages"][0]["id"]
        raise
