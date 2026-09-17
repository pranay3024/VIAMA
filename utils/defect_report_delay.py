"""Survey end-date and defect-report sent-date matching helpers."""

import base64
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError

try:
	import holidays
except ImportError:  # pragma: no cover - deployment installs requirements.txt
	holidays = None


def indian_public_holidays(start_date, end_date):
	"""Return Indian public holidays in the inclusive date range."""
	if not start_date or not end_date:
		return set()
	if holidays is None:
		return {
			date(year, month, day)
			for year in range(start_date.year, end_date.year + 1)
			for month, day in ((1, 26), (8, 15), (10, 2))
			if start_date <= date(year, month, day) <= end_date
		}
	years = range(start_date.year, end_date.year + 1)
	return set(holidays.country_holidays("IN", years=years))


def working_days_between(start_date, end_date):
	"""Count Monday-Saturday dates, only excluding Sundays."""
	if not start_date or not end_date or end_date <= start_date:
		return 0

	current = start_date + timedelta(days=1)
	working_days = 0
	while current <= end_date:
		if current.weekday() != 6:
			working_days += 1
		current += timedelta(days=1)
	return working_days


def defect_report_delay_days(start_date, sent_date, allowed_days=3):
	"""Return delay days after the allowed Monday-Saturday working days."""
	return max(
		working_days_between(start_date, sent_date) - allowed_days,
		0,
	)


def _normalize(value):
	return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _header(headers, name):
	wanted = name.lower()
	for header in headers or []:
		if header.get("name", "").lower() == wanted:
			return header.get("value", "")
	return ""


def _is_retryable_gmail_error(exc):
	"""True for transient network / rate-limit Gmail API failures."""
	if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
		return True
	if isinstance(exc, HttpError):
		return exc.resp.status in (429, 500, 502, 503, 504)
	return False


# The cached Gmail client wraps a single httplib2 connection pool, which is
# NOT thread-safe. When the sweep runs several syncs at once they must never
# touch it simultaneously (that corruption shows up as SSL "record layer
# failure" / EOF errors), so every Gmail HTTP call is serialised through this
# lock.
_GMAIL_LOCK = threading.Lock()


def _gmail_call(request, attempts=3):
	"""Execute a Gmail API request with backoff on transient failures.

	Retries are deliberately short: on Vercel the request must finish well
	under the function duration cap, so a single sync absorbs at most one or
	two quick retries instead of blowing the whole timeout.
	"""
	last_exc = None
	for attempt in range(attempts):
		try:
			with _GMAIL_LOCK:
				return request.execute()
		except Exception as exc:
			if not _is_retryable_gmail_error(exc):
				raise
			last_exc = exc
			print(
				f"[GMAIL] transient error, attempt {attempt + 1}/{attempts}: {exc}",
				flush=True,
			)
			if attempt < attempts - 1:
				time.sleep(0.5 + attempt * 1.5)
	raise last_exc


def _message_text(payload):
	parts = []
	body = payload.get("body", {}).get("data")
	if body:
		try:
			parts.append(base64.urlsafe_b64decode(body + "===").decode(
				"utf-8", errors="ignore"
			))
		except Exception:
			pass
	for child in payload.get("parts", []) or []:
		parts.append(_message_text(child))
	return " ".join(parts)


def _message_matches(survey, text):
	normalized_text = _normalize(text)
	required_parts = (
		survey.nh_number,
		survey.upc_code,
	)
	required_parts = tuple(part for part in required_parts if part)
	return all(_normalize(part) in normalized_text for part in required_parts)


def _message_matches_subject(survey, subject):
	return _message_matches(survey, subject)


def _message_matches_section_cycle(survey, text):
	"""Return whether an email explicitly identifies this section and cycle."""
	normalized_text = _normalize(text)
	section = _normalize(survey.section_no)
	cycle = str(survey.cycle_no or "")
	if not section or not cycle:
		return False

	patterns = (
		f"stretchno{section}cycle{cycle}",
		f"stretch{section}cycle{cycle}",
		f"section{section}cycle{cycle}",
	)
	if any(pattern in normalized_text for pattern in patterns):
		return True

	# The generated subject contains UPC_cycle_completion, for example
	# ``N/02005/06001/UP_008_080926``.  Require the 6-digit date suffix
	# so that e.g. cycle 010 does not accidentally match the leading digits
	# of a different cycle's number (the old ``0*`` prefix caused false
	# matches like 0*010 matching 0010 from cycle 001's text).
	upc = _normalize(survey.upc_code)
	return bool(
		upc
		and re.search(
			rf"{re.escape(upc)}{int(survey.cycle_no):03d}\d{{6}}",
			normalized_text,
		)
	)


def build_defect_email_index(gmail, survey=None):
	"""Read Sent defect-report subjects and index them by identifiers.

	When a survey is supplied, narrow Gmail's search to its identifiers so a
	request-triggered sync does not scan the entire Sent mailbox.
	"""
	matches = []
	page_token = None
	query = 'in:sent has:attachment subject:"Submission of Survey Report"'
	if survey:
		for identifier in (survey.nh_number, survey.upc_code):
			if identifier:
				query += ' "{}"'.format(str(identifier).replace('"', ''))
	while True:
		params = {
			"userId": "me",
			"q": query,
			"maxResults": 500,
		}
		if page_token:
			params["pageToken"] = page_token
		response = _gmail_call(gmail.users().messages().list(**params))

		for item in response.get("messages", []):
			message = _gmail_call(
				gmail.users().messages().get(
					userId="me",
					id=item["id"],
					format="metadata",
					metadataHeaders=["Subject"],
				)
			)
			headers = message.get("payload", {}).get("headers", [])
			subject = _header(headers, "Subject")
			internal_ms = int(message.get("internalDate", "0"))
			sent_at = datetime.utcfromtimestamp(internal_ms / 1000) if internal_ms else None
			if sent_at:
				matches.append({
					"subject": subject,
					"sent_at": sent_at,
					"message_id": message.get("id"),
				})

		page_token = response.get("nextPageToken")
		if not page_token:
			break

	return matches


def find_defect_report_email(survey, email_index, gmail=None):
	"""Return the earliest exact section/cycle email for this survey.

	NH and UPC identify the project, not a unique survey cycle. The section and
	cycle identity must also be present before an email can be selected.
	"""
	if gmail:
		candidates = []
		for item in email_index:
			try:
				message = _gmail_call(
					gmail.users().messages().get(
						userId="me",
						id=item["message_id"],
						format="full",
					)
				)
			except Exception:
				continue
			body_text = _message_text(message.get("payload", {}))
			full_text = body_text + " " + item["subject"]
			if not _message_matches(survey, full_text):
				continue
			if not _message_matches_section_cycle(survey, full_text):
				continue
			candidates.append(item)
		matches = candidates
	else:
		matches = [
			item for item in email_index
			if _message_matches(survey, item["subject"])
			and _message_matches_section_cycle(survey, item["subject"])
		]
	if not matches:
		return None

	# Use the original defect-report email, not a later reply or reply-all.
	return min(matches, key=lambda item: item["sent_at"])
