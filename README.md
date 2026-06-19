# B2B SaaS Blog Qualifier

Tool to verify whether B2B SaaS companies have an active blog or resources section on their website.

## Files

- `check_blogs.py` — Original CLI script.
- `app.py` — Streamlit web app.
- `requirements.txt` — Dependencies.

## Local usage (CLI)

```bash
pip install -r requirements.txt
python check_blogs.py --input companies.csv --output results.csv --workers 10 --timeout 10
```

## Local usage (Streamlit)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

## Deploy on Streamlit Cloud

1. Push this repository to GitHub.
2. Go to [Streamlit Cloud](https://streamlit.io/cloud) and sign in with GitHub.
3. Click **New app**.
4. Select the repository, the `app.py` file, and the `main` branch.
5. Click **Deploy**.

Streamlit Cloud will automatically install dependencies from `requirements.txt`.

## Input CSV format

```csv
Company Name,Website
HubSpot,https://hubspot.com
Stripe,https://stripe.com
```

## Verdicts

- **PASS**: Active blog with strong signals.
- **WEAK_PASS**: Some content, but weak signals.
- **NEWS_ONLY**: Only newsroom/press (does not qualify).
- **NO_BLOG**: No blog found.
- **CHECK_MANUAL**: Heavy JavaScript site, needs human review.
- **SITE_ERROR**: Could not access the site.

## Privacy note

If you deploy the app to public Streamlit Cloud, uploaded CSVs will pass through Streamlit's servers. For sensitive prospect lists, consider deploying in a private environment or using the local CLI.
