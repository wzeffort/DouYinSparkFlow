FROM spark-console-web:hide-account-id-2fb4271b71b0f05cbaa60f83544fb48f72391ab4
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/web/app.py /app/spark_console/web/app.py
COPY --chown=spark:spark spark_console/templates/runs.html /app/spark_console/templates/runs.html
