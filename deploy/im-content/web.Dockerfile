FROM spark-console-web:recipient-policy-20260917
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/models.py spark_console/message_content.py /app/spark_console/
COPY --chown=spark:spark spark_console/services/tasks.py spark_console/services/batch_execution.py /app/spark_console/services/
COPY --chown=spark:spark spark_console/web/app.py /app/spark_console/web/app.py
COPY --chown=spark:spark spark_console/static/batch_tasks.js /app/spark_console/static/batch_tasks.js
COPY --chown=spark:spark spark_console/templates/batch_fields.html spark_console/templates/runs.html spark_console/templates/tasks.html spark_console/templates/task_edit.html /app/spark_console/templates/
