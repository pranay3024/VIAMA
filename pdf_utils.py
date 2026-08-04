import io
import pikepdf


def optimize_pdf(pdf_bytes):

    input_pdf = io.BytesIO(pdf_bytes)
    output_pdf = io.BytesIO()

    with pikepdf.open(input_pdf) as pdf:

        pdf.save(
            output_pdf,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate
        )

    output_pdf.seek(0)

    return output_pdf.read()    