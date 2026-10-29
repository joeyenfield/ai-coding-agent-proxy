# Android app (`android/`)

A thin native shell, package `dev.agentproxy.app`: a list of agent-servers, and each one's console full screen in a WebView. **All features are the console's.** There is no JS bridge, and the console has no Android-specific code.

## Design rules

- Keep it thin. A new feature goes into the console (`ui/`), not here.
- No libraries: `dependencies {}` is empty on purpose. Activities extend `android.app.Activity` and JSON is `org.json`.
- The sign-in is the WebView's cookie (`ai_proxy_session`, or `ai_proxy_remote` for paired devices). The app stores only names and URLs.

## Parts

| File | Responsibility |
| --- | --- |
| `ServersActivity` | Launcher. Lists servers, adds one after `check()` passes, shows online / sign-in / not a server / offline, flags plain HTTP and an available update (opens `/get`). Remove expires that server's cookies. Opens the last-used server on start unless `EXTRA_STAY`. |
| `ConsoleActivity` | The WebView. Extras `EXTRA_URL`, `EXTRA_NAME`, `EXTRA_PATH` (default `/terminals`). Survives rotation and theme changes (`configChanges`). |
| `Servers.kt` | `ServerStore` (SharedPreferences `servers`: JSON `[{name,url}]`, plus `last`), `normalize()`, `check(url)`: `GET /health` needs `proxy_instance_id`, then `GET /api/auth/status`. Its results are `Online(signIn, version)`, `NotAServer`, `Unreachable`. |
| `Insets.kt` | `padForSystemBars()` for edge-to-edge drawing (bars, cutout, IME). |

## WebView behaviour (`ConsoleActivity.kt`)

- JavaScript and DOM storage on (the console keeps its theme and terminal settings in localStorage), `textZoom=100`, user agent with `AgentProxyAndroid/<version>` appended.
- Cookies are accepted and flushed in `onPageFinished` and `onPause`, so the sign-in survives restarts.
- Same-origin links stay in the WebView; an HTTP-to-HTTPS upgrade for the same host also stays in the app. Other sites open in the browser. Downloads (the APK) go to `ACTION_VIEW`.
- Back calls `web.goBack()`, else returns to the server list. This depends on the console pushing a history entry when its menu opens, and on the manifest not opting into `enableOnBackInvokedCallback`.
- A main-frame error shows an offline panel (Retry, Servers). Content debugging is on in debug builds.
- Not implemented: file chooser (`<input type=file>`), permissions, geolocation, fullscreen.

## Security and build

- Only the `INTERNET` permission; `allowBackup=false`, and the data extraction rules exclude everything. `network_security_config.xml` allows cleartext (LAN, Tailscale) and trusts system and user CAs.
- AGP 8.9, Kotlin 2.1, JDK 17, compile and target SDK 35, min SDK 29. The version comes from `-PappVersion`, else `../VERSION`; `versionCode = major*10000 + minor*100 + patch`. Release builds are signed from `keystore.properties` (gitignored).
- `./gradlew assembleDebug` by hand; `scripts/build-dist.*` writes `dist/<version>/android/`. The server serves the APK at `/android` and lists it on `/get`.
- Colours in `values/` and `values-night/` mirror the console's tokens. Keep them in step when the palette changes.
