#!/usr/bin/env bash
# Restart the live services (picks up whatever code is in this checkout).
exec systemctl --user restart carriermon-ingest carriermon-web
