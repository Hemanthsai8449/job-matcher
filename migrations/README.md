# Database migrations

`0001_initial` is the baseline Job Matcher schema. Apply it with:

```powershell
uv run flask --app run.py db upgrade
```

After changing models, create and review a new revision instead of editing an applied one:

```powershell
uv run flask --app run.py db migrate -m "describe the schema change"
uv run flask --app run.py db upgrade
```
