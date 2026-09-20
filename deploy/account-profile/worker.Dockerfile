FROM spark-console-worker:im-content-8bde0e97ce7a1b0182ce56af31e7cbbd6b8534a3
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/message_content.py /app/spark_console/message_content.py
