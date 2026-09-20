FROM spark-console-worker:account-profile-774099bbe517d090840fcbac98efd5e768da2a9c
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark core/page_send_evidence.py /app/core/page_send_evidence.py
COPY --chown=spark:spark spark_console/executor.py /app/spark_console/executor.py
