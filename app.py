import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import pandas as pd
import streamlit as st

from check_blogs import VerdictResult, check_company


st.set_page_config(
    page_title="B2B SaaS Blog Qualifier",
    page_icon="📝",
    layout="wide",
)

st.title("📝 B2B SaaS Blog Qualifier")
st.markdown(
    """
    Verifica si empresas B2B SaaS tienen un blog o sección de recursos activa.

    **Formato esperado del CSV:**
    - `Company Name`
    - `Website`

    Los resultados se descargan con las columnas:
    `Has Content`, `Blog URL`, `Reason`, `Evidence`.
    """
)

uploaded_file = st.file_uploader("📁 Sube tu CSV", type=["csv"])

if uploaded_file is not None:
    content = uploaded_file.read().decode("utf-8-sig")
    rows: List[Dict[str, str]] = list(csv.DictReader(io.StringIO(content)))

    if not rows:
        st.error("El CSV está vacío.")
        st.stop()

    if "Company Name" not in rows[0] or "Website" not in rows[0]:
        st.error("El CSV debe contener las columnas 'Company Name' y 'Website'.")
        st.stop()

    st.success(f"**{len(rows)}** empresas cargadas.")
    with st.expander("Vista previa"):
        st.dataframe(rows[:10], use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        timeout = st.slider("⏱️ Timeout por request (segundos)", 5, 30, 10)
    with col2:
        workers = st.slider("🔧 Workers paralelos", 1, 10, 5)

    if st.button("🔍 Analizar empresas", type="primary"):
        progress_bar = st.progress(0, text="Iniciando análisis...")
        status_text = st.empty()

        results = [None] * len(rows)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(check_company, row, timeout): i
                for i, row in enumerate(rows)
            }

            completed = 0
            for future in as_completed(future_to_index):
                i = future_to_index[future]
                try:
                    result = future.result()
                except Exception as exc:
                    company = rows[i].get("Company Name", "Unknown")
                    result = VerdictResult(
                        company=company,
                        verdict="SITE_ERROR",
                        blog_url=rows[i].get("Website", ""),
                        reason="Unexpected thread error",
                        evidence=str(exc),
                    )

                results[i] = result
                completed += 1
                progress_bar.progress(
                    completed / len(rows),
                    text=f"Analizando {completed}/{len(rows)}: {result.company} → {result.verdict}",
                )
                status_text.text(f"Último: {result.company} → {result.verdict}")

        progress_bar.empty()

        # Build output rows preserving input order
        output_rows: List[Dict[str, str]] = []
        for row, result in zip(rows, results):
            output_rows.append(
                {
                    **row,
                    "Has Content": result.verdict,
                    "Blog URL": result.blog_url,
                    "Reason": result.reason,
                    "Evidence": result.evidence,
                }
            )

        df = pd.DataFrame(output_rows)

        # Summary metrics
        st.subheader("📊 Resumen")
        counts = df["Has Content"].value_counts().to_dict()
        verdicts = ["PASS", "WEAK_PASS", "NEWS_ONLY", "NO_BLOG", "CHECK_MANUAL", "SITE_ERROR"]
        labels = {
            "PASS": "✅ PASS",
            "WEAK_PASS": "⚠️ WEAK",
            "NEWS_ONLY": "📰 NEWS",
            "NO_BLOG": "❌ NO BLOG",
            "CHECK_MANUAL": "🔍 MANUAL",
            "SITE_ERROR": "💥 ERROR",
        }
        cols = st.columns(len(verdicts))
        for i, v in enumerate(verdicts):
            with cols[i]:
                st.metric(label=labels[v], value=counts.get(v, 0))

        # Results table
        st.subheader("📋 Resultados detallados")
        st.dataframe(df, use_container_width=True)

        # Download CSV
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        st.download_button(
            label="⬇️ Descargar resultados CSV",
            data=csv_buffer.getvalue().encode("utf-8"),
            file_name="blog_qualifier_results.csv",
            mime="text/csv",
        )
