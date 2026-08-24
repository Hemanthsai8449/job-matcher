from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Regexp


class RegisterForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = StringField("Personal email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField(
        "Mobile number",
        validators=[DataRequired(), Length(min=8, max=24)],
        description="Include your country code, for example +91.",
    )
    password = PasswordField(
        "Create password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm password", validators=[DataRequired(), EqualTo("password")]
    )
    accept_terms = BooleanField(
        "I agree to the Terms and Privacy Policy", validators=[DataRequired()]
    )
    alerts_consent = BooleanField(
        "I want personalized job alerts and understand I can pause them at any time"
    )
    submit = SubmitField("Create my profile")


class OTPForm(FlaskForm):
    code = StringField(
        "Six-digit verification code",
        validators=[DataRequired(), Regexp(r"^\d{6}$", message="Enter the six-digit code.")],
    )
    submit = SubmitField("Verify email")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])
    remember = BooleanField("Keep me signed in on this device")
    submit = SubmitField("Sign in")


class GoogleCompleteForm(FlaskForm):
    phone = StringField(
        "Mobile number",
        validators=[DataRequired(), Length(min=8, max=24)],
        description="Include your country code, for example +91.",
    )
    accept_terms = BooleanField(
        "I agree to the Terms and Privacy Policy", validators=[DataRequired()]
    )
    alerts_consent = BooleanField(
        "I want personalized job alerts and understand I can pause them at any time"
    )
    submit = SubmitField("Finish creating my profile")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    submit = SubmitField("Send recovery instructions")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm new password", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Set new password")
