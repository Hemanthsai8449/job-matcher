import json
from pathlib import Path


def test_vercel_config_has_one_hobby_compatible_alert_slot_per_utc_hour():
    project_root = Path(__file__).resolve().parents[1]
    config = json.loads((project_root / "vercel.json").read_text(encoding="utf-8"))
    dispatch_crons = [
        cron
        for cron in config["crons"]
        if cron["path"].startswith("/operations/cron/dispatch-alerts/")
    ]

    assert len(dispatch_crons) == 24
    assert {cron["path"].rsplit("/", 1)[-1] for cron in dispatch_crons} == {
        str(hour) for hour in range(24)
    }
    for cron in dispatch_crons:
        minute, hour, day, month, weekday = cron["schedule"].split()
        assert minute.isdigit()
        assert hour == cron["path"].rsplit("/", 1)[-1]
        assert (day, month, weekday) == ("*", "*", "*")
