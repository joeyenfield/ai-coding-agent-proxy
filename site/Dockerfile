FROM nginx:stable-alpine

COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/index.html site/styles.css site/site.js site/robots.txt site/sitemap.xml site/site.webmanifest /usr/share/nginx/html/
COPY site/assets/ /usr/share/nginx/html/assets/
# Only the reviewed, isolated-demo captures used by the brochure.
COPY docs/screenshots/overview-dark.png docs/screenshots/machines.png docs/screenshots/session-chat.png docs/screenshots/machine-mcp.png docs/screenshots/terminal.png /usr/share/nginx/html/screenshots/
