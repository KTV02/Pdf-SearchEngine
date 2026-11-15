#!/bin/sh

# Inject backend port into settings.js (or config.js)
echo "window.BACKEND_PORT=${BACKEND_PORT:-5050};" > /usr/share/nginx/html/settings.js

# Start nginx
nginx -g 'daemon off;'