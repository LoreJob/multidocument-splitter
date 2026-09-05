"""Standalone portfolio demo for the Multidocument Splitter UI.

The product data and pipeline are intentionally simulated in the browser, so
the showcase runs without Databricks credentials or production dependencies.
"""
from __future__ import annotations

from flask import Flask, redirect, render_template

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("control_tower.html")


@app.get("/dashboard")
def dashboard():
    return redirect("/#analytics")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
