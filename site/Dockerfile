FROM nginx:stable-alpine

COPY site/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/index.html site/teams.html site/security.html site/styles.css site/site.js site/robots.txt site/sitemap.xml site/site.webmanifest /usr/share/nginx/html/
COPY site/assets/ /usr/share/nginx/html/assets/
# Only the reviewed, isolated-demo captures used by the brochure, full size behind its links (the page
# itself shows web-sized copies in assets/screens, from scripts/screenshots/site-images.py).
COPY docs/screenshots/terminals-dark.png docs/screenshots/terminal-opencode-dark.png docs/screenshots/terminal-copilot-account-dark.png docs/screenshots/terminal-qwen-dark.png docs/screenshots/live-dark.png docs/screenshots/terminal-phone.png docs/screenshots/overview-dark.png docs/screenshots/agents.png docs/screenshots/machines.png docs/screenshots/session-chat.png docs/screenshots/machine-mcp.png docs/screenshots/terminal.png /usr/share/nginx/html/screenshots/
