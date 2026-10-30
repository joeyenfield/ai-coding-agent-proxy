# Android app (`android/`)

A thin native shell, package `dev.agentproxy.app`: a list of agent-servers, and each one's console full screen in a WebView. Release features are the console's. Debug builds also have a private Android Auto session monitor. There is no JS bridge, and the console has no Android-specific code.

## Design rules

- Keep it thin. A new feature goes into the console (`ui/`), not here.
- Release has no libraries. Debug alone uses AndroidX Car App Library for car-host templates; activities still extend `android.app.Activity` and JSON is `org.json`.
- Sign-in uses OIDC access/refresh cookies in the WebView; legacy pairing no longer authenticates. The app stores only names and URLs.

## Parts

| File | Responsibility |
| --- | --- |
| `ServersActivity` | Launcher. Lists servers, adds one after `check()` passes, shows online / sign-in / not a server / offline, flags plain HTTP and an available update (opens `/get`). Remove expires that server's cookies. Opens the last-used server on start unless `EXTRA_STAY`. |
| `ConsoleActivity` | The WebView. Extras `EXTRA_URL`, `EXTRA_NAME`, `EXTRA_PATH` (default `/terminals`). Survives rotation and theme changes (`configChanges`). |
| `Servers.kt` | `ServerStore` (SharedPreferences `servers`: JSON `[{name,url}]`, plus `last`), `normalize()`, `check(url)`: `GET /health` needs `proxy_instance_id`, then `GET /api/auth/status`. Its results are `Online(signIn, version)`, `NotAServer`, `Unreachable`. |
| `ConsoleNavigation.kt` | `isOidcNavigation()` and `isInternalConsoleUrl()`: which navigations stay in the WebView (below). |
| `Insets.kt` | `padForSystemBars()` for edge-to-edge drawing (bars, cutout, IME). |
| `src/debug/`: `CarSessionsService`, `CarSessionData` | Debug only: the Android Auto session monitor (below). |

## WebView behaviour (`ConsoleActivity.kt`)

- JavaScript and DOM storage on (the console keeps its theme and terminal settings in localStorage), `textZoom=100`, user agent with `AgentProxyAndroid/<version>` appended.
- Cookies are accepted and flushed in `onPageFinished` and `onPause`, so the sign-in survives restarts.
- Before loading, `authIssuer()` (in `Servers.kt`) fetches the selected API's public auth status without redirects. `isOidcNavigation` keeps only that HTTPS issuer and same-API login/callback paths in the WebView. An issuer that isn't plain HTTPS (no user info, query or fragment) counts as a failure, so the HTTP issuer of `quickstart/` can't be used from the app. Discovery failure shows an explicit sign-in error; Retry repeats discovery. Removing a server (`ServersActivity.forgetSignIn`) expires `ai_proxy_session`, `ai_proxy_remote`, `mesh_oidc_state` and `mesh_refresh`.
- Same-origin links stay in the WebView; an HTTP-to-HTTPS upgrade for the same host also stays in the app. Only the initial main-frame HTTP redirect chain may move to another HTTPS host (split hosting); that becomes the console origin and survives rotation. No gesture-based navigation, HTTP downgrade or later external link gets that exception. `ConsoleNavigation.kt` owns these rules, covered by `ConsoleNavigationTest`. Downloads (the APK) go to `ACTION_VIEW`.
- Back calls `web.goBack()`, else returns to the server list. This depends on the console pushing a history entry when its menu opens, and on the manifest not opting into `enableOnBackInvokedCallback`.
- A main-frame error shows an offline panel (Retry, Servers). Content debugging is on in debug builds.
- Not implemented: file chooser (`<input type=file>`), permissions, geolocation, fullscreen.

## Security and build

- `src/debug/` owns the private Android Auto experiment: `CarSessionsService`, `CarSessionData`, car resources and manifest. None are in release APKs. The navigation category is only a development discovery workaround: this is not navigation or a store-eligible driving app.
- The car host must match the Android Auto release package and signing certificate (from AndroidX's host allowlist). The car UI shows up to six saved servers and five recent sessions (open first), status, model and request count; polls only while visible, every 15 seconds. No terminal output, input, launches or mutations. Session ids are not displayed.
- `GET /api/sessions` uses the WebView's cookie for the exact URL, without following redirects or persisting another credential. Server-side sharing still applies. Errors clear old data and tell the user to reconnect or sign in on the phone while parked. Requests are bounded to 2 MB. Start/stop generations discard late results.
- `testDebugUnitTest` runs `CarSessionDataTest` (`src/testDebug/`: parsing, open/recent sorting, null fields and failed exits) and `ConsoleNavigationTest` (`src/test/`, every variant). Car-host interaction needs a phone and the Android Auto Desktop Head Unit; do not treat an APK build as an on-car test.
- Android Auto's Unknown sources option alone does not enable sideloaded Car App Library apps in real vehicles; Google's supported route requires a trusted installation source there (Play Internal App Sharing or an Internal Test Track). Use the Desktop Head Unit for this private experiment. `android/README.md` also covers reported unofficial KingInstaller/Shizuku and AAWireless routes, not yet validated on this app. A navigation category does not bypass host discovery. `tools/dist` chooses release when `android/keystore.properties` exists, so those distributed APKs contain no car service.

- Only the `INTERNET` permission; `allowBackup=false`, and the data extraction rules exclude everything. `network_security_config.xml` allows cleartext (LAN, Tailscale) and trusts system and user CAs.
- AGP 8.9, Kotlin 2.1, JDK 17, compile and target SDK 35, min SDK 29. The version comes from `-PappVersion`, else `../VERSION`; `versionCode = major*10000 + minor*100 + patch`. Release builds are minified and signed from `keystore.properties` (gitignored).
- `./gradlew assembleDebug` by hand; `scripts/build-dist.*` writes `dist/<version>/android/`. The server serves the APK at `/android` and lists it on `/get`.
- Colours in `values/` and `values-night/` mirror the console's tokens. Keep them in step when the palette changes.
