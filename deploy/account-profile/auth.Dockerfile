FROM douyin-spark-console-spark-auth
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/auth_scanner.py /app/spark_console/auth_scanner.py
COPY --chown=spark:spark spark_console/services/accounts.py /app/spark_console/services/accounts.py
