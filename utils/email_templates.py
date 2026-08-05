from datetime import datetime


def survey_id(survey, completion_date):

    completion = datetime.strptime(
        completion_date,
        "%Y-%m-%d"
    ).strftime("%d%m%y")

    return (
        f"{survey.upc_code}_"
        f"{survey.cycle_no:03d}_"
        f"{completion}"
    )




def format_date(date):

    return datetime.strptime(
        date,
        "%Y-%m-%d"
    ).strftime("%d/%m/%Y")


def build_subject(
    survey,
    email_type,
    completion_date
):

    sid = survey_id(
        survey,
        completion_date
    )

    if email_type == "defect":

        return (
            f"[ZONE-E] - Submission of Survey Report "
            f"for NH-[{survey.nh_number}] "
            f"[{survey.stretch_code}], "
            f"Survey ID - [{sid}]"
        )

    elif email_type == "discrepancy":

        return (
            f"[ZONE-E] - Submission of Final Survey Report "
            f"for NH-[{survey.nh_number}] "
            f"[{survey.stretch_code}], "
            f"Survey ID - [{sid}]"
        )

    return (
        f"[ZONE-E] - Submission of Raw Data "
        f"for NH-[{survey.nh_number}] "
        f"[{survey.stretch_code}], "
        f"Survey ID - [{sid}]"
    )


def build_email_body(
    survey,
    email_type,
    start_date,
    end_date
):

    sid = survey_id(
    survey,
    end_date
)

    start = format_date(start_date)
    end = format_date(end_date)

    if email_type == "defect":

        return f"""
<div style="
font-family:Calibri;
font-size:11pt;
line-height:1.4;
color:#000;
padding:0;
background:#fff;
">

<p>Sir/Madam,</p>

<p style="margin:0 0 12px 0;">

Please find attached herewith processed data for the subject project,
survey completed on
{end}.
The survey details are as under:

</p>

<b>Project Name:</b> {survey.stretch_code}<br>
<b>UPC:</b> {survey.upc_code}<br>
<b>PIU Name:</b> {survey.piu or ""}<br>
<b>RO Name:</b> {survey.ro or ""}<br>
<b>Survey Date:</b> {start} to {end}<br>
<b>Survey ID:</b> {sid}

<p>

The processed data for the surveyed stretch including link for the
processed video and survey report form are provided below for your
kind reference and download:

</p>

<p style="margin-top:18px;">

<b>

Processed Data Download Links:

</b>

</p>

<p>

Excel file attached herewith for your reference. (Stretch No.{survey.section_no}_Cycle{survey.cycle_no})

</p>

</div>
"""

    elif email_type == "discrepancy":

        return f"""
<div style="
font-family:Calibri;
font-size:11pt;
line-height:1.4;
color:#000;
background:#fff;
">

<p>Sir/Madam,</p>

<p>

Please find attached herewith revised survey report for the subject project.
The survey details are as under:

</p>

<b>Project Name:</b> {survey.stretch_code}<br>
<b>UPC:</b> {survey.upc_code}<br>
<b>PIU Name:</b> {survey.piu or ""}<br>
<b>RO Name:</b> {survey.ro or ""}<br>
<b>Survey Date:</b> {start} to {end}<br>
<b>Survey ID:</b> {sid}

<p>

The processed data for the surveyed stretch including link for the processed video and survey report form are provided below for your kind reference and download.

</p>

<p>

<b>Final Report Link:</b>

</p>

<p>

It is requested to kindly review the submitted Final Report and consider for approval. (Stretch No.{survey.section_no}_Cycle{survey.cycle_no})

</p>

</div>
"""

    return f"""
<div style="font-family:Calibri;font-size:11pt;line-height:1.5;">

<p>Sir/Madam,</p>

<p style="margin:0 0 12px 0;">

Please find attached herewith raw data for the subject project,
survey completed on
{end}.
The survey details are as under:

</p>

<b>Project Name:</b> {survey.stretch_code}<br>
<b>UPC:</b> {survey.upc_code}<br>
<b>PIU Name:</b> {survey.piu or ""}<br>
<b>RO Name:</b> {survey.ro or ""}<br>
<b>Survey Date:</b> {start} to {end}<br>
<b>Survey ID:</b> {sid}

<p>

The raw data for the surveyed stretch including link for the
dashcam survey raw video and signed survey form are provided below
for your kind reference and download.

</p>

<p>

<b>Raw Video Download Links:</b>

</p>

<p>
Excel file attached herewith for your reference. Stretch No. {survey.section_no} Cycle {survey.cycle_no}
</p>

</div>
"""