FROM gdssingapore/airbase:python-3.13
ENV PYTHONUNBUFFERED=TRUE
WORKDIR /app
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app . .
USER app
EXPOSE 8080
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 --server.headless=true"]
