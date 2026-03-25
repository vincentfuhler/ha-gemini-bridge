FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and default system prompt
COPY src/ /app/src/
COPY system_prompt.txt /app/system_prompt.txt

# Environment defaults
ENV PYTHONPATH=/app
ENV HOST=0.0.0.0
ENV PORT=8000

# Expose port
EXPOSE 8000

# Run Uvicorn
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
