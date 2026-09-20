FROM spark-console-web:account-profile-774099bbe517d090840fcbac98efd5e768da2a9c
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=spark:spark spark_console/services/accounts.py /app/spark_console/services/accounts.py
