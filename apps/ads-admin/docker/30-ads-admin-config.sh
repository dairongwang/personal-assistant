#!/bin/sh
# Write config.js and the nginx server block from the environment (runs at container start).
set -e
API_BASE="${ADS_ADMIN_API_BASE:-}"
printf 'window.ADS_ADMIN_CONFIG = { apiBase: %s };\n' "\"$(printf '%s' "$API_BASE" | sed 's/"/\\"/g')\"" \
  > /usr/share/nginx/html/config.js
export ADS_ADMIN_API_BASE="$API_BASE"
envsubst '${ADS_ADMIN_API_BASE}' < /etc/nginx/ads-admin.conf.template > /etc/nginx/conf.d/default.conf
echo "ads-admin: apiBase=${API_BASE:-<same origin>}"
