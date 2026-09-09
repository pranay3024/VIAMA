"""Survey end-date and defect-report sent-date matching helpers."""

import base64
import re
from datetime import date, datetime, timedelta

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
	"""Count Monday-Saturday dates, excluding Indian public holidays."""
	if not start_date or not end_date or end_date <= start_date:
		return 0

	public_holidays = indian_public_holidays(start_date, end_date)
	current = start_date + timedelta(days=1)
	working_days = 0
	while current <= end_date:
		if current.weekday() != 6 and current not in public_holidays:
			working_days += 1
		current += timedelta(days=1)
	return working_days


def _normalize(value):
	return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _header(headers, name):
	wanted = name.lower()
	for header in headers or []:
		if header.get("name", "").lower() == wanted:
			return header.get("value", "")
	return ""


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


def build_defect_email_index(gmail):
	"""Read Sent defect-report subjects once and index them by identifiers."""
	matches = []
	page_token = None
	while True:
		params = {
			"userId": "me",
			"q": 'in:sent has:attachment subject:"Submission of Survey Report"',
			"maxResults": 500,
		}
		if page_token:
			params["pageToken"] = page_token
		response = gmail.users().messages().list(**params).execute()

		for item in response.get("messages", []):
			message = gmail.users().messages().get(
				userId="me",
				id=item["id"],
				format="metadata",
				metadataHeaders=["Subject"],
			).execute()
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
	"""Return the latest email matching subject and exact body stretch line."""
	matches = [
		item for item in email_index
		if _message_matches_subject(survey, item["subject"])
	]
	if gmail:
		exact_line = re.compile(
			r"stretch\s*no\.?\s*([a-z0-9./&()\s-]+?)\s*[_-]\s*cycle\s*([0-9]+)",
			re.IGNORECASE,
		)
		verified = []
		for item in matches:
			message = gmail.users().messages().get(
				userId="me",
				id=item["message_id"],
				format="full",
			).execute()
			body_text = _message_text(message.get("payload", {}))
			body_match = exact_line.search(body_text)
			if not body_match:
				continue
			section = _normalize(body_match.group(1))
			cycle = int(body_match.group(2))
			if section == _normalize(survey.section_no) and cycle == survey.cycle_no:
				verified.append(item)
		matches = verified
	if not matches:
		return None

	# Use the original defect-report email, not a later reply or reply-all.
	return min(matches, key=lambda item: item["sent_at"])
