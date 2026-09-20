FROM spark-console-web:im-content-8bde0e97ce7a1b0182ce56af31e7cbbd6b8534a3
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/services/accounts.py /app/spark_console/services/accounts.py
COPY --chown=spark:spark spark_console/message_content.py /app/spark_console/message_content.py
COPY --chown=spark:spark spark_console/static/batch_tasks.js /app/spark_console/static/batch_tasks.js
COPY --chown=spark:spark spark_console/templates/accounts.html spark_console/templates/tasks.html spark_console/templates/task_edit.html /app/spark_console/templates/
