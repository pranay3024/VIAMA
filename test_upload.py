import uuid

from google_drive import upload_file_to_drive

PDF_FOLDER_ID = "1gkUDQh63qeSkJzZa6SLbB1LD06hEm1G7"

with open("sample.pdf", "rb") as f:

    result = upload_file_to_drive(
        file_bytes=f.read(),
        filename=str(uuid.uuid4()) + ".pdf",
        folder_id=PDF_FOLDER_ID,
        mime_type="application/pdf"
    )

print(result)