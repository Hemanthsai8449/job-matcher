from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional


class JobActionForm(FlaskForm):
    status = SelectField(
        "Status",
        choices=[
            ("saved", "Saved"),
            ("applied", "Applied"),
            ("interview", "Interview"),
            ("offer", "Offer"),
            ("rejected", "Rejected"),
            ("withdrawn", "Withdrawn"),
            ("not_interested", "Not interested"),
        ],
        validators=[DataRequired()],
    )
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=3000)])
    submit = SubmitField("Update")


class JobReportForm(FlaskForm):
    reason = SelectField(
        "Reason",
        choices=[
            ("expired", "The job has expired"),
            ("scam", "Possible scam or payment request"),
            ("incorrect", "Incorrect information"),
            ("duplicate", "Duplicate listing"),
            ("other", "Other"),
        ],
        validators=[DataRequired()],
    )
    details = TextAreaField("Details", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Submit report")


class MatchFilterForm(FlaskForm):
    q = StringField("Search", validators=[Optional(), Length(max=120)])
    location = StringField("Location", validators=[Optional(), Length(max=120)])
    work_mode = SelectField(
        "Work mode",
        choices=[
            ("", "Any work mode"),
            ("remote", "Remote"),
            ("hybrid", "Hybrid"),
            ("onsite", "On-site"),
        ],
        validators=[Optional()],
    )
    job_type = SelectField(
        "Job type",
        choices=[
            ("", "Any job type"),
            ("full_time", "Full-time"),
            ("internship", "Internship"),
            ("contract", "Contract"),
        ],
        validators=[Optional()],
    )
    submit = SubmitField("Apply filters")
