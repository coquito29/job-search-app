# Job Search App

A personal job-search workspace for finding relevant roles, tailoring a CV,
tracking applications, and reducing repetitive form work. It combines a Flask
web app with an optional Chrome extension for rule-based application autofill.

## What it does

- Searches job boards through Apify and ranks results against the saved profile.
- Keeps a CV library, application tracker, follow-up list, and cover-letter
  drafts in one place.
- Provides a daily digest and an optional Chrome extension that fills supported
  application forms. A person remains responsible for reviewing and submitting
  applications.
- Works without an AI provider: matching and autofill use local rules.

## Run locally

1. Install Python 3.10+ and the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Start the app:

   ```bash
   python app.py
   ```

3. Open the local address printed by Flask. Add an Apify token in **Setup** to
   search for jobs.

The app uses a local `applications.db` file by default. Set `DATABASE_URL` to
use Postgres in a hosted environment.

## Test

```bash
python run_tests.py
```

For a fresh checkout, install the extension test dependency once:

```bash
cd chrome-extension/tests
npm install
```

## Project layout

| Path | Purpose |
| --- | --- |
| `app.py` | Flask routes and the current application orchestration layer |
| `billing.py` | Subscription and quota helpers |
| `location_filter.py` | US work-eligibility location classification |
| `work_history.py` | Work-history normalization |
| `templates/` and `static/` | Web interface and PWA assets |
| `chrome-extension/` | Optional rule-based browser autofill extension |

## Privacy and safety

Do not commit CVs, cover letters, API tokens, databases, or any personal
contact information to a public repository. Keep the repository private when
it contains personal job-search data, and use local, ignored files for those
materials. Review every form before submitting it; the extension is designed
to fill fields, not make career decisions for you.

## Deployment

The Flask service is configured for Render through `Procfile`. Configure
production secrets as environment variables rather than checking them into the
repository. The Chrome extension is loaded separately from a local checkout
and needs a reload after extension changes.
