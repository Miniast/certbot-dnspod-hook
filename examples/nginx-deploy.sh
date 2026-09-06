#!/bin/sh
# Install as /etc/letsencrypt/renewal-hooks/deploy/20-nginx-reload
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
