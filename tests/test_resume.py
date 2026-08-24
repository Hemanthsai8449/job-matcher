from io import BytesIO
from pathlib import Path

from werkzeug.datastructures import FileStorage

from app.services.file_cleanup import process_private_file, queue_private_file
from app.services.resume_service import save_and_parse_resume


def test_text_resume_is_stored_privately_and_parsed(app, user_factory):
    user = user_factory()
    upload = FileStorage(
        stream=BytesIO(
            b"Asha Student\nGraduate Python Developer\nasha@example.com\n"
            b"B.Tech Computer Science 2026\nSkills: Python, SQL, Git\nFresher"
        ),
        filename="Asha Resume.txt",
        content_type="text/plain",
    )

    record = save_and_parse_resume(user, upload)

    assert record.original_name == "Asha Resume.txt"
    assert record.stored_name.startswith(f"{user.id}/")
    assert record.parse_status == "parsed"
    assert "Python" in record.parsed_profile["skills"]
    assert record.parsed_profile["graduation_year"] == 2026
    stored = Path(app.config["UPLOAD_FOLDER"]) / record.stored_name
    assert "static" not in str(stored).lower()
    assert stored.exists()

    cleanup = queue_private_file(record.stored_name)
    from app.extensions import db

    db.session.delete(record)
    db.session.commit()
    assert stored.exists()
    assert process_private_file(cleanup.id) is True
    assert not stored.exists()
