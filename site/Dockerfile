FROM nginx:stable-alpine

COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/index.html site/quickstart.html site/teams.html site/security.html site/styles.css site/site.js site/robots.txt site/sitemap.xml site/site.webmanifest /usr/share/nginx/html/
COPY site/assets/ /usr/share/nginx/html/assets/
# Only the reviewed, isolated-demo captures used by the brochure, full size behind its links (the page
# itself shows web-sized copies in assets/screens, from devhelpers/scripts/screenshots/site-images.py).
COPY docs/screenshots/terminal-grid-dark.png docs/screenshots/real-opencode.png docs/screenshots/real-copilot.png docs/screenshots/real-qwen.png docs/screenshots/live-dark.png docs/screenshots/terminal-phone.png docs/screenshots/overview-dark.png docs/screenshots/agents.png docs/screenshots/machines.png docs/screenshots/session-chat.png docs/screenshots/machine-mcp.png docs/screenshots/analytics.png docs/screenshots/terminal.png /usr/share/nginx/html/screenshots/
# The quickstart page's steps, full size behind its links.
COPY docs/screenshots/quickstart-*.png /usr/share/nginx/html/screenshots/
