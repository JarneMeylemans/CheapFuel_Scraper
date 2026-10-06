FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py carbu.py fuel.py ./
COPY static static
ENV PRICE_SOURCE=carbu DATA_DIR=/data TZ=Europe/Brussels
VOLUME /data
EXPOSE 8099
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8099"]
