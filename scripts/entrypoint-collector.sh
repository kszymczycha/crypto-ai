#!/bin/sh
# Collector entrypoint. The full logic lives in docker/entrypoint-collector.sh;
# this script exists for consistency with the directory-structure specification.
exec /app/docker/entrypoint-collector.sh
