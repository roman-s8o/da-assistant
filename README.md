# da-assistant

A chat assistant that answers sales questions about the
`bigquery-public-data.thelook_ecommerce` dataset. It uses Gemini to write SQL and BigQuery to run it.

Work in progress: the full documentation comes later.

## Setup

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) (or pip),
the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) and a free Gemini API key.

1. Install the dependencies:

   ```bash
   uv sync
   ```

   Or with pip:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Log in to Google Cloud. BigQuery uses these credentials:

   ```bash
   gcloud auth application-default login
   ```

3. Copy `.env.example` to `.env` and set `GEMINI_API_KEY`
   (get a key at <https://aistudio.google.com/apikey>).
   Set `DA_GCP_PROJECT` to the Google Cloud project that runs (and pays for) the BigQuery jobs.
   If you leave it empty, the default project of your gcloud credentials is used,
   and it must have the BigQuery API enabled.

4. Check the setup. This runs one free test query in BigQuery and one short Gemini call:

   ```bash
   uv run da-assistant check
   ```

## Usage

```bash
uv run da-assistant chat --user alice
```

The users and the brands each of them can analyze are in `config/users.yaml`.

## Tests

```bash
uv run pytest
```
