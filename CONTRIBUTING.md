# Contributing to AI Control Mesh

Start with [Development](docs/development.md) for setup and tests, and [AGENTS.md](AGENTS.md) for repository conventions. Before changing a component, read `.context/overview.md` and its component guide. Keep user documentation in step with behavior.

## Issues and changes

- [Open an issue](https://github.com/joeyenfield/ai-coding-agent-proxy/issues) with the version, platform, expected behavior, and a minimal reproduction. Remove credentials, prompts, private code, machine addresses, and other sensitive data from logs or screenshots.
- For a change, use a branch, keep the scope focused, run the checks for the components you touched, and describe the resulting behavior and validation in a pull request. Go client/machine changes need matching Python functionality.
- For the marketing site, follow [Website development](docs/website.md). Use actual isolated-demo captures, verify product claims against the implementation, and check mobile and keyboard access.

## Security reports

Do not put credentials, private traces, or sensitive exploit details in a public issue. Check the repository's [Security tab](https://github.com/joeyenfield/ai-coding-agent-proxy/security) for a private reporting option. If none is available, contact the maintainer through their [GitHub profile](https://github.com/joeyenfield) to arrange a private channel before sharing details. This does not promise a response time or a security support policy.

The project is [MIT licensed](LICENSE). Deployment trust boundaries, recording, and transport are documented in [Security](docs/security.md).
