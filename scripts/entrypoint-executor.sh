#!/bin/sh
# Executor entrypoint. The full logic lives in docker/entrypoint-executor.sh;
# this script exists for consistency with the directory-structure specification.
exec /app/docker/entrypoint-executor.sh
