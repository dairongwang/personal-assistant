# Ads admin dashboard — static files behind nginx, deployable independently of the user app.
#   docker build -t pa-ads-admin -f apps/ads-admin/Dockerfile apps/ads-admin
#   docker run -p 8081:8080 -e ADS_ADMIN_API_BASE=https://api.example.com pa-ads-admin
FROM nginx:1.27-alpine
COPY index.html styles.css /usr/share/nginx/html/
COPY src /usr/share/nginx/html/src
COPY docker/nginx.conf /etc/nginx/ads-admin.conf.template
COPY docker/30-ads-admin-config.sh /docker-entrypoint.d/30-ads-admin-config.sh
RUN rm -f /etc/nginx/conf.d/default.conf && chmod +x /docker-entrypoint.d/30-ads-admin-config.sh
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
