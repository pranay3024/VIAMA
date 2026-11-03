"""Send mail through Microsoft Graph using app-only credentials."""

import base64
import mimetypes
import os
from urllib.parse import quote

import requests


TOKEN_TIMEOUT_SECONDS = 20
SEND_TIMEOUT_SECONDS = 30
DRAFT_TIMEOUT_SECONDS = 30
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

#: Mailbox that owns the drafts the portal creates.  This is the same mailbox
#: the approved-survey auto-mailer sends from.
DEFAULT_MAILBOX = "projects9@adorpower.com"

#: Recipients used by the manual draft builder.  These match the recipients
#: the old Gmail draft used, so the mail itself does not change - only the
#: mailbox that stores the draft.
DEFAULT_DRAFT_TO = ("dashcamzone5@nhai.org", "dashcamcell@nhai.org")
DEFAULT_DRAFT_BCC = ("teamleaderador@gmail.com",)


def _raise_for_service_error(response, service_name):
    if response.ok:
        return

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    error = payload.get("error") or {}
    if isinstance(error, dict):
        error_code = error.get("code") or "unknown_error"
        error_message = error.get("message") or "No error details returned"
    else:
        error_code = str(error)
        error_message = payload.get("error_description") or "No error details returned"

    request_id = response.headers.get("request-id")
    request_detail = f"; request-id={request_id}" if request_id else ""
    raise RuntimeError(
        f"{service_name} request failed with HTTP {response.status_code} "
        f"({error_code}): {error_message}{request_detail}"
    )


def _graph_access_token():
    """Return an app-only Microsoft Graph access token."""
    tenant_id = (os.getenv("M365_TENANT_ID") or "").strip()
    client_id = (os.getenv("M365_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("M365_CLIENT_SECRET") or "").strip()
    if not all((tenant_id, client_id, client_secret)):
        raise RuntimeError("Microsoft Graph mail credentials are not configured")

    token_response = requests.post(
        f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=TOKEN_TIMEOUT_SECONDS,
    )
    _raise_for_service_error(token_response, "Microsoft identity")
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Microsoft identity response did not include an access token")
    return access_token


def _graph_recipients(addresses):
    """Turn a comma-separated string or iterable into Graph recipients."""
    if isinstance(addresses, str):
        addresses = addresses.split(",")
    return [
        {"emailAddress": {"address": address.strip()}}
        for address in addresses
        if address and address.strip()
    ]


def create_microsoft_graph_draft(
    subject,
    html_body,
    attachment_bytes=None,
    attachment_filename=None,
    from_email=DEFAULT_MAILBOX,
    to_emails=None,
    cc_emails=None,
    bcc_emails=None,
):
    """Create a draft in the M365 mailbox and return its message id.

    ``POST /users/{id}/messages`` stores the message in that mailbox's Drafts
    folder, so it appears in Outlook and Outlook on the web.  The draft is not
    sent: a person still opens it, checks it, and presses Send.
    """
    access_token = _graph_access_token()

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": _graph_recipients(to_emails or DEFAULT_DRAFT_TO),
    }

    cc_recipients = _graph_recipients(cc_emails or ())
    if cc_recipients:
        message["ccRecipients"] = cc_recipients

    bcc_recipients = _graph_recipients(bcc_emails or DEFAULT_DRAFT_BCC)
    if bcc_recipients:
        message["bccRecipients"] = bcc_recipients

    if attachment_bytes and attachment_filename:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment_filename,
                "contentType": (
                    mimetypes.guess_type(attachment_filename)[0]
                    or "application/octet-stream"
                ),
                "contentBytes": base64.b64encode(attachment_bytes).decode("ascii"),
            }
        ]

    response = requests.post(
        f"{GRAPH_ROOT}/users/{quote(from_email, safe='')}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json=message,
        timeout=DRAFT_TIMEOUT_SECONDS,
    )
    _raise_for_service_error(response, "Microsoft Graph create draft")

    payload = response.json()
    draft_id = payload.get("id")
    if not draft_id:
        raise RuntimeError("Microsoft Graph create draft returned no message id")
    return draft_id


def send_microsoft_graph_email(
    to_email,
    from_email,
    subject,
    html_body,
    attachment_bytes,
    attachment_filename,
    message_id=None,
    additional_attachments=None,
    cc_emails=None,
    bcc_emails=None,
):
    """Send an HTML email and its attachments from the configured M365 mailbox."""
    access_token = _graph_access_token()

    attachments = [(attachment_bytes, attachment_filename)]
    attachments.extend(additional_attachments or [])
    graph_attachments = []
    for content, filename in attachments:
        graph_attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": (
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                ),
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": _graph_recipients(to_email),
        "attachments": graph_attachments,
    }
    cc_recipients = _graph_recipients(cc_emails or ())
    if cc_recipients:
        message["ccRecipients"] = cc_recipients
    bcc_recipients = _graph_recipients(bcc_emails or ())
    if bcc_recipients:
        message["bccRecipients"] = bcc_recipients
    if message_id:
        message["internetMessageHeaders"] = [
            {
                "name": "X-VIAMA-Message-ID",
                "value": message_id.strip("<>")
            }
        ]

    response = requests.post(
        f"{GRAPH_ROOT}/users/{quote(from_email, safe='')}/sendMail",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"message": message, "saveToSentItems": True},
        timeout=SEND_TIMEOUT_SECONDS,
    )
    _raise_for_service_error(response, "Microsoft Graph sendMail")
    if response.status_code != 202:
        raise RuntimeError(
            f"Microsoft Graph returned unexpected send status {response.status_code}"
        )

    return message_id or response.headers.get("request-id") or "accepted"