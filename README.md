# B2B SaaS Blog Qualifier

Herramienta para verificar si empresas B2B SaaS tienen un blog o sección de recursos activa en su sitio web.

## Archivos

- `check_blogs.py` — Script CLI original.
- `app.py` — Aplicación web con Streamlit.
- `requirements.txt` — Dependencias.

## Uso local (CLI)

```bash
pip install -r requirements.txt
python check_blogs.py --input companies.csv --output results.csv --workers 10 --timeout 10
```

## Uso local (Streamlit)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Luego abre http://localhost:8501 en tu navegador.

## Despliegue en Streamlit Cloud

1. Sube este repositorio a GitHub.
2. Ve a [Streamlit Cloud](https://streamlit.io/cloud) e inicia sesión con GitHub.
3. Haz clic en **New app**.
4. Selecciona el repositorio, el archivo `app.py` y la rama `main`.
5. Haz clic en **Deploy**.

Streamlit Cloud instalará automáticamente las dependencias desde `requirements.txt`.

## Formato del CSV de entrada

```csv
Company Name,Website
HubSpot,https://hubspot.com
Stripe,https://stripe.com
```

## Veredictos

- **PASS**: Blog activo con señales fuertes.
- **WEAK_PASS**: Algo de contenido, pero señales débiles.
- **NEWS_ONLY**: Solo tiene newsroom/press (no califica).
- **NO_BLOG**: No se encontró blog.
- **CHECK_MANUAL**: Sitio con mucho JavaScript, requiere revisión humana.
- **SITE_ERROR**: No se pudo acceder al sitio.

## Nota sobre privacidad

Si subes la app a Streamlit Cloud pública, los CSV que cargues pasarán por los servidores de Streamlit. Para listas de prospectos sensibles, considera desplegar en un entorno privado o usar el CLI local.
