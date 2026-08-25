from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    DecimalField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp


class ResumeUploadForm(FlaskForm):
    resume = FileField(
        "Resume file",
        validators=[
            FileRequired(),
            FileAllowed(
                [
                    "pdf",
                    "docx",
                    "doc",
                    "odt",
                    "rtf",
                    "txt",
                    "md",
                    "html",
                    "htm",
                    "jpg",
                    "jpeg",
                    "png",
                    "webp",
                ],
                "Choose a supported resume file.",
            ),
        ],
    )
    submit = SubmitField("Read my resume")


class ResumeReviewForm(FlaskForm):
    headline = StringField("Professional headline", validators=[Optional(), Length(max=160)])
    education = TextAreaField("Education", validators=[Optional(), Length(max=3000)])
    experience_years = DecimalField(
        "Years of experience", validators=[Optional(), NumberRange(min=0, max=60)], places=1
    )
    skills = TextAreaField(
        "Skills",
        validators=[Optional(), Length(max=3000)],
        description="Separate skills with commas.",
    )
    submit = SubmitField("Confirm resume details")


class PreferencesForm(FlaskForm):
    desired_roles = TextAreaField(
        "Roles you want",
        validators=[DataRequired(), Length(max=1000)],
        description="Separate roles with commas, for example Python Developer, Data Analyst.",
    )
    preferred_locations = TextAreaField(
        "Preferred locations",
        validators=[DataRequired(), Length(max=1000)],
        description="Separate locations with commas. Include Remote if you want remote roles.",
    )
    remote = BooleanField("Remote")
    hybrid = BooleanField("Hybrid")
    onsite = BooleanField("On-site")
    full_time = BooleanField("Full-time")
    internship = BooleanField("Internship")
    contract = BooleanField("Contract")
    graduation_year = IntegerField(
        "Graduation year", validators=[Optional(), NumberRange(min=1990, max=2100)]
    )
    daily_job_limit = SelectField(
        "Daily job links",
        choices=[("3", "3 jobs"), ("5", "5 jobs"), ("10", "10 jobs")],
        default="5",
    )
    preferred_time = StringField(
        "Preferred delivery time",
        validators=[
            DataRequired(),
            Regexp(r"^(?:[01]\d|2[0-3]):[0-5]\d$", message="Choose a valid 24-hour time."),
        ],
    )
    timezone = SelectField(
        "Timezone",
        choices=[
            ("Asia/Kolkata", "India — Asia/Kolkata"),
            ("UTC", "UTC"),
            ("Asia/Dubai", "UAE — Asia/Dubai"),
            ("Europe/London", "United Kingdom — Europe/London"),
            ("America/New_York", "US Eastern — America/New_York"),
            ("America/Los_Angeles", "US Pacific — America/Los_Angeles"),
        ],
        default="Asia/Kolkata",
    )
    salary_preference = StringField(
        "Salary preference (optional)", validators=[Optional(), Length(max=80)]
    )
    submit = SubmitField("Save preferences")


class TelegramConnectForm(FlaskForm):
    submit = SubmitField("Create secure Telegram link")
