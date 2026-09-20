FROM spark-console-worker:recipient-policy-20260917
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark core/web_chat.py core/im_evidence.py /app/core/
COPY --chown=spark:spark spark_console/models.py spark_console/message_content.py spark_console/executor.py spark_console/worker.py /app/spark_console/
COPY --chown=spark:spark spark_console/services/batch_execution.py /app/spark_console/services/batch_execution.py
