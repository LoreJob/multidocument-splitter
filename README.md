# Multidocument Splitter

Portfolio-ready product demo for an AI-assisted PDF splitting workflow.

The interface shows how a multi-document PDF bundle can be reviewed, divided
into individual documents and tracked through a deterministic processing
pipeline. All documents, activity and analytics currently shown are fictional.

## Demo views

- **Overview** — product introduction, headline metrics and recent activity.
- **Splitter** — interactive page-boundary selection, full-page viewer and live split summary.
- **Pipeline** — six-step simulated run from upload to exported documents.
- **Analytics** — fictional precision, recall, F1 and throughput reporting.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install Flask
python app.py
```

Open [http://localhost:8000](http://localhost:8000).

No credentials, database or external API are required. The demo uses vanilla
HTML, CSS and JavaScript served by a minimal Flask entry point.

## Notes

- Uploaded PDFs stay in the browser and are not sent to a server.
- The five fictional PDFs in `pdfs/` form the 75-page demo bundle.
- Optimized page previews are generated in `static/pdf-pages/`.
- High-resolution fullscreen previews live in `static/pdf-full/`.
- Pipeline timing and model-quality metrics are intentionally simulated.
- Document export is disabled in the public demo; generated outputs are visual-only.
